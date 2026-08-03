"""Shared per-org lead-generation tallies for the leads/Dialtone tracking API.

Sends live in two tables (see CLAUDE.md "Sent mail lives in two tables") —
``EmailSend`` for manual/template/reply/smart-reply sends, ``OutreachEvent``
for the sequence engine. Both are unioned here, mirroring
``app.routers.dialtone._period_counts``, but grouped by ``org_id`` so a
``platform_admin`` caller (whose requests bypass the global org filter — see
``app.models.mixins``) gets a real cross-tenant breakdown instead of one
merged total. A non-admin's queries are already scoped to their own org by
that same global filter, so the same functions serve both callers unchanged.
"""

import uuid
from collections import Counter, defaultdict
from datetime import datetime
from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.prospecting import ProspectCandidate
from app.models.tenant import Organization
from app.schemas.lead_tracking import RecentLeadEvent
from app.services.timeframes import ensure_aware, in_range

# EmailSend statuses that represent a delivery failure (not a bounce).
_ERROR_STATUSES = {"error", "failed"}

# OutreachEvent.event_type -> the counter it increments / the feed label it maps to.
_OUTREACH_COUNTERS = {
    "sent": "emails_sent",
    "opened": "opens",
    "clicked": "clicks",
    "replied": "replies",
    "bounced": "bounces",
}
_OUTREACH_TYPE_MAP = {
    "sent": "email_sent",
    "opened": "email_opened",
    "clicked": "email_clicked",
    "replied": "reply",
    "bounced": "bounced",
}

# Company.stage values that represent a qualified-or-later lead (see the check
# constraint in app.models.company).
_QUALIFIED_STAGES = ("qualified", "opportunity", "customer")


def parse_iso_bound(value: Optional[str]) -> Optional[datetime]:
    """Parse a ``start``/``end`` query value (ISO date or datetime) to aware UTC.

    Tolerates the ``YYYY-MM-DD`` shape date pickers send; returns ``None`` on
    empty/invalid input so a bad param never 500s the endpoint.
    """
    if not value:
        return None
    try:
        return ensure_aware(datetime.fromisoformat(value))
    except (ValueError, TypeError):
        return None


def rate(numerator: int, denominator: int) -> float:
    """Fraction (0..1), 0 when the denominator is 0 (never divide by zero)."""
    return round(numerator / denominator, 4) if denominator else 0.0


def funnel_rates(counts: Counter) -> tuple[float, float, float, float]:
    """(open_rate, click_rate, reply_rate, bounce_rate) for one tally Counter."""
    sent = counts.get("emails_sent", 0)
    attempted = sent + counts.get("bounces", 0)
    return (
        rate(counts.get("opens", 0), sent),
        rate(counts.get("clicks", 0), sent),
        rate(counts.get("replies", 0), sent),
        rate(counts.get("bounces", 0), attempted),
    )


async def tally_by_org(
    db: AsyncSession, start: Optional[datetime], end: Optional[datetime]
) -> dict[Optional[uuid.UUID], Counter]:
    """org_id -> Counter(emails_sent/opens/clicks/replies/bounces/errors/leads_discovered)
    for the half-open ``[start, end)`` window (``None`` bound = unbounded).
    """
    tallies: dict[Optional[uuid.UUID], Counter] = defaultdict(Counter)

    send_cols = select(
        EmailSend.org_id, EmailSend.status, EmailSend.sent_at,
        EmailSend.opened_at, EmailSend.clicked_at, EmailSend.replied_at,
    )
    for org_id, status, sent_at, opened_at, clicked_at, replied_at in (
        await db.execute(send_cols)
    ).all():
        if status == "skipped":
            continue
        c = tallies[org_id]
        if in_range(sent_at, start, end):
            if status in _ERROR_STATUSES:
                c["errors"] += 1
            elif status == "bounced":
                c["bounces"] += 1
            else:
                c["emails_sent"] += 1
        if in_range(opened_at, start, end):
            c["opens"] += 1
        if in_range(clicked_at, start, end):
            c["clicks"] += 1
        if in_range(replied_at, start, end):
            c["replies"] += 1

    event_cols = select(OutreachEvent.org_id, OutreachEvent.event_type, OutreachEvent.occurred_at)
    for org_id, event_type, occurred_at in (await db.execute(event_cols)).all():
        metric = _OUTREACH_COUNTERS.get(event_type)
        if metric and in_range(occurred_at, start, end):
            tallies[org_id][metric] += 1

    cand_cols = select(ProspectCandidate.org_id, ProspectCandidate.created_at)
    for org_id, created_at in (await db.execute(cand_cols)).all():
        if in_range(created_at, start, end):
            tallies[org_id]["leads_discovered"] += 1

    return tallies


