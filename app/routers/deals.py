"""Deal CRUD and revenue-summary endpoints."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import UserScope, get_scope
from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.deal_activity import DealActivity
from app.models.deal_note import DealNote
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.schemas.deal_prediction import DealForecast, DealPredictionRead
from app.services import pipeline_forecaster
from app.schemas.deal import (
    DEAL_STAGE_PROBABILITIES,
    AttributionSummary,
    DealActivityRead,
    DealCreate,
    DealMetrics,
    DealNoteCreate,
    DealNoteRead,
    DealRead,
    DealStageBreakdown,
    DealStatusBreakdown,
    DealSummary,
    DealUpdate,
    SequenceAttribution,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/deals", tags=["deals"])


def _f(value) -> float:
    """Coerce a (possibly Decimal/None) numeric to float."""
    return float(value) if value is not None else 0.0


#: A deal in one of these statuses has been decided — the opportunity is over.
DECIDED_STATUSES = ("won", "lost")


def _apply_decision_date(
    values: dict,
    provided: set[str],
    *,
    previous_status: Optional[str] = None,
) -> dict:
    """Stamp ``close_date`` when a deal is decided; clear it when it reopens.

    ``close_date`` is the only record of *when* a deal was decided, and nothing
    ever wrote it. It is declared optional on ``DealCreate``/``DealUpdate`` and
    an operator flipping a deal to won in the UI has no reason to also type a
    date, so in practice every won deal carried ``close_date = NULL``.

    Everything downstream reads that column and quietly degrades to zero:

    * ``/reports/client-retention`` filters decided deals through
      ``_in_window(d.close_date, ...)``, and ``_in_window`` returns ``False``
      for ``None``. So ``won_deals_in_period`` and ``won_revenue_in_period``
      were a structural 0 and $0 in *every* period, on a report whose whole
      subject is revenue.
    * ``/reports/top-clients`` with a date window re-aggregates through the same
      predicate, so a windowed Top Clients table was always empty.
    * ``/deals/metrics`` averages ``close_date - created_at`` over won deals
      ``WHERE close_date IS NOT NULL``, so ``avg_days_to_close`` was 0.0.
    * ``/reports/pipeline-analytics`` computes ``avg_deal_cycle_days`` the same
      way — also 0.0.

    None of those failed loudly. They reported a real-looking zero, which is the
    one wrong answer an operator won't question on a report of a month with no
    closes.

    The rules, in order:

    * An explicit ``close_date`` in the payload always wins — backfilling a
      historical deal must stay possible, including setting it to ``None``.
    * Only a *transition* into a decided status stamps the date. Re-saving a
      deal that is already won (an edited note, a corrected value) leaves its
      close date alone rather than walking it forward to today.
    * A decided deal reopening to ``pending`` has its ``close_date`` cleared —
      otherwise it keeps a decision date for a decision that was undone, and
      re-winning it later would keep the *first* date.

    ``values`` is the payload dict and ``provided`` the caller's
    ``model_fields_set``; ``values`` is mutated and returned.
    """
    if "close_date" in provided:
        return values

    new_status = values.get("status", previous_status)
    if new_status in DECIDED_STATUSES and previous_status not in DECIDED_STATUSES:
        values["close_date"] = datetime.now(timezone.utc)
    elif new_status not in DECIDED_STATUSES and previous_status in DECIDED_STATUSES:
        values["close_date"] = None
    return values


async def _infer_attribution(
    db: AsyncSession, company_id: uuid.UUID
) -> tuple[Optional[uuid.UUID], Optional[uuid.UUID]]:
    """Trace a company back to the enrollment that first brought it in.

    Returns ``(enrollment_id, sequence_id)`` for the earliest enrollment among
    any of the company's contacts, or ``(None, None)`` if the company was never
    enrolled. This is the "which outreach produced this deal" link.
    """
    row = (
        await db.execute(
            select(OutreachEnrollment.id, OutreachEnrollment.sequence_id)
            .join(Contact, Contact.id == OutreachEnrollment.contact_id)
            .where(Contact.company_id == company_id)
            .order_by(OutreachEnrollment.enrolled_at.asc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None, None
    return row[0], row[1]


@router.post("", response_model=DealRead, status_code=201)
async def create_deal(payload: DealCreate, db: AsyncSession = Depends(get_db)):
    """Create a deal for a company.

    When the caller doesn't pin a sequence, we infer attribution by tracing the
    company back to the earliest enrollment among its contacts, so a won deal
    can always credit the outreach that started the conversation.
    """
    company = (
        await db.execute(select(Company.id).where(Company.id == payload.company_id))
    ).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    values = payload.model_dump()
    # Infer attribution only when the caller hasn't set it. If they passed an
    # enrollment but no sequence, backfill the sequence from that enrollment.
    if values.get("source_enrollment_id") is None and values.get("source_sequence_id") is None:
        enr_id, seq_id = await _infer_attribution(db, payload.company_id)
        values["source_enrollment_id"] = enr_id
        values["source_sequence_id"] = seq_id
    elif values.get("source_enrollment_id") is not None and values.get("source_sequence_id") is None:
        enr = (
            await db.execute(
                select(OutreachEnrollment.sequence_id).where(
                    OutreachEnrollment.id == values["source_enrollment_id"]
                )
            )
        ).scalar_one_or_none()
        values["source_sequence_id"] = enr

    # A deal can be created already decided (a historical import, a close logged
    # after the fact). Stamp its decision date the same way an update would.
    _apply_decision_date(values, payload.model_fields_set, previous_status=None)

    deal = Deal(**values)
    db.add(deal)
    await db.flush()
    await db.refresh(deal)

    activity = DealActivity(
        deal_id=deal.id,
        activity_type="created",
        description=f"Deal '{deal.title}' created",
        new_value=deal.status,
    )
    db.add(activity)
    await db.flush()

    logger.info("Created deal %s for company %s", deal.id, payload.company_id)
    return deal


def _attach_company_name(deal, company_name: str | None):
    """Return a dict with company_name injected for serialization."""
    d = DealRead.model_validate(deal).model_dump()
    d["company_name"] = company_name
    return d


@router.get("", response_model=list[DealRead])
async def list_deals(
    company_id: Optional[uuid.UUID] = Query(None),
    status: Optional[str] = Query(None, pattern="^(won|lost|pending)$"),
    deal_stage: Optional[str] = Query(None, pattern="^(qualification|discovery|proposal|negotiation|closing)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List deals, newest first, with optional filters."""
    query = select(Deal, Company.name.label("company_name")).join(
        Company, Deal.company_id == Company.id, isouter=True
    )
    if company_id:
        query = query.where(Deal.company_id == company_id)
    if status:
        query = query.where(Deal.status == status)
    if deal_stage:
        query = query.where(Deal.deal_stage == deal_stage)
    query = query.order_by(Deal.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(query)).all()
    return [_attach_company_name(deal, cname) for deal, cname in rows]


