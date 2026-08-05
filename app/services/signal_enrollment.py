"""Signal-based outreach timing (Feature 6).

Cold outreach lands best when it rides a *reason to reach out now*. When a
company just raised, hired a leader, or announced an expansion, its buyers are
suddenly attentive and budget-holders are moving — the single best moment to
show up. The news monitor already records those signals as ``CompanyNews`` rows
(``category``, ``relevance_score``, ``is_actionable``); nothing acted on them for
outreach.

This service closes that loop. On a fresh, relevant signal it finds the
company's *un-enrolled* emailable contacts (prospects not already in an active
sequence) and either:

* **flags them for priority outreach** — a due-now ``FollowUp`` naming the signal
  so a human sends while the lead is warm (the safe default), or
* **auto-enrolls them** into the company's resolved vertical sequence
  (``signal_enrollment_auto_enroll``) — reserved for orgs that have opted into
  fully autonomous reactivation.

Processed signals are marked ``actioned`` so a company is surfaced once per
signal, not once per sweep. The heavy lifting is a plain async function so it is
unit-testable against a session; the Celery task in :mod:`app.tasks.news` wraps
it per-org.

**Two signal tables, one reaction.** ``CompanyNews`` is what the press said;
``IntentSignal`` is ``intent_monitor``'s scored read of a *buying* event — a
Quality Engineer role posted, a raise, a new plant — and it is the stronger of
the two, because relevance and urgency were assessed against our value prop
rather than inferred from a headline. It was consumed in exactly two places:
``scoring`` folded it into ``fit_score``, and ``personalization`` handed it to
the copywriter. Both improve a message that was going to be sent anyway. Neither
causes an email to exist, so the strongest buying signal this system can detect
still waited for the next daily sweep to reach that company on the merits.

Both sources now feed the same reaction through :class:`_Signal`. Their scales
differ and are kept apart on purpose — ``CompanyNews.relevance_score`` is 1-10,
``IntentSignal`` scores relevance and urgency 0-100 each — so ``min_relevance``
and ``min_intent_score`` are separate arguments. Reusing one threshold across
both admits every intent signal ever recorded.

Intent signals take the *same* safe default as news: a ``FollowUp`` for a human,
unless the org has opted into ``signal_enrollment_auto_enroll``. The open
question on the roadmap was whether a hot signal should bypass the human review
gate; it does not. A signal is evidence about timing, and the case for skipping
review would have to be about the copy, which no signal speaks to.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.company_news import CompanyNews
from app.models.contact import Contact
from app.models.follow_up import FollowUp
from app.services.email_validation import is_role_address
from app.models.intent_signal import IntentSignal
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence

logger = logging.getLogger(__name__)

# Enrollment statuses that mean "already being worked" — a contact in one of
# these is NOT an un-enrolled prospect. 'completed'/'replied' count as worked so
# a signal doesn't re-pester someone a past sequence already reached.
_ACTIVE_ENROLLMENT_STATUSES = ("active", "pending_review", "paused", "completed", "replied")

# Minimum ``IntentSignal.combined_score`` (relevance×0.6 + urgency×0.4) worth
# interrupting for. The monitor already drops anything under 30 relevance, so
# this is a second, higher bar for *acting* rather than merely recording: at 45
# a signal is either strongly relevant or urgent enough that the timing argument
# stands on its own.
DEFAULT_MIN_INTENT_SCORE = 45.0


@dataclass
class _Signal:
    """A reason to reach out now, from either source table.

    Normalised so the reaction below does not branch per source. ``row`` is kept
    so the ``actioned`` flag is set on the real record — that flag is the
    idempotency key, and marking a copy would surface the same company on every
    sweep forever.
    """

    company_id: UUID
    headline: str
    category: str
    strength: float  # comparable *within* a source, not across them
    detected_at: datetime
    row: Any
    kind: str  # 'news' | 'intent'


async def _emailable(db: AsyncSession, contact: Contact) -> bool:
    """Whether this contact may be acted on at all.

    ``suppression.blocking_reason``, not the two contact flags this used to
    read. The missing third clause is the org do-not-contact LIST, and it is the
    one that cannot be inferred from the row in front of you: ``add_entry``
    deliberately does not touch ``Contact.suppressed``, because an address
    outlives every contact row that carries it. A compliance CSV upload, a
    bounce recorded against a re-scraped duplicate, and an erasure whose subject
    the scraper later found again all leave exactly this shape — clean flags, an
    address that must never be mailed.

    Gates the follow-up branch as well as the enrollment one, which is why it is
    a predicate over the contact rather than a filter on the enrol path: putting
    "reach out while the lead is warm" on an operator's task list for someone
    who unsubscribed is the same mistake with a human carrying it out.
    """
    from app.services import suppression as _suppression

    return await _suppression.blocking_reason(db, contact) is None


async def _emailable_subset(db: AsyncSession, contacts: list[Contact]) -> list[Contact]:
    """:func:`_emailable` over a company's contacts, in one list query.

    The flags are already loaded, so they filter for free and shrink the set of
    addresses the list has to be asked about — which matters because this runs
    per company inside the sweep, and a per-contact existence check would make
    it N+1 in the loop.
    """
    from app.services import suppression as _suppression

    candidates = [c for c in contacts if _suppression.blocking_flag(c) is None]
    if not candidates:
        return []
    listed = await _suppression.suppressed_addresses(db, [c.email for c in candidates])
    if not listed:
        return candidates
    return [
        c for c in candidates
        if _suppression.normalize_email(c.email) not in listed
    ]


async def _resolve_sequence_for_company(
    db: AsyncSession, company: Company
) -> Optional[OutreachSequence]:
    """The active vertical sequence for a company, or None.

    Resolves the company's industry/name to a vertical, then finds the seeded
    sequence tagged with that vertical key. Only used on the auto-enroll path.
    """
    from app.services.industry_verticals import resolve_vertical

    vertical = resolve_vertical(company.industry_segment or "", company.name or "")
    if vertical is None:
        return None
    return (
        await db.execute(
            select(OutreachSequence)
            .where(
                OutreachSequence.industry == vertical.key,
                OutreachSequence.is_active.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _collect_news(
    db: AsyncSession, *, cutoff: datetime, min_relevance: int
) -> list[_Signal]:
    """Fresh, unactioned, relevant press signals."""
    rows = (
        await db.execute(
            select(CompanyNews).where(
                CompanyNews.discovered_at >= cutoff,
                CompanyNews.actioned.is_(False),
                or_(
                    CompanyNews.is_actionable.is_(True),
                    CompanyNews.relevance_score >= min_relevance,
                ),
            )
        )
    ).scalars().all()
    return [
        _Signal(
            company_id=row.company_id,
            headline=(row.title or "company signal").strip()[:200],
            category=row.category or "news",
            strength=float(row.relevance_score or 0),
            detected_at=row.discovered_at,
            row=row,
            kind="news",
        )
        for row in rows
    ]


async def _collect_intent(
    db: AsyncSession, *, cutoff: datetime, now: datetime, min_score: float
) -> list[_Signal]:
    """Fresh, unactioned, *live* buyer-intent signals above the acting bar.

    Live matters as much as fresh here. ``detected_at >= cutoff`` says we only
    just learned it; ``expires_at`` is the buying window the analyst assigned,
    and a signal past its window is a fact about the past. Both must hold — a
    signal detected an hour ago with a window that already closed describes a
    purchase that has happened without us.
    """
    rows = (
        await db.execute(
            select(IntentSignal).where(
                IntentSignal.detected_at >= cutoff,
                IntentSignal.actioned.is_(False),
                or_(
                    IntentSignal.expires_at.is_(None),
                    IntentSignal.expires_at >= now,
                ),
            )
        )
    ).scalars().all()
    signals = []
    for row in rows:
        combined = row.combined_score
        if combined < min_score:
            continue
        signals.append(
            _Signal(
                company_id=row.company_id,
                headline=(row.title or "buying signal").strip()[:200],
                category=row.signal_type or "intent",
                strength=float(combined),
                detected_at=row.detected_at,
                row=row,
                kind="intent",
            )
        )
    return signals


async def surface_prospects_on_signals(
    db: AsyncSession,
    *,
    fresh_hours: int = 48,
    min_relevance: int = 5,
    auto_enroll: bool = False,
    min_intent_score: float = DEFAULT_MIN_INTENT_SCORE,
    include_intent: bool = True,
    max_contacts_per_company: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict:
    """React to fresh company signals by surfacing un-enrolled prospects.

    Reads both signal tables — press (``CompanyNews``) and scored buyer intent
    (``IntentSignal``). Returns a summary dict: companies acted on, prospects
    flagged, prospects auto-enrolled, and signals marked actioned, with the
    actioned count split by source so a change in either is attributable.
    Idempotent per signal via each row's ``actioned`` flag.

    ``max_contacts_per_company`` caps the fan-out per company (default
    ``settings.max_contacts_per_company_per_signal``); see the ranking note at
    the cap itself. A value below 1 is treated as uncapped.
    """
    now = now or datetime.now(timezone.utc)
    if max_contacts_per_company is None:
        from app.config import get_settings

        max_contacts_per_company = get_settings().max_contacts_per_company_per_signal
    if max_contacts_per_company < 1:
        max_contacts_per_company = None  # uncapped
    cutoff = now - timedelta(hours=fresh_hours)

    signals = await _collect_news(db, cutoff=cutoff, min_relevance=min_relevance)
    if include_intent:
        signals += await _collect_intent(
            db, cutoff=cutoff, now=now, min_score=min_intent_score
        )

    empty = {
        "companies": 0, "flagged": 0, "enrolled": 0,
        "signals_actioned": 0, "news_actioned": 0, "intent_actioned": 0,
    }
    if not signals:
        return empty

    # Group signals by company, keeping the strongest as the "trigger" headline.
    signals_by_company: dict = {}
    for n in signals:
        signals_by_company.setdefault(n.company_id, []).append(n)

    flagged = enrolled = companies_acted = signals_actioned = 0
    news_actioned = intent_actioned = 0
    enrollment_dispatch: list[str] = []

    for company_id, items in signals_by_company.items():
        company = (
            await db.execute(select(Company).where(Company.id == company_id))
        ).scalar_one_or_none()
        if company is None:
            continue

        # Emailable contacts at this company.
        contacts = list(
            (
                await db.execute(select(Contact).where(Contact.company_id == company_id))
            ).scalars().all()
        )
        emailable = await _emailable_subset(db, contacts)
        if not emailable:
            continue

        # Which of them are already being worked?
        enrolled_ids = {
            row[0]
            for row in (
                await db.execute(
                    select(OutreachEnrollment.contact_id).where(
                        OutreachEnrollment.contact_id.in_([c.id for c in emailable]),
                        OutreachEnrollment.status.in_(_ACTIVE_ENROLLMENT_STATUSES),
                    )
                )
            ).all()
        }
        prospects = [c for c in emailable if c.id not in enrolled_ids]
        if not prospects:
            continue

        # Rank, then cap. Acting on every contact at a company turns one signal
        # into a simultaneous multi-recipient blast — four addresses, one buyer,
        # and a recipient mail server watching an unknown sender hit four
        # mailboxes at once. ``send_limiter.can_send_to_company`` is the
        # authoritative brake (it covers every enrolment path), but creating the
        # rows here anyway would just park colleagues in a queue that drains one
        # per day for a week; better to pick the best few and leave the rest
        # un-enrolled, where a later signal or the prospector can still reach
        # them.
        #
        # Ranking prefers the primary contact, then a named person over a shared
        # inbox: ``info@`` is a real mailbox but a queue somebody triages, and
        # cold mail is the first thing that queue deletes.
        prospects.sort(
            key=lambda c: (
                not bool(getattr(c, "is_primary", False)),
                is_role_address(c.email),
                (c.email or "").lower(),
            )
        )
        prospects = prospects[:max_contacts_per_company]

        # Strongest signal drives the headline shown to the operator. Intent
        # outranks press at equal recency regardless of the numbers: the scales
        # are not comparable (1-10 against 0-100), and an intent signal was
        # scored against our value proposition where a news item was only
        # scored for being about this company.
        trigger = max(
            items,
            key=lambda n: (n.kind == "intent", n.strength, n.detected_at or now),
        )
        headline = trigger.headline
        category = trigger.category

        sequence = None
        if auto_enroll:
            sequence = await _resolve_sequence_for_company(db, company)

        for contact in prospects:
            if auto_enroll and sequence is not None:
                enrollment = OutreachEnrollment(
                    contact_id=contact.id, sequence_id=sequence.id
                )
                db.add(enrollment)
                await db.flush()
                enrollment_dispatch.append(str(enrollment.id))
                enrolled += 1
            else:
                db.add(
                    FollowUp(
                        contact_id=contact.id,
                        company_id=company_id,
                        due_date=now,
                        reason=(
                            f"PRIORITY: {company.name} — fresh {category} signal "
                            f"(\"{headline}\"). Reach out while the lead is warm."
                        )[:500],
                        status="pending",
                    )
                )
                flagged += 1

        # Marked on the underlying row, not the normalised copy — the flag is
        # the idempotency key for the next sweep.
        for n in items:
            n.row.actioned = True
            signals_actioned += 1
            if n.kind == "intent":
                intent_actioned += 1
            else:
                news_actioned += 1
        companies_acted += 1

    await db.commit()

    if enrollment_dispatch:
        try:
            from app.tasks.outreach import send_sequence_step

            for eid in enrollment_dispatch:
                send_sequence_step.delay(eid)
        except Exception as exc:  # pragma: no cover - broker-dependent
            logger.warning("Could not dispatch signal-enrolled step(s): %s", exc)

    logger.info(
        "Signal enrollment: %d companies, %d flagged, %d enrolled, %d signals "
        "actioned (%d news, %d intent)",
        companies_acted, flagged, enrolled, signals_actioned,
        news_actioned, intent_actioned,
    )
    return {
        "companies": companies_acted,
        "flagged": flagged,
        "enrolled": enrolled,
        "signals_actioned": signals_actioned,
        "news_actioned": news_actioned,
        "intent_actioned": intent_actioned,
    }
