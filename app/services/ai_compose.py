"""AI composition for sequence first touches, with a human review gate.

Connects three things that already existed but were never joined up:

    research_agent (pain points, angles)  →  ai_email_generator (cold copy)
                                          →  send_sequence_step

Before this, the sequence path rendered ``step.body_template`` through Jinja and
sent it. Mail-merge, not personalization. The AI copywriter was reachable only
from the manual-send router, and the research briefs it accepts were written
every six hours and read by nobody in the outreach path.

Design constraints, in the order they matter:

1. **Never block a send.** Every failure — no API key, exhausted credits, no
   research brief, a malformed response — falls back to the static render. This
   mirrors ``sequence_optimizer``'s fail-safe contract. AI composition is an
   improvement on the copy, never a dependency of the sequence.
2. **Review before autonomy.** The first ``ai_compose_review_batch_size`` AI
   emails per sequence are queued for a human instead of sent. Cold outreach is
   irreversible and reputation-linked; an LLM writing something off-key at
   volume costs the sending domain, not just the campaign.
3. **First touch only, by default.** Step 1 is where the static template hurts
   most and where the optimizer cannot help (it rewrites off engagement history,
   and step 1 has none). Later steps already have the optimizer.

The spam-word scan is applied to generated copy before it is ever proposed:
``deliverability_optimizer.scan_content`` existed but only ran on demand from a
router, so nothing checked outbound mail. A model writing "FREE!!! ACT NOW" into
a cold email is exactly the risk that check was built for.
"""

import logging
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.ai_email_draft import AiEmailDraft
from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import SequenceStep

logger = logging.getLogger(__name__)

# Reject generated copy at or above this spam score and send the static template
# instead. 50 is deliberately the same threshold ``deliverability_optimizer``
# itself calls "high" risk (the scale is 0-100: high >= 50, medium >= 25) — we
# gain nothing by inventing a second, stricter definition of spammy here. The
# static templates are human-written and already trusted, so falling back costs
# only personalization.
MAX_SPAM_SCORE = 50.0


class ComposeResult:
    """Outcome of composing one step.

    ``action`` is one of:
      * ``"send"``   — use ``subject``/``body`` (AI or static) and send now.
      * ``"review"`` — a draft was queued; do not send. The caller must park the
        enrollment rather than advancing it.
    """

    def __init__(
        self,
        action: str,
        subject: str,
        body: str,
        *,
        ai_generated: bool = False,
        research_used: bool = False,
        draft_id=None,
        reason: str = "",
    ) -> None:
        self.action = action
        self.subject = subject
        self.body = body
        self.ai_generated = ai_generated
        self.research_used = research_used
        self.draft_id = draft_id
        self.reason = reason

    def because(self, reason: str) -> "ComposeResult":
        """Same result, annotated with why. Used by the static fallback paths."""
        self.reason = reason
        return self

    def __repr__(self) -> str:
        return f"<ComposeResult {self.action} ai={self.ai_generated} ({self.reason})>"


async def _reviewed_count(db: AsyncSession, sequence_id) -> int:
    """AI drafts already raised for this sequence.

    Counts every draft ever created, not just pending ones: the quota is "the
    first N this sequence produced got human eyes", so approving them must not
    re-open the gate and send the next N unreviewed.
    """
    return (
        await db.execute(
            select(func.count(AiEmailDraft.id)).where(AiEmailDraft.sequence_id == sequence_id)
        )
    ).scalar() or 0


async def _latest_draft(db: AsyncSession, enrollment_id, step_number: int) -> Optional[AiEmailDraft]:
    """The most recent draft for this exact step, whatever its status.

    Deliberately not filtered to ``pending``. A reviewer's decision has to be
    *terminal* for the step: the outreach queue re-dispatches an enrollment
    repeatedly, so if composition only recognised pending drafts it would mint a
    fresh one on every pass — a rejected draft would be replaced by another draft
    and the step would loop through review forever, and an approved draft would
    be regenerated instead of sent.
    """
    return (
        await db.execute(
            select(AiEmailDraft)
            .where(
                AiEmailDraft.enrollment_id == enrollment_id,
                AiEmailDraft.step_number == step_number,
            )
            .order_by(AiEmailDraft.created_at.desc())
            .limit(1)
        )
    ).scalars().first()


async def _sequence_tone(db: AsyncSession, sequence_id) -> Optional[str]:
    """The voice this sequence writes in, or None to let the generator decide.

    Read as a scalar rather than off ``step.sequence``/``enrollment.sequence``:
    neither relationship is eager-loaded, so touching them here would emit a
    lazy load in async context and raise MissingGreenlet — turning a cosmetic
    field into a dropped first touch.

    Fails soft for the same reason everything in this module does. Tone shapes
    copy; it does not decide whether copy is safe to send, so a lookup failure
    means "write in the house voice", not "don't write".
    """
    try:
        from app.models.sequence import OutreachSequence

        return (
            await db.execute(
                select(OutreachSequence.tone).where(OutreachSequence.id == sequence_id)
            )
        ).scalar_one_or_none()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Tone lookup failed for sequence %s: %s", sequence_id, exc)
        return None


