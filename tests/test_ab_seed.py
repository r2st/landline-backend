"""F4 — seed subject-line A/B tests from the static variant bank + activate them.

``seed_subject_ab_tests`` turns ``SUBJECT_AB_VARIANTS`` into live AbTest +
AbTestVariant rows (control A inheriting the step subject + challengers), which
is exactly what ``resolve_variant`` needs to start rotating subjects on cold
sends. Covers the control-not-duplicated rule, idempotency, and that the
resolver then picks a seeded variant.
"""

import uuid

import pytest
from sqlalchemy import func, select

from app.models.ab_test import AbTest, AbTestAssignment, AbTestVariant
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence, SequenceStep
from app.services.ab_test_resolver import resolve_variant
from app.services.ab_test_seed import seed_subject_ab_tests
from app.services.outreach_templates import SUBJECT_AB_VARIANTS


async def _molding_like_sequence(db):
    """A sequence whose step subjects match the SUBJECT_AB_VARIANTS bank."""
    seq = OutreachSequence(id=uuid.uuid4(), name=f"seq-{uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    steps = {}
    for step_number, subjects in SUBJECT_AB_VARIANTS.items():
        step = SequenceStep(
            id=uuid.uuid4(), sequence_id=seq.id, step_number=step_number,
            delay_days=0,
            subject_template=subjects[0],  # control == first listed subject
            body_template="<p>Body</p>",
        )
        db.add(step)
        steps[step_number] = step
    await db.flush()
    return seq, steps


@pytest.mark.asyncio
async def test_seeding_creates_tests_and_variants(db_session):
    seq, steps = await _molding_like_sequence(db_session)

    result = await seed_subject_ab_tests(db_session, seq.id)

    assert set(result["created"]) == set(SUBJECT_AB_VARIANTS)
    assert result["skipped"] == []

    for step_number, step in steps.items():
        tests = (
            await db_session.execute(
                select(AbTest).where(AbTest.sequence_step_id == step.id)
            )
        ).scalars().all()
        assert len(tests) == 1
        assert tests[0].status == "running"

        variants = (
            await db_session.execute(
                select(AbTestVariant).where(AbTestVariant.sequence_step_id == step.id)
            )
        ).scalars().all()
        labels = sorted(v.label for v in variants)
        # control A + the distinct challengers (SUBJECT_AB_VARIANTS has 3 each,
        # one of which equals the control → A, B, C).
        assert labels[0] == "A"
        assert len(variants) >= 2

        control = next(v for v in variants if v.is_control)
        # A is the control and inherits the step subject (NULL template).
        assert control.label == "A"
        assert control.subject_template is None
        # No challenger duplicates the control's subject.
        challenger_subjects = [v.subject_template for v in variants if not v.is_control]
        assert step.subject_template not in challenger_subjects


@pytest.mark.asyncio
async def test_seeding_is_idempotent(db_session):
    seq, steps = await _molding_like_sequence(db_session)
    await seed_subject_ab_tests(db_session, seq.id)

    second = await seed_subject_ab_tests(db_session, seq.id)
    assert second["created"] == []
    assert set(second["skipped"]) == set(SUBJECT_AB_VARIANTS)

    # Exactly one test per step — no duplicates.
    total_tests = (await db_session.execute(select(func.count()).select_from(AbTest))).scalar_one()
    assert total_tests == len(SUBJECT_AB_VARIANTS)


@pytest.mark.asyncio
async def test_resolver_picks_a_seeded_variant(db_session, sample_contact):
    seq, steps = await _molding_like_sequence(db_session)
    await seed_subject_ab_tests(db_session, seq.id)
    step = steps[1]

    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db_session.add(enr)
    await db_session.flush()

    resolution = await resolve_variant(db_session, enr, step)
    assert resolution.is_ab is True
    assert resolution.variant_label in ("A", "B", "C", "D")
    # An assignment was persisted before the (would-be) send.
    count = (
        await db_session.execute(
            select(func.count()).select_from(AbTestAssignment).where(
                AbTestAssignment.enrollment_id == enr.id
            )
        )
    ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_seed_endpoint(client, db_session):
    seq, steps = await _molding_like_sequence(db_session)
    await db_session.commit()

    resp = await client.post(f"/api/v1/dialtone/ab-tests/seed-subjects/{seq.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["created"]) == set(SUBJECT_AB_VARIANTS)
