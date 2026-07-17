"""AI-Optimized Sequence Engine (feature 1.2).

Wraps the static outreach queue with an AI decision layer. Before each sequence
step is sent, ``SequenceOptimizer.decide`` gathers the contact's engagement
history (opens/clicks/replies), the latest reply sentiment, and company/industry
context, and asks Claude for a structured decision:

    action ∈ {send, skip, delay, switch_channel, rewrite}

- **send**      — send the static step as written (the safe default).
- **skip**      — don't send this step (e.g. they just replied positively);
                  the step is advanced without an email going out.
- **delay**     — push the step out ``delay_days`` days (e.g. optimal timing).
- **switch_channel** — this touch should be a call / LinkedIn instead of email;
                  the email is held and a channel-switch is logged for a human.
- **rewrite**   — send this step, but with the AI-rewritten subject/body
                  (e.g. reference a demo-link click, address a prior objection).

Every decision (with reasoning) is appended to the enrollment's
``optimization_context`` JSONB so the sequence's AI behaviour is auditable and
can be A/B-compared against static sequences.

Fail-safe by construction: if the feature is disabled, there's no API key, or
anything errors, ``decide`` returns a plain ``send`` decision so outreach never
stalls on the optimizer.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_sentiment import EmailSentimentScore
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import SequenceStep
from app.services.ai_service import get_ai_service

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"send", "skip", "delay", "switch_channel", "rewrite"}
VALID_CHANNELS = {"email", "call", "linkedin", "video", "direct_mail"}

# The maximum a single optimizer decision may defer a step, so a hallucinated
# huge number can't park an enrollment for a year.
MAX_DELAY_DAYS = 14

# How many sends may go unanswered before a non-email touch is worth suggesting.
# Keyed on replies rather than opens: an unopened-but-delivered email is a claim
# we cannot actually substantiate (see the prompt's signal-reliability note),
# whereas "we have emailed them N times and they have never replied" is a fact.
SWITCH_CHANNEL_MIN_UNANSWERED = 3


def _send_decision(reason: str = "optimizer disabled — sending static step") -> dict:
    """The safe default decision: send the step exactly as authored."""
    return {"action": "send", "reasoning": reason, "confidence": 1.0}


class SequenceOptimizer:
    """AI decision layer over ``send_sequence_step``."""

    def __init__(self) -> None:
        self.ai = get_ai_service()

    async def _gather_context(
        self,
        db: AsyncSession,
        enrollment: OutreachEnrollment,
        contact: Contact,
        company: Company,
        step: SequenceStep,
    ) -> dict:
        """Assemble the per-enrollment signals Claude needs to decide."""
        now = datetime.now(timezone.utc)

        # Engagement tallies from this enrollment's events.
        events = (
            await db.execute(
                select(OutreachEvent)
                .where(OutreachEvent.enrollment_id == enrollment.id)
                .order_by(OutreachEvent.occurred_at.asc())
            )
        ).scalars().all()

        counts = {"sent": 0, "opened": 0, "clicked": 0, "replied": 0, "bounced": 0}
        last_event_at: Optional[datetime] = None
        for e in events:
            if e.event_type in counts:
                counts[e.event_type] += 1
            last_event_at = e.occurred_at

        days_since_last = None
        if last_event_at is not None:
            if last_event_at.tzinfo is None:
                last_event_at = last_event_at.replace(tzinfo=timezone.utc)
            days_since_last = (now - last_event_at).days

        # Sends with no reply — the primary "this isn't landing" signal.
        #
        # This used to be ``consecutive_unopened``, which was unusable twice
        # over. Structurally: nothing wrote ``opened`` events, so the count was
        # always ``sent`` and the optimizer was told "they never open" on every
        # single enrollment — reliably over-recommending switch_channel. And
        # substantively: opens are weak evidence even when instrumented, because
        # Apple MPP pre-fetches images (phantom opens) while other clients block
        # them (missed opens). A reply is unambiguous and un-spoofable, so the
        # no-reply streak is what drives channel switching now.
        unanswered_sends = counts["sent"] if counts["replied"] == 0 else 0

        # Opens are still surfaced, but only as corroboration — never as the
        # sole basis for a decision. See the prompt's signal-reliability note.
        tracked_sends = sum(
            1
            for e in events
            if e.event_type == "sent" and bool((e.event_metadata or {}).get("tracked"))
        )
        open_signal_reliable = tracked_sends > 0

        # Latest reply sentiment for this contact (if any).
        sentiment = None
        if contact.id is not None:
            srow = (
                await db.execute(
                    select(EmailSentimentScore)
                    .where(EmailSentimentScore.contact_id == contact.id)
                    .order_by(EmailSentimentScore.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if srow is not None:
                sentiment = {
                    "score": srow.sentiment_score,
                    "label": srow.sentiment_label,
                    "intent": srow.intent,
                    "buying_signals": srow.buying_signals or [],
                    "objections": srow.objections or [],
                }

        return {
            "contact": {
                "name": contact.full_name,
                "title": contact.title,
                "email": contact.email,
            },
            "company": {
                "name": company.name,
                "industry": company.industry_segment,
                "pipeline_stage": company.pipeline_stage,
            },
            "step": {
                "number": step.step_number,
                "channel": step.channel,
                "delay_days": step.delay_days,
                "subject": step.subject_template,
            },
            "engagement": counts,
            "days_since_last_event": days_since_last,
            "unanswered_sends": unanswered_sends,
            "open_signal_reliable": open_signal_reliable,
            "latest_sentiment": sentiment,
        }

    async def decide(
        self,
        db: AsyncSession,
        enrollment: OutreachEnrollment,
        contact: Contact,
        company: Company,
        step: SequenceStep,
        rendered_subject: str,
        rendered_body: str,
    ) -> dict:
        """Return a structured decision for the current step.

        Never raises. Always returns a dict containing at least ``action`` and
        ``reasoning``; on any failure it returns a ``send`` decision.
        """
        settings = get_settings()
        if not settings.sequence_optimizer_enabled or not settings.anthropic_api_key:
            return _send_decision()

        try:
            context = await self._gather_context(db, enrollment, contact, company, step)
        except Exception as e:  # noqa: BLE001
            logger.warning("Optimizer context gathering failed: %s", e)
            return _send_decision("context error — sending static step")

        import json as _json

        prompt = f"""You are the decision engine for an AI-optimized B2B outreach sequence at
