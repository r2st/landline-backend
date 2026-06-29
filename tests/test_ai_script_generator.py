"""Tests for the AI custom call-script generator: service, caching, fallback,
queue pre-generation, and the API endpoints."""

import uuid as _uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import CallTask
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.services import ai_script_generator as gen


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeGen:
    """Stand-in for AIScriptGenerator with a controllable availability + result."""

    def __init__(self, available=True, result=None, error=False):
        self.available = available
        self.calls = 0
        self._result = result or {
            "opening_hook": "Hi Pat, saw Maple CNC just added a 5-axis cell — congrats.",
            "personalization_notes": "Used their recent equipment news + Owner role.",
            "talking_points": ["ROI on scrap", "protect your best customer"],
            "qualifying_questions": ["Which machine scraps the most?", "How do you catch tool wear?"],
            "pain_points": ["tool wear", "first-article rejection"],
            "objection_handling": [{"objection": "We're fine", "rebuttal": "Cheap insurance"}],
            "call_to_action": "Free 48-hour pilot — Tuesday or Thursday?",
            "voicemail": "Hi Pat, {rep_name} at Apprend, call me at {rep_phone}.",
        }
        self._error = error

    async def generate_script_json(self, prompt: str) -> dict:
        self.calls += 1
        if self._error:
            return {"error": "boom"}
        return dict(self._result)


def _install_fake(monkeypatch, fake: _FakeGen):
    monkeypatch.setattr(gen, "get_script_generator", lambda: fake)
    return fake


# --------------------------------------------------------------------------- #
# Unit tests — role detection, call-type mapping, parsing
# --------------------------------------------------------------------------- #


def test_detect_role_levels():
    assert gen._detect_role("Owner / President")[0] == "executive"
    assert gen._detect_role("VP of Quality")[0] == "quality"
    assert gen._detect_role("Plant Manager")[0] == "operations"
    assert gen._detect_role("Process Engineer")[0] == "engineering"
    assert gen._detect_role(None)[0] == "balanced"
    assert gen._detect_role("Marketing Intern")[0] == "balanced"


def test_normalize_call_type():
    assert gen._normalize_call_type("initial") == "initial_cold_call"
    assert gen._normalize_call_type("follow_up") == "follow_up"
    assert gen._normalize_call_type("voicemail") == "voicemail"
    assert gen._normalize_call_type("nonsense") == "initial_cold_call"


def test_coerce_objections_normalises_shapes():
    raw = [
        {"objection": "No budget", "rebuttal": "Free pilot"},
        {"concern": "Too busy", "response": "20 seconds"},
        {"garbage": "x"},
        "not a dict",
    ]
    out = gen._coerce_objections(raw)
    assert {"objection": "No budget", "rebuttal": "Free pilot"} in out
    assert {"objection": "Too busy", "rebuttal": "20 seconds"} in out
    assert len(out) == 2


# --------------------------------------------------------------------------- #
# Service — fallback and AI paths
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_generate_falls_back_to_template_without_key(
    db_session: AsyncSession, monkeypatch
):
    _install_fake(monkeypatch, _FakeGen(available=False))
    company = Company(id=_uuid.uuid4(), name="Maple CNC", source="test",
                      industry_segment="CNC Machining")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="Pat",
                      last_name="Lee", title="Owner", source="test")
    db_session.add(contact)
    await db_session.flush()

    resp = await gen.generate_for_contact(db_session, company=company, contact=contact)
    assert resp.source == "template"
    assert resp.ai_available is False
    assert resp.industry == "cnc_machining"
    assert resp.script.opening_hook  # template hook present
    assert len(resp.script.qualifying_questions) >= 3
    assert "unavailable" in (resp.note or "").lower()


@pytest.mark.asyncio
async def test_generate_uses_ai_when_available(db_session: AsyncSession, monkeypatch):
    fake = _install_fake(monkeypatch, _FakeGen(available=True))
    company = Company(id=_uuid.uuid4(), name="Maple CNC", source="test",
                      industry_segment="CNC Machining")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="Pat",
                      title="Owner", source="test")
    db_session.add(contact)
    await db_session.flush()

    resp = await gen.generate_for_contact(db_session, company=company, contact=contact)
    assert fake.calls == 1
    assert resp.source == "ai"
    assert "5-axis" in resp.script.opening_hook
    assert resp.script.personalization_notes
    assert resp.script.objection_handling[0]["objection"] == "We're fine"


@pytest.mark.asyncio
async def test_generate_falls_back_on_ai_error(db_session: AsyncSession, monkeypatch):
    _install_fake(monkeypatch, _FakeGen(available=True, error=True))
    company = Company(id=_uuid.uuid4(), name="Acme", source="test",
                      industry_segment="Injection Molding")
    db_session.add(company)
    await db_session.flush()

    resp = await gen.generate_for_contact(db_session, company=company, contact=None)
    assert resp.source == "template"
    assert resp.ai_available is True
    assert "failed" in (resp.note or "").lower()


