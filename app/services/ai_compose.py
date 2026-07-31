"""AI composition for sequence emails, with a human review gate.

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
   emails per sequence *step* are queued for a human instead of sent. Cold
   outreach is irreversible and reputation-linked; an LLM writing something
   off-key at volume costs the sending domain, not just the campaign.
3. **Every step writes as itself.** Composition covers the whole sequence
   (``ai_compose_all_steps``), because ``outreach_sequences.tone`` only shapes
   copy this module generates: with step 1 composed and steps 2-4 rendered from
   Jinja, a sequence spoke in its chosen voice for one email and in the static
   templates' voice for the rest. But a step is not interchangeable with any
   other — step 4 is a breakup, step 2 is the cost argument — so each one is
   composed under its own prompt hint (``STEP_TEMPLATE_TYPES``) rather than all
   of them under "first_touch", which would introduce the sender four times to
   someone who has already heard from them three times.

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
from app.services.llm_providers import has_any_provider_key

logger = logging.getLogger(__name__)

# Reject generated copy at or above this spam score and send the static template
# instead. 50 is deliberately the same threshold ``deliverability_optimizer``
# itself calls "high" risk (the scale is 0-100: high >= 50, medium >= 25) — we
# gain nothing by inventing a second, stricter definition of spammy here. The
# static templates are human-written and already trusted, so falling back costs
# only personalization.
MAX_SPAM_SCORE = 50.0

# Which prompt hint each step writes under — keys of
# ``ai_email_generator.TEMPLATE_HINTS``, pinned by ``test_ai_compose``.
#
# Composition used to be step 1 only, so the ``template_type="first_touch"`` it
# passed was a constant and correct by construction. Extending composition to
# the rest of the sequence makes it a lie for three emails out of four: a step-4
# breakup composed as a first touch opens by introducing a stranger who has
# already written three times, and drops the pilot offer the step exists for.
#
# The mapping follows the arc every seeded sequence is built on (see
# ``outreach_templates``: personal stakes -> the real cost of scrap ->
# tribal-knowledge loss -> breakup + free pilot). Step 1 keeps "first_touch"
# rather than the arc's "pain_point": that copy is already live and reviewed,
# and this change is about the steps that had no voice, not about relitigating
# the one that did.
STEP_TEMPLATE_TYPES: dict[int, str] = {
    1: "first_touch",
    2: "scrap_math",
    3: "follow_up_2",
    4: "free_pilot",
}

# Steps past the seeded arc's fourth. "follow_up_2" is the deliberate choice for
# an unknown position: it is the one hint that asks for a *new angle* without
# assuming what came before, and it forbids customer stories outright. The
# alternative, "follow_up_3", is a breakup — wrong for any step that has more
# emails after it, and unrecoverable, since the recipient has been told goodbye.
LATER_STEP_TEMPLATE_TYPE = "follow_up_2"


def template_type_for(step_number: int) -> str:
    """The prompt hint step ``step_number`` writes under.

    Total: an out-of-range or malformed step number degrades to a generic
    follow-up rather than raising. Composition is fail-safe end to end, and the
    one thing that must never happen is a step number the map has no opinion
    about turning into a dropped touch.
    """
    return STEP_TEMPLATE_TYPES.get(step_number, LATER_STEP_TEMPLATE_TYPE)


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


async def _reviewed_count(db: AsyncSession, sequence_id, step_number: int) -> int:
    """AI drafts already raised for this sequence *and step*.

    Counts every draft ever created, not just pending ones: the quota is "the
    first N this step produced got human eyes", so approving them must not
    re-open the gate and send the next N unreviewed.

    Per step, not per sequence. The quota is a sample of what the model writes
    here, and each step asks it for a different email under a different hint —
    a step-4 breakup shares nothing with a step-1 intro but the sequence id.
    Counting per sequence, once composition covers every step, means the first
    touch alone exhausts the quota (it runs on every enrollment, and it runs
    first), and every follow-up the model has ever written then sends with no
    human having read one — the exact autonomy this gate exists to withhold.
    """
    return (
        await db.execute(
            select(func.count(AiEmailDraft.id)).where(
                AiEmailDraft.sequence_id == sequence_id,
                AiEmailDraft.step_number == step_number,
            )
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
    # Either backend will do. AIService picks OpenRouter *or* Anthropic (see its
    # docstring), so the old bare ``anthropic_api_key`` check disabled
    # composition for every OpenRouter-only deployment — silently, and in the
    # one direction that looks like healthy static copy rather than an outage.
    #
    # Read the keys off settings rather than asking ``get_ai_service()``: that
    # singleton resolves its backend once, at first use, so it answers from
    # whatever configuration was live then. This gate has to reflect the
    # configuration *now*. A key that is present but rejected at call time still
    # lands on the AI-error path below and falls back to static, so guessing
    # generously here costs one wasted call, never a dropped touch.
    #
    # Ask the provider chain, not one key: the gate now has four OpenAI-compatible
    # providers plus Anthropic to choose from, and naming only OpenRouter here
    # would re-introduce the exact failure above one level up — a deployment
    # holding a Gemini key would report "no API key" and mail static copy.
    if not (has_any_provider_key(settings) or settings.anthropic_api_key):
        # Silent until now: this gate returning static looks identical to a
        # healthy send in every metric the sequence path reports (open rate,
        # send count), so a deployment with a stale/missing key sends nothing
        # but mail-merge indefinitely without a single log line to find it by.
        logger.warning(
            "AI composition disabled for step %d (enrollment %s): no provider key configured",
            step.step_number, enrollment.id,
        )
        return static.because("no API key")

    template_type = template_type_for(step.step_number)

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
        # "there", not "", so ``ai_email_generator``'s greeting default actually
        # fires: it reads first_name off this dict, and an empty string is a
        # present-but-blank key that ``.get(..., "there")`` would never override,
        # producing "Hi ,".
        "first_name": contact.first_name or "there",
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
            lead_data, template_type=template_type, research_context=research_context,
            tone=tone,
        )
    except Exception as exc:
        logger.warning("AI composition failed for %s: %s", company.name, exc)
        return static.because("AI error")

    if not generated or not generated.get("ai_generated"):
        # The generator's own template fallback — no better than the static step,
        # and the static step is at least the one a human wrote for this sequence.
        # ``generate_email`` already logs the specific provider error at the
        # point it happened; this is the second, louder signal — at the call
        # site that decides what actually ships — because a chain that is
        # exhausted on every single call produces exactly this outcome, on
        # every send, with nothing in the sequence's own metrics to show it.
        logger.warning(
            "AI generator fell back to its own template for step %d (enrollment %s) "
            "— sending static sequence copy instead",
            step.step_number, enrollment.id,
        )
        return static.because("generator fell back to template")

    subject = (generated.get("subject") or "").strip()
    body = (generated.get("body") or "").strip()
    if not subject or not body:
        logger.warning(
            "AI generator returned empty copy for step %d (enrollment %s)",
            step.step_number, enrollment.id,
        )
        return static.because("generator returned empty copy")

    if not await _scan_is_safe(db, subject, body):
        return static.because("AI copy failed the spam scan")

    # Review gate: the first N per sequence step go to a human — but, by
    # default, only for the first touch. Gating follow-ups too is what froze
    # mid-sequence enrollments in ``pending_review`` when nobody was reviewing;
    # see ``ai_compose_review_first_step_only``.
    gated = not settings.ai_compose_review_first_step_only or step.step_number == 1
    already = await _reviewed_count(db, enrollment.sequence_id, step.step_number)
    if gated and already < settings.ai_compose_review_batch_size:
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
            "AI draft %s queued for review (%d/%d for step %d of this sequence)",
            draft.id, already + 1, settings.ai_compose_review_batch_size, step.step_number,
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
        reason=f"AI-composed as {template_type} (step past its review quota)",
    )