async def leads_and_deals_by_org(
    db: AsyncSession, start: Optional[datetime], end: Optional[datetime]
) -> dict[Optional[uuid.UUID], dict]:
    """org_id -> {"leads_qualified", "deals_created", "deals_won"} for the window.

    ``leads_qualified`` uses ``Company.updated_at`` as a proxy for "reached a
    qualified-or-later stage in this window" — there is no dedicated
    stage-transition timestamp, so a company touched for an unrelated reason
    while already qualified is (harmlessly) recounted each time it's touched.
    ``deals_won`` uses ``Deal.updated_at`` the same way, since there's no
    dedicated won-at column either.
    """
    out: dict[Optional[uuid.UUID], dict] = defaultdict(
        lambda: {"leads_qualified": 0, "deals_created": 0, "deals_won": 0}
    )

    company_cols = (
        select(Company.org_id, Company.updated_at)
        .where(Company.stage.in_(_QUALIFIED_STAGES))
    )
    for org_id, updated_at in (await db.execute(company_cols)).all():
        if in_range(updated_at, start, end):
            out[org_id]["leads_qualified"] += 1

    deal_cols = select(Deal.org_id, Deal.status, Deal.created_at, Deal.updated_at)
    for org_id, status, created_at, updated_at in (await db.execute(deal_cols)).all():
        if in_range(created_at, start, end):
            out[org_id]["deals_created"] += 1
        if status == "won" and in_range(updated_at, start, end):
            out[org_id]["deals_won"] += 1

    return out


def window_clause(
    start: Optional[datetime], end: Optional[datetime], *columns
):
    """SQL predicate for "any of ``columns`` lands in the half-open window".

    Mirrors :func:`app.services.timeframes.in_range` — inclusive lower bound,
    exclusive upper, a ``None`` bound unbounded on that side, and a NULL column
    never a match. Returns ``None`` when both bounds are open, so the caller can
    skip the ``.where()`` entirely.

    A row is kept when *any* of its timestamps qualifies because one row can
    emit several events at different times: an ``EmailSend`` sent in March and
    replied to in May belongs to the May window on the strength of
    ``replied_at`` alone.
    """
    if start is None and end is None:
        return None
    legs = []
    for col in columns:
        bounds = [col.isnot(None)]
        if start is not None:
            bounds.append(col >= start)
        if end is not None:
            bounds.append(col < end)
        legs.append(and_(*bounds))
    return or_(*legs)


async def org_names(db: AsyncSession, org_ids) -> dict[uuid.UUID, str]:
    """Resolve org ids to names (Organization is not org-scoped, so no filter applies)."""
    ids = [i for i in org_ids if i is not None]
    if not ids:
        return {}
    rows = (
        await db.execute(select(Organization.id, Organization.name).where(Organization.id.in_(ids)))
    ).all()
    return {oid: name for oid, name in rows}


