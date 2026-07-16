"""Tests for Feature 3 — A/B Testing (Dialtone).

Covers the pillars the spec calls out:

* ``resolve_variant`` even distribution at low N (deterministic round-robin),
* no double-assign (idempotent per enrollment+step; unique constraint),
* winner declaration only at min-sample **and** confidence (leader-vs-control),
* send-path passthrough for non-A/B steps + backwards compatibility,
* the hard cap forcing a default winner,
* the ``/dialtone/ab-tests`` endpoints (create / list / detail / variants /
  declare-winner) and the ``ab_test_concluded`` activity event.

SQLite model tests, mirroring the existing suite style.
"""

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ab_test import AbTest, AbTestAssignment, AbTestVariant
from app.models.outreach import OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services.ab_test_resolver import (
    HARD_CAP_SENDS,
    backfill_assignment_send,
    compute_variant_stats,
    evaluate_test,
    resolve_variant,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────


class _Enrollment:
    """Minimal stand-in — resolve_variant only reads id + contact_id."""

    def __init__(self):
        self.id = uuid.uuid4()
        self.contact_id = uuid.uuid4()


async def _make_step(db, *, subject="Hi {first_name}", body="Body orig", step_number=2):
    seq = OutreachSequence(id=uuid.uuid4(), name=f"seq-{uuid.uuid4().hex[:6]}", status="active")
    db.add(seq)
    await db.flush()
    step = SequenceStep(
        id=uuid.uuid4(),
        sequence_id=seq.id,
        step_number=step_number,
        delay_days=1,
        subject_template=subject,
        body_template=body,
        channel="email",
    )
    db.add(step)
    await db.flush()
    return seq, step


# Default: A (control, inherits step) + B (own copy), both active.
_DEFAULT_VARIANTS = (
    ("A", None, None, True),
    ("B", "Subj B", "Body B", False),
)


async def _make_test(
    db,
    step,
    *,
    strategy="even",
    metric="replied",
    min_sample=200,
    confidence_target=0.95,
    created_at=None,
    status="running",
    variants=_DEFAULT_VARIANTS,
):
    test = AbTest(
        id=uuid.uuid4(),
        sequence_step_id=step.id,
        strategy=strategy,
        metric=metric,
        min_sample=min_sample,
        confidence_target=confidence_target,
        status=status,
    )
    if created_at is not None:
        test.created_at = created_at
    db.add(test)
    vobjs = {}
    for label, subj, bdy, is_control in variants:
        v = AbTestVariant(
            id=uuid.uuid4(),
            sequence_step_id=step.id,
            label=label,
            subject_template=subj,
            body_template=bdy,
            is_control=is_control,
            is_active=True,
        )
        db.add(v)
        vobjs[label] = v
    await db.flush()
    return test, vobjs


async def _seed_sends(db, step, variant, *, sent, replied=0, opened=0, clicked=0):
    """Seed ``sent`` assignments + outreach events for a variant at a step."""
    for i in range(sent):
        eid = uuid.uuid4()
        db.add(AbTestAssignment(
            id=uuid.uuid4(), variant_id=variant.id,
            sequence_step_id=step.id, enrollment_id=eid,
        ))
        db.add(OutreachEvent(
            id=uuid.uuid4(), enrollment_id=eid,
            step_number=step.step_number, event_type="sent",
        ))
        if i < replied:
            db.add(OutreachEvent(
                id=uuid.uuid4(), enrollment_id=eid,
                step_number=step.step_number, event_type="replied",
            ))
        if i < opened:
            db.add(OutreachEvent(
                id=uuid.uuid4(), enrollment_id=eid,
                step_number=step.step_number, event_type="opened",
            ))
        if i < clicked:
            db.add(OutreachEvent(
                id=uuid.uuid4(), enrollment_id=eid,
                step_number=step.step_number, event_type="clicked",
            ))
    await db.flush()


async def _assignment_count(db, enrollment_id=None) -> int:
    q = select(func.count()).select_from(AbTestAssignment)
    if enrollment_id is not None:
        q = q.where(AbTestAssignment.enrollment_id == enrollment_id)
    return (await db.execute(q)).scalar() or 0


# ─── resolve_variant: assignment + distribution ──────────────────────────────


@pytest.mark.asyncio
async def test_resolve_variant_even_distribution_low_n(db_session: AsyncSession):
    """Even round-robin gives an exact 2/2 split over 4 assignments (no RNG)."""
    _, step = await _make_step(db_session)
    await _make_test(db_session, step, strategy="even")

    labels = []
    for _ in range(4):
        res = await resolve_variant(db_session, _Enrollment(), step)
        assert res.is_ab is True
        assert res.assignment_id is not None
        labels.append(res.variant_label)

    # Deterministic round-robin: A, B, A, B — perfectly even at low N.
    assert labels == ["A", "B", "A", "B"]
    assert labels.count("A") == 2 and labels.count("B") == 2


@pytest.mark.asyncio
async def test_control_variant_inherits_step_template(db_session: AsyncSession):
    """A NULL variant template (the control) resolves to the step's template."""
    _, step = await _make_step(db_session, subject="orig subj", body="orig body")
    _, v = await _make_test(db_session, step)

    enr = _Enrollment()
    res = await resolve_variant(db_session, enr, step)
    # First assignment is A (control, NULL templates) → inherits the step copy.
    assert res.variant_label == "A"
    assert res.subject_template == "orig subj"
    assert res.body_template == "orig body"


@pytest.mark.asyncio
async def test_no_double_assign_same_enrollment(db_session: AsyncSession):
    """Re-resolving the same enrollment+step reuses the one assignment."""
    _, step = await _make_step(db_session)
    await _make_test(db_session, step)

    enr = _Enrollment()
    r1 = await resolve_variant(db_session, enr, step)
    r2 = await resolve_variant(db_session, enr, step)

    assert r1.variant_label == r2.variant_label
    assert r1.assignment_id == r2.assignment_id
    assert await _assignment_count(db_session, enr.id) == 1


@pytest.mark.asyncio
async def test_double_assign_blocked_by_unique_constraint(db_session: AsyncSession):
    """The (enrollment_id, sequence_step_id) unique constraint is enforced."""
    _, step = await _make_step(db_session)
    _, v = await _make_test(db_session, step)
    eid = uuid.uuid4()

    db_session.add(AbTestAssignment(
        variant_id=v["A"].id, sequence_step_id=step.id, enrollment_id=eid,
    ))
    await db_session.flush()

    db_session.add(AbTestAssignment(
        variant_id=v["B"].id, sequence_step_id=step.id, enrollment_id=eid,
    ))
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ─── Backwards compatibility / passthrough ───────────────────────────────────


@pytest.mark.asyncio
async def test_passthrough_when_no_test(db_session: AsyncSession):
    """A step with no A/B test returns its own template and writes nothing."""
    _, step = await _make_step(db_session, subject="S1", body="B1")

    res = await resolve_variant(db_session, _Enrollment(), step)

    assert res.is_ab is False
    assert res.assignment_id is None
    assert res.subject_template == "S1"
    assert res.body_template == "B1"
    assert await _assignment_count(db_session) == 0


@pytest.mark.asyncio
async def test_passthrough_when_single_active_variant(db_session: AsyncSession):
    """A step needs ≥2 active variants to be A/B-enabled; 1 → passthrough."""
    _, step = await _make_step(db_session, subject="S1", body="B1")
    await _make_test(db_session, step, variants=(("A", None, None, True),))

    res = await resolve_variant(db_session, _Enrollment(), step)

    assert res.is_ab is False
    assert res.subject_template == "S1"
    assert await _assignment_count(db_session) == 0


@pytest.mark.asyncio
async def test_passthrough_when_test_completed(db_session: AsyncSession):
    """A concluded test falls back to the step template (winner already promoted)."""
    _, step = await _make_step(db_session, subject="winner subj", body="winner body")
    await _make_test(db_session, step, status="completed")

    res = await resolve_variant(db_session, _Enrollment(), step)

    assert res.is_ab is False
    assert res.subject_template == "winner subj"
    assert await _assignment_count(db_session) == 0


@pytest.mark.asyncio
async def test_passthrough_when_second_variant_inactive(db_session: AsyncSession):
    """Deactivating all but one variant reverts the step to single-template."""
    _, step = await _make_step(db_session, subject="S1", body="B1")
    _, v = await _make_test(db_session, step)
    v["B"].is_active = False
    await db_session.flush()

    res = await resolve_variant(db_session, _Enrollment(), step)
    assert res.is_ab is False
    assert res.subject_template == "S1"


# ─── backfill ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_assignment_send(db_session: AsyncSession):
    """backfill links an assignment to an EmailSend id when one is available."""
    _, step = await _make_step(db_session)
    _, v = await _make_test(db_session, step)
    enr = _Enrollment()
    res = await resolve_variant(db_session, enr, step)

    send_id = uuid.uuid4()
    await backfill_assignment_send(db_session, res.assignment_id, send_id)

    row = (
        await db_session.execute(
            select(AbTestAssignment).where(AbTestAssignment.id == res.assignment_id)
        )
    ).scalar_one()
    assert row.email_send_id == send_id

    # No-op when either id is missing (the sequence send path passes None).
    await backfill_assignment_send(db_session, res.assignment_id, None)
    await backfill_assignment_send(db_session, None, send_id)


# ─── metrics ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_variant_stats(db_session: AsyncSession):
    """Per-variant sends/opens/replies + rates aggregate from outreach events."""
    _, step = await _make_step(db_session)
    _, v = await _make_test(db_session, step)
    await _seed_sends(db_session, step, v["A"], sent=10, replied=2, opened=5)
    await _seed_sends(db_session, step, v["B"], sent=10, replied=4, opened=6)

    stats = await compute_variant_stats(db_session, step, list(v.values()), "replied")
    by_label = {s["label"]: s for s in stats}

    assert by_label["A"]["sent"] == 10
    assert by_label["A"]["replied"] == 2
    assert by_label["A"]["opened"] == 5
    assert by_label["A"]["reply_rate"] == 0.2
    assert by_label["A"]["open_rate"] == 0.5
    assert by_label["B"]["reply_rate"] == 0.4


@pytest.mark.asyncio
async def test_stats_ignore_other_steps(db_session: AsyncSession):
    """Events for a different step number don't leak into a step's stats."""
    _, step = await _make_step(db_session, step_number=2)
    _, v = await _make_test(db_session, step)
    await _seed_sends(db_session, step, v["A"], sent=3, replied=1)
    # An event on a different step number for a real assignment's enrollment.
    other_eid = (
        await db_session.execute(
            select(AbTestAssignment.enrollment_id).where(
                AbTestAssignment.variant_id == v["A"].id
            ).limit(1)
        )
    ).scalar_one()
    db_session.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=other_eid, step_number=99, event_type="replied",
    ))
    await db_session.flush()

    stats = await compute_variant_stats(db_session, step, list(v.values()), "replied")
    a = next(s for s in stats if s["label"] == "A")
    assert a["sent"] == 3 and a["replied"] == 1  # step-99 reply excluded


