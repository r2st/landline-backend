"""Everything we know about a prospect, formatted for the copywriter.

``ai_compose`` already grounds cold copy in the research brief. But the brief is
one of *nine* things Landline knows about a prospect by the time it writes to
them, and the other eight never reached the prompt:

    CompanyNews          — a funding round, a new plant, a leadership change
    IntentSignal         — job postings, expansion, the outreach angle already
                           computed for that signal
    DiscoveredBusiness   — the Places record that sourced them: rating, review
                           count, the search query that found them
    OutreachEvent        — what they did with the last three emails
    prior step copy      — what we already said, so step 3 doesn't re-pitch
                           step 2's angle in different words
    Contact              — department, seniority read off the title, whether we
                           have a LinkedIn
    Company              — employee band, state, NAICS description, website

The prompt that shipped before this module named five fields (first name,
company, industry, title, city) plus the brief. A cold email built on those five
is a mail-merge with better grammar: everything in it is true of a hundred other
shops in the same vertical. Specificity is the whole ballgame in cold outreach —
"your 3-week quote turnaround" beats any amount of tone tuning — and the
specifics were sitting in five tables nobody joined.

Three rules this module keeps:

1. **Only facts, never inference.** Every line handed to the model is something a
   row actually says. The model is told, in the block header, that it may use
   these facts and may not invent neighbours to them. A cold email that asserts
   something false about someone's business is worse than a generic one — it is
   the reply that ends the conversation.
2. **Fail soft, per source.** Each source is fetched in its own try/except. A
   dead news table degrades the email by one paragraph; it never drops the send.
   This mirrors ``ai_compose``'s contract end to end.
3. **Say what was used.** :attr:`PersonalizationContext.points` names each data
   point that made it into the prompt, and rides onto the send event and the AI
   draft. "Why did this email say that?" is answerable, and
   ``personalization_depth`` gives the analytics surface something to correlate
   reply rate against — which is the only way to find out whether any of this
   works.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# How far back a news item still counts as a conversation opener. Older than a
# quarter and "congrats on the expansion" reads as someone who just found the
# press release, not someone who follows the industry.
NEWS_MAX_AGE_DAYS = 90
MAX_NEWS_ITEMS = 2

# Intent signals are already scoped by ``expires_at`` (their buying window), so
# recency is enforced by the row, not by us.
MAX_INTENT_SIGNALS = 2

# Prior steps summarised for the model. Three is enough to keep a four-step
# sequence from repeating itself without spending the context window on copy.
MAX_PRIOR_STEPS = 3

# Employee-count bands. The raw number is noise to a copywriter ("47 employees"
# invites the model to write it into the email, which is creepy); the band is
# what actually changes how you write to someone.
_SIZE_BANDS: tuple[tuple[int, str], ...] = (
    (10, "a very small shop (under 10 people)"),
    (50, "a small shop (10-50 people)"),
    (200, "a mid-size manufacturer (50-200 people)"),
    (1000, "a large manufacturer (200-1000 people)"),
)
_SIZE_BAND_LARGE = "an enterprise manufacturer (1000+ people)"

# Title fragments that mark a decision maker. Used to tell the model who it is
# writing to, not to gate anything — miscategorising a title costs a slightly
# wrong register, so the list stays short and obvious rather than exhaustive.
_OWNER_TITLES = ("owner", "founder", "president", "ceo", "principal")
_EXEC_TITLES = ("chief", "cto", "coo", "cfo", "vp", "vice president", "head of", "director")
_MANAGER_TITLES = ("manager", "supervisor", "lead", "foreman", "superintendent")


def size_band(employee_count: Optional[int]) -> Optional[str]:
    """Describe a headcount as a band, or None when we don't know it."""
    if not employee_count or employee_count <= 0:
        return None
    for ceiling, label in _SIZE_BANDS:
        if employee_count < ceiling:
            return label
    return _SIZE_BAND_LARGE


def seniority_of(title: Optional[str]) -> Optional[str]:
    """Classify a job title into owner / executive / manager / individual.

    Returns None for a missing title rather than guessing "individual": we write
    differently to an owner than to a buyer, and "we don't know" is a better
    input to that decision than a default that is wrong for half the list.
    """
    t = (title or "").strip().lower()
    if not t:
        return None
    if any(k in t for k in _OWNER_TITLES):
        return "owner"
    if any(k in t for k in _EXEC_TITLES):
        return "executive"
    if any(k in t for k in _MANAGER_TITLES):
        return "manager"
    return "individual contributor"


