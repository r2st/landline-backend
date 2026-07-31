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
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.ab_test import AbTest, AbTestVariant
from app.models.company import Company
from app.models.contact import Contact
from app.models.danger_signal import DangerSignalEvent
from app.models.deal import Deal
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.prospecting import ProspectCandidate
from app.models.sender_health import SenderHealth
from app.models.sender_warmup import SenderWarmupPlan
from app.models.sequence import OutreachSequence, SequenceStep
from app.models.smtp_account import SmtpAccount
from app.models.suppression import SuppressionEntry
from app.schemas.booking import BookingConfig, BookingConfigUpdate
from app.schemas.sequence_analytics import SequenceAnalytics, SequenceStepAnalytics
from app.schemas.suppression import (
    SuppressionEntryCreate,
    SuppressionEntryRead,
    SuppressionImportResult,
    SuppressionListResponse,
)
from app.schemas.warmup import WarmupPlanCreate, WarmupPlanRead
from app.schemas.dialtone import (
    DIALTONE_EVENT_TYPES,
    DialtoneActivityFeed,
    DialtoneConsole,
    DialtoneDeltas,
    DialtoneEvent,
    DialtoneFunnelRates,
    DialtoneLiveFeed,
    DialtoneSenderStatus,
    DialtoneSending,
    DialtoneTodayCounts,
    DialtoneWarmup,
    PhoneEnrichmentRecent,
    PhoneEnrichmentSummary,
)
from app.schemas.lead_tracking import DialtoneStatus, LeadOrgBreakdown
from app.dependencies import OrgScope, get_org_scope
from app.services import lead_metrics, suppression, system_settings, warmup
from app.services.booking import validate_booking_url
from app.services.company_contacts import display_name, resolve_primary_contacts
from app.services.send_limiter import get_send_limiter
from app.services.timeframes import (
    DEFAULT_TIMEFRAME,
    TIMEFRAMES,
    Timeframe,
    ensure_aware as _aware,
    in_range as _in_range,
    pct_change,
    resolve_timeframe,
)

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


_TIMEFRAME_QUERY = Query(
    DEFAULT_TIMEFRAME,
    description=(
        "Reporting window for the numbers: " + ", ".join(TIMEFRAMES) +
        ". Anchored to the reporting timezone; weeks start Monday."
    ),
)


