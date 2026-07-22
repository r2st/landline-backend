"""A/B Testing surface for the Dialtone Console (Feature 3).

Lives under ``/dialtone/ab-tests``. Persistence + assignment live in
:mod:`app.models.ab_test` / :mod:`app.services.ab_test_resolver`; the statistics
are the existing engine in :mod:`app.services.ab_testing_service`.

Endpoints:
* ``GET  /dialtone/ab-tests``                      — list active/completed tests.
* ``GET  /dialtone/ab-tests/{test_id}``            — detail + per-variant stats.
* ``POST /dialtone/ab-tests``                      — create a test for a step.
* ``PUT  /dialtone/ab-tests/{test_id}/variants``   — add / edit variants.
* ``POST /dialtone/ab-tests/{test_id}/declare-winner`` — manual override.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.ab_test import (
    AB_METRICS,
    AB_STRATEGIES,
    VARIANT_LABELS,
    AbTest,
    AbTestVariant,
)
from app.models.sequence import SequenceStep
from app.schemas.ab_tests import (
    AbTestCreate,
    AbTestDetail,
    AbTestList,
    AbTestSummary,
    DeclareWinnerRequest,
    VariantInput,
    VariantsUpdate,
    VariantStats,
)
from app.services.ab_test_resolver import compute_variant_stats, declare_winner, evaluate_test
from app.services.ab_testing_service import calculate_required_sample_size

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dialtone/ab-tests", tags=["dialtone-ab-tests"])


async def _load_test(db: AsyncSession, test_id: uuid.UUID) -> AbTest:
    test = (
        await db.execute(select(AbTest).where(AbTest.id == test_id))
    ).scalar_one_or_none()
    if test is None:
        raise HTTPException(status_code=404, detail="A/B test not found")
    return test


async def _step_context(db: AsyncSession, step_id) -> tuple[Optional[SequenceStep], Optional[str]]:
    step = (
        await db.execute(select(SequenceStep).where(SequenceStep.id == step_id))
    ).scalar_one_or_none()
    sequence_id = str(step.sequence_id) if step is not None else None
    return step, sequence_id


async def _variants(db: AsyncSession, step_id) -> list[AbTestVariant]:
    rows = list(
        (
            await db.execute(
                select(AbTestVariant).where(AbTestVariant.sequence_step_id == step_id)
            )
        ).scalars().all()
    )
    rows.sort(key=lambda v: v.label)
    return rows


async def _build_summary(db: AsyncSession, test: AbTest) -> AbTestSummary:
    step, sequence_id = await _step_context(db, test.sequence_step_id)
    variants = await _variants(db, test.sequence_step_id)
    evaluation = await evaluate_test(db, test, persist=False)
    stats = evaluation.get("variants", [])
    total_sent = sum(s.get("sent", 0) for s in stats)
    winner_label = None
    if test.winner_variant_id is not None:
        winner_label = next(
            (v.label for v in variants if v.id == test.winner_variant_id), None
        )
    return AbTestSummary(
        id=str(test.id),
        sequence_step_id=str(test.sequence_step_id),
        sequence_id=sequence_id,
        step_number=step.step_number if step is not None else None,
        status=test.status,
        strategy=test.strategy,
        metric=test.metric,
        min_sample=test.min_sample,
        confidence_target=test.confidence_target,
        winner_variant_id=str(test.winner_variant_id) if test.winner_variant_id else None,
        winner_label=winner_label,
        declared_at=test.declared_at,
        created_at=test.created_at,
        variant_count=len(variants),
        total_sent=total_sent,
        leader_label=evaluation.get("leader_label"),
        confidence=evaluation.get("confidence"),
    )


async def _build_detail(db: AsyncSession, test: AbTest) -> AbTestDetail:
    summary = await _build_summary(db, test)
    step, _ = await _step_context(db, test.sequence_step_id)
    variants = await _variants(db, test.sequence_step_id)
    stats = await compute_variant_stats(db, step, variants, test.metric) if step else []
    stats_by_id = {s["variant_id"]: s for s in stats}

    variant_stats: list[VariantStats] = []
    for v in variants:
        s = stats_by_id.get(v.id, {})
        variant_stats.append(VariantStats(
            variant_id=str(v.id),
            label=v.label,
            is_control=bool(v.is_control),
            is_active=bool(v.is_active),
            subject_template=v.subject_template,
            body_template=v.body_template,
            assigned=s.get("assigned", 0),
            sent=s.get("sent", 0),
            opened=s.get("opened", 0),
            clicked=s.get("clicked", 0),
            replied=s.get("replied", 0),
            open_rate=s.get("open_rate", 0.0),
            click_rate=s.get("click_rate", 0.0),
            reply_rate=s.get("reply_rate", 0.0),
        ))

    active_sent = [s.sent for s in variant_stats if s.is_active]
    min_sent = min(active_sent) if active_sent else 0
    progress = min(1.0, min_sent / test.min_sample) if test.min_sample else 0.0
    evaluation = await evaluate_test(db, test, persist=False)

    return AbTestDetail(
        **summary.model_dump(),
        variants=variant_stats,
        is_significant=bool(evaluation.get("is_significant")),
        default_winner=bool(evaluation.get("default_winner")),
        sample_progress=round(progress, 4),
    )


@router.get("", response_model=AbTestList)
async def list_ab_tests(
    status: Optional[str] = Query(None, description="Filter by status (running/insufficient_data/completed)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List A/B tests, newest-first, optionally filtered by status."""
    count_q = select(func.count()).select_from(AbTest)
    q = select(AbTest).order_by(AbTest.created_at.desc())
    if status is not None:
        q = q.where(AbTest.status == status)
        count_q = count_q.where(AbTest.status == status)

    total = (await db.execute(count_q)).scalar() or 0
    q = q.offset((page - 1) * page_size).limit(page_size)
    tests = list((await db.execute(q)).scalars().all())
    items = [await _build_summary(db, t) for t in tests]
    return AbTestList(items=items, total=total)