@router.get("/summary", response_model=DealSummary)
async def deals_summary(
    company_id: Optional[uuid.UUID] = Query(None, description="Scope to one company"),
    db: AsyncSession = Depends(get_db),
):
    """Revenue metrics across deals: won revenue, open pipeline, win rate."""
    base = select(Deal.status, func.count(Deal.id), func.coalesce(func.sum(Deal.value), 0))
    if company_id:
        base = base.where(Deal.company_id == company_id)
    base = base.group_by(Deal.status)

    rows = (await db.execute(base)).all()
    by_status_map = {status: (count, _f(total)) for status, count, total in rows}

    breakdown = [
        DealStatusBreakdown(status=s, count=by_status_map.get(s, (0, 0.0))[0],
                            total_value=by_status_map.get(s, (0, 0.0))[1])
        for s in ("won", "lost", "pending")
    ]

    won_count = by_status_map.get("won", (0, 0.0))[0]
    lost_count = by_status_map.get("lost", (0, 0.0))[0]
    pending_count = by_status_map.get("pending", (0, 0.0))[0]
    won_revenue = by_status_map.get("won", (0, 0.0))[1]
    lost_value = by_status_map.get("lost", (0, 0.0))[1]
    pipeline_value = by_status_map.get("pending", (0, 0.0))[1]
    decided = won_count + lost_count

    return DealSummary(
        total_deals=won_count + lost_count + pending_count,
        won_count=won_count,
        lost_count=lost_count,
        pending_count=pending_count,
        won_revenue=won_revenue,
        pipeline_value=pipeline_value,
        lost_value=lost_value,
        win_rate=round(won_count / decided * 100, 1) if decided else 0.0,
        by_status=breakdown,
    )