async def _scan_is_safe(db: AsyncSession, subject: str, body: str) -> bool:
    """Whether generated copy passes the spam-content scan. Fails open."""
    try:
        from app.services.deliverability_optimizer import get_deliverability_optimizer

        # has_unsubscribe=True: the dispatcher appends the CAN-SPAM footer after
        # composition, so the copy legitimately has no unsubscribe link yet and
        # scoring it as missing one would penalise every draft.
        result = await get_deliverability_optimizer().scan_content(
            subject=subject, body=body, has_unsubscribe=True,
        )
        score = float((result or {}).get("spam_score", 0) or 0)
        if score >= MAX_SPAM_SCORE:
            logger.warning(
                "AI copy rejected by spam scan (score %.0f/100, risk=%s): %s",
                score, (result or {}).get("risk"), (result or {}).get("issues"),
            )
            return False
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Spam scan unavailable, accepting copy: %s", exc)
        return True


async def compose_step(
    db: AsyncSession,
    enrollment: OutreachEnrollment,
    contact: Contact,
    company: Company,
    step: SequenceStep,
    static_subject: str,
    static_body: str,
) -> ComposeResult:
    """Compose this step, returning either copy to send or a queued draft.

    Falls back to the static render on every failure path — see the module
    docstring. The caller may treat a ``"send"`` result as "business as usual".
    """
    settings = get_settings()
    static = ComposeResult("send", static_subject, static_body, reason="static template")

    if not settings.ai_compose_enabled:
        return static
    if step.step_number != 1 and not settings.ai_compose_all_steps:
        return static.because("not a first touch — optimizer handles later steps")
    if not settings.anthropic_api_key:
        return static.because("no API key")

    # Honour any decision already made about this step. This must come before
    # generating anything: a reviewer's verdict is terminal for the step, and
    # re-composing would either loop a rejected step through review forever or
    # send copy nobody approved.
    existing = await _latest_draft(db, enrollment.id, step.step_number)
    if existing is not None:
        if existing.status == "pending":
            return ComposeResult(
                "review", static_subject, static_body,
                draft_id=existing.id, reason="draft already awaiting review",
            )
        if existing.status == "approved":
            return ComposeResult(
                "send", existing.subject, existing.body,
                ai_generated=True, research_used=bool(existing.research_used),
                draft_id=existing.id, reason="approved by reviewer",
            )
        if existing.status == "rejected":
            return static.because("AI copy rejected by reviewer — sending static template")
        # 'sent' / 'failed': the draft has run its course; don't re-compose.
        return static.because(f"draft already {existing.status}")

    # Ground the copy in the research brief when one exists.
    research_context = ""
    try:
        from app.services.research_agent import format_research_for_prompt, get_active_research

        research = await get_active_research(db, company.id)
        research_context = format_research_for_prompt(research) or ""
    except Exception as exc:
        logger.debug("Research brief unavailable for %s: %s", company.name, exc)

    lead_data = {
        "first_name": contact.first_name or "",
        "last_name": contact.last_name or "",
        "title": contact.title or "",
        "company_name": company.name or "",
        "industry": company.industry_segment or "",
        "city": company.city or "",
        "website": company.website or company.domain or "",
        "employee_count": company.employee_count,
    }

    tone = await _sequence_tone(db, enrollment.sequence_id)

    try:
        from app.services.ai_email_generator import generate_email

        generated = await generate_email(
            lead_data, template_type="first_touch", research_context=research_context,
            tone=tone,
        )
    except Exception as exc:
        logger.warning("AI composition failed for %s: %s", company.name, exc)
        return static.because("AI error")

    if not generated or not generated.get("ai_generated"):
        # The generator's own template fallback — no better than the static step,
        # and the static step is at least the one a human wrote for this sequence.
        return static.because("generator fell back to template")

    subject = (generated.get("subject") or "").strip()
    body = (generated.get("body") or "").strip()
    if not subject or not body:
        return static.because("generator returned empty copy")

    if not await _scan_is_safe(db, subject, body):
        return static.because("AI copy failed the spam scan")

    # Review gate: the first N per sequence go to a human.
    already = await _reviewed_count(db, enrollment.sequence_id)
    if already < settings.ai_compose_review_batch_size:
        draft = AiEmailDraft(
            enrollment_id=enrollment.id,
            sequence_id=enrollment.sequence_id,
            contact_id=contact.id,
            company_id=company.id,
            step_number=step.step_number,
            subject=subject,
            body=body,
            static_subject=static_subject,
            static_body=static_body,
            ai_generated=True,
            research_used=bool(research_context),
            status="pending",
        )
        db.add(draft)
        await db.flush()
        logger.info(
            "AI draft %s queued for review (%d/%d for this sequence)",
            draft.id, already + 1, settings.ai_compose_review_batch_size,
        )
        return ComposeResult(
            "review", subject, body,
            ai_generated=True, research_used=bool(research_context),
            draft_id=draft.id,
            reason=f"review batch {already + 1}/{settings.ai_compose_review_batch_size}",
        )

    return ComposeResult(
        "send", subject, body,
        ai_generated=True, research_used=bool(research_context),
        reason="AI-composed (sequence past its review quota)",
    )