def _timeframe(value: Optional[str]) -> Timeframe:
    """Resolve a ``timeframe`` query value, 400-ing on an unknown key.

    A typo must not silently report the default window — the operator would read
    the wrong period's numbers and never know.
    """
    try:
        return resolve_timeframe(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
                # The sent copy itself, when the send task recorded it. Lets the
                # Console show *what* Dialtone sent, not just who/when.
                "subject": meta.get("subject"),
                "body_html": meta.get("body_html"),
            },
        ))

    # --- Reply classification (Feature 1) ------------------------------------
    # Surface every auto-classified reply so the feed reflects the routing
    # outcome (hot lead flagged, unsubscribe suppressed, OOO re-queued, …).
    reply_query = (
        select(EmailReply, Contact)
        .outerjoin(Contact, EmailReply.contact_id == Contact.id)
        .where(EmailReply.reply_classification.isnot(None))
        .order_by(EmailReply.classified_at.desc())
        .limit(cap)
    )
    if contact_id is not None:
        reply_query = reply_query.where(EmailReply.contact_id == contact_id)
    for reply, contact in (await db.execute(reply_query)).all():
        who = contact.full_name if contact is not None else (reply.from_email or "a contact")
        if q_lower and q_lower not in (reply.from_email or "").lower() and q_lower not in who.lower():
            continue
        category = reply.reply_classification
        items.append(DialtoneEvent(
            id=f"reply_classified:{reply.id}",
            type="reply_classified",
            timestamp=_aware(reply.classified_at or reply.received_at),
            title=f"Reply from {who} classified: {category}",
            description=(reply.classification_meta or {}).get("reason") or reply.subject,
            source="reply",
            severity="warn" if category in ("unsubscribe", "bounce", "unknown") else "ok",
            metadata={
                "reply_id": str(reply.id),
                "contact_id": str(reply.contact_id) if reply.contact_id else None,
                "contact_name": who,
                "from_email": reply.from_email,
                "subject": reply.subject,
                "classification": category,
                "confidence": reply.classification_confidence,
                "priority_flag": bool(reply.priority_flag),
                "handled_action": reply.handled_action,
            },
        ))

    # --- Phone enrichment (warm leads) ---------------------------------------
    # Surface every phone number the warm-lead sweep auto-found, so the feed
    # tells the operator "this engaged lead now has a number — call them". Only
    # sweep-sourced numbers ('apollo'/'company') appear; a hand-typed or scanned
    # number is not a Dialtone action.
    phone_query = (
        select(Contact)
        .where(
            Contact.phone_enriched_at.isnot(None),
            Contact.phone_source.in_(("apollo", "company")),
        )
        .order_by(Contact.phone_enriched_at.desc())
        .limit(cap)
    )
    if contact_id is not None:
        phone_query = phone_query.where(Contact.id == contact_id)
    for contact in (await db.execute(phone_query)).scalars().all():
        who = contact.full_name
        number = contact.mobile or contact.phone
        if not number:
            continue
        if q_lower and q_lower not in who.lower() and q_lower not in number.lower():
            continue
        src_label = "direct dial" if contact.phone_source == "apollo" else "company line"
        items.append(DialtoneEvent(
            id=f"phone_enriched:{contact.id}",
            type="phone_enriched",
            timestamp=_aware(contact.phone_enriched_at),
            title=f"Phone found for {who}",
            description=f"{number} ({src_label}) — warm lead, worth a call",
            source="enrichment",
            severity="ok",
            metadata={
                "contact_id": str(contact.id),
                "company_id": str(contact.company_id) if contact.company_id else None,
                "contact_name": who,
                "phone": contact.phone,
                "mobile": contact.mobile,
                "phone_source": contact.phone_source,
            },
        ))

    # --- Lead discovery (autonomous prospecting) -----------------------------
    if contact_id is None:  # lead discovery isn't contact-scoped
        cand_query = (
            select(ProspectCandidate)
            .order_by(ProspectCandidate.created_at.desc())
            .limit(cap)
        )
        candidates = [
            cand
            for cand in (await db.execute(cand_query)).scalars().all()
            if not q_lower or q_lower in (cand.company_name or "").lower()
        ]
        # "Lead discovered" without the address is only half the event — the
        # console's whole point is seeing what the autonomous side found.
        lead_contacts = await resolve_primary_contacts(
            db, [c.company_id for c in candidates if c.company_id is not None]
        )
        for cand in candidates:
            contact = lead_contacts.get(cand.company_id) if cand.company_id else None
            contact_email = contact.email if contact else None
            description = f"Fit {cand.fit_score}/100 · {cand.status}"
            items.append(DialtoneEvent(
                id=f"lead_discovered:{cand.id}",
                type="lead_discovered",
                timestamp=_aware(cand.created_at),
                title=f"Lead discovered: {cand.company_name}",
                description=description,
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
                    "contact_email": contact_email,
                    "contact_name": display_name(contact) if contact else None,
                    "contact_phone": (
                        (contact.phone or contact.mobile) if contact else None
                    ),
                },
            ))

    # --- Auto-pause danger-signal trips (Feature 2) --------------------------
    if contact_id is None:  # danger trips are system-wide, not contact-scoped
        trip_query = (
            select(DangerSignalEvent)
            .order_by(DangerSignalEvent.triggered_at.desc())
            .limit(cap)
        )
        for trip in (await db.execute(trip_query)).scalars().all():
            reason = (trip.details or {}).get("reason") or (
                f"{trip.metric} breached on {trip.scope}"
            )
            if q_lower and q_lower not in reason.lower() and q_lower not in (trip.account_email or "").lower():
                continue
            trip_meta = {
                "danger_signal_event_id": str(trip.id),
                "metric": trip.metric,
                "scope": trip.scope,
                "account_email": trip.account_email,
                "observed_value": trip.observed_value,
                "threshold": trip.threshold,
                "sample_size": trip.sample_size,
                "window": trip.window,
                "resolved": trip.resolved_at is not None,
            }
            # The auto-pause itself (error severity — drives the console banner).
            items.append(DialtoneEvent(
                id=f"auto_paused:{trip.id}",
                type="auto_paused",
                timestamp=_aware(trip.triggered_at),
                title="Dialtone auto-paused — deliverability danger signal",
                description=reason,
                source="danger_monitor",
                severity="error",
                metadata=trip_meta,
            ))
            # The operator's acknowledged resume that cleared it, if any.
            if trip.resolved_at is not None:
                items.append(DialtoneEvent(
                    id=f"resumed:{trip.id}",
                    type="resumed",
                    timestamp=_aware(trip.resolved_at),
                    title="Sending resumed after auto-pause",
                    description=trip.resolve_note,
                    source="danger_monitor",
                    severity="ok",
                    metadata={**trip_meta, "resolved_by": trip.resolved_by},
                ))

    # --- A/B test conclusions (Feature 3) ------------------------------------
    # Surface each declared winner so the feed reflects that a test converged and
    # the step's copy was promoted.
    if contact_id is None:  # tests are step-scoped, not contact-scoped
        concluded_query = (
            select(AbTest)
            .where(AbTest.status == "completed", AbTest.winner_variant_id.isnot(None))
            .order_by(AbTest.declared_at.desc())
            .limit(cap)
        )
        for test in (await db.execute(concluded_query)).scalars().all():
            winner = (
                await db.execute(
                    select(AbTestVariant).where(AbTestVariant.id == test.winner_variant_id)
                )
            ).scalar_one_or_none()
            label = winner.label if winner is not None else "?"
            if q_lower and q_lower not in f"variant {label}".lower():
                continue
            items.append(DialtoneEvent(
                id=f"ab_test_concluded:{test.id}",
                type="ab_test_concluded",
                timestamp=_aware(test.declared_at),
                title=f"A/B test concluded — variant {label} won",
                description=f"{test.metric} rate winner promoted to the step template",
                source="ab_testing",
                severity="ok",
                metadata={
                    "ab_test_id": str(test.id),
                    "sequence_step_id": str(test.sequence_step_id),
                    "winner_variant_id": str(test.winner_variant_id),
                    "winner_label": label,
                    "metric": test.metric,
                    "strategy": test.strategy,
                },
            ))

    # Apply the date window per-event (events carry their own timestamps).
    if start is not None or end is not None:
        items = [i for i in items if _in_window(i.timestamp, start, end)]
    return items


