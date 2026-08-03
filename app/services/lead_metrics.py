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
from app.models.email_template import NON_WIRE_STATUSES, EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.prospecting import ProspectCandidate
from app.models.tenant import Organization
from app.schemas.lead_tracking import RecentLeadEvent
from app.services.outreach_events import SEND_ATTEMPT_TYPES, AttemptTally
from app.services.timeframes import ensure_aware, in_range

# EmailSend statuses that represent a delivery failure (not a bounce).
_ERROR_STATUSES = {"error", "failed"}

# Statuses meaning nothing left the building and that aren't reported as errors
# either — the queued states plus the two refusals the send gate records. Taken
# wholesale from the model's own classification so a status added later has to
# be classified there rather than defaulting into the send count here.
_NOT_SENT_STATUSES = NON_WIRE_STATUSES - _ERROR_STATUSES

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
    """(open_rate, click_rate, reply_rate, bounce_rate) for one tally Counter.

    **A bounce is already inside ``emails_sent``, so it is not added to it.** The
    bounce rate used to divide by ``sent + bounces`` on the theory that a bounced
    message is an attempt the send count doesn't hold. That is true of nothing
    this function is given. Sequence mail — the bulk of the volume — writes an
    ``OutreachEvent(event_type='sent')`` when it goes out and a *second*
    ``bounced`` event when the DSN arrives (``bounce_handler._suppress_contact``),
    so the send is counted once already and adding it again inflates the
    denominator by exactly the numerator. The other table can't rescue the
    formula either: no code path ever writes ``EmailSend.status = 'bounced'``, so
    that leg contributed nothing.

    The error is not a rounding one, and it grows in the direction that flatters:
    a true 10% bounce rate reports 9.1%, a true 50% reports 33%, and a domain
    where *every* address is dead reports 50% instead of 100%. Worse, it made the
    operator's number disagree with the machine's — ``danger_monitor`` trips the
    auto-pause on ``bounces / sent`` and ``deliverability_optimizer`` throttles a
    sender on the same ratio, so the console read comfortably below a threshold
    the rail was already acting on.

    ``sent`` is the denominator everywhere now, matching
    ``funnel_analytics._with_rates``.
    """
    sent = counts.get("emails_sent", 0)
    return (
        rate(counts.get("opens", 0), sent),
        rate(counts.get("clicks", 0), sent),
        rate(counts.get("replies", 0), sent),
        rate(counts.get("bounces", 0), sent),
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
        # Only ``skipped`` was excluded, and the `else` below swept up whatever
        # it didn't name — so `pending`, `scheduled`, `suppressed` and `halted`
        # all landed in `emails_sent`. Two of those are the send gate refusing
        # (`suppressed` = a do-not-contact match, `halted` = the kill switch), so
        # the number grew precisely when sending stopped; the other two are
        # queued rows, which `sent_at`'s NOT NULL `server_default` stamps and
        # therefore counts the moment they're written rather than when they go.
        if status in _NOT_SENT_STATUSES:
            continue
        c = tallies[org_id]
        if in_range(sent_at, start, end):
            if status in _ERROR_STATUSES:
                c["errors"] += 1
            elif status == "bounced":
                # Both counters, deliberately. A bounce is mail that went out —
                # the model says so itself (``WIRE_STATUSES``) — and the sequence
                # table records it that way, as a ``sent`` event plus a later
                # ``bounced`` one. Counting it here as a bounce *instead of* a
                # send made the two tables mean different things by
                # ``emails_sent``, so any bounce rate over the union was wrong
                # whichever denominator it picked.
                c["emails_sent"] += 1
                c["bounces"] += 1
            else:
                c["emails_sent"] += 1
        if in_range(opened_at, start, end):
            c["opens"] += 1
        if in_range(clicked_at, start, end):
            c["clicks"] += 1
        if in_range(replied_at, start, end):
            c["replies"] += 1

    # Sequence sends. ``sent``/``bounced`` are counted through an
    # :class:`AttemptTally` rather than row-by-row: the two paths that write a
    # ``bounced`` event disagree about whether it replaces the ``sent`` one (a
    # synchronous failure) or accompanies it (an async DSN), so a row count is
    # wrong under one shape or the other. See ``AttemptTally`` for the full
    # reasoning. Opens/clicks/replies stay per-row — several are expected.
    attempts: dict[Optional[uuid.UUID], AttemptTally] = defaultdict(AttemptTally)
    event_cols = select(
        OutreachEvent.org_id, OutreachEvent.event_type, OutreachEvent.occurred_at,
        OutreachEvent.enrollment_id, OutreachEvent.step_number,
    )
    for org_id, event_type, occurred_at, enrollment_id, step_number in (
        await db.execute(event_cols)
    ).all():
        if not in_range(occurred_at, start, end):
            continue
        if event_type in SEND_ATTEMPT_TYPES:
            attempts[org_id].add(event_type, enrollment_id, step_number)
            continue
        metric = _OUTREACH_COUNTERS.get(event_type)
        if metric:
            tallies[org_id][metric] += 1

    for org_id, tally in attempts.items():
        tallies[org_id]["emails_sent"] += tally.attempts
        tallies[org_id]["bounces"] += tally.bounces

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
        # Same classification as the tally above: a row the send gate refused,
        # or one still queued, is not an "email sent" entry in the feed either.
        if send.status in _NOT_SENT_STATUSES:
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
