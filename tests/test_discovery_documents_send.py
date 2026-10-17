"""Discovery documents, stage 3: AI draft → preview → send as a sequence.

Plus the send-path guarantee that makes "send" mean what the user approved:
a ``send_as_written`` sequence is never rewritten by the first-touch composer
or the optimizer, and never parked for a second review.
"""

import json
import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.discovery_document import DiscoveryDocument
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence, SequenceStep
from app.services import discovery_documents as svc
from app.services import org_gemini
from tests.test_discovery_documents import (  # noqa: F401 — fixtures
    CSV,
    _crypto_key,
    _reset_org_breaker,
    _save_key,
    bg_sessions,
    fake_gemini,
)

DRAFT = {"steps": [
    {"subject": "{{first_name}}, quick idea for {{ company_name }}",
     "body": "Hi {{ first_name }},\n\nWe cut die changeover time. {{ secret_var }}Worth a chat?"},
    {"subject": "Re: quick idea", "body": "Hi {{ first_name }}, bumping this up."},
    {"subject": "Re: quick idea", "body": "Last note from me, {{ first_name }}."},
]}


async def _ready_imported_doc(client, db_session):
    """Upload the shared CSV, import Acme; return (doc_id, contact_ids)."""
    await _save_key(db_session)
    doc_id = (await client.post(
        "/api/v1/discovery/documents", files={"file": ("leads.csv", CSV, "text/csv")}
    )).json()["id"]
    imp = (await client.post(
        f"/api/v1/discovery/documents/{doc_id}/import", json={"candidate_ids": ["c1"]}
    )).json()
    return doc_id, imp["contact_ids"]


@pytest.fixture
def draft_gemini(monkeypatch):
    """Answer table prompts with the mapping and draft prompts with DRAFT."""
    from tests.test_discovery_documents import CSV_MAPPING

    prompts = []

    async def fake_chat(messages, *, providers, **kw):
        prompt = messages[0]["content"]
        prompts.append(prompt)
        answer = DRAFT if "BRIEF" in prompt else CSV_MAPPING
        return json.dumps(answer), providers[0].name, providers[0].models[0]

    monkeypatch.setattr(org_gemini, "chat_completion", fake_chat)
    return prompts


def test_sanitize_keeps_only_known_merge_fields():
    out = svc.sanitize_template(
        "Hi {{first_name}} at {{ company_name }}{{ x.__class__ }}{% if 1 %}!{% endif %}{# c #}"
    )
    assert out == "Hi {{ first_name }} at {{ company_name }}!"


@pytest.mark.asyncio
async def test_draft_returns_editable_steps_with_approved_cadence(
    client, db_session, draft_gemini, bg_sessions
):
    doc_id, _ = await _ready_imported_doc(client, db_session)
    resp = await client.post(
        f"/api/v1/discovery/documents/{doc_id}/draft",
        json={"brief": "We make stamping dies that change over in 5 minutes.", "tone": "friendly"},
    )
    assert resp.status_code == 200, resp.text
    steps = resp.json()["steps"]
    assert [s["delay_days"] for s in steps] == [0, 3, 4]  # business days 0, 3, 7
    assert "{{ secret_var }}" not in steps[0]["body"]
    assert steps[0]["subject"].startswith("{{ first_name }}")
    # The brief went to Gemini; the document's companies were offered as data.
    assert "Acme Stamping" in draft_gemini[-1]


@pytest.mark.asyncio
async def test_draft_respects_follow_up_count_and_needs_a_key(
    client, db_session, draft_gemini, bg_sessions
):
    doc_id, _ = await _ready_imported_doc(client, db_session)
    resp = await client.post(
        f"/api/v1/discovery/documents/{doc_id}/draft",
        json={"brief": "We make stamping dies.", "follow_ups": 0},
    )
    assert len(resp.json()["steps"]) == 1

    from app.models.org_llm_key import OrgLlmKey
    for row in (await db_session.execute(select(OrgLlmKey))).scalars():
        await db_session.delete(row)
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/discovery/documents/{doc_id}/draft", json={"brief": "We make stamping dies."}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "gemini_key_required"


