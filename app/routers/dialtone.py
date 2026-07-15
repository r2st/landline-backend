"""Dialtone Console — a unified activity log for Landline's autonomous
cold-email agent, "Dialtone".

Two endpoints:

* ``GET /dialtone/activity`` — the real-time event stream. Stitches every
  Dialtone touchpoint (emails sent, opens, clicks, replies, bounces, send
  errors, and lead discovery) into one chronological feed, newest-first,
  filterable by date range, event type, status, and contact.
* ``GET /dialtone/console`` — the at-a-glance header: kill-switch state, warm-up
  ramp progress (today vs the daily cap), today's engagement tallies, and each
  sender's warm-up/health.

No new tables: the feed is assembled from the existing ``email_sends``,
``outreach_events`` and ``prospect_candidates`` records, so it reflects exactly
what Dialtone has already logged.
"""

import logging
import uuid
from collections import Counter
from datetime import date as date_cls, datetime, time, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.prospecting import ProspectCandidate
from app.models.sender_health import SenderHealth
from app.models.smtp_account import SmtpAccount
from app.schemas.dialtone import (
    DIALTONE_EVENT_TYPES,
    DialtoneActivityFeed,
    DialtoneConsole,
    DialtoneEvent,
    DialtoneSenderStatus,
    DialtoneSending,
    DialtoneTodayCounts,
    DialtoneWarmup,
)
from app.services import system_settings
from app.services.send_limiter import get_send_limiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dialtone", tags=["dialtone"])

# Statuses on an EmailSend row that represent a delivery failure (surfaced as an
# ``error`` event, not a plain ``email_sent``).
_ERROR_STATUSES = {"error", "failed"}
# OutreachEvent.event_type → Dialtone feed event type.
_OUTREACH_TYPE_MAP = {
    "sent": "email_sent",
    "delivered": "delivered",
    "opened": "email_opened",
    "clicked": "email_clicked",
    "replied": "reply",
    "bounced": "bounced",
    "unsubscribed": "unsubscribed",
}
# Feed event type → severity (drives the console status dot).
_SEVERITY = {
    "bounced": "error",
    "error": "error",
    "unsubscribed": "warn",
}


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a possibly-naive datetime to aware UTC.

    SQLite (tests) returns naive datetimes, Postgres returns aware ones; the feed
    sorts across sources so they must be comparable.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_bound(value: Optional[str]) -> Optional[datetime]:
    """Parse a ``start``/``end`` query value (ISO date or datetime) to aware UTC.

    Tolerates the ``YYYY-MM-DD`` shape the date pickers send; returns ``None`` on
    empty/invalid input so a bad param never 500s the feed."""
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value))
    except (ValueError, TypeError):
        return None


def _in_window(
    ts: Optional[datetime], start: Optional[datetime], end: Optional[datetime]
) -> bool:
    ts = _aware(ts)
    if ts is None:
        return False
    if start is not None and ts < start:
        return False
    if end is not None and ts > end:
        return False
    return True


def _sev(event_type: str) -> str:
    return _SEVERITY.get(event_type, "ok")