@router.get("/activity/live", response_model=DialtoneLiveFeed)
async def get_live_activity(
    limit: int = Query(50, ge=1, le=200, description="Newest N events to return"),
    since: Optional[str] = Query(
        None,
        description="ISO timestamp from a previous response's `latest_at`. Only "
                    "events strictly newer than this are returned.",
    ),
    window_hours: int = Query(
        24, ge=1, le=168,
        description="How far back to look. Bounds the query cost — a live feed "
                    "must not get slower as the send history grows.",
    ),
    types: Optional[str] = Query(None, description="Comma-separated event types to include"),
    db: AsyncSession = Depends(get_db),
):
    """The newest activity, cheap enough to poll.

    ``GET /activity`` is the *archive*: it scans up to 5,000 rows per source and
    pages through the whole history, which is the right shape for someone
    scrolling back through last month and the wrong shape for a widget asking
    "anything new?" every few seconds.

    This is the live view. A fixed lookback window bounds the work regardless of
    how much history exists, and ``since`` makes every poll after the first
    return only what has happened in the meantime — usually nothing, for the
    price of an indexed range scan.

    Ordering is newest-first. A client that polls with ``since`` and prepends the
    result gets a correct running feed; one that drops ``since`` gets a correct
    snapshot. Both are safe, because ``since`` is exclusive and events carry
    stable ids, so a reconnect can't duplicate or lose an entry.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)

    cursor = _parse_bound(since)
    if cursor is not None and cursor > window_start:
        # A caller that's been polling steadily only needs what's new; don't
        # re-scan the whole window for it.
        window_start = cursor

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

    # Cap per source generously above ``limit`` — one source can dominate a quiet
    # window (a sequence blast produces only outreach events), and under-pulling
    # would let it crowd every other source out of the merged result.
    items = await _collect_events(
        db, start=window_start, end=None, contact_id=None, q=None,
        cap=max(limit * 4, 200),
    )

    if wanted is not None:
        items = [i for i in items if i.type in wanted]
    if cursor is not None:
        # Strictly newer: the client already has the event at exactly `since`.
        items = [i for i in items if i.timestamp > cursor]

    items.sort(key=lambda i: i.timestamp, reverse=True)
    items = items[:limit]

    return DialtoneLiveFeed(
        items=items,
        server_time=now,
        latest_at=items[0].timestamp if items else None,
        window_hours=window_hours,
    )


@router.get("/activity", response_model=DialtoneActivityFeed)
async def get_dialtone_activity(
    start: Optional[str] = Query(None, description="ISO date/datetime lower bound (inclusive)"),
    end: Optional[str] = Query(None, description="ISO date/datetime upper bound (inclusive)"),
    timeframe: Optional[str] = Query(
        None,
        description=(
            "Named reporting window (" + ", ".join(TIMEFRAMES) + ") used when "
            "neither start nor end is given. Explicit bounds win."
        ),
    ),
    types: Optional[str] = Query(None, description="Comma-separated event types to include"),
    status: Optional[str] = Query(None, description="Filter to events whose underlying send status matches"),
    contact_id: Optional[uuid.UUID] = Query(None, description="Restrict to one contact"),
    q: Optional[str] = Query(None, description="Search contact name / email / company"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Chronological (newest-first) log of everything Dialtone has done.

    Accepts either explicit ``start``/``end`` bounds or a named ``timeframe`` so a
    console stat tile can drill down into exactly the window its number counted.
    """
    lo, hi = _parse_bound(start), _parse_bound(end)
    if lo is None and hi is None and timeframe:
        tf = _timeframe(timeframe)
        lo, hi = tf.start, tf.end
        # `_in_window` treats `end` as inclusive while a timeframe's end is the
        # exclusive next-period boundary; step back so the last event of the
        # period is included and the first of the next one isn't.
        if hi is not None:
            hi -= timedelta(microseconds=1)

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


_WINDOW = tuple[Optional[datetime], Optional[datetime]]