# ─── winner declaration ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_winner_declared_at_min_sample_and_confidence(db_session: AsyncSession):
    """A significant leader at min-sample is declared, promoted, and concludes."""
    _, step = await _make_step(db_session, subject="orig subj", body="orig body")
    test, v = await _make_test(db_session, step, min_sample=200)
    await _seed_sends(db_session, step, v["A"], sent=200, replied=10)   # 5%
    await _seed_sends(db_session, step, v["B"], sent=200, replied=40)   # 20%

    result = await evaluate_test(db_session, test)

    assert result["is_significant"] is True
    assert result["winner_variant_id"] == str(v["B"].id)
    assert result["status"] == "completed"
    assert test.status == "completed"
    assert test.winner_variant_id == v["B"].id
    assert test.declared_at is not None

    # Winner promoted to the step's active template; all variants deactivated.
    await db_session.flush()
    await db_session.refresh(step)
    assert step.subject_template == "Subj B"
    assert step.body_template == "Body B"
    for var in v.values():
        await db_session.refresh(var)
        assert var.is_active is False


@pytest.mark.asyncio
async def test_no_winner_below_min_sample(db_session: AsyncSession):
    """A strong lead below the minimum sample is NOT declared (noise guard)."""
    _, step = await _make_step(db_session)
    test, v = await _make_test(db_session, step, min_sample=200)
    await _seed_sends(db_session, step, v["A"], sent=50, replied=2)
    await _seed_sends(db_session, step, v["B"], sent=50, replied=20)  # big diff, tiny n

    result = await evaluate_test(db_session, test)

    assert result["is_significant"] is False
    assert result["status"] != "completed"
    assert test.status == "running"
    assert test.winner_variant_id is None