@router.get("/metrics", response_model=DealMetrics)
async def deal_metrics(
    company_id: Optional[uuid.UUID] = Query(None, description="Scope to one company"),
    db: AsyncSession = Depends(get_db),
):
    """Pipeline metrics: total/weighted value, avg deal size, win rate, days to close, by-stage breakdown."""
    # Pipeline value (pending deals)
    pipeline_q = select(
        func.coalesce(func.sum(Deal.value), 0),
        func.count(Deal.id),
    ).where(Deal.status == "pending")
    if company_id:
        pipeline_q = pipeline_q.where(Deal.company_id == company_id)
    row = (await db.execute(pipeline_q)).one()
    total_pipeline_value = _f(row[0])
    deal_count = row[1]
    average_deal_size = total_pipeline_value / deal_count if deal_count else 0.0

    # Weighted pipeline
    weighted_q = select(Deal.deal_stage, Deal.value).where(Deal.status == "pending")
    if company_id:
        weighted_q = weighted_q.where(Deal.company_id == company_id)
    deals_rows = (await db.execute(weighted_q)).all()
    weighted_pipeline_value = sum(
        _f(val) * DEAL_STAGE_PROBABILITIES.get(stage, 0) / 100
        for stage, val in deals_rows
    )

    # Win rate
    decided_q = select(Deal.status, func.count(Deal.id))
    if company_id:
        decided_q = decided_q.where(Deal.company_id == company_id)
    decided_q = decided_q.where(Deal.status.in_(["won", "lost"])).group_by(Deal.status)
    decided_rows = {status: cnt for status, cnt in (await db.execute(decided_q)).all()}
    won_count = decided_rows.get("won", 0)
    lost_count = decided_rows.get("lost", 0)
    decided = won_count + lost_count
    win_rate = round(won_count / decided * 100, 1) if decided else 0.0

    # Avg days to close (won deals)
    won_deals_q = select(Deal.created_at, Deal.close_date).where(Deal.status == "won", Deal.close_date.isnot(None))
    if company_id:
        won_deals_q = won_deals_q.where(Deal.company_id == company_id)
    won_deals = (await db.execute(won_deals_q)).all()
    if won_deals:
        total_days = sum((cd - ca).days for ca, cd in won_deals)
        avg_days_to_close = round(total_days / len(won_deals), 1)
    else:
        avg_days_to_close = 0.0

    # By-stage breakdown (pending deals)
    stage_q = select(
        Deal.deal_stage,
        func.count(Deal.id),
        func.coalesce(func.sum(Deal.value), 0),
    ).where(Deal.status == "pending")
    if company_id:
        stage_q = stage_q.where(Deal.company_id == company_id)
    stage_q = stage_q.group_by(Deal.deal_stage)
    stage_rows = (await db.execute(stage_q)).all()
    by_stage = [
        DealStageBreakdown(stage=stage, count=cnt, value=_f(val))
        for stage, cnt, val in stage_rows
    ]

    return DealMetrics(
        total_pipeline_value=total_pipeline_value,
        weighted_pipeline_value=round(weighted_pipeline_value, 2),
        average_deal_size=round(average_deal_size, 2),
        deal_count=deal_count,
        win_rate=win_rate,
        avg_days_to_close=avg_days_to_close,
        by_stage=by_stage,
    )