# OutreachEvent.event_type → the counter it increments. Sequence sends only exist
# here (see CLAUDE.md: there is no EmailSend row for them), so dropping this half
# would under-report the bulk of outreach volume.
_OUTREACH_COUNTERS = {
    "sent": "emails_sent",
    "opened": "opens",
    "clicked": "clicks",
    "replied": "replies",
    "bounced": "bounces",
}


async def _period_counts(
    db: AsyncSession, windows: list[_WINDOW]
) -> list[DialtoneTodayCounts]:
    """Tally Dialtone activity across EmailSend + OutreachEvent + leads, once per
    window in ``windows``.

    Both the selected timeframe and the previous period are tallied in a single
    pass over each source table: the row scan is the expensive part, and asking
    for a delta shouldn't double the cost of the console.
    """
    tallies: list[Counter] = [Counter() for _ in windows]

    def bump(metric: str, ts: Optional[datetime]) -> None:
        for counts, (start, end) in zip(tallies, windows):
            if _in_range(ts, start, end):
                counts[metric] += 1

    # EmailSend engagement (each timestamp column counts once when in-window):
    # manual sends, template sends, inbox replies, smart-reply auto-sends.
    send_cols = select(
        EmailSend.status, EmailSend.sent_at, EmailSend.opened_at,
        EmailSend.clicked_at, EmailSend.replied_at,
    )
    for status, sent_at, opened_at, clicked_at, replied_at in (await db.execute(send_cols)).all():
        if status == "skipped":
            continue
        if status in _ERROR_STATUSES:
            bump("errors", sent_at)
        elif status == "bounced":
            bump("bounces", sent_at)
        else:
            bump("emails_sent", sent_at)
        bump("opens", opened_at)
        bump("clicks", clicked_at)
        bump("replies", replied_at)

    # OutreachEvent (Dialtone's sequence log) — the other half of sent mail.
    event_cols = select(OutreachEvent.event_type, OutreachEvent.occurred_at)
    for event_type, occurred_at in (await db.execute(event_cols)).all():
        metric = _OUTREACH_COUNTERS.get(event_type)
        if metric:
            bump(metric, occurred_at)

    # Leads discovered.
    for created_at in (await db.execute(select(ProspectCandidate.created_at))).scalars().all():
        bump("leads_discovered", created_at)

    return [
        DialtoneTodayCounts(
            emails_sent=c["emails_sent"],
            opens=c["opens"],
            clicks=c["clicks"],
            replies=c["replies"],
            bounces=c["bounces"],
            errors=c["errors"],
            leads_discovered=c["leads_discovered"],
        )
        for c in tallies
    ]


def _deltas(current: DialtoneTodayCounts, previous: Optional[DialtoneTodayCounts]) -> DialtoneDeltas:
    """Percent change per metric vs the previous period (all ``None`` if absent)."""
    if previous is None:
        return DialtoneDeltas()
    return DialtoneDeltas(**{
        field: pct_change(getattr(current, field), getattr(previous, field))
        for field in DialtoneDeltas.model_fields
    })


