"""Tests for feature 3.5 — Voice Intelligence (Call Recording Analysis)."""

import uuid

import pytest

from app.models.call import CallTask
from app.models.call_recording import CallRecording
from app.services.voice_intelligence import get_voice_intelligence

pytestmark = pytest.mark.asyncio


TRANSCRIPT = """
Rep: Hi Dana, this is Suman from Apprend. Quick question about your quoting process.
Prospect: We're pretty busy, and honestly it's too expensive to switch systems right now.
Rep: Totally fair. What if I showed you how a similar stamping shop cut quoting from two days to twenty minutes?
Prospect: That's interesting. We currently use an old ERP that everyone hates.
Rep: Would a 15-minute demo next Tuesday work?
Prospect: Yes, Tuesday sounds good. Send me a calendar invite.
"""


async def test_talk_listen_ratio():
    vi = get_voice_intelligence()
    ratio = vi.talk_listen_ratio(TRANSCRIPT)
    assert ratio is not None
    assert ratio > 0


async def test_talk_listen_ratio_no_labels():
    vi = get_voice_intelligence()
    assert vi.talk_listen_ratio("just some text with no speaker labels at all") is None


async def test_heuristic_analysis():
    vi = get_voice_intelligence()
    result = await vi.analyze_transcript(TRANSCRIPT, use_ai=False)
    assert result["_source"] == "heuristic"
    # Objection ("too expensive") and competitor cue ("currently use") should surface.
    objections = " ".join(o["objection"] for o in result["objections"])
    assert "too expensive" in objections
    assert result["competitors_mentioned"]
    assert result["deal_signals"]["sentiment"] == "positive"
    assert result["talk_listen_ratio"] is not None


async def _call_task(db_session, company_and_contact=None):
    from app.models.company import Company

    company = Company(
        id=uuid.uuid4(), name="Voice Co", country="US", source="test",
        stage="new", pipeline_stage="new_lead",
    )
    db_session.add(company)
    await db_session.flush()
    task = CallTask(
        id=uuid.uuid4(), company_id=company.id, status="pending", priority="warm",
    )
    db_session.add(task)
    await db_session.flush()
    return task


async def test_process_recording_with_transcript(db_session):
    vi = get_voice_intelligence()
    task = await _call_task(db_session)
    recording = CallRecording(
        id=uuid.uuid4(), call_task_id=task.id, transcription=TRANSCRIPT,
        transcription_source="provided", status="uploaded",
    )
    db_session.add(recording)
    await db_session.flush()

    await vi.process_recording(db_session, recording, use_ai=False)
    assert recording.status == "analyzed"
    assert recording.analysis is not None
    assert recording.talk_listen_ratio is not None
    assert recording.model_used == "heuristic"


async def test_process_recording_no_transcript_fails_gracefully(db_session):
    vi = get_voice_intelligence()
    task = await _call_task(db_session)
    recording = CallRecording(
        id=uuid.uuid4(), call_task_id=task.id, status="uploaded",
    )
    db_session.add(recording)
    await db_session.flush()

    await vi.process_recording(db_session, recording, use_ai=False)
    assert recording.status == "failed"
    assert recording.error


async def test_recording_api_flow(client):
    # Create the company + call task through the API so they're in the request scope.
    r = await client.post("/api/v1/companies/", json={
        "name": "API Voice Co", "country": "US", "source": "test",
    })
    assert r.status_code in (200, 201), r.text
    company_id = r.json()["id"]

    r = await client.post("/api/v1/calls/tasks", json={
        "company_id": company_id, "priority": "warm",
    })
    assert r.status_code in (200, 201), r.text
    task_id = r.json()["id"]

    r = await client.post(
        f"/api/v1/calls/tasks/{task_id}/recording",
        json={"transcription": TRANSCRIPT, "analyze": True, "use_ai": False},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "analyzed"
    assert body["analysis"] is not None

    r = await client.get(f"/api/v1/calls/tasks/{task_id}/analysis")
    assert r.status_code == 200
    assert r.json()["analysis"] is not None
    assert r.json()["talk_listen_ratio"] is not None
