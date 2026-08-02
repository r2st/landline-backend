"""Which enrollments get today's sends, when there are not enough to go round.

``process_outreach_queue`` selects every active enrollment and walks them in
whatever order the database happens to return:

    select(OutreachEnrollment).where(OutreachEnrollment.status == "active")

No ``ORDER BY``. That is harmless while capacity is unlimited and quietly
destructive the moment it is not — and it is not. A warm-up plan opens a new
mailbox at 5 sends a day (``services/warmup.py``), the org daily cap latches the
sweep off for the rest of the run once it trips, and ``can_send_to_contact``
rations per recipient. With 40 active enrollments against a 5/day cap, 35
enrollments do not send today. Which 35 was decided by row order.

Two things follow, and the second is the serious one:

1. **The capacity went somewhere arbitrary.** A step-3 email to someone who
   clicked step 2 is worth many multiples of a cold step-1 to a role inbox
   scraped off a Places listing, and today they are equally likely to get the
   slot.
2. **The same enrollments lose every day.** An unordered sequential scan is not
   random — it is stable. Postgres returns roughly physical order, so the rows
   that lost yesterday's capacity are in the same place in tomorrow's result
   set, behind the same winners. A contact past the cap line is not delayed;
   they are *starved*, indefinitely, and nothing in the system reports it. This
   is the failure that motivated the module: an unfair order that changes is a
   nuisance, an unfair order that repeats is a contact who is never emailed.

:func:`rank` fixes the order. It decides nothing about *whether* a send happens
— every gate in the sweep (kill switch, quota, suppression, send window, invalid
domain, smart send-time hold) runs unchanged, on the same enrollments. This
module only chooses who reaches those gates first.

**The score is value plus waiting, and waiting is unbounded.** Value is capped
at :data:`MAX_VALUE_POINTS`; wait pressure grows at :data:`WAIT_POINTS_PER_DAY`
forever. So an enrollment with nothing going for it overtakes a perfect one
after ``MAX_VALUE_POINTS / WAIT_POINTS_PER_DAY`` days of being overdue — around
three and a half weeks at the current constants, and
:func:`wait_breakeven_days` is the authority on the number rather than this
sentence. That crossing point is the whole design:
a pure value ranking is precisely the starvation bug above, restated with better
taste, because the same low-value rows would lose forever on purpose instead of
by accident. Value decides who goes first; waiting guarantees everyone goes.

Every lookup is batched across the whole sweep — four queries total, regardless
of enrollment count. The sweep already goes out of its way to avoid per-
enrollment queries (see the ``step_channels`` batch in ``tasks/outreach.py``),
and a ranking pass that reintroduced them would cost more than the ordering is
worth.

:func:`rank` never raises and never drops or duplicates an enrollment: on any
failure it returns the input list unchanged, because an unordered sweep is the
status quo and a sweep that does not run is an outage.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# --- Wait pressure -----------------------------------------------------------
# Points per day an enrollment has been due and unsent. Unbounded on purpose —
# this is the anti-starvation term and capping it would recreate the bug.
WAIT_POINTS_PER_DAY = 4.0

# --- Value terms (all bounded) ----------------------------------------------
# Engagement on a *prior* step of this enrollment. Taken as the strongest single
# signal rather than a sum: someone who clicked also opened, and counting both
# would rate one interested contact above two.
CLICK_POINTS = 30.0
OPEN_POINTS = 14.0

# Touches already spent on this enrollment. A mid-sequence contact is in a live
# thread that cost real sends to open; abandoning them at step 3 throws that
# away and leaves a half-finished conversation, which is worse than never having
# started one. Capped so a long sequence cannot outrank live engagement on
# length alone.
STEP_POINTS = 6.0
MAX_STEP_POINTS = 18.0

# Firmographic fit, from the company's best LeadScore. Deliberately the smallest
# term: fit is a prediction, engagement is an observation, and the observation
# should win where they disagree.
MAX_FIT_POINTS = 16.0

# A live buyer-intent signal (hiring, funding, expansion) inside its buying
# window. Scaled by the signal's own relevance/urgency blend, which is what
# ``IntentSignal.combined_score`` already means.
MAX_INTENT_POINTS = 22.0

# --- Who the mail is actually addressed to -----------------------------------
# The paradigm low-value case in this module's own docstring is "a cold step-1 to
# a role inbox scraped off a Places listing" — and until now nothing scored it.
# Both terms below are about the *recipient*, not the company, and both are
# deliberately smaller than engagement: they are priors, and one open outranks
# any prior.
#
# A named person versus a shared inbox. ``info@`` is a real mailbox — which is
# why ``is_non_prospect_address`` lets it through and why it still gets sent —
# but it lands in a queue triaged by whoever is on rota, and cold mail is the
# first thing that queue deletes. Round 9's scraper fix deliberately keeps these
# so SMB shops aren't stranded, which is right, and makes ordering them behind a
# named colleague the place to express the difference.
#
# Both are sized so their *sum* stays under :data:`OPEN_POINTS`. A single open is
# an observation and these are guesses about a stranger; a named, verified
# address that has never responded must not displace a shared inbox that has.
NAMED_CONTACT_POINTS = 8.0
# A verified-deliverable address versus a guess. When Apollo and Hunter are out
# of credits ``email_pattern_generator`` *derives* addresses and writes them
# ``unknown`` on purpose; those are the sends most likely to become a hard
# bounce, and a hard bounce is charged against the global rate the kill switch
# reads. Spending scarce capacity on the proven address first is free insurance.
VERIFIED_ADDRESS_POINTS = 4.0
MAX_RECIPIENT_POINTS = NAMED_CONTACT_POINTS + VERIFIED_ADDRESS_POINTS

# The ceiling of everything above. Kept as a derived constant so the docstring's
# crossing-point claim cannot drift away from the constants it is computed from.
MAX_VALUE_POINTS = (
    CLICK_POINTS + MAX_STEP_POINTS + MAX_FIT_POINTS + MAX_INTENT_POINTS
    + MAX_RECIPIENT_POINTS
)

# Enrollment ids per ``IN`` clause. Large sweeps chunk rather than building one
# unbounded parameter list.
CHUNK = 500

# Event types that count as engagement, strongest first.
_ENGAGEMENT_POINTS = (("clicked", CLICK_POINTS), ("opened", OPEN_POINTS))


def wait_breakeven_days() -> float:
    """Days overdue after which wait pressure outweighs any possible value."""
    return MAX_VALUE_POINTS / WAIT_POINTS_PER_DAY


@dataclass
class Score:
    """One enrollment's priority, with the reason it got it.

    The breakdown is not decoration: "why did this contact not send for a week"
    is the question this module exists to answer, and a bare float cannot.
    """

    enrollment_id: UUID
    total: float = 0.0
    parts: dict = field(default_factory=dict)

    def add(self, name: str, points: float) -> None:
        if points <= 0:
            return
        self.parts[name] = round(points, 2)
        self.total = round(self.total + points, 3)


@dataclass
class _Facts:
    """Batched lookups for one sweep, keyed for O(1) access per enrollment."""

    sent_counts: dict[UUID, int] = field(default_factory=dict)
    last_sent_at: dict[UUID, datetime] = field(default_factory=dict)
    engagement: dict[UUID, set[str]] = field(default_factory=lambda: defaultdict(set))
    company_of: dict[UUID, UUID] = field(default_factory=dict)      # contact -> company
    fit_of: dict[UUID, int] = field(default_factory=dict)           # company -> fit_score
    intent_of: dict[UUID, float] = field(default_factory=dict)      # company -> combined
    named_contacts: set[UUID] = field(default_factory=set)          # not a role inbox
    verified_contacts: set[UUID] = field(default_factory=set)       # proven deliverable


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Attach UTC to a naive timestamp. SQLite drops tzinfo on storage."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _chunks(values: Sequence, size: int = CHUNK) -> Iterable[Sequence]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


async def _load_events(db: AsyncSession, facts: _Facts, enrollment_ids: list[UUID]) -> None:
    """Sends, engagement and last-send time per enrollment, in one grouped query.

    Grouped rather than fetching rows: an enrollment on step 4 with opens and
    clicks has a dozen events, and the ranking needs three aggregates from them.
    """
    from app.models.outreach import OutreachEvent

    for chunk in _chunks(enrollment_ids):
        rows = (
            await db.execute(
                select(
                    OutreachEvent.enrollment_id,
                    OutreachEvent.event_type,
                    func.count(OutreachEvent.id),
                    func.max(OutreachEvent.occurred_at),
                )
                .where(OutreachEvent.enrollment_id.in_(chunk))
                .group_by(OutreachEvent.enrollment_id, OutreachEvent.event_type)
            )
        ).all()
        for enrollment_id, event_type, count, latest in rows:
            if event_type == "sent":
                facts.sent_counts[enrollment_id] = int(count or 0)
                latest = _aware(latest)
                if latest is not None:
                    facts.last_sent_at[enrollment_id] = latest
            elif event_type in ("opened", "clicked"):
                facts.engagement[enrollment_id].add(event_type)


async def _load_company_signals(
    db: AsyncSession, facts: _Facts, contact_ids: list[UUID], now: datetime
) -> None:
    """Contact → company, then the company's best fit score and live intent.

    The contact pass also picks up the two recipient-level priors, out of the
    same rows: adding a second query over the same primary keys to read two more
    columns would be exactly the per-enrollment cost this module goes out of its
    way to avoid.
    """
    from app.models.contact import Contact
    from app.models.intent_signal import IntentSignal
    from app.models.lead_score import LeadScore
    from app.services.email_validation import is_role_address

    for chunk in _chunks(contact_ids):
        rows = (
            await db.execute(
                select(
                    Contact.id,
                    Contact.company_id,
                    Contact.email,
                    Contact.email_verification_result,
                ).where(Contact.id.in_(chunk))
            )
        ).all()
        for contact_id, company_id, email, verification in rows:
            if company_id is not None:
                facts.company_of[contact_id] = company_id
            # An address we cannot parse is not credited as a named person; the
            # absence of a bonus is the whole penalty, and a contact with no
            # address is stopped by a gate long before ordering matters.
            if email and not is_role_address(email):
                facts.named_contacts.add(contact_id)
            # Only a positive verdict counts. ``unknown`` is the default a
            # pattern-derived guess carries and ``risky`` is what a catch-all
            # domain returns — neither is proof of a mailbox.
            if verification == "deliverable":
                facts.verified_contacts.add(contact_id)

    company_ids = list({c for c in facts.company_of.values()})
    if not company_ids:
        return

    for chunk in _chunks(company_ids):
        # Best fit across score types: a company scored both rule_based and ai
        # has two rows, and the ranking wants one number.
        rows = (
            await db.execute(
                select(LeadScore.company_id, func.max(LeadScore.fit_score))
                .where(LeadScore.company_id.in_(chunk))
                .group_by(LeadScore.company_id)
            )
        ).all()
        for company_id, fit in rows:
            facts.fit_of[company_id] = int(fit or 0)

        # Live signals only. ``expires_at`` is the buying window the analyst
        # assigned; a signal past it is history, not intent. Relevance and
        # urgency are blended in Python because ``max(relevance)`` and
        # ``max(urgency)`` can come from two different signals, and their
        # combination would describe a signal that does not exist.
        rows = (
            await db.execute(
                select(
                    IntentSignal.company_id,
                    IntentSignal.relevance_score,
                    IntentSignal.urgency_score,
                )
                .where(IntentSignal.company_id.in_(chunk))
                .where(
                    or_(IntentSignal.expires_at.is_(None), IntentSignal.expires_at >= now)
                )
            )
        ).all()
        for company_id, relevance, urgency in rows:
            combined = (int(relevance or 0) * 0.6) + (int(urgency or 0) * 0.4)
            if combined > facts.intent_of.get(company_id, 0.0):
                facts.intent_of[company_id] = combined


def _waiting_since(enrollment, facts: _Facts) -> Optional[datetime]:
    """When this enrollment started waiting for the send it has not had.

    ``next_send_at`` when the scheduler set one — that is the moment the step
    became due. Otherwise the anchor the legacy due-date path uses: the last
    send, or the enrollment itself for a first touch. Deliberately *not*
    re-deriving the true due date through ``step_delay`` — that needs the step
    row per enrollment, and this is a tiebreak, not a gate. Anything not yet due
    is filtered out below by comparing against ``now``.
    """
    scheduled = _aware(getattr(enrollment, "next_send_at", None))
    if scheduled is not None:
        return scheduled
    last = facts.last_sent_at.get(enrollment.id)
    if last is not None:
        return last
    return _aware(getattr(enrollment, "enrolled_at", None))


def _score_one(enrollment, facts: _Facts, now: datetime) -> Score:
    score = Score(enrollment_id=enrollment.id)

    waiting_since = _waiting_since(enrollment, facts)
    if waiting_since is not None:
        overdue_days = (now - waiting_since).total_seconds() / 86400.0
        if overdue_days > 0:
            score.add("waiting", WAIT_POINTS_PER_DAY * overdue_days)

    signals = facts.engagement.get(enrollment.id) or set()
    for event_type, points in _ENGAGEMENT_POINTS:
        if event_type in signals:
            score.add(f"engaged_{event_type}", points)
            break  # strongest signal only

    sent = facts.sent_counts.get(enrollment.id, 0)
    if sent > 0:
        score.add("in_flight", min(STEP_POINTS * sent, MAX_STEP_POINTS))

    if enrollment.contact_id in facts.named_contacts:
        score.add("named_recipient", NAMED_CONTACT_POINTS)
    if enrollment.contact_id in facts.verified_contacts:
        score.add("verified_address", VERIFIED_ADDRESS_POINTS)

    company_id = facts.company_of.get(enrollment.contact_id)
    if company_id is not None:
        fit = facts.fit_of.get(company_id, 0)
        if fit > 0:
            score.add("fit", MAX_FIT_POINTS * min(fit, 100) / 100.0)
        intent = facts.intent_of.get(company_id, 0.0)
        if intent > 0:
            score.add("intent", MAX_INTENT_POINTS * min(intent, 100.0) / 100.0)

    return score


async def score_enrollments(db: AsyncSession, enrollments, *, now: datetime) -> dict[UUID, Score]:
    """Score every enrollment. Exposed separately so the breakdown is testable."""
    facts = _Facts()
    enrollment_ids = [e.id for e in enrollments]
    contact_ids = list({e.contact_id for e in enrollments if e.contact_id is not None})

    await _load_events(db, facts, enrollment_ids)
    if contact_ids:
        await _load_company_signals(db, facts, contact_ids, now)

    return {e.id: _score_one(e, facts, now) for e in enrollments}


async def rank(db: AsyncSession, enrollments, *, now: Optional[datetime] = None) -> list:
    """Order ``enrollments`` most-deserving first. Same rows, same count.

    Ties resolve on ``enrolled_at`` then id, so the order is total and stable:
    two enrollments that genuinely tie must not swap places between sweeps, or
    the pair takes turns losing the last slot and neither ever completes.
    """
    items = list(enrollments)
    if len(items) < 2:
        return items

    now = now or datetime.now(timezone.utc)
    try:
        scores = await score_enrollments(db, items, now=now)
    except Exception as exc:  # never let ranking cost a sweep
        logger.warning("Queue prioritization failed, falling back to unordered: %s", exc)
        return items

    def key(enrollment):
        score = scores.get(enrollment.id)
        enrolled = _aware(getattr(enrollment, "enrolled_at", None)) or now
        return (-(score.total if score else 0.0), enrolled, str(enrollment.id))

    ordered = sorted(items, key=key)
    if len(ordered) != len(items):  # pragma: no cover - defensive
        return items

    top = ordered[0]
    logger.debug(
        "Queue prioritized: %d enrollments, top=%s score=%.1f %s",
        len(ordered), top.id,
        scores[top.id].total if top.id in scores else 0.0,
        scores[top.id].parts if top.id in scores else {},
    )
    return ordered