@router.get("/{test_id}", response_model=AbTestDetail)
async def get_ab_test(test_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Full test detail with per-variant stats, leader, and confidence."""
    test = await _load_test(db, test_id)
    return await _build_detail(db, test)


def _validate_and_normalize_variants(
    inputs: list[VariantInput], *, existing_labels: Optional[set] = None
) -> list[dict]:
    """Validate + auto-label variants. Returns normalized dicts.

    Auto-assigns the next free label (A→D) to any variant that omits one, caps at
    4 total, rejects duplicate/unknown labels, and enforces at most one control.
    """
    existing_labels = set(existing_labels or set())
    used = set(existing_labels)
    normalized: list[dict] = []
    controls = 0
    for vi in inputs:
        label = (vi.label or "").strip().upper() or None
        if label is not None and label not in VARIANT_LABELS:
            raise HTTPException(status_code=400, detail=f"Invalid variant label '{label}' (A-D)")
        if label is None:
            label = next((c for c in VARIANT_LABELS if c not in used), None)
            if label is None:
                raise HTTPException(status_code=400, detail="At most 4 variants per step")
        if label in used and label not in existing_labels:
            raise HTTPException(status_code=400, detail=f"Duplicate variant label '{label}'")
        used.add(label)
        if vi.is_control:
            controls += 1
        normalized.append({
            "label": label,
            "subject_template": vi.subject_template,
            "body_template": vi.body_template,
            "is_control": vi.is_control,
            "is_active": vi.is_active,
        })
    if len(used) > 4:
        raise HTTPException(status_code=400, detail="At most 4 variants per step")
    if controls > 1:
        raise HTTPException(status_code=400, detail="At most one control variant")
    return normalized


@router.post("", response_model=AbTestDetail, status_code=201)
async def create_ab_test(payload: AbTestCreate, db: AsyncSession = Depends(get_db)):
    """Create an A/B test for a sequence step with its variants.

    Requires ≥2 variants. If none is flagged ``is_control``, the first (label A)
    becomes the control. A step may host at most one test.
    """
    if payload.strategy not in AB_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"strategy must be one of {list(AB_STRATEGIES)}")
    if payload.metric not in AB_METRICS:
        raise HTTPException(status_code=400, detail=f"metric must be one of {list(AB_METRICS)}")
    try:
        step_id = uuid.UUID(str(payload.sequence_step_id))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid sequence_step_id")

    step = (
        await db.execute(select(SequenceStep).where(SequenceStep.id == step_id))
    ).scalar_one_or_none()
    if step is None:
        raise HTTPException(status_code=404, detail="sequence step not found")

    existing = (
        await db.execute(select(AbTest).where(AbTest.sequence_step_id == step_id))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="an A/B test already exists for this step")

    if len(payload.variants) < 2:
        raise HTTPException(status_code=400, detail="an A/B test needs at least 2 variants")

    normalized = _validate_and_normalize_variants(payload.variants)
    # Default the control to label A when none was flagged.
    if not any(v["is_control"] for v in normalized):
        first = min(normalized, key=lambda v: v["label"])
        first["is_control"] = True

    baseline = 0.05  # cold-email reply-rate prior for the sample-size default
    min_sample = payload.min_sample or calculate_required_sample_size(baseline)

    test = AbTest(
        sequence_step_id=step_id,
        strategy=payload.strategy,
        metric=payload.metric,
        min_sample=min_sample,
        confidence_target=payload.confidence_target,
        status="running",
    )
    db.add(test)
    for v in normalized:
        db.add(AbTestVariant(
            sequence_step_id=step_id,
            label=v["label"],
            subject_template=v["subject_template"],
            body_template=v["body_template"],
            is_control=v["is_control"],
            is_active=v["is_active"],
        ))
    await db.flush()
    return await _build_detail(db, test)


@router.post("/seed-subjects/{sequence_id}")
async def seed_subject_tests(
    sequence_id: uuid.UUID,
    strategy: str = Query("even"),
    metric: str = Query("replied"),
    db: AsyncSession = Depends(get_db),
):
    """Seed subject-line A/B tests for a sequence from the static variant bank.

    Turns the hand-written ``SUBJECT_AB_VARIANTS`` alternatives into live tests
    (control A inheriting the step subject + challengers) so cold sends start
    rotating subjects. Idempotent: steps already under test are skipped.
    """
    if strategy not in AB_STRATEGIES:
        raise HTTPException(status_code=400, detail=f"strategy must be one of {list(AB_STRATEGIES)}")
    if metric not in AB_METRICS:
        raise HTTPException(status_code=400, detail=f"metric must be one of {list(AB_METRICS)}")

    from app.services.ab_test_seed import seed_subject_ab_tests

    result = await seed_subject_ab_tests(
        db, sequence_id, strategy=strategy, metric=metric
    )
    return {"sequence_id": str(sequence_id), **result}


@router.put("/{test_id}/variants", response_model=AbTestDetail)
async def update_variants(
    test_id: uuid.UUID, payload: VariantsUpdate, db: AsyncSession = Depends(get_db)
):
    """Add or edit variants on a test (upsert by label).

    An existing label is updated in place; a new label is added (capped at 4).
    Refuses once a winner has been declared.
    """
    test = await _load_test(db, test_id)
    if test.status == "completed":
        raise HTTPException(status_code=409, detail="test already concluded")

    existing = await _variants(db, test.sequence_step_id)
    existing_by_label = {v.label: v for v in existing}
    normalized = _validate_and_normalize_variants(
        payload.variants, existing_labels=set(existing_by_label)
    )

    for v in normalized:
        current = existing_by_label.get(v["label"])
        if current is not None:
            current.subject_template = v["subject_template"]
            current.body_template = v["body_template"]
            current.is_control = v["is_control"]
            current.is_active = v["is_active"]
        else:
            db.add(AbTestVariant(
                sequence_step_id=test.sequence_step_id,
                label=v["label"],
                subject_template=v["subject_template"],
                body_template=v["body_template"],
                is_control=v["is_control"],
                is_active=v["is_active"],
            ))
    await db.flush()
    return await _build_detail(db, test)


@router.post("/{test_id}/declare-winner", response_model=AbTestDetail)
async def declare_winner_endpoint(
    test_id: uuid.UUID, payload: DeclareWinnerRequest, db: AsyncSession = Depends(get_db)
):
    """Manually declare a winner (operator override), by variant id or label."""
    test = await _load_test(db, test_id)
    if test.status == "completed":
        raise HTTPException(status_code=409, detail="test already concluded")

    variants = await _variants(db, test.sequence_step_id)
    target: Optional[AbTestVariant] = None
    if payload.variant_id:
        try:
            vid = uuid.UUID(str(payload.variant_id))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="invalid variant_id")
        target = next((v for v in variants if v.id == vid), None)
    elif payload.label:
        label = payload.label.strip().upper()
        target = next((v for v in variants if v.label == label), None)
    if target is None:
        raise HTTPException(status_code=400, detail="variant_id or a valid label is required")

    await declare_winner(db, test, target.id)
    await db.flush()
    return await _build_detail(db, test)