@pytest.mark.asyncio
async def test_no_winner_when_confidence_insufficient(db_session: AsyncSession):
    """Enough sample but no real difference → no winner."""
    _, step = await _make_step(db_session)
    test, v = await _make_test(db_session, step, min_sample=200)
    await _seed_sends(db_session, step, v["A"], sent=200, replied=20)  # 10%
    await _seed_sends(db_session, step, v["B"], sent=200, replied=24)  # 12% (n.s.)

    result = await evaluate_test(db_session, test)

    assert result["is_significant"] is False
    assert test.status == "running"
    assert test.winner_variant_id is None


@pytest.mark.asyncio
async def test_hard_cap_declares_default_winner(db_session: AsyncSession):
    """Past the hard cap (age) with no significance → highest-rate default winner."""
    _, step = await _make_step(db_session)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    # min_sample huge so significance never fires; age (30d ≥ 21d) trips the cap.
    test, v = await _make_test(db_session, step, min_sample=10_000, created_at=old)
    await _seed_sends(db_session, step, v["A"], sent=5, replied=1)   # 20%
    await _seed_sends(db_session, step, v["B"], sent=5, replied=3)   # 60%

    result = await evaluate_test(db_session, test)

    assert result["default_winner"] is True
    assert result["is_significant"] is False
    assert result["winner_variant_id"] == str(v["B"].id)
    assert test.status == "completed"
    assert test.winner_variant_id == v["B"].id