Apprend Technologies (manufacturing software). A scheduled sequence step is about
to be sent. Decide whether to send it as-is, rewrite it, delay it, switch channel,
or skip it — based on how this specific contact has behaved.

CONTEXT (JSON):
{_json.dumps(context, indent=2, default=str)}

THE STEP ABOUT TO SEND (email):
Subject: {rendered_subject}
Body:
{rendered_body[:1200]}

SIGNAL RELIABILITY — read this before weighing the context:
- A REPLY is the only unambiguous signal. Weigh it above everything else.
- A CLICK is strong: it took a deliberate human action.
- An OPEN is WEAK and must never be the sole basis for a decision. Apple Mail
  Privacy Protection pre-fetches images, inventing opens nobody performed; other
  clients block images, hiding opens that did happen. When
  "open_signal_reliable" is false the send carried no pixel at all, so
  engagement.opened == 0 means "not measured", NOT "not opened" — do not read a
  zero there as disinterest.

DECISION RULES:
- "send": engagement is normal / early; send the step unchanged. This is the default — prefer it unless a signal clearly justifies otherwise.
- "skip": they already replied positively (intent interested / meeting_request) — don't send a scheduled follow-up on top of a live conversation.
- "delay": send later. Provide delay_days (1-{MAX_DELAY_DAYS}). Use for timing optimization only when justified.
- "switch_channel": {SWITCH_CHANNEL_MIN_UNANSWERED}+ sends with no reply ("unanswered_sends") — recommend a non-email touch. Provide channel ∈ ["call","linkedin"]. The email is held for a human to action the switch. Base this on the absence of REPLIES, never on the absence of opens.
- "rewrite": send now, but adapt the copy. Use when they clicked but didn't reply (reference the click), a prior reply raised an objection (address it), or several sends have gone unanswered and the angle deserves a shift. Provide new subject and body. Keep Apprend's voice: short, concrete, no buzzwords, no exclamation marks, one ask.

Return ONLY valid JSON:
{{
  "action": "send|skip|delay|switch_channel|rewrite",
  "delay_days": 0,
  "channel": null,
  "subject": null,
  "body": null,
  "reasoning": "one sentence tying the decision to a specific signal",
  "confidence": 0.0
}}"""

        try:
            result = await self.ai._complete_json(prompt, max_tokens=1024)
        except Exception as e:  # noqa: BLE001
            logger.warning("Optimizer AI call failed: %s", e)
            return _send_decision("AI error — sending static step")

        if not isinstance(result, dict) or result.get("error"):
            return _send_decision("AI returned no decision — sending static step")

        return self._sanitize(result)

    @staticmethod
    def _sanitize(result: dict) -> dict:
        """Clamp/validate a raw AI decision into a safe, well-formed decision."""
        action = str(result.get("action", "send")).strip().lower()
        if action not in VALID_ACTIONS:
            action = "send"

        decision: dict = {
            "action": action,
            "reasoning": str(result.get("reasoning", ""))[:500],
            "confidence": _clamp_float(result.get("confidence", 0.0)),
        }

        if action == "delay":
            days = result.get("delay_days") or 1
            try:
                days = int(days)
            except (TypeError, ValueError):
                days = 1
            days = max(1, min(MAX_DELAY_DAYS, days))
            decision["delay_days"] = days

        elif action == "switch_channel":
            channel = str(result.get("channel") or "call").strip().lower()
            if channel not in VALID_CHANNELS or channel == "email":
                channel = "call"
            decision["channel"] = channel

        elif action == "rewrite":
            subject = (result.get("subject") or "").strip()
            body = (result.get("body") or "").strip()
            # A rewrite must actually provide new content; otherwise fall back to send.
            if not subject and not body:
                decision["action"] = "send"
                decision["reasoning"] = (
                    decision["reasoning"] + " (rewrite had no content — sent static)"
                ).strip()
            else:
                decision["subject"] = subject or None
                decision["body"] = body or None

        return decision


def _clamp_float(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def record_decision(enrollment: OutreachEnrollment, step_number: int, decision: dict) -> None:
    """Append a decision to the enrollment's ``optimization_context`` audit log.

    Reassigns the attribute (rather than mutating in place) so SQLAlchemy's
    change tracking reliably flags the JSONB column as dirty.
    """
    ctx = dict(enrollment.optimization_context or {})
    decisions = list(ctx.get("decisions", []))
    decisions.append(
        {
            "step": step_number,
            "action": decision.get("action"),
            "reasoning": decision.get("reasoning"),
            "confidence": decision.get("confidence"),
            "channel": decision.get("channel"),
            "delay_days": decision.get("delay_days"),
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )
    ctx["decisions"] = decisions
    ctx["last_action"] = decision.get("action")
    enrollment.optimization_context = ctx


def get_sequence_optimizer() -> SequenceOptimizer:
    return SequenceOptimizer()
