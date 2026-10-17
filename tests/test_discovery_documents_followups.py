"""Discovery documents, stage 4: follow-ups at +3/+7 business days, stop on reply.

The cadence itself is the sequence engine's (covered in the send tests); this
covers the two things stage 4 adds on top: a recorded reply ends the cadence
whichever path recorded it, and the document's follow-up status endpoint.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from tests.test_discovery_documents import (  # noqa: F401 — fixtures
    _crypto_key,
    _reset_org_breaker,
    bg_sessions,
)
from tests.test_discovery_documents_send import (  # noqa: F401 — fixtures
    DRAFT,
    _ready_imported_doc,
    _stub_send_path,
    draft_gemini,
)


async def _follow_up_due(db, contact, *, replied: bool):
    """An enrollment whose first email went out and whose follow-up is due."""
    seq = OutreachSequence(
        id=_uuid.uuid4(), name="Document: leads.csv", is_active=True, status="active",
        send_as_written=True,
    )
    db.add(seq)
    await db.flush()
    for n, delay in ((1, 0), (2, 3)):
        db.add(SequenceStep(
            id=_uuid.uuid4(), sequence_id=seq.id, step_number=n, delay_days=delay,
            delay_business_days=n > 1,
            subject_template="Quick idea for {{ company_name }}",
            body_template=f"Hi {{{{ first_name }}}}, step {n}.",
        ))
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, current_step=2,
        status="active", next_send_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(enrollment)
    await db.flush()
    db.add(OutreachEvent(enrollment_id=enrollment.id, step_number=1, event_type="sent"))
    if replied:
        db.add(OutreachEvent(enrollment_id=enrollment.id, step_number=1, event_type="replied"))
    await db.commit()
    return enrollment


@pytest.mark.asyncio
async def test_recorded_reply_stops_the_follow_up(
    db_session, sample_company, sample_contact, monkeypatch
):
    """A reply event alone (no SmartReply status flip) still ends the cadence."""
    import app.tasks.outreach as outreach_tasks

    enrollment = await _follow_up_due(db_session, sample_contact, replied=True)
    sends, _ = _stub_send_path(monkeypatch, db_session)
    result = await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    assert result == {"status": "skipped", "reason": "contact_replied",
                      "enrollment_id": str(enrollment.id)}
    assert sends == []
    await db_session.refresh(enrollment)
    assert enrollment.status == "replied" and enrollment.completed_at is not None


@pytest.mark.asyncio
async def test_follow_up_goes_out_when_nobody_replied(
    db_session, sample_company, sample_contact, monkeypatch
):
    """The control: same enrollment, no reply → step 2 is sent."""
    import app.tasks.outreach as outreach_tasks

    enrollment = await _follow_up_due(db_session, sample_contact, replied=False)
    sends, _ = _stub_send_path(monkeypatch, db_session)
    result = await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    assert result["status"] == "sent", result
    assert "step 2." in sends[0]["body_html"]


@pytest.mark.asyncio
async def test_outreach_status_before_send_is_empty(
    client, db_session, draft_gemini, bg_sessions
):
    doc_id, _ = await _ready_imported_doc(client, db_session)
    resp = await client.get(f"/api/v1/discovery/documents/{doc_id}/outreach")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sequence_id": None, "sequence": None, "contacts": [], "totals": {}}


@pytest.mark.asyncio
async def test_outreach_status_tracks_each_contact(
    client, db_session, draft_gemini, bg_sessions
):
    doc_id, contact_ids = await _ready_imported_doc(client, db_session)
    steps = [
        {"subject": s["subject"], "body": s["body"], "delay_days": d}
        for s, d in zip(DRAFT["steps"], (0, 3, 4))
    ]
    sent = (await client.post(
        f"/api/v1/discovery/documents/{doc_id}/send",
        json={"contact_ids": contact_ids, "steps": steps},
    )).json()
    enrollments = (await db_session.execute(
        select(OutreachEnrollment).where(
            OutreachEnrollment.sequence_id == _uuid.UUID(sent["sequence_id"])
        )
    )).scalars().all()
    replier, waiting = enrollments
    # Both got the first email (one twice — a reconciled send writes a second
    # ``sent`` row and must not count as two emails); one replied.
    for e in (replier, waiting, waiting):
        db_session.add(OutreachEvent(enrollment_id=e.id, step_number=1, event_type="sent"))
    db_session.add(OutreachEvent(enrollment_id=replier.id, step_number=1, event_type="replied"))
    replier.status = "replied"
    waiting.current_step = 2
    waiting.next_send_at = datetime.now(timezone.utc) + timedelta(days=3)
    await db_session.commit()

    resp = await client.get(f"/api/v1/discovery/documents/{doc_id}/outreach")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sequence_id"] == sent["sequence_id"]
    assert body["sequence"]["total_steps"] == 3 and body["sequence"]["is_active"]
    by_id = {c["enrollment_id"]: c for c in body["contacts"]}
    r, w = by_id[str(replier.id)], by_id[str(waiting.id)]
    assert r["state"] == "replied" and r["replied_at"] and r["next_send_at"] is None
    assert w["state"] == "active" and w["next_send_at"] and w["replied_at"] is None
    assert r["emails_sent"] == 1 and w["emails_sent"] == 1
    assert body["totals"] == {"enrolled": 2, "active": 1, "replied": 1, "completed": 0,
                              "stopped": 0, "emails_sent": 2}


@pytest.mark.asyncio
async def test_outreach_status_is_org_scoped(client, db_session, draft_gemini, bg_sessions):
    resp = await client.get(f"/api/v1/discovery/documents/{_uuid.uuid4()}/outreach")
    assert resp.status_code == 404