@pytest.mark.asyncio
async def test_hard_cap_by_send_volume(db_session: AsyncSession):
    """The send-volume arm of the hard cap also forces a conclusion."""
    _, step = await _make_step(db_session)
    test, v = await _make_test(db_session, step, min_sample=10_000)
    # One variant crosses HARD_CAP_SENDS; no significance possible at this min.
    await _seed_sends(db_session, step, v["A"], sent=HARD_CAP_SENDS, replied=5)
    await _seed_sends(db_session, step, v["B"], sent=10, replied=8)

    result = await evaluate_test(db_session, test)
    assert result["default_winner"] is True
    assert test.status == "completed"


@pytest.mark.asyncio
async def test_evaluate_completed_test_is_noop(db_session: AsyncSession):
    """Re-evaluating a concluded test doesn't change its winner (idempotent)."""
    _, step = await _make_step(db_session)
    test, v = await _make_test(db_session, step, min_sample=200)
    await _seed_sends(db_session, step, v["A"], sent=200, replied=10)
    await _seed_sends(db_session, step, v["B"], sent=200, replied=40)
    await evaluate_test(db_session, test)
    assert test.status == "completed"
    winner = test.winner_variant_id

    result2 = await evaluate_test(db_session, test)
    assert result2["status"] == "completed"
    assert test.winner_variant_id == winner


# ─── bandit strategy ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bandit_explores_unsent_variants_first(db_session: AsyncSession):
    """Bandit assignment explores every never-sent arm before exploiting."""
    _, step = await _make_step(db_session)
    await _make_test(db_session, step, strategy="bandit")

    seen = set()
    for _ in range(2):
        res = await resolve_variant(db_session, _Enrollment(), step)
        seen.add(res.variant_label)
    # With no send data yet, UCB1 explores both arms before repeating.
    assert seen == {"A", "B"}