@dataclass
class PersonalizationContext:
    """The assembled facts, plus a record of which ones we had.

    ``lines`` are prompt-ready sentences. ``points`` names the data points behind
    them — the audit trail that travels with the send.
    """

    lines: list[str] = field(default_factory=list)
    points: list[str] = field(default_factory=list)

    def add(self, point: str, line: str) -> None:
        """Record one fact under a named data point. Blank lines are dropped."""
        text = (line or "").strip()
        if not text:
            return
        self.lines.append(text)
        if point not in self.points:
            self.points.append(point)

    @property
    def depth(self) -> int:
        """How many distinct data points fed this email.

        A count, not a 0-100 score: a score implies a weighting we have no
        evidence for yet. Correlate it against reply rate first, weight it after.
        """
        return len(self.points)

    def as_prompt_block(self) -> str:
        """The block appended to the copywriter's user prompt, or "" if empty.

        The header is load-bearing. Without an explicit "these are facts, do not
        extrapolate", a model handed "hiring 3 CNC machinists" will happily write
        "since you're scaling your machining division to meet new demand" — an
        invention, in an email to the person who would know it is one.
        """
        if not self.lines:
            return ""
        body = "\n".join(f"- {line}" for line in self.lines)
        return (
            "\nWHAT WE KNOW ABOUT THIS PROSPECT (verified facts from our own "
            "records):\n"
            f"{body}\n\n"
            "Use at most TWO of these facts, and only where one makes the email "
            "more specific. State them as plainly as they are written above. Do "
            "NOT extrapolate beyond them, do not guess at causes or consequences, "
            "and do not mention that we track any of this.\n"
        )

    def as_metadata(self) -> dict:
        """Compact record for the send event / draft row."""
        return {"points": list(self.points), "depth": self.depth}


async def _add_news(db: AsyncSession, ctx: PersonalizationContext, company_id: UUID, now: datetime) -> None:
    from app.models.company_news import CompanyNews

    cutoff = now - timedelta(days=NEWS_MAX_AGE_DAYS)
    rows = (
        await db.execute(
            select(CompanyNews)
            .where(
                CompanyNews.company_id == company_id,
                CompanyNews.is_actionable.is_(True),
                or_(
                    CompanyNews.published_at.is_(None),
                    CompanyNews.published_at >= cutoff,
                ),
            )
            # Relevance first, then recency: a 10/10 item from six weeks ago is a
            # better hook than a 3/10 from yesterday.
            .order_by(
                CompanyNews.relevance_score.desc().nullslast(),
                CompanyNews.discovered_at.desc(),
            )
            .limit(MAX_NEWS_ITEMS)
        )
    ).scalars().all()
    for item in rows:
        detail = (item.summary or "").strip()
        category = f" ({item.category})" if item.category else ""
        line = f"Recent company news{category}: {item.title.strip()}"
        if detail:
            line += f" — {detail[:200]}"
        ctx.add("company_news", line)


async def _add_intent(db: AsyncSession, ctx: PersonalizationContext, company_id: UUID, now: datetime) -> None:
    from app.models.intent_signal import IntentSignal

    rows = (
        await db.execute(
            select(IntentSignal)
            .where(
                IntentSignal.company_id == company_id,
                or_(IntentSignal.expires_at.is_(None), IntentSignal.expires_at >= now),
            )
            .order_by(IntentSignal.urgency_score.desc(), IntentSignal.detected_at.desc())
            .limit(MAX_INTENT_SIGNALS)
        )
    ).scalars().all()
    for signal in rows:
        line = f"Buying signal ({signal.signal_type}): {signal.title.strip()}"
        if signal.description:
            line += f" — {signal.description.strip()[:200]}"
        ctx.add("intent_signal", line)
        # The angle was written by the intent analyser for exactly this moment;
        # it is guidance to the copywriter, not a fact about the prospect, so it
        # is recorded as its own data point.
        if signal.outreach_angle:
            ctx.add(
                "intent_angle",
                f"Suggested angle for that signal: {signal.outreach_angle.strip()[:200]}",
            )