def _reporting_today() -> tuple[date_cls, datetime, datetime]:
    """Return (today, day_start_utc, day_end_utc) in the reporting timezone."""
    tz_name = get_settings().daily_summary_timezone
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    now_local = datetime.now(tz)
    today = now_local.date()
    start_local = datetime.combine(today, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return today, start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def _collect_events(
    db: AsyncSession,
    *,
    start: Optional[datetime],
    end: Optional[datetime],
    contact_id: Optional[uuid.UUID],
    q: Optional[str],
    cap: int = 5000,
) -> list[DialtoneEvent]:
    """Assemble Dialtone's activity events across every source table.

    Date filtering is applied per-emitted-event (one EmailSend row can produce a
    ``sent`` + ``opened`` + ``clicked`` + ``reply`` event at different times).
    ``cap`` bounds how many rows we pull per source so a huge history can't blow
    up the response.
    """
    items: list[DialtoneEvent] = []
    q_lower = q.lower().strip() if q else None

    # --- Emails sent by Dialtone (+ their engagement) via EmailSend ----------
    send_query = (
        select(EmailSend, Contact)
        .outerjoin(Contact, EmailSend.contact_id == Contact.id)
        .order_by(EmailSend.sent_at.desc())
        .limit(cap)
    )
    if contact_id is not None:
        send_query = send_query.where(EmailSend.contact_id == contact_id)
    for send, contact in (await db.execute(send_query)).all():
        if send.status == "skipped":
            continue  # never sent → not real Dialtone activity
        who = contact.full_name if contact is not None else (send.to_email or "unknown")
        if q_lower and q_lower not in (send.to_email or "").lower() and q_lower not in who.lower():
            continue
        base_meta = {
            "email_send_id": str(send.id),
            "contact_id": str(send.contact_id) if send.contact_id else None,
            "contact_name": who,
            "to_email": send.to_email,
            "from_email": send.from_email,
            "subject": send.subject,
            "status": send.status,
        }
        is_error = send.status in _ERROR_STATUSES
        is_bounce = send.status == "bounced"
        if send.sent_at:
            if is_error:
                items.append(DialtoneEvent(
                    id=f"error:{send.id}",
                    type="error",
                    timestamp=_aware(send.sent_at),
                    title=f"Send failed to {who}",
                    description=send.error or send.subject,
                    source="email_send",
                    severity="error",
                    metadata={**base_meta, "error": send.error},
                ))
            elif is_bounce:
                items.append(DialtoneEvent(
                    id=f"bounced:{send.id}",
                    type="bounced",
                    timestamp=_aware(send.sent_at),
                    title=f"Email to {who} bounced",
                    description=send.subject,
                    source="email_send",
                    severity="error",
                    metadata=base_meta,
                ))
            else:
                items.append(DialtoneEvent(
                    id=f"email_sent:{send.id}",
                    type="email_sent",
                    timestamp=_aware(send.sent_at),
                    title=f"Email sent to {who}",
                    description=send.subject,
                    source="email_send",
                    severity="ok",
                    metadata=base_meta,
                ))
        if send.opened_at:
            items.append(DialtoneEvent(
                id=f"email_opened:{send.id}",
                type="email_opened",
                timestamp=_aware(send.opened_at),
                title=f"{who} opened an email",
                description=send.subject,
                source="email_send",
                metadata=base_meta,
            ))
        if send.clicked_at:
            items.append(DialtoneEvent(
                id=f"email_clicked:{send.id}",
                type="email_clicked",
                timestamp=_aware(send.clicked_at),
                title=f"{who} clicked a link",
                description=send.subject,
                source="email_send",
                metadata=base_meta,
            ))
        if send.replied_at:
            items.append(DialtoneEvent(
                id=f"reply:{send.id}",
                type="reply",
                timestamp=_aware(send.replied_at),
                title=f"{who} replied",
                description=send.subject,
                source="email_send",
                metadata=base_meta,
            ))

    # --- Sequence outreach events (Dialtone's own send/engagement log) --------
    event_query = (
        select(OutreachEvent, Contact)
        .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
        .outerjoin(Contact, OutreachEnrollment.contact_id == Contact.id)
        .order_by(OutreachEvent.occurred_at.desc())
        .limit(cap)
    )
    if contact_id is not None:
        event_query = event_query.where(OutreachEnrollment.contact_id == contact_id)
    for event, contact in (await db.execute(event_query)).all():
        etype = _OUTREACH_TYPE_MAP.get(event.event_type)
        if etype is None:
            continue
        who = contact.full_name if contact is not None else "a contact"
        meta = event.event_metadata or {}
        to_email = meta.get("to_email") or (contact.email if contact is not None else None)
        if q_lower and q_lower not in (to_email or "").lower() and q_lower not in who.lower():
            continue
        titles = {
            "email_sent": f"Sequence step {event.step_number}: email to {who}",
            "delivered": f"Delivered to {who}",
            "email_opened": f"{who} opened an email",
            "email_clicked": f"{who} clicked a link",
            "reply": f"{who} replied",
            "bounced": f"Email to {who} bounced",
            "unsubscribed": f"{who} unsubscribed",
        }
        items.append(DialtoneEvent(
            id=f"outreach:{event.id}",
            type=etype,
            timestamp=_aware(event.occurred_at),
            title=titles.get(etype, f"{event.event_type} — {who}"),
            description=meta.get("error") or meta.get("subject"),
            source="outreach",
            severity=_sev(etype),
            metadata={
                "outreach_event_id": str(event.id),
                "enrollment_id": str(event.enrollment_id),
                "contact_id": str(contact.id) if contact is not None else None,
                "contact_name": who,
                "to_email": to_email,
                "step_number": event.step_number,
                "event_type": event.event_type,
                "error": meta.get("error"),
            },
        ))

    # --- Lead discovery (autonomous prospecting) -----------------------------
    if contact_id is None:  # lead discovery isn't contact-scoped
        cand_query = (
            select(ProspectCandidate)
            .order_by(ProspectCandidate.created_at.desc())
            .limit(cap)
        )
        for cand in (await db.execute(cand_query)).scalars().all():
            if q_lower and q_lower not in (cand.company_name or "").lower():
                continue
            items.append(DialtoneEvent(
                id=f"lead_discovered:{cand.id}",
                type="lead_discovered",
                timestamp=_aware(cand.created_at),
                title=f"Lead discovered: {cand.company_name}",
                description=f"Fit {cand.fit_score}/100 · {cand.status}",
                source="prospecting",
                severity="ok",
                metadata={
                    "candidate_id": str(cand.id),
                    "company_id": str(cand.company_id) if cand.company_id else None,
                    "company_name": cand.company_name,
                    "domain": cand.domain,
                    "source": cand.source,
                    "fit_score": cand.fit_score,
                    "status": cand.status,
                },
            ))

    # Apply the date window per-event (events carry their own timestamps).
    if start is not None or end is not None:
        items = [i for i in items if _in_window(i.timestamp, start, end)]
    return items


@router.get("/activity", response_model=DialtoneActivityFeed)
async def get_dialtone_activity(
    start: Optional[str] = Query(None, description="ISO date/datetime lower bound (inclusive)"),
    end: Optional[str] = Query(None, description="ISO date/datetime upper bound (inclusive)"),
    types: Optional[str] = Query(None, description="Comma-separated event types to include"),
    status: Optional[str] = Query(None, description="Filter to events whose underlying send status matches"),
    contact_id: Optional[uuid.UUID] = Query(None, description="Restrict to one contact"),
    q: Optional[str] = Query(None, description="Search contact name / email / company"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Chronological (newest-first) log of everything Dialtone has done."""
    lo, hi = _parse_bound(start), _parse_bound(end)

    wanted: Optional[set[str]] = None
    if types:
        wanted = {t.strip() for t in types.split(",") if t.strip()}
        invalid = wanted - set(DIALTONE_EVENT_TYPES)
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown event type(s): {sorted(invalid)}. "
                       f"Valid types: {list(DIALTONE_EVENT_TYPES)}",
            )

    items = await _collect_events(db, start=lo, end=hi, contact_id=contact_id, q=q)

    if wanted is not None:
        items = [i for i in items if i.type in wanted]
    if status:
        s = status.lower()
        items = [i for i in items if str(i.metadata.get("status", "")).lower() == s]

    items.sort(key=lambda i: i.timestamp, reverse=True)

    total = len(items)
    offset = (page - 1) * page_size
    return DialtoneActivityFeed(
        items=items[offset:offset + page_size],
        total=total,
        page=page,
        page_size=page_size,
    )


async def _today_counts(db: AsyncSession, day_start: datetime, day_end: datetime) -> DialtoneTodayCounts:
    """Tally today's Dialtone activity across EmailSend + OutreachEvent + leads."""
    counts: Counter = Counter()

    # EmailSend engagement (each timestamp column counts once when in-window).
    for send in (await db.execute(select(EmailSend))).scalars().all():
        if send.status == "skipped":
            continue
        if _in_window(send.sent_at, day_start, day_end):
            if send.status in _ERROR_STATUSES:
                counts["errors"] += 1
            elif send.status == "bounced":
                counts["bounces"] += 1
            else:
                counts["emails_sent"] += 1
        if _in_window(send.opened_at, day_start, day_end):
            counts["opens"] += 1
        if _in_window(send.clicked_at, day_start, day_end):
            counts["clicks"] += 1
        if _in_window(send.replied_at, day_start, day_end):
            counts["replies"] += 1

    # OutreachEvent (Dialtone's sequence log).
    for event in (await db.execute(select(OutreachEvent))).scalars().all():
        if not _in_window(event.occurred_at, day_start, day_end):
            continue
        etype = event.event_type
        if etype == "sent":
            counts["emails_sent"] += 1
        elif etype == "opened":
            counts["opens"] += 1
        elif etype == "clicked":
            counts["clicks"] += 1
        elif etype == "replied":
            counts["replies"] += 1
        elif etype == "bounced":
            counts["bounces"] += 1

    # Leads discovered today.
    for cand in (await db.execute(select(ProspectCandidate.created_at))).scalars().all():
        if _in_window(cand, day_start, day_end):
            counts["leads_discovered"] += 1

    return DialtoneTodayCounts(
        emails_sent=counts["emails_sent"],
        opens=counts["opens"],
        clicks=counts["clicks"],
        replies=counts["replies"],
        bounces=counts["bounces"],
        errors=counts["errors"],
        leads_discovered=counts["leads_discovered"],
    )


async def _sender_statuses(db: AsyncSession) -> list[DialtoneSenderStatus]:
    """Per-sender warm-up + health for the console (Gmail + SMTP accounts)."""
    limiter = get_send_limiter()
    out: list[DialtoneSenderStatus] = []

    accounts: list = []
    accounts += list((await db.execute(select(GmailAccount))).scalars().all())
    accounts += list((await db.execute(select(SmtpAccount))).scalars().all())

    for acct in accounts:
        transport = "smtp" if type(acct).__name__ == "SmtpAccount" else "gmail"
        try:
            cap = await limiter.account_capacity(db, acct)
        except Exception as exc:  # pragma: no cover - defensive
            logger.info("account_capacity failed for %s: %s", getattr(acct, "email", "?"), exc)
            cap = {}
        out.append(DialtoneSenderStatus(
            account_id=str(acct.id),
            account_type=transport,
            email=getattr(acct, "email", None),
            health_status=cap.get("health_status", "unknown"),
            reputation_score=cap.get("reputation_score"),
            throttle_factor=cap.get("throttle_factor", 1.0),
            effective_daily_limit=cap.get("effective_daily_limit", 0),
            warmup_daily_limit=cap.get("warmup_daily_limit"),
            used_today=cap.get("used_today", 0),
            remaining_today=cap.get("remaining_today", 0),
        ))
    return out


@router.get("/console", response_model=DialtoneConsole)
async def get_dialtone_console(db: AsyncSession = Depends(get_db)):
    """The Dialtone Console header: sending state, warm-up ramp, today's tallies,
    and per-sender warm-up/health — everything the console's top section needs in
    one call."""
    today, day_start, day_end = _reporting_today()

    status = await system_settings.get_sending_status(db)
    sending = DialtoneSending(
        enabled=status.get("enabled", True),
        reason=status.get("reason"),
        updated_by=status.get("updated_by"),
        updated_at=status.get("updated_at"),
    )

    try:
        stats = await get_send_limiter().get_send_stats(db)
    except Exception as exc:  # pragma: no cover - best-effort context
        logger.info("send stats unavailable: %s", exc)
        stats = {}
    warmup = DialtoneWarmup(
        today_sent=stats.get("today_sent", 0) or 0,
        daily_limit=stats.get("daily_limit", 0) or 0,
        daily_remaining=stats.get("daily_remaining", 0) or 0,
        monthly_sent=stats.get("monthly_sent", 0) or 0,
        warmup_active=bool(stats.get("warmup_active", False)),
        warmup_week=stats.get("warmup_week"),
    )

    today_counts = await _today_counts(db, day_start, day_end)
    senders = await _sender_statuses(db)

    return DialtoneConsole(
        generated_at=datetime.now(timezone.utc),
        date=today.isoformat(),
        sending=sending,
        warmup=warmup,
        today=today_counts,
        senders=senders,
    )