# ─── endpoints ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_list_and_get_endpoints(
    client: AsyncClient, db_session: AsyncSession
):
    _, step = await _make_step(db_session, subject="orig", body="orig body")
    await db_session.commit()

    payload = {
        "sequence_step_id": str(step.id),
        "strategy": "even",
        "metric": "replied",
        "variants": [
            {"is_control": True, "subject_template": None, "body_template": None},
            {"subject_template": "Subj B", "body_template": "Body B"},
        ],
    }
    resp = await client.post("/api/v1/dialtone/ab-tests", json=payload)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["variant_count"] == 2
    assert data["status"] == "running"
    assert sorted(v["label"] for v in data["variants"]) == ["A", "B"]
    control = [v for v in data["variants"] if v["is_control"]]
    assert len(control) == 1 and control[0]["label"] == "A"
    test_id = data["id"]

    resp = await client.get(f"/api/v1/dialtone/ab-tests/{test_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == test_id

    resp = await client.get("/api/v1/dialtone/ab-tests")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1

    resp = await client.get("/api/v1/dialtone/ab-tests", params={"status": "running"})
    assert resp.status_code == 200
    assert all(t["status"] == "running" for t in resp.json()["items"])


@pytest.mark.asyncio
async def test_create_rejects_too_few_variants(
    client: AsyncClient, db_session: AsyncSession
):
    _, step = await _make_step(db_session)
    await db_session.commit()
    resp = await client.post("/api/v1/dialtone/ab-tests", json={
        "sequence_step_id": str(step.id),
        "variants": [{"is_control": True}],
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_rejects_unknown_step_and_duplicate(
    client: AsyncClient, db_session: AsyncSession
):
    resp = await client.post("/api/v1/dialtone/ab-tests", json={
        "sequence_step_id": str(uuid.uuid4()),
        "variants": [{"is_control": True}, {"subject_template": "b"}],
    })
    assert resp.status_code == 404

    _, step = await _make_step(db_session)
    await db_session.commit()
    body = {
        "sequence_step_id": str(step.id),
        "variants": [{"is_control": True}, {"subject_template": "b"}],
    }
    assert (await client.post("/api/v1/dialtone/ab-tests", json=body)).status_code == 201
    # A step hosts at most one test.
    assert (await client.post("/api/v1/dialtone/ab-tests", json=body)).status_code == 409


@pytest.mark.asyncio
async def test_update_variants_endpoint(
    client: AsyncClient, db_session: AsyncSession
):
    _, step = await _make_step(db_session)
    await db_session.commit()
    create = await client.post("/api/v1/dialtone/ab-tests", json={
        "sequence_step_id": str(step.id),
        "variants": [{"is_control": True}, {"subject_template": "b"}],
    })
    test_id = create.json()["id"]

    resp = await client.put(f"/api/v1/dialtone/ab-tests/{test_id}/variants", json={
        "variants": [{"label": "C", "subject_template": "Subj C"}],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["variant_count"] == 3
    labels = sorted(v["label"] for v in resp.json()["variants"])
    assert labels == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_declare_winner_endpoint_and_activity_event(
    client: AsyncClient, db_session: AsyncSession
):
    _, step = await _make_step(db_session)
    await db_session.commit()
    create = await client.post("/api/v1/dialtone/ab-tests", json={
        "sequence_step_id": str(step.id),
        "variants": [{"is_control": True}, {"label": "B", "subject_template": "Subj B", "body_template": "Body B"}],
    })
    test_id = create.json()["id"]

    resp = await client.post(
        f"/api/v1/dialtone/ab-tests/{test_id}/declare-winner", json={"label": "B"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "completed"
    assert data["winner_label"] == "B"

    # Declaring again is refused (already concluded).
    again = await client.post(
        f"/api/v1/dialtone/ab-tests/{test_id}/declare-winner", json={"label": "A"}
    )
    assert again.status_code == 409

    # The conclusion surfaces in the Dialtone activity feed.
    feed = await client.get(
        "/api/v1/dialtone/activity", params={"types": "ab_test_concluded"}
    )
    assert feed.status_code == 200
    items = feed.json()["items"]
    assert any(i["metadata"].get("ab_test_id") == test_id for i in items)
