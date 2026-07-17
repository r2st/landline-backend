"""Smart Send-Time Optimization — Console endpoints (Feature 6).

Shares the ``/dialtone`` prefix with the Console router but lives here to keep
the send-timing surface self-contained.

* ``GET /dialtone/send-timing/optimal``               — global best times (wraps
  the existing ``SendTimingAnalyzer.get_optimal_times``) for the insight card.
* ``GET /dialtone/send-timing/contacts/{contact_id}`` — a contact's resolved tz,
  learned engagement profile, and next computed optimal send (debug/inspection).
* ``GET /dialtone/send-timing/experiment``            — optimized-vs-control
  results: per-arm open/reply rates, lift, and z-test significance.

All read-only. The per-contact view resolves the timezone WITHOUT persisting
(the nightly task / send queue own caching), so inspecting a contact has no side
effects.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.models.contact_engagement import ContactEngagementProfile
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.config import get_settings
from app.schemas.send_timing import (
    ContactProfile,
    ContactSendTiming,
    ContactWindowList,
    ContactWindowSummary,
    ExperimentArm,
    OptimalWindow,
    SendTimingExperiment,
    SendTimingHeatmap,
    SendTimingStatus,
)
from app.services.ab_testing_service import _z_score_two_proportions, _z_to_confidence
from app.services.send_time_optimizer import get_send_time_optimizer
from app.services.send_timing import get_send_timing

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dialtone", tags=["dialtone"])

_CONFIDENCE_TARGET = 0.95


@router.get("/send-timing/optimal")
async def get_global_optimal_times(db: AsyncSession = Depends(get_db)):
    """Global optimal send times from historical opens (wraps get_optimal_times)."""
    return await get_send_timing().get_optimal_times(db)


@router.get("/send-timing/heatmap", response_model=SendTimingHeatmap)
async def get_send_timing_heatmap(db: AsyncSession = Depends(get_db)):
    """Full (day-of-week, hour) grid of open rates for the results heatmap."""
    data = await get_send_timing().get_heatmap(db)
    return SendTimingHeatmap(**data)


@router.get("/send-timing/status", response_model=SendTimingStatus)
async def get_send_timing_status(db: AsyncSession = Depends(get_db)):
    """Overall optimizer status: is it on, and how much has it learned so far."""
    s = get_settings()
    min_opens = max(1, s.smart_timing_min_opens)

    profiles_total = (
        await db.execute(select(func.count(ContactEngagementProfile.id)))
    ).scalar() or 0
    profiles_with_opens = (
        await db.execute(
            select(func.count(ContactEngagementProfile.id)).where(
                ContactEngagementProfile.opens_total > 0
            )
        )
    ).scalar() or 0
    profiles_with_signal = (
        await db.execute(
            select(func.count(ContactEngagementProfile.id)).where(
                ContactEngagementProfile.opens_total >= min_opens
            )
        )
    ).scalar() or 0
    contacts_with_tz = (
        await db.execute(
            select(func.count(Contact.id)).where(Contact.timezone.isnot(None))
        )
    ).scalar() or 0
    last_computed = (
        await db.execute(select(func.max(ContactEngagementProfile.last_computed)))
    ).scalar()

    return SendTimingStatus(
        enabled=s.smart_send_timing_enabled,
        optimized_fraction=s.smart_timing_optimized_fraction,
        min_opens=s.smart_timing_min_opens,
        max_delay_hours=s.max_smart_delay_hours,
        reply_weight=s.smart_timing_reply_weight,
        default_dows=list(s.smart_timing_default_dows),
        default_hour_start=s.smart_timing_default_hour_start,
        default_hour_end=s.smart_timing_default_hour_end,
        business_hour_start=s.send_window_start_hour,
        business_hour_end=s.send_window_end_hour,
        weekdays_only=s.send_window_weekdays_only,
        profiles_total=profiles_total,
        profiles_with_signal=profiles_with_signal,
        profiles_with_opens=profiles_with_opens,
        contacts_with_timezone=contacts_with_tz,
        last_computed=last_computed,
    )


@router.get("/send-timing/contacts", response_model=ContactWindowList)
async def list_contact_windows(
    limit: int = 20, db: AsyncSession = Depends(get_db)
):
    """Top learned contacts (by opens) with their computed optimal windows."""
    limit = max(1, min(int(limit), 100))
    optimizer = get_send_time_optimizer()

    total = (
        await db.execute(select(func.count(ContactEngagementProfile.id)))
    ).scalar() or 0

    rows = (
        await db.execute(
            select(ContactEngagementProfile, Contact)
            .join(Contact, Contact.id == ContactEngagementProfile.contact_id)
            .order_by(
                ContactEngagementProfile.opens_total.desc(),
                ContactEngagementProfile.confidence.desc(),
            )
            .limit(limit)
        )
    ).all()

    contacts = []
    for profile, contact in rows:
        tz = profile.tz or contact.timezone or "America/Toronto"
        window = optimizer.compute_optimal_window(
            tz=tz,
            best_dow=profile.best_dow,
            best_hour=profile.best_hour,
            opens_total=profile.opens_total,
        )
        name = " ".join(
            p for p in [contact.first_name, contact.last_name] if p
        ).strip() or None
        contacts.append(
            ContactWindowSummary(
                contact_id=str(contact.id),
                name=name,
                email=contact.email,
                timezone=tz,
                tz_source=profile.tz_source,
                best_dow=profile.best_dow,
                best_hour=profile.best_hour,
                opens_total=profile.opens_total,
                confidence=profile.confidence,
                window=OptimalWindow(**window),
                last_computed=profile.last_computed,
            )
        )

    return ContactWindowList(contacts=contacts, total=total)


@router.get("/send-timing/contacts/{contact_id}", response_model=ContactSendTiming)
async def get_contact_send_timing(
    contact_id: str, db: AsyncSession = Depends(get_db)
):
    """A contact's resolved timezone, engagement profile, and next optimal send."""
    try:
        cid = uuid.UUID(str(contact_id))
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Contact not found")
    contact = (
        await db.execute(select(Contact).where(Contact.id == cid))
    ).scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")

    optimizer = get_send_time_optimizer()

    company = None
    if contact.company_id is not None:
        company = (
            await db.execute(select(Company).where(Company.id == contact.company_id))
        ).scalar_one_or_none()
    tz, source = optimizer.resolve_timezone(contact, company)

    profile = (
        await db.execute(
            select(ContactEngagementProfile).where(
                ContactEngagementProfile.contact_id == contact.id
            )
        )
    ).scalar_one_or_none()

    window = optimizer.compute_optimal_window(
        tz=tz,
        best_dow=profile.best_dow if profile else None,
        best_hour=profile.best_hour if profile else None,
        opens_total=profile.opens_total if profile else 0,
    )
    now = datetime.now(timezone.utc)
    next_send_at = optimizer.next_send_slot(tz=tz, window=window, due_at=now, now=now)

    profile_out = None
    if profile is not None:
        profile_out = ContactProfile(
            buckets=profile.buckets or {},
            opens_total=profile.opens_total,
            best_dow=profile.best_dow,
            best_hour=profile.best_hour,
            confidence=profile.confidence,
            last_computed=profile.last_computed,
        )

    return ContactSendTiming(
        contact_id=str(contact.id),
        email=contact.email,
        timezone=tz,
        tz_source=(profile.tz_source if profile and profile.tz_source else source),
        profile=profile_out,
        window=OptimalWindow(**window),
        next_send_at=next_send_at,
    )