async def recent_events(
    db: AsyncSession,
    *,
    start: Optional[datetime],
    end: Optional[datetime],
    cap: int = 5000,
) -> list[RecentLeadEvent]:
    """Recent lead-activity events across EmailSend, OutreachEvent, and lead
    discovery — the read-side union behind ``GET /leads/recent``. ``cap`` bounds
    how many rows are pulled per source so a huge history can't blow up the
    response; the caller sorts/paginates the merged result.

    **The window is a SQL predicate, not a post-filter.** ``cap`` truncates to
    the newest rows, so applying the window afterwards means the window can only
    ever see the most recent ``cap`` rows in the *table* — and a request for an
    older window is answered from rows that are all newer than it. On a tenant
    past ``cap`` sends the endpoint returns an empty feed for every historical
    timeframe, with no error and a plausible-looking ``total: 0``. Filtering
    first makes ``cap`` bound the window's rows, which is what it was for.
    """
    items: list[RecentLeadEvent] = []

    send_window = window_clause(
        start, end,
        EmailSend.sent_at, EmailSend.opened_at,
        EmailSend.clicked_at, EmailSend.replied_at,
    )
    send_query = (
        select(EmailSend, Contact, Company)
        .outerjoin(Contact, EmailSend.contact_id == Contact.id)
        .outerjoin(Company, Contact.company_id == Company.id)
        .order_by(EmailSend.sent_at.desc())
        .limit(cap)
    )
    if send_window is not None:
        send_query = send_query.where(send_window)
    for send, contact, company in (await db.execute(send_query)).all():
        if send.status == "skipped":
            continue
        who = contact.full_name if contact is not None else send.to_email
        org_id = str(send.org_id) if send.org_id else None
        company_name = company.name if company is not None else None
        if send.sent_at is not None and in_range(send.sent_at, start, end):
            if send.status in _ERROR_STATUSES:
                etype, severity, desc = "error", "error", send.error or send.subject
            elif send.status == "bounced":
                etype, severity, desc = "bounced", "error", send.subject
            else:
                etype, severity, desc = "email_sent", "ok", send.subject
            items.append(RecentLeadEvent(
                id=f"email:{etype}:{send.id}", type=etype, timestamp=send.sent_at,
                org_id=org_id, contact_name=who, to_email=send.to_email,
                company_name=company_name, subject=send.subject,
                description=desc, severity=severity,
            ))
        if send.opened_at is not None and in_range(send.opened_at, start, end):
            items.append(RecentLeadEvent(
                id=f"email:opened:{send.id}", type="email_opened", timestamp=send.opened_at,
                org_id=org_id, contact_name=who, to_email=send.to_email,
                company_name=company_name, subject=send.subject, severity="ok",
            ))
        if send.clicked_at is not None and in_range(send.clicked_at, start, end):
            items.append(RecentLeadEvent(
                id=f"email:clicked:{send.id}", type="email_clicked", timestamp=send.clicked_at,
                org_id=org_id, contact_name=who, to_email=send.to_email,
                company_name=company_name, subject=send.subject, severity="ok",
            ))
        if send.replied_at is not None and in_range(send.replied_at, start, end):
            items.append(RecentLeadEvent(
                id=f"email:reply:{send.id}", type="reply", timestamp=send.replied_at,
                org_id=org_id, contact_name=who, to_email=send.to_email,
                company_name=company_name, subject=send.subject, severity="ok",
            ))

    event_query = (
        select(OutreachEvent, Contact, Company)
        .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
        .outerjoin(Contact, OutreachEnrollment.contact_id == Contact.id)
        .outerjoin(Company, Contact.company_id == Company.id)
        .order_by(OutreachEvent.occurred_at.desc())
        .limit(cap)
    )
    event_window = window_clause(start, end, OutreachEvent.occurred_at)
    if event_window is not None:
        event_query = event_query.where(event_window)
    for event, contact, company in (await db.execute(event_query)).all():
        etype = _OUTREACH_TYPE_MAP.get(event.event_type)
        if etype is None or not in_range(event.occurred_at, start, end):
            continue
        who = contact.full_name if contact is not None else None
        meta = event.event_metadata or {}
        to_email = meta.get("to_email") or (contact.email if contact is not None else None)
        items.append(RecentLeadEvent(
            id=f"outreach:{event.id}", type=etype, timestamp=event.occurred_at,
            org_id=str(event.org_id) if event.org_id else None,
            contact_name=who, to_email=to_email,
            company_name=company.name if company is not None else None,
            subject=meta.get("subject"),
            description=meta.get("error") or meta.get("subject"),
            severity="error" if etype == "bounced" else "ok",
        ))

    cand_query = select(ProspectCandidate).order_by(ProspectCandidate.created_at.desc()).limit(cap)
    cand_window = window_clause(start, end, ProspectCandidate.created_at)
    if cand_window is not None:
        cand_query = cand_query.where(cand_window)
    for cand in (await db.execute(cand_query)).scalars().all():
        if not in_range(cand.created_at, start, end):
            continue
        items.append(RecentLeadEvent(
            id=f"lead_discovered:{cand.id}", type="lead_discovered", timestamp=cand.created_at,
            org_id=str(cand.org_id) if cand.org_id else None,
            company_name=cand.company_name,
            description=f"Fit {cand.fit_score}/100 · {cand.status}",
            severity="ok",
        ))

    return items