@pytest.mark.asyncio
async def test_preview_renders_like_the_sender(client, db_session, draft_gemini, bg_sessions):
    doc_id, contact_ids = await _ready_imported_doc(client, db_session)
    resp = await client.post(
        f"/api/v1/discovery/documents/{doc_id}/preview",
        json={"steps": DRAFT["steps"][:1]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contact_id"] in contact_ids
    first = body["steps"][0]
    assert "Acme Stamping" in first["subject"]
    assert first["body"].startswith("Hi Jane,") or first["body"].startswith("Hi Raj,")


@pytest.mark.asyncio
async def test_send_creates_send_as_written_sequence_and_enrolls(
    client, db_session, draft_gemini, bg_sessions
):
    doc_id, contact_ids = await _ready_imported_doc(client, db_session)
    steps = [
        {"subject": s["subject"], "body": s["body"], "delay_days": d}
        for s, d in zip(DRAFT["steps"], (0, 3, 4))
    ]
    resp = await client.post(
        f"/api/v1/discovery/documents/{doc_id}/send",
        json={"contact_ids": contact_ids, "steps": steps},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["enrolled_count"] == 2 and result["skipped_count"] == 0

    seq = (await db_session.execute(
        select(OutreachSequence).where(OutreachSequence.id == _uuid.UUID(result["sequence_id"]))
    )).scalar_one()
    assert seq.send_as_written is True and seq.is_active
    rows = (await db_session.execute(
        select(SequenceStep).where(SequenceStep.sequence_id == seq.id).order_by(SequenceStep.step_number)
    )).scalars().all()
    assert [(r.delay_days, r.delay_business_days) for r in rows] == [(0, False), (3, True), (4, True)]
    assert "secret_var" not in rows[0].body_template
    enrolled = (await db_session.execute(
        select(OutreachEnrollment.contact_id).where(OutreachEnrollment.sequence_id == seq.id)
    )).scalars().all()
    assert sorted(map(str, enrolled)) == sorted(contact_ids)

    doc = (await db_session.execute(
        select(DiscoveryDocument).where(DiscoveryDocument.id == _uuid.UUID(doc_id))
    )).scalar_one()
    assert doc.sequence_id == seq.id


@pytest.mark.asyncio
async def test_send_refuses_contacts_not_from_this_document(
    client, db_session, draft_gemini, bg_sessions, sample_contact
):
    doc_id, contact_ids = await _ready_imported_doc(client, db_session)
    resp = await client.post(
        f"/api/v1/discovery/documents/{doc_id}/send",
        json={"contact_ids": [*contact_ids, str(sample_contact.id)],
              "steps": [{"subject": "Hi", "body": "Hello"}]},
    )
    assert resp.status_code == 422
    assert (await db_session.execute(select(OutreachEnrollment))).first() is None


# ── the send path ────────────────────────────────────────────────────────────

async def _due_enrollment(db, contact, *, send_as_written):
    seq = OutreachSequence(
        id=_uuid.uuid4(), name="Document: leads.csv", is_active=True, status="active",
        send_as_written=send_as_written,
    )
    db.add(seq)
    await db.flush()
    db.add(SequenceStep(
        id=_uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
        subject_template="Quick idea for {{ company_name }}",
        body_template="Hi {{ first_name }}, approved copy.",
    ))
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, current_step=1,
        status="active", next_send_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(enrollment)
    await db.commit()
    return enrollment


def _stub_send_path(monkeypatch, db_session):
    """Composer wants to rewrite *and* park for review; optimizer wants to rewrite."""
    import app.tasks.outreach as outreach_tasks
    from app.services import ai_compose

    monkeypatch.setattr(outreach_tasks, "_run_async", lambda coro: coro)

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(outreach_tasks, "celery_session", _session)
    calls = {"compose": 0, "optimizer": 0}

    async def _compose(db, enrollment, contact, company, step, subject, body):
        calls["compose"] += 1
        return ai_compose.ComposeResult("review", "AI subject", "AI body", reason="first batch")

    monkeypatch.setattr(ai_compose, "compose_step", _compose)

    class _Optimizer:
        async def decide(self, *a, **k):
            calls["optimizer"] += 1
            return {"action": "rewrite", "subject": "Optimized", "body": "Optimized body"}

    monkeypatch.setattr(outreach_tasks, "get_sequence_optimizer", lambda: _Optimizer())
    monkeypatch.setattr(outreach_tasks, "record_decision", lambda *a, **k: None)
    sends = []

    async def _send(db, **kwargs):
        sends.append(kwargs)
        return {"status": "sent", "message_id": "m1", "thread_id": "t1"}

    monkeypatch.setattr(outreach_tasks, "send_outreach_email", _send)
    return sends, calls


@pytest.mark.asyncio
async def test_send_as_written_sequence_goes_out_exactly_as_approved(
    db_session, sample_company, sample_contact, monkeypatch
):
    import app.tasks.outreach as outreach_tasks

    enrollment = await _due_enrollment(db_session, sample_contact, send_as_written=True)
    sends, calls = _stub_send_path(monkeypatch, db_session)
    result = await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    assert result["status"] == "sent", result
    assert calls == {"compose": 0, "optimizer": 0}
    assert sends[0]["subject"] == f"Quick idea for {sample_company.name}"
    assert "approved copy." in sends[0]["body_html"]
    assert "Optimized" not in sends[0]["body_html"]


@pytest.mark.asyncio
async def test_ordinary_sequences_still_get_the_composer(
    db_session, sample_company, sample_contact, monkeypatch
):
    """The control: without the flag, the composer runs (and here parks it)."""
    import app.tasks.outreach as outreach_tasks

    enrollment = await _due_enrollment(db_session, sample_contact, send_as_written=False)
    sends, calls = _stub_send_path(monkeypatch, db_session)
    result = await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    assert calls["compose"] == 1
    assert result["status"] == "pending_review"
    assert sends == []