@router.get("/attribution", response_model=AttributionSummary)
async def deal_attribution(db: AsyncSession = Depends(get_db)):
    """Sequence attribution: deals and revenue credited to each outreach sequence.

    Answers "which sequences are producing results" by rolling deals up on their
    ``source_sequence_id``. Deals with no attribution land in an ``unattributed``
    bucket (``sequence_id: null``) rather than being dropped, so the totals still
    reconcile against the overall won-revenue figure.
    """
    # One grouped pass over deals: per crediting sequence, count by status and
    # sum won revenue / open pipeline.
    agg_rows = (
        await db.execute(
            select(
                Deal.source_sequence_id,
                func.count(Deal.id),
                func.count(case((Deal.status == "won", Deal.id))),
                func.count(case((Deal.status == "pending", Deal.id))),
                func.count(case((Deal.status == "lost", Deal.id))),
                func.coalesce(func.sum(case((Deal.status == "won", Deal.value), else_=0)), 0),
                func.coalesce(func.sum(case((Deal.status == "pending", Deal.value), else_=0)), 0),
            ).group_by(Deal.source_sequence_id)
        )
    ).all()

    # Sequence names/status for the attributed buckets.
    seq_ids = [r[0] for r in agg_rows if r[0] is not None]
    seq_meta: dict = {}
    if seq_ids:
        for sid, name, status in (
            await db.execute(
                select(OutreachSequence.id, OutreachSequence.name, OutreachSequence.status)
                .where(OutreachSequence.id.in_(seq_ids))
            )
        ).all():
            seq_meta[sid] = (name, status)

    # Enrollment counts per sequence — the top of the funnel for each row.
    enroll_counts = {
        sid: cnt
        for sid, cnt in (
            await db.execute(
                select(OutreachEnrollment.sequence_id, func.count(OutreachEnrollment.id))
                .group_by(OutreachEnrollment.sequence_id)
            )
        ).all()
    }

    by_sequence: list[SequenceAttribution] = []
    total_won_revenue = 0.0
    total_attributed_won_revenue = 0.0
    attributed_won_deals = 0
    unattributed_won_deals = 0

    for sid, total, won, pending, lost, won_rev, pipeline in agg_rows:
        won_rev_f = _f(won_rev)
        total_won_revenue += won_rev_f
        name, status = seq_meta.get(sid, (None, None))
        if sid is not None:
            total_attributed_won_revenue += won_rev_f
            attributed_won_deals += won
        else:
            unattributed_won_deals += won
        by_sequence.append(
            SequenceAttribution(
                sequence_id=sid,
                sequence_name=name,
                sequence_status=status,
                enrollments=enroll_counts.get(sid) if sid is not None else None,
                total_deals=total,
                won_deals=won,
                pending_deals=pending,
                lost_deals=lost,
                won_revenue=won_rev_f,
                pipeline_value=_f(pipeline),
            )
        )

    # Best sequences first: won revenue, then open pipeline, then deal volume.
    # Keep the unattributed bucket last regardless of its size.
    by_sequence.sort(
        key=lambda a: (
            a.sequence_id is None,
            -a.won_revenue,
            -a.pipeline_value,
            -a.total_deals,
        )
    )

    return AttributionSummary(
        by_sequence=by_sequence,
        total_won_revenue=round(total_won_revenue, 2),
        total_attributed_won_revenue=round(total_attributed_won_revenue, 2),
        attributed_won_deals=attributed_won_deals,
        unattributed_won_deals=unattributed_won_deals,
    )


@router.get("/stages")
async def list_deal_stages():
    """Return the list of valid deal stages with probabilities."""
    return [
        {"stage": s, "probability": p, "label": s.replace("_", " ").title()}
        for s, p in DEAL_STAGE_PROBABILITIES.items()
    ]


@router.get("/forecast", response_model=DealForecast)
async def deal_forecast(
    refresh: bool = Query(False, description="Recompute predictions before aggregating"),
    db: AsyncSession = Depends(get_db),
):
    """AI-adjusted revenue forecast: weighted pipeline, by-month breakdown, at-risk deals.

    Aggregates the latest stored ``DealPrediction`` per active deal. Pass
    ``refresh=true`` to retrain and rescore first (slower; also runs weekly via
    the ``rescore_deal_predictions`` Celery task).
    """
    if refresh:
        # Skip the (network) AI blend on the synchronous refresh path.
        await pipeline_forecaster.rescore_active_deals(db, use_ai=False)
    forecast = await pipeline_forecaster.build_forecast(db)
    return forecast