# --------------------------------------------------------------------------- #
# Caching on a call task
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_or_generate_for_task_caches(db_session: AsyncSession, monkeypatch):
    fake = _install_fake(monkeypatch, _FakeGen(available=True))
    company = Company(id=_uuid.uuid4(), name="Maple CNC", source="test",
                      industry_segment="CNC Machining")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="Pat",
                      title="Owner", source="test")
    db_session.add(contact)
    await db_session.flush()
    task = CallTask(id=_uuid.uuid4(), company_id=company.id, contact_id=contact.id,
                    status="pending", priority="hot")
    db_session.add(task)
    await db_session.flush()

    first = await gen.get_or_generate_for_task(db_session, task)
    assert fake.calls == 1
    assert first.call_task_id == task.id
    assert task.custom_script is not None
    assert task.script_generated_at is not None
    assert task.script_context["engagement"]["opens"] == 0

    # Second call returns the cache — no new AI call.
    second = await gen.get_or_generate_for_task(db_session, task)
    assert fake.calls == 1
    assert second.script.opening_hook == first.script.opening_hook

    # Force regenerates.
    await gen.get_or_generate_for_task(db_session, task, force=True)
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_pregenerate_only_hot_and_warm(db_session: AsyncSession, monkeypatch):
    fake = _install_fake(monkeypatch, _FakeGen(available=True))
    company = Company(id=_uuid.uuid4(), name="Co", source="test",
                      industry_segment="CNC Machining")
    db_session.add(company)
    await db_session.flush()
    tasks = []
    for pri in ("hot", "warm", "cold"):
        t = CallTask(id=_uuid.uuid4(), company_id=company.id, status="pending", priority=pri)
        db_session.add(t)
        tasks.append(t)
    await db_session.flush()

    count = await gen.pregenerate_for_tasks(db_session, tasks)
    assert count == 2  # hot + warm only
    cold = [t for t in tasks if t.priority == "cold"][0]
    assert cold.custom_script is None


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_post_generate_endpoint_template_fallback(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    _install_fake(monkeypatch, _FakeGen(available=False))
    company = Company(id=_uuid.uuid4(), name="Acme Molding", source="test",
                      industry_segment="Injection Molding", pipeline_stage="outreach")
    db_session.add(company)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/calls/scripts/generate",
        json={"company_id": str(company.id), "call_type": "initial"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "template"
    assert body["industry"] == "injection_molding"
    assert body["script"]["opening_hook"]


@pytest.mark.asyncio
async def test_post_generate_endpoint_invalid_call_type(
    client: AsyncClient, db_session: AsyncSession
):
    company = Company(id=_uuid.uuid4(), name="Acme", source="test")
    db_session.add(company)
    await db_session.commit()
    resp = await client.post(
        "/api/v1/calls/scripts/generate",
        json={"company_id": str(company.id), "call_type": "shouting"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_generate_unknown_company(client: AsyncClient):
    resp = await client.post(
        "/api/v1/calls/scripts/generate",
        json={"company_id": str(_uuid.uuid4())},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_generate_caches_on_open_task(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    fake = _install_fake(monkeypatch, _FakeGen(available=True))
    company = Company(id=_uuid.uuid4(), name="Maple CNC", source="test",
                      industry_segment="CNC Machining")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="Pat",
                      title="Owner", source="test")
    db_session.add(contact)
    await db_session.flush()
    task = CallTask(id=_uuid.uuid4(), company_id=company.id, contact_id=contact.id,
                    status="pending", priority="hot")
    db_session.add(task)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/calls/scripts/generate",
        json={"company_id": str(company.id), "contact_id": str(contact.id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "ai"
    assert body["call_task_id"] == str(task.id)

    # The script was cached onto the task.
    refreshed = (
        await db_session.execute(select(CallTask).where(CallTask.id == task.id))
    ).scalar_one()
    assert refreshed.custom_script is not None


@pytest.mark.asyncio
async def test_get_task_custom_script_generates_and_caches(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    fake = _install_fake(monkeypatch, _FakeGen(available=True))
    company = Company(id=_uuid.uuid4(), name="Maple CNC", source="test",
                      industry_segment="CNC Machining")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="Pat",
                      title="Quality Manager", source="test")
    db_session.add(contact)
    await db_session.flush()
    task = CallTask(id=_uuid.uuid4(), company_id=company.id, contact_id=contact.id,
                    status="pending", priority="warm")
    db_session.add(task)
    await db_session.commit()

    first = await client.get(f"/api/v1/calls/tasks/{task.id}/custom-script")
    assert first.status_code == 200, first.text
    assert first.json()["source"] == "ai"
    assert fake.calls == 1

    # Second GET serves the cache, no new AI call.
    second = await client.get(f"/api/v1/calls/tasks/{task.id}/custom-script")
    assert second.status_code == 200
    assert fake.calls == 1

    # regenerate=true forces a fresh generation.
    third = await client.get(
        f"/api/v1/calls/tasks/{task.id}/custom-script", params={"regenerate": "true"}
    )
    assert third.status_code == 200
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_get_task_custom_script_unknown_task(client: AsyncClient):
    resp = await client.get(f"/api/v1/calls/tasks/{_uuid.uuid4()}/custom-script")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_queue_generate_pregenerates_scripts(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    fake = _install_fake(monkeypatch, _FakeGen(available=True))
    company = Company(id=_uuid.uuid4(), name="Hot CNC", source="test",
                      industry_segment="CNC Machining", pipeline_stage="outreach")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="Hot",
                      email="hot@x.com", source="test")
    db_session.add(contact)
    await db_session.flush()
    # Make the contact hot (replied) so it pre-generates.
    db_session.add(EmailSend(id=_uuid.uuid4(), contact_id=contact.id, to_email="hot@x.com",
                             status="sent", sent_at=datetime.now(timezone.utc),
                             replied_at=datetime.now(timezone.utc)))
    await db_session.commit()

    resp = await client.post("/api/v1/calls/queue/generate", json={"limit": 5})
    assert resp.status_code == 200, resp.text
    assert resp.json()["generated"] == 1
    assert fake.calls == 1  # the hot lead's script was pre-generated

    # The pre-generated script is cached and served without a new AI call.
    task = (
        await db_session.execute(select(CallTask).where(CallTask.company_id == company.id))
    ).scalar_one()
    assert task.custom_script is not None
    got = await client.get(f"/api/v1/calls/tasks/{task.id}/custom-script")
    assert got.status_code == 200
    assert fake.calls == 1
