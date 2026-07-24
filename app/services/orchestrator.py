"""Autonomous multi-channel orchestration (feature 2.2).

The orchestrator is the central decision engine for an outreach enrollment. For
each enrollment it produces a :class:`TouchDecision` — *which* channel to use,
*when*, *what* to say (a content hint), or whether to **flag for a human** or
**stop** entirely.

Design:

* **Guardrails are deterministic and checked first.** Policy caps (max total /
  daily touches, minimum spacing) and hard stops never depend on the LLM — they
  hold even when the AI is unavailable or misbehaves.
* **Channel escalation is an explicit ladder.** Email → LinkedIn → phone, gated
  by what the policy allows and what contact data we actually have (a LinkedIn
  URL, a phone number). If email has gone unopened repeatedly, escalate.
* **The LLM refines, it doesn't override.** When a key is configured, Claude is
  asked to pick the channel/timing/angle within the allowed set and to raise a
  human flag for nuanced situations; its answer is clamped back to the
  guardrails. With no key, the deterministic ladder is the decision.

Every decision is appended to ``enrollment.orchestration_log`` so the cadence is
fully auditable. Execution of the actual send stays with the existing outreach
pipeline; the orchestrator's job is the *decision* plus the safe side effects
(flag a human via a FollowUp, or mark the enrollment exhausted/stopped).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.follow_up import FollowUp
from app.models.orchestration import (
    ORCHESTRATION_CHANNELS,
    DEFAULT_HUMAN_REVIEW_TRIGGERS,
    OrchestrationPolicy,
)
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.services.llm_providers import ChainClient, any_llm_available, chain_client

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

# Actions a TouchDecision can carry.
ACTION_SEND = "send"          # execute a touch on `channel` now
ACTION_WAIT = "wait"          # hold off; `wait_hours` says how long
ACTION_FLAG_HUMAN = "flag_human"  # needs human judgement; a FollowUp is created
ACTION_STOP = "stop"          # cadence is exhausted / lead unreachable

# How many unopened emails before we escalate off email onto another channel.
_UNOPENED_EMAIL_ESCALATION = 3


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a possibly tz-naive datetime (SQLite loses tzinfo) to UTC-aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class TouchDecision:
    """A single orchestration decision for an enrollment."""

    action: str = ACTION_WAIT
    channel: Optional[str] = None          # email | linkedin | phone | None
    wait_hours: Optional[int] = None       # for ACTION_WAIT
    content_hint: Optional[str] = None     # what angle/message to use
    reasoning: str = ""
    requires_human: bool = False
    human_reason: Optional[str] = None
    confidence: float = 0.5
    source: str = "rules"                  # rules | ai
    decided_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_log_entry(self) -> dict:
        return asdict(self)


# A conservative fallback used when neither a per-user nor a global policy exists.
# Deliberately a plain object, NOT an OrchestrationPolicy() instance: instantiating
# a mapped class at import time would force early SQLAlchemy mapper configuration
# (and, under the SQLite test harness, freeze column types before they're patched).
_DEFAULT_POLICY = SimpleNamespace(
    name="built-in default",
    max_daily_touches=2,
    max_total_touches=12,
    min_hours_between_touches=24,
    allowed_channels=list(ORCHESTRATION_CHANNELS),
    required_channels=[],
    human_review_triggers=list(DEFAULT_HUMAN_REVIEW_TRIGGERS),
)


class Orchestrator:
    """Decision engine for autonomous multi-channel outreach."""

    def __init__(self) -> None:
        settings = get_settings()
        self.model = _MODEL
        self._api_key = settings.anthropic_api_key
        self._client: ChainClient | None = None

    @property
    def available(self) -> bool:
        # Any chain provider counts, not just the Anthropic key: this service is
        # chain-backed now, and gating on one credential would keep it dark on a
        # deployment that carries only a Gemini or Groq key.
        return any_llm_available()

    def _get_client(self) -> ChainClient:
        if self._client is None:
            self._client = chain_client(self._api_key)
        return self._client

    # ------------------------------------------------------------------ #
    # Policy resolution
    # ------------------------------------------------------------------ #

    async def resolve_policy(
        self, db: AsyncSession, user_id: Optional[UUID]
    ) -> OrchestrationPolicy:
        """Most-specific active policy: the user's own, else global, else default."""
        if user_id is not None:
            row = (
                await db.execute(
                    select(OrchestrationPolicy)
                    .where(OrchestrationPolicy.user_id == user_id)
                    .where(OrchestrationPolicy.is_active.is_(True))
                    .order_by(OrchestrationPolicy.created_at.desc())
                )
            ).scalars().first()
            if row is not None:
                return row
        row = (
            await db.execute(
                select(OrchestrationPolicy)
                .where(OrchestrationPolicy.user_id.is_(None))
                .where(OrchestrationPolicy.is_active.is_(True))
                .order_by(OrchestrationPolicy.created_at.desc())
            )
        ).scalars().first()
        return row or _DEFAULT_POLICY

    # ------------------------------------------------------------------ #
    # Context gathering
    # ------------------------------------------------------------------ #

    @staticmethod
    def _channel_available(channel: str, contact: Optional[Contact], company: Optional[Company]) -> bool:
        """Do we have the data needed to use this channel for this contact?"""
        if channel == "email":
            return bool(contact and contact.email)
        if channel == "linkedin":
            return bool(contact and contact.linkedin_url)
        if channel == "phone":
            return bool((contact and (contact.phone or contact.mobile)) or (company and company.phone))
        return False

    async def _gather_context(
        self, db: AsyncSession, enrollment: OutreachEnrollment
    ) -> dict:
        """Assemble the touch history + engagement signals for one enrollment."""
        contact = (
            await db.execute(select(Contact).where(Contact.id == enrollment.contact_id))
        ).scalar_one_or_none()
        company = None
        if contact is not None and contact.company_id is not None:
            company = (
                await db.execute(select(Company).where(Company.id == contact.company_id))
            ).scalar_one_or_none()

        events = (
            await db.execute(
                select(OutreachEvent)
                .where(OutreachEvent.enrollment_id == enrollment.id)
                .order_by(OutreachEvent.occurred_at.asc())
            )
        ).scalars().all()

        now = datetime.now(timezone.utc)
        sends = [e for e in events if e.event_type == "sent"]
        opens = [e for e in events if e.event_type == "opened"]
        replies = [e for e in events if e.event_type == "replied"]

        # Channel history is recorded in each send event's metadata (defaults to email).
        channels_used: list[str] = []
        for e in sends:
            ch = (e.event_metadata or {}).get("channel", "email")
            channels_used.append(ch)

        last_touch_at = _aware(sends[-1].occurred_at) if sends else None
        touches_today = 0
        if sends:
            today = now.date()
            touches_today = sum(
                1 for e in sends
                if e.occurred_at and _aware(e.occurred_at).date() == today
            )

        email_sends = channels_used.count("email")
        return {
            "contact": contact,
            "company": company,
            "total_touches": len(sends),
            "touches_today": touches_today,
            "last_touch_at": last_touch_at,
            "hours_since_last": (
                (now - last_touch_at).total_seconds() / 3600.0 if last_touch_at else None
            ),
            "channels_used": channels_used,
            "email_sends": email_sends,
            "opens": len(opens),
            "replies": len(replies),
            "now": now,
        }

    # ------------------------------------------------------------------ #
    # Deterministic decision (guardrails + escalation ladder)
    # ------------------------------------------------------------------ #

    def _usable_channels(self, policy: OrchestrationPolicy, contact, company) -> list[str]:
        allowed = [c for c in (policy.allowed_channels or ORCHESTRATION_CHANNELS) if c in ORCHESTRATION_CHANNELS]
        return [c for c in allowed if self._channel_available(c, contact, company)]

    def _rule_decision(self, ctx: dict, policy: OrchestrationPolicy) -> TouchDecision:
        """Guardrail + escalation-ladder decision. Never touches the network."""
        contact, company = ctx["contact"], ctx["company"]

        # Hard stop: cadence exhausted.
        if ctx["total_touches"] >= policy.max_total_touches:
            return TouchDecision(
                action=ACTION_STOP,
                reasoning=(
                    f"Reached max_total_touches ({policy.max_total_touches}); "
                    "no reply — stop and mark exhausted."
                ),
                confidence=0.9,
            )

        # Daily cap.
        if ctx["touches_today"] >= policy.max_daily_touches:
            return TouchDecision(
                action=ACTION_WAIT,
                wait_hours=24,
                reasoning=f"Hit daily cap ({policy.max_daily_touches}); wait until tomorrow.",
                confidence=0.9,
            )

        # Minimum spacing between touches.
        hrs = ctx["hours_since_last"]
        if hrs is not None and hrs < policy.min_hours_between_touches:
            wait = int(round(policy.min_hours_between_touches - hrs)) or 1
            return TouchDecision(
                action=ACTION_WAIT,
                wait_hours=wait,
                reasoning=(
                    f"Only {hrs:.0f}h since last touch; policy wants "
                    f"{policy.min_hours_between_touches}h — wait {wait}h."
                ),
                confidence=0.85,
            )

        usable = self._usable_channels(policy, contact, company)
        if not usable:
            return TouchDecision(
                action=ACTION_FLAG_HUMAN,
                reasoning="No usable channel (missing email/LinkedIn/phone) — needs a human to enrich.",
                requires_human=True,
                human_reason="missing_contact_channels",
                confidence=0.7,
            )

        # Escalation ladder: prefer email first, but if email has been tried
        # repeatedly with no opens, move to the next available channel.
        email_exhausted = (
            ctx["email_sends"] >= _UNOPENED_EMAIL_ESCALATION and ctx["opens"] == 0
        )
        order = ["email", "linkedin", "phone"]
        # Rotate through channels we haven't used yet before repeating one.
        used = set(ctx["channels_used"])
        if email_exhausted and "email" in usable:
            usable = [c for c in usable if c != "email"] or usable
        # Choose first usable channel, preferring an unused one to widen coverage.
        unused = [c for c in order if c in usable and c not in used]
        pick = (unused or [c for c in order if c in usable])[0]

        if email_exhausted and pick != "email":
            hint = "Email got no opens after several tries — switch channel and change the angle."
        elif ctx["opens"] and pick == "email":
            hint = "They've opened but not replied — reference the interest and make one clear ask."
        elif ctx["total_touches"] == 0:
            hint = "First touch — lead with their world/industry pain, not the product."
        else:
            hint = "Follow up with a fresh angle; keep it short."

        return TouchDecision(
            action=ACTION_SEND,
            channel=pick,
            content_hint=hint,
            reasoning=(
                f"Touch #{ctx['total_touches'] + 1} on {pick} "
                f"(opens={ctx['opens']}, prior emails={ctx['email_sends']})."
            ),
            confidence=0.75,
        )

    # ------------------------------------------------------------------ #
    # AI refinement
    # ------------------------------------------------------------------ #

    def _parse_json(self, text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end > start:
                return json.loads(text[start:end + 1])
            raise

    async def _ai_refine(
        self, ctx: dict, policy: OrchestrationPolicy, base: TouchDecision
    ) -> TouchDecision:
        """Ask Claude to refine an actionable (send) decision within guardrails.

        Only called when the deterministic layer decided to SEND — waits/stops are
        pure policy and don't need the LLM. Falls back to ``base`` on any failure.
        """
        contact, company = ctx["contact"], ctx["company"]
        usable = self._usable_channels(policy, contact, company)
        name = contact.full_name if contact else "the contact"
        title = (contact.title if contact else "") or "unknown role"
        cname = (company.name if company else "") or "their company"
        industry = (company.industry_segment if company else "") or "manufacturing"
        triggers = ", ".join(policy.human_review_triggers or [])

        prompt = f"""You are the SDR orchestration brain for Apprend Technologies (manufacturing
quality software). Decide the single best NEXT touch for this lead.

LEAD: {name}, {title} at {cname} ({industry})
CADENCE SO FAR: {ctx['total_touches']} touches, channels used: {ctx['channels_used'] or 'none'},
email opens: {ctx['opens']}, replies: {ctx['replies']}.
ALLOWED CHANNELS (pick ONE of these only): {usable}
HUMAN-REVIEW TRIGGERS (raise requires_human if any apply to what you'd do): {triggers}

Rules:
- Pick the channel most likely to get a response given what has/hasn't worked.
- If email has gone unopened repeatedly, do NOT keep emailing — escalate.
- If the next best move needs human judgement (a pricing/legal ask, a nuanced
  objection, a warm-referral opening), set requires_human true with a reason.
- Keep content_hint to one concrete sentence a rep can act on.

Return ONLY valid JSON:
{{
  "channel": "{'|'.join(usable)}",
  "content_hint": "one concrete sentence",
  "requires_human": false,
  "human_reason": null,
  "reasoning": "why this channel/angle now",
  "confidence": 0.0
}}"""
        try:
            message = await self._get_client().messages.create(
                model=self.model,
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            data = self._parse_json(message.content[0].text)
        except Exception as e:  # noqa: BLE001 — AI is best-effort; fall back to rules
            logger.warning("Orchestrator AI refine failed: %s", e)
            return base

        channel = data.get("channel")
        if channel not in usable:
            channel = base.channel  # clamp to a channel we can actually use
        requires_human = bool(data.get("requires_human"))
        return TouchDecision(
            action=ACTION_FLAG_HUMAN if requires_human else ACTION_SEND,
            channel=None if requires_human else channel,
            content_hint=data.get("content_hint") or base.content_hint,
            reasoning=data.get("reasoning") or base.reasoning,
            requires_human=requires_human,
            human_reason=data.get("human_reason") if requires_human else None,
            confidence=float(data.get("confidence", base.confidence) or base.confidence),
            source="ai",
        )

    # ------------------------------------------------------------------ #
    # Public decision API
    # ------------------------------------------------------------------ #

    async def decide(
        self,
        db: AsyncSession,
        enrollment: OutreachEnrollment,
        policy: Optional[OrchestrationPolicy] = None,
        *,
        use_ai: bool = True,
    ) -> TouchDecision:
        """Compute the next :class:`TouchDecision` for an enrollment (no side effects)."""
        if policy is None:
            # Enrollments aren't user-owned directly, so scoping a policy by the
            # sequence owner is out of scope — use the global/default policy.
            policy = await self.resolve_policy(db, None)

        ctx = await self._gather_context(db, enrollment)
        decision = self._rule_decision(ctx, policy)

        # Only refine actionable sends with the LLM; waits/stops/flags are policy.
        if decision.action == ACTION_SEND and use_ai and self.available:
            decision = await self._ai_refine(ctx, policy, decision)
        return decision

    # ------------------------------------------------------------------ #
    # Execution (safe side effects only)
    # ------------------------------------------------------------------ #

    async def run_for_enrollment(
        self,
        db: AsyncSession,
        enrollment: OutreachEnrollment,
        policy: Optional[OrchestrationPolicy] = None,
        *,
        use_ai: bool = True,
    ) -> TouchDecision:
        """Decide + record + take the safe side effect for one enrollment.

        Records the decision on ``orchestration_log``. For a STOP decision the
        enrollment is completed; for FLAG_HUMAN a FollowUp is created so a rep
        picks it up. Actual message sending is left to the outreach pipeline —
        the orchestration_log entry is the instruction it will read.
        """
        if policy is None:
            policy = await self.resolve_policy(db, None)
        decision = await self.decide(db, enrollment, policy, use_ai=use_ai)

        # Append to the audit log (reassign so SQLAlchemy sees the JSONB change).
        log = list(enrollment.orchestration_log or [])
        log.append(decision.as_log_entry())
        enrollment.orchestration_log = log

        contact = (
            await db.execute(select(Contact).where(Contact.id == enrollment.contact_id))
        ).scalar_one_or_none()

        if decision.action == ACTION_STOP:
            enrollment.status = "completed"
            enrollment.completed_at = datetime.now(timezone.utc)
        elif decision.action == ACTION_WAIT and decision.wait_hours:
            enrollment.next_send_at = datetime.now(timezone.utc) + timedelta(hours=decision.wait_hours)
        elif decision.action == ACTION_FLAG_HUMAN:
            db.add(FollowUp(
                contact_id=enrollment.contact_id,
                company_id=contact.company_id if contact else None,
                due_date=datetime.now(timezone.utc),
                reason=f"Orchestrator flagged for human review: {decision.human_reason or 'needs judgement'}",
                status="pending",
                notes=decision.reasoning[:500],
            ))

        await db.flush()
        return decision

    async def run_all_autonomous(self, db: AsyncSession, limit: int = 50) -> list[dict]:
        """Process every active, autonomous-mode enrollment and record a decision."""
        enrollments = (
            await db.execute(
                select(OutreachEnrollment)
                .where(OutreachEnrollment.orchestration_mode == "autonomous")
                .where(OutreachEnrollment.status == "active")
                .limit(limit)
            )
        ).scalars().all()

        policy = await self.resolve_policy(db, None)
        results = []
        for enrollment in enrollments:
            try:
                decision = await self.run_for_enrollment(db, enrollment, policy)
                results.append({
                    "enrollment_id": str(enrollment.id),
                    "action": decision.action,
                    "channel": decision.channel,
                    "requires_human": decision.requires_human,
                })
            except Exception as e:  # noqa: BLE001 — one bad enrollment shouldn't stop the batch
                logger.error("Orchestrator failed for enrollment %s: %s", enrollment.id, e)
        await db.commit()
        return results


_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