async def _arm_stats(db: AsyncSession, arm: str) -> ExperimentArm:
    """Aggregate sent/open/reply counts for one timing arm."""
    enrollments = (
        await db.execute(
            select(func.count(OutreachEnrollment.id)).where(
                OutreachEnrollment.timing_arm == arm
            )
        )
    ).scalar() or 0

    async def _event_count(event_type: str) -> int:
        return (
            await db.execute(
                select(func.count(OutreachEvent.id))
                .join(
                    OutreachEnrollment,
                    OutreachEvent.enrollment_id == OutreachEnrollment.id,
                )
                .where(
                    OutreachEnrollment.timing_arm == arm,
                    OutreachEvent.event_type == event_type,
                )
            )
        ).scalar() or 0

    sent = await _event_count("sent")
    opens = await _event_count("opened")
    replies = await _event_count("replied")
    denom = max(sent, 1)
    return ExperimentArm(
        arm=arm,
        enrollments=enrollments,
        sent=sent,
        opens=opens,
        replies=replies,
        open_rate=round(opens / denom, 4),
        reply_rate=round(replies / denom, 4),
    )


@router.get("/send-timing/experiment", response_model=SendTimingExperiment)
async def get_send_timing_experiment(db: AsyncSession = Depends(get_db)):
    """Optimized-vs-control results: per-arm rates, lift, and z-test confidence."""
    optimized = await _arm_stats(db, "optimized")
    control = await _arm_stats(db, "control")

    open_lift = reply_lift = None
    open_conf = reply_conf = None
    note = ""
    if optimized.sent > 0 and control.sent > 0:
        open_lift = round(optimized.open_rate - control.open_rate, 4)
        reply_lift = round(optimized.reply_rate - control.reply_rate, 4)
        open_conf = round(
            _z_to_confidence(
                _z_score_two_proportions(
                    optimized.open_rate, optimized.sent, control.open_rate, control.sent
                )
            ),
            4,
        )
        reply_conf = round(
            _z_to_confidence(
                _z_score_two_proportions(
                    optimized.reply_rate, optimized.sent, control.reply_rate, control.sent
                )
            ),
            4,
        )
    else:
        note = "Not enough data yet — both arms need sends before a lift can be measured."

    significant = bool(
        reply_conf is not None
        and reply_conf >= _CONFIDENCE_TARGET
        and (reply_lift or 0) > 0
    )

    return SendTimingExperiment(
        arms=[optimized, control],
        open_rate_lift=open_lift,
        reply_rate_lift=reply_lift,
        open_rate_confidence=open_conf,
        reply_rate_confidence=reply_conf,
        significant=significant,
        confidence_target=_CONFIDENCE_TARGET,
        note=note,
    )
