"""Subject-line A/B tests are decided on open rate.

The variant machinery already carried a ``subject_template``, but every test was
scored on reply rate: ``evaluate_experiment`` made replies primary regardless of
what the test declared, the hard-cap default winner sorted by ``reply_rate``, and
the conclusion alert counted replies. A subject test judged that way is measuring
the body's work and crediting it to the subject — and needs an order of magnitude
more volume to conclude, since cold reply rates run a few percent against open
rates near half.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.ab_test import AbTest, AbTestAssignment, AbTestVariant
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services.ab_test_resolver import evaluate_test
from app.services.ab_test_seed import generate_subject_test_for_step, seed_subject_ab_tests
from app.services.ab_testing_service import auto_declare_winner, evaluate_experiment
from app.services.outreach_templates import SUBJECT_AB_VARIANTS


class _Experiment:
    """Duck-typed stand-in for the shape the engine consumes."""

    def __init__(self, **counts):
        self.id = uuid.uuid4()
        self.name = "subject test"
        for k, v in counts.items():
            setattr(self, k, v)
        self.winner = None
        self.confidence_level = None
        self.is_significant = False
        self.status = "running"
        self.completed_at = None


# --- engine: the metric under test decides ------------------------------------


def test_open_rate_metric_declares_a_subject_winner():
    """B doubles opens on ample volume; replies are identical and irrelevant."""
    exp = _Experiment(
        variant_a_sent=1000, variant_a_opened=200, variant_a_clicked=10, variant_a_replied=30,
        variant_b_sent=1000, variant_b_opened=400, variant_b_clicked=10, variant_b_replied=30,
    )
    result = evaluate_experiment(exp, "opened")
    assert result["primary_metric"] == "opened"
    assert result["overall_significant"] is True
    assert result["overall_winner"] == "B"


def test_reply_noise_cannot_decide_an_open_rate_test():
    """Subjects tie on opens; B happens to reply better. No subject winner."""
    exp = _Experiment(
        variant_a_sent=1000, variant_a_opened=300, variant_a_clicked=10, variant_a_replied=10,
        variant_b_sent=1000, variant_b_opened=302, variant_b_clicked=10, variant_b_replied=80,
    )
    result = evaluate_experiment(exp, "opened")
    assert result["overall_significant"] is False
    assert result["overall_winner"] is None
    # The reply difference is real — it just isn't this test's business.
    assert result["reply_rate"]["is_significant"] is True


def test_omitting_the_metric_keeps_the_legacy_behaviour():
    """Callers that never said what they were testing get the old semantics."""
    exp = _Experiment(
        variant_a_sent=1000, variant_a_opened=300, variant_a_clicked=10, variant_a_replied=10,
        variant_b_sent=1000, variant_b_opened=302, variant_b_clicked=10, variant_b_replied=80,
    )
    legacy = evaluate_experiment(exp)
    assert legacy["primary_metric"] is None
    assert legacy["overall_winner"] == "B"  # decided on replies, as before


def test_auto_declare_honours_the_metric():
    exp = _Experiment(
        variant_a_sent=1000, variant_a_opened=200, variant_a_clicked=10, variant_a_replied=30,
        variant_b_sent=1000, variant_b_opened=400, variant_b_clicked=10, variant_b_replied=30,
    )
    auto_declare_winner(exp, datetime.now(timezone.utc), metric="opened")
    assert exp.winner == "B"
    assert exp.status == "completed"


# --- persistence + resolver ---------------------------------------------------


async def _step(db, *, subject="quick question about your press line"):
    seq = OutreachSequence(id=uuid.uuid4(), name=f"seq-{uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    step = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
        subject_template=subject, body_template="<p>Body</p>",
    )
    db.add(step)
    await db.flush()
    return seq, step


async def _record(db, test, step, variant, *, sends, opens, replies=0):
    """Assign ``sends`` enrollments to ``variant`` and log their engagement."""
    for i in range(sends):
        enr = OutreachEnrollment(
            id=uuid.uuid4(), contact_id=uuid.uuid4(), sequence_id=step.sequence_id,
            status="active", current_step=1,
        )
        db.add(enr)
        await db.flush()
        db.add(AbTestAssignment(
            id=uuid.uuid4(), variant_id=variant.id, sequence_step_id=step.id,
            enrollment_id=enr.id,
        ))
        db.add(OutreachEvent(
            enrollment_id=enr.id, step_number=1, event_type="sent",
            occurred_at=datetime.now(timezone.utc),
        ))
        if i < opens:
            db.add(OutreachEvent(
                enrollment_id=enr.id, step_number=1, event_type="opened",
                occurred_at=datetime.now(timezone.utc),
            ))
        if i < replies:
            db.add(OutreachEvent(
                enrollment_id=enr.id, step_number=1, event_type="replied",
                occurred_at=datetime.now(timezone.utc),
            ))
    await db.flush()


@pytest.mark.asyncio
async def test_seeded_subject_tests_default_to_open_rate(db_session):
    """A subject test scored on replies needs ~10x the volume to ever conclude."""
    seq = OutreachSequence(id=uuid.uuid4(), name=f"seq-{uuid.uuid4().hex[:6]}")
    db_session.add(seq)
    await db_session.flush()
    for step_number, subjects in SUBJECT_AB_VARIANTS.items():
        db_session.add(SequenceStep(
            id=uuid.uuid4(), sequence_id=seq.id, step_number=step_number, delay_days=0,
            subject_template=subjects[0], body_template="<p>Body</p>",
        ))
    await db_session.flush()

    await seed_subject_ab_tests(db_session, seq.id)

    tests = (await db_session.execute(select(AbTest))).scalars().all()
    assert tests
    assert all(t.metric == "opened" for t in tests)


@pytest.mark.asyncio
async def test_subject_test_concludes_on_opens_not_replies(db_session):
    """End to end: the better-opening subject wins, and it wins the step."""
    seq, step = await _step(db_session)
    test = AbTest(
        id=uuid.uuid4(), sequence_step_id=step.id, status="running",
        strategy="even", metric="opened", min_sample=100, confidence_target=0.95,
    )
    db_session.add(test)
    control = AbTestVariant(
        id=uuid.uuid4(), sequence_step_id=step.id, label="A",
        subject_template=None, body_template=None, is_control=True, is_active=True,
    )
    challenger = AbTestVariant(
        id=uuid.uuid4(), sequence_step_id=step.id, label="B",
        subject_template="downtime on your presses?", body_template=None,
        is_control=False, is_active=True,
    )
    db_session.add_all([control, challenger])
    await db_session.flush()

    # B opens far better; replies are deliberately identical, so a reply-scored
    # evaluation would find nothing.
    await _record(db_session, test, step, control, sends=200, opens=40, replies=8)
    await _record(db_session, test, step, challenger, sends=200, opens=100, replies=8)

    result = await evaluate_test(db_session, test)
    await db_session.flush()

    assert result["is_significant"] is True
    assert result["winner_variant_id"] == str(challenger.id)
    assert test.status == "completed"
    # The winning subject is promoted onto the step, so 100% of future sends use it.
    await db_session.refresh(step)
    assert step.subject_template == "downtime on your presses?"
    # A subject-only test must not touch the body.
    assert step.body_template == "<p>Body</p>"


@pytest.mark.asyncio
async def test_hard_cap_default_winner_uses_the_test_metric(db_session):
    """Past the cap with no significance, pick the best OPENER, not the best replier."""
    seq, step = await _step(db_session)
    test = AbTest(
        id=uuid.uuid4(), sequence_step_id=step.id, status="running",
        strategy="even", metric="opened",
        # min_sample above what we record, so the significance path can't fire and
        # the hard cap is what decides.
        min_sample=10_000, confidence_target=0.95,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(test)
    a = AbTestVariant(
        id=uuid.uuid4(), sequence_step_id=step.id, label="A",
        subject_template=None, body_template=None, is_control=True, is_active=True,
    )
    b = AbTestVariant(
        id=uuid.uuid4(), sequence_step_id=step.id, label="B",
        subject_template="the better opener", body_template=None,
        is_control=False, is_active=True,
    )
    db_session.add_all([a, b])
    await db_session.flush()

    # A replies better; B opens better. On the old reply-sorted default, A wins.
    await _record(db_session, test, step, a, sends=500, opens=100, replies=50)
    await _record(db_session, test, step, b, sends=500, opens=300, replies=10)

    result = await evaluate_test(db_session, test)
    assert result["default_winner"] is True
    assert result["winner_variant_id"] == str(b.id)


# --- AI generation ------------------------------------------------------------


class _FakeAI:
    def __init__(self, variants):
        self._variants = variants
        self.calls = 0

    async def _complete_json(self, prompt, max_tokens=None):
        self.calls += 1
        return {"variants": self._variants}


@pytest.mark.asyncio
async def test_generate_subject_test_builds_a_runnable_test(db_session):
    seq, step = await _step(db_session, subject="quick question")
    ai = _FakeAI(["downtime on your presses?", "an hour a shift", "quick question"])

    result = await generate_subject_test_for_step(db_session, step, ai=ai)

    assert ai.calls == 1
    # The duplicate of the step's own subject is dropped, not offered as a
    # challenger against itself.
    assert "quick question" not in result["challengers"]
    assert len(result["challengers"]) == 2

    variants = (
        await db_session.execute(
            select(AbTestVariant).where(AbTestVariant.sequence_step_id == step.id)
        )
    ).scalars().all()
    assert len(variants) == 3

    control = next(v for v in variants if v.is_control)
    assert control.label == "A"
    # Control inherits the step subject rather than copying it, so editing the
    # step keeps the control in sync.
    assert control.subject_template is None
    # Subject-only: the body is held constant across every arm.
    assert all(v.body_template is None for v in variants)
    assert result["test"].metric == "opened"


@pytest.mark.asyncio
async def test_generate_falls_back_when_no_llm_is_reachable(db_session):
    """No key, no network — still produces a runnable test."""

    class _DeadAI:
        async def _complete_json(self, prompt, max_tokens=None):
            raise RuntimeError("no provider available")

    seq, step = await _step(db_session, subject="quick question")
    result = await generate_subject_test_for_step(db_session, step, ai=_DeadAI())

    assert result["challengers"]
    assert result["test"].status == "running"


@pytest.mark.asyncio
async def test_generate_refuses_a_second_test_on_the_same_step(db_session):
    seq, step = await _step(db_session)
    ai = _FakeAI(["a", "b"])
    await generate_subject_test_for_step(db_session, step, ai=ai)

    with pytest.raises(ValueError, match="already exists"):
        await generate_subject_test_for_step(db_session, step, ai=ai)


@pytest.mark.asyncio
async def test_generate_subjects_endpoint(client, db_session):
    seq, step = await _step(db_session, subject="quick question about downtime")
    await db_session.commit()

    resp = await client.post(f"/api/v1/dialtone/ab-tests/generate-subjects/{step.id}")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["metric"] == "opened"
    assert len(body["variants"]) >= 2

    # One test per step.
    again = await client.post(f"/api/v1/dialtone/ab-tests/generate-subjects/{step.id}")
    assert again.status_code == 409