async def _add_engagement(
    db: AsyncSession, ctx: PersonalizationContext, enrollment_id: UUID, current_step: int
) -> None:
    """What this contact did with the earlier steps of this sequence.

    Only counts events on *prior* steps — the current step hasn't been sent, and
    an event stamped against it would be from a re-dispatch.
    """
    from app.models.outreach import OutreachEvent

    rows = (
        await db.execute(
            select(OutreachEvent.event_type, OutreachEvent.step_number, OutreachEvent.event_metadata)
            .where(OutreachEvent.enrollment_id == enrollment_id)
            .order_by(OutreachEvent.occurred_at.asc())
        )
    ).all()

    sent_steps: list[tuple[int, str]] = []
    opened = clicked = 0
    for event_type, step_number, meta in rows:
        if step_number is None or step_number >= current_step:
            continue
        if event_type == "sent":
            subject = ((meta or {}).get("subject") or "").strip()
            sent_steps.append((step_number, subject))
        elif event_type == "opened":
            opened += 1
        elif event_type == "clicked":
            clicked += 1

    if not sent_steps:
        return

    # Deduplicate by step (a re-dispatch can log twice) and keep the last few.
    by_step: dict[int, str] = {}
    for step_number, subject in sent_steps:
        by_step[step_number] = subject or by_step.get(step_number, "")
    recent = sorted(by_step.items())[-MAX_PRIOR_STEPS:]

    prior = "; ".join(
        f"step {n} ({subject})" if subject else f"step {n}" for n, subject in recent
    )
    ctx.add(
        "prior_touches",
        f"We have already emailed this person {len(by_step)} time(s) in this "
        f"sequence — {prior}. Do not repeat those angles or re-introduce yourself.",
    )

    if clicked:
        ctx.add(
            "engagement",
            f"They clicked a link in an earlier email ({clicked} click(s)) but "
            "have not replied — they are interested and it is safe to be more direct.",
        )
    elif opened:
        ctx.add(
            "engagement",
            f"They opened {opened} of the earlier emails but never replied — the "
            "subject is landing and the ask is not.",
        )


async def _add_discovery(db: AsyncSession, ctx: PersonalizationContext, company_id: UUID) -> None:
    """Facts from the Places record that sourced this company."""
    from app.models.discovered_business import DiscoveredBusiness

    row = (
        await db.execute(
            select(DiscoveredBusiness)
            .where(DiscoveredBusiness.company_id == company_id)
            .order_by(DiscoveredBusiness.discovered_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return
    if row.rating and row.review_count:
        ctx.add(
            "public_reviews",
            f"Publicly rated {row.rating}/5 across {row.review_count} reviews.",
        )
    if row.category:
        ctx.add("business_category", f"Listed business category: {row.category}.")


def _add_firmographics(ctx: PersonalizationContext, contact, company) -> None:
    """Fields already on the two rows the send path holds — no query needed."""
    band = size_band(getattr(company, "employee_count", None))
    if band:
        ctx.add("company_size", f"They are {band}.")

    state = (getattr(company, "state_province", None) or "").strip()
    city = (getattr(company, "city", None) or "").strip()
    if city and state:
        ctx.add("location", f"Based in {city}, {state}.")

    naics = (getattr(company, "naics_description", None) or "").strip()
    if naics:
        ctx.add("naics", f"Classified industry: {naics}.")

    seniority = seniority_of(getattr(contact, "title", None))
    if seniority:
        title = (getattr(contact, "title", None) or "").strip()
        ctx.add(
            "seniority",
            f"You are writing to the {seniority} ({title}) — pitch at that level.",
        )

    department = (getattr(contact, "department", None) or "").strip()
    if department:
        ctx.add("department", f"They work in {department}.")


async def build_context(
    db: AsyncSession,
    *,
    contact,
    company,
    enrollment=None,
    current_step: int = 1,
    now: Optional[datetime] = None,
) -> PersonalizationContext:
    """Assemble everything known about this prospect into prompt-ready facts.

    Never raises. Each source is isolated, so one bad table costs one paragraph
    of specificity rather than the send — see the module docstring, rule 2.
    """
    ctx = PersonalizationContext()
    if now is None:
        now = datetime.now(timezone.utc)

    try:
        _add_firmographics(ctx, contact, company)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Firmographic personalization failed: %s", exc)

    company_id = getattr(company, "id", None)
    if company_id is not None:
        for label, coro in (
            ("news", _add_news(db, ctx, company_id, now)),
            ("intent", _add_intent(db, ctx, company_id, now)),
            ("discovery", _add_discovery(db, ctx, company_id)),
        ):
            try:
                await coro
            except Exception as exc:
                logger.debug("Personalization source %s unavailable: %s", label, exc)

    enrollment_id = getattr(enrollment, "id", None)
    if enrollment_id is not None and current_step and current_step > 1:
        try:
            await _add_engagement(db, ctx, enrollment_id, current_step)
        except Exception as exc:
            logger.debug("Personalization source engagement unavailable: %s", exc)

    return ctx