def _funnel_rates(counts: DialtoneTodayCounts) -> DialtoneFunnelRates:
    """Open/click/reply rates against sends, bounce rate against attempts."""
    sent = counts.emails_sent
    attempted = sent + counts.bounces
    return DialtoneFunnelRates(
        open_rate=_rate(counts.opens, sent),
        click_rate=_rate(counts.clicks, sent),
        reply_rate=_rate(counts.replies, sent),
        bounce_rate=_rate(counts.bounces, attempted),
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
async def get_dialtone_console(
    timeframe: Optional[str] = _TIMEFRAME_QUERY,
    db: AsyncSession = Depends(get_db),
):
    """The Dialtone Console header: sending state, warm-up ramp, the selected
    timeframe's tallies (with a vs-previous-period delta and funnel rates), and
    per-sender warm-up/health — everything the console's top section needs in one
    call.

    ``timeframe`` defaults to ``today``, which is what this endpoint has always
    returned; the console UI asks for ``this_week``. Warm-up and sender numbers
    are daily by nature and ignore the timeframe.
    """
    tf = _timeframe(timeframe)

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

    windows = [tf.window]
    if tf.has_previous:
        windows.append(tf.previous_window)
    tallies = await _period_counts(db, windows)
    current = tallies[0]
    previous = tallies[1] if tf.has_previous else None

    senders = await _sender_statuses(db)

    def iso(d) -> Optional[str]:
        return d.isoformat() if d is not None else None

    return DialtoneConsole(
        generated_at=datetime.now(timezone.utc),
        date=tf.today.isoformat(),
        sending=sending,
        warmup=warmup,
        stats=current,
        today=current,
        senders=senders,
        timeframe=tf.key,
        timeframe_label=tf.label,
        range_start=iso(tf.start_date),
        range_end=iso(tf.end_date),
        previous=previous,
        previous_range_start=iso(tf.previous_start_date),
        previous_range_end=iso(tf.previous_end_date),
        deltas=_deltas(current, previous),
        rates=_funnel_rates(current),
    )


@router.get("/status", response_model=DialtoneStatus)
async def get_dialtone_status(
    start: Optional[str] = Query(None, description="ISO date/datetime lower bound (inclusive)"),
    end: Optional[str] = Query(None, description="ISO date/datetime upper bound (exclusive); overrides timeframe"),
    timeframe: Optional[str] = _TIMEFRAME_QUERY,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Dialtone agent status + performance: kill-switch state, warm-up ramp,
    sender health, and the selected window's send/engagement funnel.

    Meant for an external poller (Claude or otherwise) rather than the console
    UI — same underlying counters as ``/dialtone/console``, plus a per-org
    breakdown (``by_org``; more than one entry only for a platform_admin caller,
    see ``app.services.lead_metrics.tally_by_org``).
    """
    lo, hi = _parse_bound(start), _parse_bound(end)
    if lo is not None or hi is not None:
        tf_key, tf_label, range_start, range_end = "custom", "Custom range", start, end
    else:
        tf = _timeframe(timeframe)
        lo, hi = tf.start, tf.end
        tf_key, tf_label = tf.key, tf.label
        range_start = tf.start_date.isoformat() if tf.start_date else None
        range_end = tf.end_date.isoformat() if tf.end_date else None

    sending_status = await system_settings.get_sending_status(db)
    sending = DialtoneSending(
        enabled=sending_status.get("enabled", True),
        reason=sending_status.get("reason"),
        updated_by=sending_status.get("updated_by"),
        updated_at=sending_status.get("updated_at"),
    )

    try:
        send_stats = await get_send_limiter().get_send_stats(db)
    except Exception as exc:  # pragma: no cover - best-effort context
        logger.info("send stats unavailable: %s", exc)
        send_stats = {}
    warmup_progress = DialtoneWarmup(
        today_sent=send_stats.get("today_sent", 0) or 0,
        daily_limit=send_stats.get("daily_limit", 0) or 0,
        daily_remaining=send_stats.get("daily_remaining", 0) or 0,
        monthly_sent=send_stats.get("monthly_sent", 0) or 0,
        warmup_active=bool(send_stats.get("warmup_active", False)),
        warmup_week=send_stats.get("warmup_week"),
    )

    tallies = await lead_metrics.tally_by_org(db, lo, hi)
    names = await lead_metrics.org_names(db, set(tallies))
    total = Counter()
    breakdown: list[LeadOrgBreakdown] = []
    for org_id, c in tallies.items():
        total.update(c)
        open_rate, click_rate, reply_rate, bounce_rate = lead_metrics.funnel_rates(c)
        breakdown.append(LeadOrgBreakdown(
            org_id=str(org_id) if org_id else None,
            org_name=names.get(org_id),
            counts=DialtoneTodayCounts(**{
                f: c.get(f, 0) for f in
                ("emails_sent", "opens", "clicks", "replies", "bounces", "errors", "leads_discovered")
            }),
            rates=DialtoneFunnelRates(
                open_rate=open_rate, click_rate=click_rate, reply_rate=reply_rate, bounce_rate=bounce_rate,
            ),
        ))
    breakdown.sort(key=lambda b: b.counts.emails_sent, reverse=True)
    total_open, total_click, total_reply, total_bounce = lead_metrics.funnel_rates(total)

    senders = await _sender_statuses(db)

    return DialtoneStatus(
        generated_at=datetime.now(timezone.utc),
        timeframe=tf_key,
        timeframe_label=tf_label,
        range_start=range_start,
        range_end=range_end,
        sending=sending,
        warmup=warmup_progress,
        counts=DialtoneTodayCounts(**{
            f: total.get(f, 0) for f in
            ("emails_sent", "opens", "clicks", "replies", "bounces", "errors", "leads_discovered")
        }),
        rates=DialtoneFunnelRates(
            open_rate=total_open, click_rate=total_click, reply_rate=total_reply, bounce_rate=total_bounce,
        ),
        active_senders=len(senders),
        healthy_senders=sum(1 for s in senders if s.health_status == "healthy"),
        by_org=breakdown,
    )


# ─── Phone enrichment summary ────────────────────────────────────────────────

# The warm-lead sweep only ever stamps these two sources; a 'card'/'manual'
# number is a human action, not an enrichment.
_ENRICHED_SOURCES = ("apollo", "company")
_SOURCE_LABELS = {"apollo": "direct dial", "company": "company line"}


@router.get("/phone-enrichment/summary", response_model=PhoneEnrichmentSummary)
async def phone_enrichment_summary(
    recent_limit: int = Query(8, ge=0, le=50),
    timeframe: Optional[str] = Query(
        "all_time",
        description=(
            "Reporting window for the roll-up (" + ", ".join(TIMEFRAMES) + "), "
            "measured on when each number was found. Defaults to all_time — the "
            "card is a standing scoreboard."
        ),
    ),
    db: AsyncSession = Depends(get_db),
):
    """Roll-up of the warm-lead phone-enrichment sweep: how many contacts were
    enriched, the hit rate against attempts, the Apollo-vs-company-line split,
    and the most recent finds — the numbers behind the Console's enrichment card.
    """
    tf = _timeframe(timeframe)

    def within(*extra):
        """Enrichment-stamped rows inside the selected window."""
        clauses = [Contact.phone_enriched_at.isnot(None), *extra]
        if tf.start is not None:
            clauses.append(Contact.phone_enriched_at >= tf.start)
        if tf.end is not None:
            clauses.append(Contact.phone_enriched_at < tf.end)
        return clauses

    # Attempts: every contact the sweep has looked up (hit or miss).
    attempts = (
        await db.execute(
            select(func.count()).select_from(Contact).where(*within())
        )
    ).scalar() or 0

    # Enriched, split by source, in one grouped pass.
    by_source_rows = (
        await db.execute(
            select(Contact.phone_source, func.count())
            .where(*within(Contact.phone_source.in_(_ENRICHED_SOURCES)))
            .group_by(Contact.phone_source)
        )
    ).all()
    by_source = {src: count for src, count in by_source_rows}
    apollo = by_source.get("apollo", 0)
    company = by_source.get("company", 0)
    enriched = apollo + company
    hit_rate = round(enriched / attempts, 4) if attempts else 0.0

    recent: list[PhoneEnrichmentRecent] = []
    if recent_limit:
        rows = (
            await db.execute(
                select(Contact, Company)
                .outerjoin(Company, Contact.company_id == Company.id)
                .where(*within(Contact.phone_source.in_(_ENRICHED_SOURCES)))
                .order_by(Contact.phone_enriched_at.desc())
                .limit(recent_limit)
            )
        ).all()
        for contact, company_row in rows:
            recent.append(
                PhoneEnrichmentRecent(
                    contact_id=str(contact.id),
                    contact_name=contact.full_name,
                    company_name=company_row.name if company_row is not None else None,
                    phone=contact.mobile or contact.phone,
                    source=contact.phone_source,
                    source_label=_SOURCE_LABELS.get(contact.phone_source, ""),
                    enriched_at=_aware(contact.phone_enriched_at),
                )
            )

    return PhoneEnrichmentSummary(
        enriched=enriched,
        attempts=attempts,
        hit_rate=hit_rate,
        apollo=apollo,
        company=company,
        recent=recent,
        timeframe=tf.key,
        timeframe_label=tf.label,
    )


# ─── Per-sender Calendar Booking config (Feature 4) ──────────────────────────


async def _load_sender(db: AsyncSession, account_id: str):
    """Load a sending identity (Gmail first, then SMTP) by id.

    Returns ``(account, transport)`` or ``(None, None)`` when the id doesn't
    parse or no account matches.
    """
    try:
        acct_uuid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        return None, None

    account = (
        await db.execute(select(GmailAccount).where(GmailAccount.id == acct_uuid))
    ).scalar_one_or_none()
    if account is not None:
        return account, "gmail"

    account = (
        await db.execute(select(SmtpAccount).where(SmtpAccount.id == acct_uuid))
    ).scalar_one_or_none()
    if account is not None:
        return account, "smtp"

    return None, None


def _booking_config(account, transport: str) -> BookingConfig:
    return BookingConfig(
        account_id=str(account.id),
        account_type=transport,
        email=getattr(account, "email", None),
        booking_url=getattr(account, "booking_url", None),
        booking_provider=getattr(account, "booking_provider", None),
        booking_auto_inject=bool(getattr(account, "booking_auto_inject", True)),
    )


@router.get("/senders/{account_id}/booking", response_model=BookingConfig)
async def get_sender_booking(account_id: str, db: AsyncSession = Depends(get_db)):
    """Read a sender's calendar-booking configuration."""
    account, transport = await _load_sender(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Sender account not found")
    return _booking_config(account, transport)


@router.put("/senders/{account_id}/booking", response_model=BookingConfig)
async def set_sender_booking(
    account_id: str,
    payload: BookingConfigUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Set a sender's ``booking_url`` / ``booking_provider`` / ``booking_auto_inject``.

    The booking URL is validated (https + known-scheduler-host allowlist) before
    it is persisted — a rejected URL returns 400 and nothing is written. Passing
    an empty string clears the configured URL.
    """
    account, transport = await _load_sender(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Sender account not found")

    fields = payload.model_dump(exclude_unset=True)  # booking update fields

    if "booking_url" in fields:
        url = fields["booking_url"]
        ok, error = validate_booking_url(url)
        if not ok:
            raise HTTPException(status_code=400, detail=error)
        # Normalize an empty/blank URL to NULL (cleared).
        account.booking_url = url.strip() if url and url.strip() else None

    if "booking_provider" in fields:
        provider = fields["booking_provider"]
        account.booking_provider = provider.strip() if provider and provider.strip() else None

    if "booking_auto_inject" in fields and fields["booking_auto_inject"] is not None:
        account.booking_auto_inject = bool(fields["booking_auto_inject"])

    await db.commit()
    await db.refresh(account)
    return _booking_config(account, transport)


# ─── Automated warm-up plans (Feature 1) ─────────────────────────────────────


def _warmup_read(plan: SenderWarmupPlan, account) -> WarmupPlanRead:
    """Serialise a plan, adding today's usage from the account when available."""
    used = warmup.used_today(account) if account is not None else 0
    return WarmupPlanRead(
        id=plan.id,
        account_id=plan.account_id,
        account_type=plan.account_type,
        account_email=plan.account_email,
        start_date=plan.start_date,
        current_day=plan.current_day,
        daily_limit=plan.daily_limit,
        status=plan.status,
        used_today=used,
        remaining_today=max(0, plan.daily_limit - used),
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


@router.get("/warmup", response_model=list[WarmupPlanRead])
async def list_warmup_plans(db: AsyncSession = Depends(get_db)):
    """List every sender's warm-up plan, newest first, with today's usage.

    The Sender Health page merges these onto each account row (by ``account_id``)
    to show the warm-up day, daily cap, and progress toward it.
    """
    plans = (
        await db.execute(
            select(SenderWarmupPlan).order_by(SenderWarmupPlan.created_at.desc())
        )
    ).scalars().all()

    # Index accounts so each plan's used-today reflects the live daily counter.
    gmail = {a.id: a for a in (await db.execute(select(GmailAccount))).scalars().all()}
    smtp = {a.id: a for a in (await db.execute(select(SmtpAccount))).scalars().all()}

    out: list[WarmupPlanRead] = []
    for plan in plans:
        account = (gmail if plan.account_type == "gmail" else smtp).get(plan.account_id)
        out.append(_warmup_read(plan, account))
    return out


@router.post("/warmup", response_model=WarmupPlanRead, status_code=201)
async def create_warmup_plan(
    payload: WarmupPlanCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create (or return the existing) warm-up plan for a sending account.

    ``account_type`` is inferred from which table the account lives in when not
    supplied. Idempotent: a sender that already has a plan gets it back with a
    409 so the caller knows it wasn't newly created.
    """
    account, transport = await _load_sender(db, str(payload.account_id))
    if account is None:
        raise HTTPException(status_code=404, detail="Sender account not found")

    account_type = payload.account_type or transport
    existing = await warmup.get_plan(db, payload.account_id, account_type)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="A warm-up plan already exists for this sender.",
        )

    plan = await warmup.ensure_warmup_plan(
        db,
        account_id=payload.account_id,
        account_type=account_type,
        account_email=getattr(account, "email", None),
        start=payload.start_date,
        org_id=getattr(account, "org_id", None),
    )
    await db.commit()
    await db.refresh(plan)
    return _warmup_read(plan, account)


# ─── Suppression list (Feature 2) ────────────────────────────────────────────

# Cap an uploaded file at 5 MB so a runaway upload can't exhaust memory. A CSV
# of email addresses is tiny; 5 MB is ~150k addresses.
_MAX_SUPPRESSION_UPLOAD = 5 * 1024 * 1024


@router.get("/suppression", response_model=SuppressionListResponse)
async def list_suppression(
    q: Optional[str] = Query(None, description="Filter by email substring"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Paginated do-not-contact list, newest first, optionally search-filtered."""
    total = await suppression.count_entries(db, q)

    query = select(SuppressionEntry).order_by(SuppressionEntry.created_at.desc())
    if q:
        query = query.where(SuppressionEntry.email.ilike(f"%{q.strip().lower()}%"))
    query = query.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(query)).scalars().all()

    return SuppressionListResponse(
        items=[SuppressionEntryRead.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/suppression", response_model=SuppressionEntryRead, status_code=201)
async def add_suppression(
    payload: SuppressionEntryCreate,
    db: AsyncSession = Depends(get_db),
):
    """Add a single address to the suppression list by hand."""
    entry, status = await suppression.add_entry(
        db, payload.email, reason=payload.reason, source="manual"
    )
    if status == "invalid":
        raise HTTPException(status_code=400, detail="Not a valid email address.")
    if status == "duplicate":
        raise HTTPException(status_code=409, detail="Address is already suppressed.")
    await db.commit()
    await db.refresh(entry)
    return SuppressionEntryRead.model_validate(entry)


@router.post("/suppression/import", response_model=SuppressionImportResult)
async def import_suppression(
    file: UploadFile = File(..., description="CSV or newline-separated address list"),
    reason: str = Query("unsubscribe", description="Default reason for rows without one"),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-import a CSV (or plain text) of addresses into the suppression list.

    Accepts a header row naming ``email``/``reason`` columns, a headerless CSV
    (email first), or one address per line. Returns per-outcome counts.
    """
    raw = await file.read()
    if len(raw) > _MAX_SUPPRESSION_UPLOAD:
        raise HTTPException(status_code=413, detail="File too large (max 5 MB).")
    try:
        content = raw.decode("utf-8-sig")  # tolerate a UTF-8 BOM from Excel
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 text/CSV.")

    result = await suppression.import_csv(db, content, default_reason=reason)
    return SuppressionImportResult(**result)


@router.delete("/suppression/{entry_id}", status_code=204)
async def delete_suppression(entry_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Remove an address from the suppression list (re-allows contacting it)."""
    entry = (
        await db.execute(
            select(SuppressionEntry).where(SuppressionEntry.id == entry_id)
        )
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Suppression entry not found")
    await db.delete(entry)
    await db.commit()


# ─── Per-sequence analytics (Feature 4) ──────────────────────────────────────


def _rate(numerator: int, denominator: int) -> float:
    """Fraction (0..1), 0 when the denominator is 0 (never divide by zero)."""
    return round(numerator / denominator, 4) if denominator else 0.0


@router.get("/sequences/{sequence_id}/analytics", response_model=SequenceAnalytics)
async def sequence_analytics(sequence_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Per-step funnel + outcome analytics for one sequence.

    For each step: sent / opened / clicked / replied / bounced counts and the
    corresponding rates (as a fraction of that step's sends). Plus enrollment
    outcomes and the enrolled → deal conversion rate.
    """
    sequence = (
        await db.execute(
            select(OutreachSequence).where(OutreachSequence.id == sequence_id)
        )
    ).scalar_one_or_none()
    if sequence is None:
        raise HTTPException(status_code=404, detail="Sequence not found")

    # Per-(step, event_type) counts for this sequence's enrollments, in one pass.
    event_rows = (
        await db.execute(
            select(
                OutreachEvent.step_number,
                OutreachEvent.event_type,
                func.count(OutreachEvent.id),
            )
            .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
            .where(OutreachEnrollment.sequence_id == sequence_id)
            .group_by(OutreachEvent.step_number, OutreachEvent.event_type)
        )
    ).all()
    # counts[step_number][event_type] = n
    counts: dict[int, dict[str, int]] = {}
    for step_number, event_type, n in event_rows:
        counts.setdefault(step_number, {})[event_type] = n

    # Step definitions (channel/delay/label), plus any step numbers that only
    # appear in events (defensive — a step deleted after sending).
    steps = (
        await db.execute(
            select(SequenceStep)
            .where(SequenceStep.sequence_id == sequence_id)
            .order_by(SequenceStep.step_number)
        )
    ).scalars().all()
    step_defs = {s.step_number: s for s in steps}
    all_step_numbers = sorted(set(step_defs) | set(counts))

    step_analytics: list[SequenceStepAnalytics] = []
    for sn in all_step_numbers:
        c = counts.get(sn, {})
        sent = c.get("sent", 0)
        opened = c.get("opened", 0)
        clicked = c.get("clicked", 0)
        replied = c.get("replied", 0)
        bounced = c.get("bounced", 0)
        sdef = step_defs.get(sn)
        label = None
        if sdef is not None and sdef.subject_template:
            label = sdef.subject_template[:80]
        step_analytics.append(SequenceStepAnalytics(
            step_number=sn,
            channel=sdef.channel if sdef is not None else "email",
            delay_days=sdef.delay_days if sdef is not None else 0,
            label=label,
            sent=sent,
            opened=opened,
            clicked=clicked,
            replied=replied,
            bounced=bounced,
            open_rate=_rate(opened, sent),
            click_rate=_rate(clicked, sent),
            reply_rate=_rate(replied, sent),
            bounce_rate=_rate(bounced, sent),
        ))

    # Enrollment outcomes, grouped by status in one pass.
    status_rows = (
        await db.execute(
            select(OutreachEnrollment.status, func.count(OutreachEnrollment.id))
            .where(OutreachEnrollment.sequence_id == sequence_id)
            .group_by(OutreachEnrollment.status)
        )
    ).all()
    by_status = {status: n for status, n in status_rows}
    total_enrollments = sum(by_status.values())

    # Enrolled → deal: deals credited to this sequence (attribution, migration 075).
    deals_attributed = (
        await db.execute(
            select(func.count(Deal.id)).where(Deal.source_sequence_id == sequence_id)
        )
    ).scalar() or 0

    return SequenceAnalytics(
        sequence_id=sequence_id,
        name=sequence.name,
        total_enrollments=total_enrollments,
        active_enrollments=by_status.get("active", 0),
        completed_enrollments=by_status.get("completed", 0),
        replied_enrollments=by_status.get("replied", 0),
        bounced_enrollments=by_status.get("bounced", 0),
        deals_attributed=deals_attributed,
        conversion_rate=_rate(deals_attributed, total_enrollments),
        steps=step_analytics,
    )
