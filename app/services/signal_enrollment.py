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
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.company_news import CompanyNews
from app.models.contact import Contact
from app.models.follow_up import FollowUp
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence

logger = logging.getLogger(__name__)

# Enrollment statuses that mean "already being worked" — a contact in one of
# these is NOT an un-enrolled prospect. 'completed'/'replied' count as worked so
# a signal doesn't re-pester someone a past sequence already reached.
_ACTIVE_ENROLLMENT_STATUSES = ("active", "pending_review", "paused", "completed", "replied")


def _emailable(contact: Contact) -> bool:
    return bool(
        contact.email
        and not getattr(contact, "unsubscribed", False)
        and not getattr(contact, "suppressed", False)
    )


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


async def surface_prospects_on_signals(
    db: AsyncSession,
    *,
    fresh_hours: int = 48,
    min_relevance: int = 5,
    auto_enroll: bool = False,
    now: Optional[datetime] = None,
) -> dict:
    """React to fresh company signals by surfacing un-enrolled prospects.

    Returns a summary dict: companies acted on, prospects flagged, prospects
    auto-enrolled, and signals marked actioned. Idempotent per signal via the
    ``actioned`` flag.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=fresh_hours)

    news = list(
        (
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
    )
    if not news:
        return {"companies": 0, "flagged": 0, "enrolled": 0, "signals_actioned": 0}

    # Group signals by company, keeping the strongest as the "trigger" headline.
    signals_by_company: dict = {}
    for n in news:
        signals_by_company.setdefault(n.company_id, []).append(n)

    flagged = enrolled = companies_acted = signals_actioned = 0
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
        emailable = [c for c in contacts if _emailable(c)]
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

        # Strongest signal drives the headline shown to the operator.
        trigger = max(
            items, key=lambda n: (n.relevance_score or 0, n.discovered_at or now)
        )
        headline = (trigger.title or "company signal").strip()[:200]
        category = trigger.category or "news"

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

        for n in items:
            n.actioned = True
            signals_actioned += 1
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
        "Signal enrollment: %d companies, %d flagged, %d enrolled, %d signals actioned",
        companies_acted, flagged, enrolled, signals_actioned,
    )
    return {
        "companies": companies_acted,
        "flagged": flagged,
        "enrolled": enrolled,
        "signals_actioned": signals_actioned,
    }
