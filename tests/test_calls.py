"""Tests for the cold-call notification system: tasks, scripts, notifications,
queue generation, briefing, and the Celery trigger logic."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import CallNotification, CallTask
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.services import call_scripts_data
from app.services.call_queue import generate_call_queue_for_session, score_lead, _Engagement
from app.tasks.calls import (
    detect_hot_leads_for_session,
    flag_missed_calls_for_session,
    generate_morning_call_queue_for_session,
    send_followup_reminders_for_session,
    send_precall_reminders_for_session,
)

INDUSTRIES = ("injection_molding", "cnc_machining", "metal_stamping", "food_beverage")


# --------------------------------------------------------------------------- #
# Script data (single source of truth)
# --------------------------------------------------------------------------- #


def test_scripts_cover_all_industries_and_types():
    by_key = {(s.industry, s.script_type) for s in call_scripts_data.all_scripts()}
    for ind in INDUSTRIES:
        for st in call_scripts_data.SCRIPT_TYPES:
            assert (ind, st) in by_key, f"missing {ind}/{st}"


def test_initial_scripts_are_fully_populated():
    for ind in INDUSTRIES:
        s = call_scripts_data.get_script(ind, "initial_cold_call")
        assert s is not None
        assert s.opening_hook
        assert 3 <= len(s.qualifying_questions) <= 6
        assert len(s.pain_points) >= 3
        assert "Apprend" in (s.value_proposition or "")
        assert "pages.dev" in (s.value_proposition or "")  # demo link present
        assert len(s.objection_handling) >= 3
        assert all({"objection", "rebuttal"} <= set(o) for o in s.objection_handling)
        assert s.call_to_action


def test_resolve_script_routes_by_industry_text():
    s = call_scripts_data.resolve_script(industry="precision CNC machine shop")
    assert s.industry == "cnc_machining"
    # Unknown text falls back to the flagship vertical.
    s2 = call_scripts_data.resolve_script(industry="something unrelated")
    assert s2.industry == "injection_molding"


# --------------------------------------------------------------------------- #
# Call task CRUD
# --------------------------------------------------------------------------- #


async def _make_task(client, company_id, **kw):
    payload = {"company_id": str(company_id)}
    payload.update(kw)
    return await client.post("/api/v1/calls/tasks", json=payload)


@pytest.mark.asyncio
async def test_create_call_task(client: AsyncClient, sample_company: Company, sample_contact: Contact):
    resp = await _make_task(
        client, sample_company.id,
        contact_id=str(sample_contact.id), priority="hot", assigned_to="rep@apprend.io",
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["priority"] == "hot"
    assert data["status"] == "pending"
    assert data["assigned_to"] == "rep@apprend.io"


@pytest.mark.asyncio
async def test_create_call_task_unknown_company(client: AsyncClient):
    assert (await _make_task(client, _uuid.uuid4())).status_code == 404


@pytest.mark.asyncio
async def test_create_call_task_invalid_enum(client: AsyncClient, sample_company: Company):
    assert (await _make_task(client, sample_company.id, priority="boiling")).status_code == 422


@pytest.mark.asyncio
async def test_call_task_complete_stamps_completed_at(client: AsyncClient, sample_company: Company):
    tid = (await _make_task(client, sample_company.id)).json()["id"]
    upd = await client.put(
        f"/api/v1/calls/tasks/{tid}",
        json={"status": "completed", "outcome": "connected", "call_duration_seconds": 180,
              "notes": "Great chat"},
    )
    assert upd.status_code == 200, upd.text
    body = upd.json()
    assert body["status"] == "completed"
    assert body["outcome"] == "connected"
    assert body["call_duration_seconds"] == 180
    assert body["completed_at"] is not None


@pytest.mark.asyncio
async def test_call_task_invalid_outcome_rejected(client: AsyncClient, sample_company: Company):
    tid = (await _make_task(client, sample_company.id)).json()["id"]
    resp = await client.put(f"/api/v1/calls/tasks/{tid}", json={"outcome": "hung_up"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_call_tasks_filters(client: AsyncClient, sample_company: Company):
    await _make_task(client, sample_company.id, priority="hot")
    await _make_task(client, sample_company.id, priority="cold")
    hot = await client.get("/api/v1/calls/tasks", params={"priority": "hot"})
    assert hot.status_code == 200
    assert all(t["priority"] == "hot" for t in hot.json())


@pytest.mark.asyncio
async def test_todays_call_queue(client: AsyncClient, sample_company: Company):
    now = datetime.now(timezone.utc)
    # Scheduled today (hot), scheduled yesterday (overdue), unscheduled today.
    await _make_task(client, sample_company.id, priority="warm",
                     scheduled_at=now.isoformat())
    await _make_task(client, sample_company.id, priority="hot",
                     scheduled_at=(now - timedelta(days=1)).isoformat())
    await _make_task(client, sample_company.id, priority="cold")

    resp = await client.get("/api/v1/calls/tasks/today")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 3
    # Hottest first.
    assert tasks[0]["priority"] == "hot"


@pytest.mark.asyncio
async def test_todays_queue_excludes_overdue_when_disabled(client: AsyncClient, sample_company: Company):
    now = datetime.now(timezone.utc)
    await _make_task(client, sample_company.id, scheduled_at=(now - timedelta(days=2)).isoformat())
    resp = await client.get("/api/v1/calls/tasks/today", params={"include_overdue": "false"})
    assert resp.json() == []


# --------------------------------------------------------------------------- #
# Scripts API
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_scripts_falls_back_to_defaults(client: AsyncClient):
    """With no DB rows, the API serves the canonical in-code scripts."""
    resp = await client.get("/api/v1/calls/scripts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == len(call_scripts_data.all_scripts())


@pytest.mark.asyncio
async def test_get_script_for_industry(client: AsyncClient):
    resp = await client.get("/api/v1/calls/scripts/cnc_machining")
    assert resp.status_code == 200
    assert resp.json()["industry"] == "cnc_machining"


@pytest.mark.asyncio
async def test_get_script_unknown_industry(client: AsyncClient):
    resp = await client.get("/api/v1/calls/scripts/aerospace")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upsert_script_creates_then_updates(client: AsyncClient):
    payload = {
        "industry": "metal_stamping",
        "script_type": "initial_cold_call",
        "title": "Custom stamping script",
        "opening_hook": "Hi there",
        "qualifying_questions": ["q1", "q2"],
        "pain_points": ["die wear"],
        "value_proposition": "We help",
        "objection_handling": [{"objection": "no", "rebuttal": "yes"}],
        "call_to_action": "Book a pilot",
    }
    created = await client.post("/api/v1/calls/scripts", json=payload)
    assert created.status_code == 201, created.text
    assert created.json()["title"] == "Custom stamping script"

    payload["title"] = "Updated stamping script"
    updated = await client.post("/api/v1/calls/scripts", json=payload)
    assert updated.status_code == 201
    assert updated.json()["title"] == "Updated stamping script"

    # Now the DB has a row; GET returns the DB version.
    got = await client.get("/api/v1/calls/scripts/metal_stamping")
    assert got.json()["title"] == "Updated stamping script"


@pytest.mark.asyncio
async def test_upsert_script_invalid_industry(client: AsyncClient):
    resp = await client.post(
        "/api/v1/calls/scripts",
        json={"industry": "spaceships", "title": "x", "opening_hook": "y"},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Queue generation
# --------------------------------------------------------------------------- #


def test_score_lead_priority_buckets():
    now = datetime.now(timezone.utc)

    replied = _Engagement(); replied.replies = 1
    assert score_lead(replied, 50, "cnc_machining", now)[0] == "hot"

    clicked = _Engagement(); clicked.clicks = 1
    assert score_lead(clicked, 0, "cnc_machining", now)[0] == "hot"

    opened3 = _Engagement(); opened3.opens = 3
    assert score_lead(opened3, 0, "cnc_machining", now)[0] == "hot"

    opened1 = _Engagement(); opened1.opens = 1
    assert score_lead(opened1, 0, "cnc_machining", now)[0] == "warm"

    recent = _Engagement(); recent.last_contact_at = now - timedelta(days=2)
    assert score_lead(recent, 0, "cnc_machining", now)[0] == "warm"

    cold = _Engagement()  # never contacted
    assert score_lead(cold, 0, "cnc_machining", now)[0] == "cold"


@pytest.mark.asyncio
async def test_generate_queue_orders_engaged_first(db_session: AsyncSession):
    company = Company(
        id=_uuid.uuid4(), name="Precision CNC Co", source="test",
        industry_segment="CNC Machining", pipeline_stage="outreach",
    )
    db_session.add(company)
    await db_session.flush()

    engaged = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="En",
                      last_name="Gaged", email="en@x.com", source="test")
    cold = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="Co",
                   last_name="Ld", email="co@x.com", source="test")
    db_session.add_all([engaged, cold])
    await db_session.flush()

    # The engaged contact replied to an email.
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=engaged.id, to_email="en@x.com",
        status="sent", sent_at=datetime.now(timezone.utc),
        opened_at=datetime.now(timezone.utc), replied_at=datetime.now(timezone.utc),
    ))
    await db_session.flush()

    result = await generate_call_queue_for_session(db_session, assigned_to="rep@x.com", limit=10)
    assert result["generated"] == 2
    assert result["hot"] == 1
    assert result["tasks"][0].contact_id == engaged.id  # engaged ranked first
    assert result["tasks"][0].priority == "hot"
    assert result["tasks"][0].assigned_to == "rep@x.com"


@pytest.mark.asyncio
async def test_generate_queue_skips_existing_open_tasks(db_session: AsyncSession):
    company = Company(id=_uuid.uuid4(), name="Co", source="test", pipeline_stage="engaged")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="A",
                      email="a@x.com", source="test")
    db_session.add(contact)
    await db_session.flush()
    db_session.add(CallTask(id=_uuid.uuid4(), company_id=company.id,
                            contact_id=contact.id, status="pending", priority="warm"))
    await db_session.flush()

    result = await generate_call_queue_for_session(db_session, limit=10)
    assert result["generated"] == 0
    assert result["skipped_existing"] == 1


@pytest.mark.asyncio
async def test_generate_queue_api(client: AsyncClient, db_session: AsyncSession):
    company = Company(id=_uuid.uuid4(), name="Co", source="test", pipeline_stage="outreach")
    db_session.add(company)
    await db_session.flush()
    db_session.add(Contact(id=_uuid.uuid4(), company_id=company.id, first_name="A",
                           email="a@x.com", source="test"))
    await db_session.commit()

    resp = await client.post("/api/v1/calls/queue/generate", json={"limit": 5})
    assert resp.status_code == 200, resp.text
    assert resp.json()["generated"] == 1


# --------------------------------------------------------------------------- #
# Briefing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_call_briefing(client: AsyncClient, db_session: AsyncSession):
    company = Company(id=_uuid.uuid4(), name="Maple CNC", source="test",
                      industry_segment="CNC Machining", phone="555-1000",
                      pipeline_stage="outreach")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="Pat",
                      last_name="Lee", title="Owner", email="pat@x.com",
                      phone="555-2000", source="test")
    db_session.add(contact)
    await db_session.flush()
    task = CallTask(id=_uuid.uuid4(), company_id=company.id, contact_id=contact.id,
                    status="pending", priority="warm")
    db_session.add(task)
    await db_session.commit()

    resp = await client.get(f"/api/v1/calls/tasks/{task.id}/briefing")
    assert resp.status_code == 200, resp.text
    brief = resp.json()
    assert brief["company_name"] == "Maple CNC"
    assert brief["contact_name"] == "Pat Lee"
    assert brief["industry"] == "cnc_machining"
    assert brief["phone"] == "555-2000"
    assert brief["script"]["industry"] == "cnc_machining"
    assert len(brief["pain_points"]) >= 3
    assert len(brief["talking_points"]) >= 2


@pytest.mark.asyncio
async def test_briefing_unknown_task(client: AsyncClient):
    assert (await client.get(f"/api/v1/calls/tasks/{_uuid.uuid4()}/briefing")).status_code == 404


# --------------------------------------------------------------------------- #
# Notifications + Celery trigger logic
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_notifications_list_and_mark_read(client: AsyncClient, db_session: AsyncSession):
    notif = CallNotification(id=_uuid.uuid4(), notification_type="hot_lead_alert",
                             message="Hot lead!", sent_at=datetime.now(timezone.utc))
    db_session.add(notif)
    await db_session.commit()

    unread = await client.get("/api/v1/calls/notifications")
    assert unread.status_code == 200
    assert len(unread.json()) == 1

    marked = await client.put(f"/api/v1/calls/notifications/{notif.id}/read")
    assert marked.status_code == 200
    assert marked.json()["read_at"] is not None

    assert await client.get("/api/v1/calls/notifications") is not None
    assert (await client.get("/api/v1/calls/notifications")).json() == []


@pytest.mark.asyncio
async def test_precall_reminder_fires_within_window(db_session: AsyncSession):
    company = Company(id=_uuid.uuid4(), name="Co", source="test")
    db_session.add(company)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    task = CallTask(id=_uuid.uuid4(), company_id=company.id, status="pending",
                    priority="hot", scheduled_at=now + timedelta(minutes=10))
    db_session.add(task)
    await db_session.flush()

    result = await send_precall_reminders_for_session(db_session, now=now)
    assert result["created"] == 1
    # Idempotent — no duplicate on a second run.
    assert (await send_precall_reminders_for_session(db_session, now=now))["created"] == 0


@pytest.mark.asyncio
async def test_followup_reminder_for_due_callback(db_session: AsyncSession):
    company = Company(id=_uuid.uuid4(), name="Co", source="test")
    db_session.add(company)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    task = CallTask(id=_uuid.uuid4(), company_id=company.id, status="pending",
                    priority="warm", outcome="callback_scheduled",
                    scheduled_at=now - timedelta(minutes=5))
    db_session.add(task)
    await db_session.flush()

    result = await send_followup_reminders_for_session(db_session, now=now)
    assert result["created"] == 1


@pytest.mark.asyncio
async def test_detect_hot_leads_creates_task_and_alert(db_session: AsyncSession):
    company = Company(id=_uuid.uuid4(), name="Co", source="test")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="Hot",
                      last_name="Lead", email="hot@x.com", source="test")
    db_session.add(contact)
    await db_session.flush()
    # Three opened emails → hot.
    for _ in range(3):
        db_session.add(EmailSend(id=_uuid.uuid4(), contact_id=contact.id,
                                 to_email="hot@x.com", status="sent",
                                 sent_at=datetime.now(timezone.utc),
                                 opened_at=datetime.now(timezone.utc)))
    await db_session.flush()

    result = await detect_hot_leads_for_session(db_session)
    assert result["alerts"] == 1
    assert result["tasks_created"] == 1
    # Idempotent.
    assert (await detect_hot_leads_for_session(db_session))["alerts"] == 0


@pytest.mark.asyncio
async def test_flag_missed_calls(db_session: AsyncSession):
    company = Company(id=_uuid.uuid4(), name="Co", source="test")
    db_session.add(company)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    task = CallTask(id=_uuid.uuid4(), company_id=company.id, status="pending",
                    priority="warm", scheduled_at=now - timedelta(hours=2))
    db_session.add(task)
    await db_session.flush()

    result = await flag_missed_calls_for_session(db_session, now=now)
    assert result["flagged"] == 1


@pytest.mark.asyncio
async def test_morning_queue_creates_summary_notification(db_session: AsyncSession):
    company = Company(id=_uuid.uuid4(), name="Co", source="test", pipeline_stage="outreach")
    db_session.add(company)
    await db_session.flush()
    db_session.add(Contact(id=_uuid.uuid4(), company_id=company.id, first_name="A",
                           email="a@x.com", source="test"))
    await db_session.flush()

    result = await generate_morning_call_queue_for_session(db_session)
    assert result["generated"] == 1
    from sqlalchemy import select
    notifs = (await db_session.execute(
        select(CallNotification).where(CallNotification.notification_type == "upcoming_call")
    )).scalars().all()
    assert len(notifs) == 1
