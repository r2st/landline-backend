"""F3 — outreach funnel analytics from OutreachEvent (not EmailSend).

Verifies the sent→opened→replied→bounced funnel, the three breakdowns (step,
sequence, subject), per-contact dedup (an open pixel firing twice is one open),
A/B variant attribution, and the sequence filter.
"""

import uuid

import pytest

from app.models.ab_test import AbTestAssignment, AbTestVariant
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep

SUBJ_A = "the scrap math your CFO hasn't run yet"
SUBJ_B = "your scrap bin is more expensive than you think"


async def _seq(db, name):
    seq = OutreachSequence(id=uuid.uuid4(), name=name)
    db.add(seq)
    await db.flush()
    step = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
        subject_template=SUBJ_A, body_template="B",
    )
    db.add(step)
    await db.flush()
    return seq, step


async def _enroll(db, contact, seq):
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db.add(enr)
    await db.flush()
    return enr


def _event(db, enr, event_type, *, step=1, subject=None):
    meta = {}
    if subject is not None:
        meta["subject"] = subject
    db.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=step,
        event_type=event_type, event_metadata=meta,
    ))


@pytest.mark.asyncio
async def test_funnel_counts_and_breakdowns(client, db_session, sample_company, sample_contact):
    seq, step = await _seq(db_session, "Funnel Seq")
    enr1 = await _enroll(db_session, sample_contact, seq)

    # A second contact/enrollment in the same sequence.
    from app.models.contact import Contact
    c2 = Contact(
        id=uuid.uuid4(), company_id=sample_company.id, first_name="Jane",
        last_name="Doe", email="jane@acmestamp.com", source="test",
    )
    db_session.add(c2)
    await db_session.flush()
    enr2 = await _enroll(db_session, c2, seq)

    # enr1 step1: sent(Subj A) + opened (twice → dedup to 1) + replied
    _event(db_session, enr1, "sent", subject=SUBJ_A)
    _event(db_session, enr1, "opened")
    _event(db_session, enr1, "opened")
    _event(db_session, enr1, "replied")
    # enr2 step1: sent(Subj A) + bounced
    _event(db_session, enr2, "sent", subject=SUBJ_A)
    _event(db_session, enr2, "bounced")
    await db_session.commit()

    resp = await client.get("/api/v1/reports/outreach-funnel")
    assert resp.status_code == 200
    data = resp.json()

    overall = data["overall"]
    assert overall["sent"] == 2
    assert overall["opened"] == 1     # deduped: two opens on one send count once
    assert overall["replied"] == 1
    assert overall["bounced"] == 1
    assert overall["open_rate"] == 0.5
    assert overall["reply_rate"] == 0.5
    assert overall["bounce_rate"] == 0.5

    # by_step
    assert len(data["by_step"]) == 1
    assert data["by_step"][0]["step_number"] == 1
    assert data["by_step"][0]["counts"]["sent"] == 2

    # by_sequence
    seqs = {s["sequence_name"]: s for s in data["by_sequence"]}
    assert seqs["Funnel Seq"]["counts"]["sent"] == 2

    # by_subject — both sends carried Subj A, so opened/replied/bounced attribute
    # to it via the (enrollment, step) subject lookup.
    subs = {s["subject"]: s for s in data["by_subject"]}
    assert SUBJ_A in subs
    assert subs[SUBJ_A]["counts"]["sent"] == 2
    assert subs[SUBJ_A]["counts"]["opened"] == 1
    assert subs[SUBJ_A]["counts"]["replied"] == 1


@pytest.mark.asyncio
async def test_funnel_attributes_ab_variant_label(
    client, db_session, sample_company, sample_contact
):
    seq, step = await _seq(db_session, "AB Funnel Seq")
    enr = await _enroll(db_session, sample_contact, seq)

    variant = AbTestVariant(
        id=uuid.uuid4(), sequence_step_id=step.id, label="B",
        subject_template=SUBJ_B, is_control=False, is_active=True,
    )
    db_session.add(variant)
    await db_session.flush()
    db_session.add(AbTestAssignment(
        id=uuid.uuid4(), variant_id=variant.id, sequence_step_id=step.id,
        enrollment_id=enr.id, contact_id=sample_contact.id,
    ))
    _event(db_session, enr, "sent", subject=SUBJ_B)
    _event(db_session, enr, "opened")
    await db_session.commit()

    resp = await client.get("/api/v1/reports/outreach-funnel")
    data = resp.json()
    by_subject = {(s["subject"], s["variant_label"]): s for s in data["by_subject"]}
    assert (SUBJ_B, "B") in by_subject
    assert by_subject[(SUBJ_B, "B")]["counts"]["sent"] == 1


@pytest.mark.asyncio
async def test_funnel_sequence_filter(client, db_session, sample_company, sample_contact):
    seq_a, _ = await _seq(db_session, "Seq A")
    seq_b, _ = await _seq(db_session, "Seq B")
    enr_a = await _enroll(db_session, sample_contact, seq_a)

    from app.models.contact import Contact
    c2 = Contact(
        id=uuid.uuid4(), company_id=sample_company.id, first_name="Bo",
        email="bo@acmestamp.com", source="test",
    )
    db_session.add(c2)
    await db_session.flush()
    enr_b = await _enroll(db_session, c2, seq_b)

    _event(db_session, enr_a, "sent", subject=SUBJ_A)
    _event(db_session, enr_b, "sent", subject=SUBJ_B)
    await db_session.commit()

    resp = await client.get(f"/api/v1/reports/outreach-funnel?sequence_id={seq_a.id}")
    data = resp.json()
    assert data["overall"]["sent"] == 1
    names = {s["sequence_name"] for s in data["by_sequence"]}
    assert names == {"Seq A"}