@router.get("/{deal_id}", response_model=DealRead)
async def get_deal(deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get a single deal by ID."""
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    return deal


@router.get("/{deal_id}/prediction", response_model=DealPredictionRead)
async def get_deal_prediction(
    deal_id: uuid.UUID,
    refresh: bool = Query(False, description="Force a fresh prediction instead of the latest stored one"),
    db: AsyncSession = Depends(get_db),
):
    """Close-probability prediction, risk flags, and predicted close date for one deal.

    Returns the most recent stored prediction; generates one on the fly (without
    the AI blend, for latency) when none exists or ``refresh=true``.
    """
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    prediction = None
    if not refresh:
        latest = await pipeline_forecaster.latest_predictions(db, [deal_id])
        prediction = latest.get(deal_id)
    if prediction is None:
        prediction = await pipeline_forecaster.predict_deal(db, deal, use_ai=refresh)

    company_name = (
        await db.execute(select(Company.name).where(Company.id == deal.company_id))
    ).scalar_one_or_none()
    out = DealPredictionRead.model_validate(prediction).model_dump()
    out["deal_title"] = deal.title
    out["company_name"] = company_name
    return out


@router.patch("/{deal_id}", response_model=DealRead)
async def update_deal(
    deal_id: uuid.UUID, payload: DealUpdate, db: AsyncSession = Depends(get_db)
):
    """Update deal fields."""
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    changes = payload.model_dump(exclude_unset=True)

    # Track stage change
    if "deal_stage" in changes and changes["deal_stage"] != deal.deal_stage:
        db.add(DealActivity(
            deal_id=deal.id,
            activity_type="stage_change",
            description=f"Stage changed from {deal.deal_stage} to {changes['deal_stage']}",
            old_value=deal.deal_stage,
            new_value=changes["deal_stage"],
        ))

    # Track status change
    if "status" in changes and changes["status"] != deal.status:
        db.add(DealActivity(
            deal_id=deal.id,
            activity_type="status_changed",
            description=f"Status changed from {deal.status} to {changes['status']}",
            old_value=deal.status,
            new_value=changes["status"],
        ))

    # Track value change
    if "value" in changes and changes["value"] != deal.value:
        db.add(DealActivity(
            deal_id=deal.id,
            activity_type="value_changed",
            description=f"Value changed from {deal.value} to {changes['value']}",
            old_value=str(deal.value) if deal.value is not None else None,
            new_value=str(changes["value"]) if changes["value"] is not None else None,
        ))

    _apply_decision_date(changes, payload.model_fields_set, previous_status=deal.status)

    for field, value in changes.items():
        setattr(deal, field, value)
    await db.flush()
    await db.refresh(deal)
    logger.info("Updated deal %s", deal_id)
    return deal


@router.delete("/{deal_id}", status_code=204)
async def delete_deal(deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Delete a deal."""
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    await db.delete(deal)
    await db.flush()
    logger.info("Deleted deal %s", deal_id)


@router.post("/{deal_id}/notes", response_model=DealNoteRead, status_code=201)
async def add_deal_note(
    deal_id: uuid.UUID,
    payload: DealNoteCreate,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Add a note to a deal, attributed to the current user."""
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    note = DealNote(deal_id=deal_id, content=payload.content, user_id=scope.owner_id)
    db.add(note)
    await db.flush()
    await db.refresh(note)

    db.add(DealActivity(
        deal_id=deal_id,
        activity_type="note_added",
        description="Note added to deal",
    ))
    await db.flush()

    logger.info("Added note %s to deal %s", note.id, deal_id)
    return note


@router.get("/{deal_id}/notes", response_model=list[DealNoteRead])
async def list_deal_notes(
    deal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """List notes for a deal, newest first.

    Scoped to the caller's own notes; admins/managers may pass
    ``view_as_user_id`` (or ``all``) to see other users' notes on the deal.
    """
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    query = scope.apply(
        select(DealNote).where(DealNote.deal_id == deal_id),
        DealNote.user_id,
    ).order_by(DealNote.created_at.desc())
    return list((await db.execute(query)).scalars().all())


@router.get("/{deal_id}/activity", response_model=list[DealActivityRead])
async def list_deal_activity(deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get activity timeline for a deal, newest first."""
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    query = (
        select(DealActivity)
        .where(DealActivity.deal_id == deal_id)
        .order_by(DealActivity.created_at.desc())
    )
    return list((await db.execute(query)).scalars().all())
