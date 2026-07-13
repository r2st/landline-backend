"""Tests for autonomous multi-channel orchestration (feature 2.2).

Covers the deterministic decision engine (guardrails + escalation ladder), the
OrchestrationPolicy CRUD router, enrollment mode/decide/log endpoints, and a
bounded autonomous run. No AI key in tests → the orchestrator uses its rule-based
path, which is what these assert.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services.orchestrator import ACTION_SEND, ACTION_STOP, ACTION_WAIT, get_orchestrator


async def _mk_sequence(db) -> OutreachSequence:
    seq = OutreachSequence(id=uuid.uuid4(), name="Test cadence")
    db.add(seq)
    await db.flush()
    return seq


async def _mk_enrollment(db, contact_id, sequence_id, *, mode="manual", status="active") -> OutreachEnrollment:
    enr = OutreachEnrollment(
        id=uuid.uuid4(),
        contact_id=contact_id,
        sequence_id=sequence_id,
        status=status,
        orchestration_mode=mode,
    )
    db.add(enr)
    await db.flush()
    return enr


async def _add_send(db, enrollment_id, *, channel="email", when=None, step=1):
    db.add(OutreachEvent(
        id=uuid.uuid4(),
        enrollment_id=enrollment_id,
        step_number=step,
        event_type="sent",
        occurred_at=when or datetime.now(timezone.utc),
        event_metadata={"channel": channel},
    ))
    await db.flush()


class TestRuleDecisions:
    @pytest.mark.asyncio
    async def test_first_touch_sends_email(self, db_session, sample_contact):
        seq = await _mk_sequence(db_session)
        enr = await _mk_enrollment(db_session, sample_contact.id, seq.id)
        decision = await get_orchestrator().decide(db_session, enr, use_ai=False)
        assert decision.action == ACTION_SEND
        assert decision.channel == "email"

    @pytest.mark.asyncio
    async def test_max_total_touches_stops(self, db_session, sample_contact):
        seq = await _mk_sequence(db_session)
        enr = await _mk_enrollment(db_session, sample_contact.id, seq.id)
        old = datetime.now(timezone.utc) - timedelta(days=10)
        for i in range(12):
            await _add_send(db_session, enr.id, when=old - timedelta(days=i))
        decision = await get_orchestrator().decide(db_session, enr, use_ai=False)
        assert decision.action == ACTION_STOP

    @pytest.mark.asyncio
    async def test_min_spacing_waits(self, db_session, sample_contact):
        seq = await _mk_sequence(db_session)
        enr = await _mk_enrollment(db_session, sample_contact.id, seq.id)
        # A touch one hour ago; default policy wants 24h between touches.
        await _add_send(db_session, enr.id, when=datetime.now(timezone.utc) - timedelta(hours=1))
        decision = await get_orchestrator().decide(db_session, enr, use_ai=False)
        assert decision.action == ACTION_WAIT
        assert decision.wait_hours and decision.wait_hours > 0

    @pytest.mark.asyncio
    async def test_escalates_off_email_when_unopened(self, db_session, sample_company):
        # Contact with a LinkedIn URL so escalation has somewhere to go.
        contact = Contact(
            id=uuid.uuid4(), company_id=sample_company.id, first_name="Lin",
            last_name="Ked", title="Buyer", email="lin@acmestamp.com",
            linkedin_url="https://linkedin.com/in/linked", source="test",
        )
        db_session.add(contact)
        await db_session.flush()
        seq = await _mk_sequence(db_session)
        enr = await _mk_enrollment(db_session, contact.id, seq.id)
        # 3 emails, all long ago (past min spacing), no opens recorded.
        base = datetime.now(timezone.utc) - timedelta(days=5)
        for i in range(3):
            await _add_send(db_session, enr.id, channel="email", when=base + timedelta(days=i))
        decision = await get_orchestrator().decide(db_session, enr, use_ai=False)
        assert decision.action == ACTION_SEND
        assert decision.channel == "linkedin"

    @pytest.mark.asyncio
    async def test_no_usable_channel_flags_human(self, db_session):
        # Company with no phone + contact with no email/phone/linkedin → nothing usable.
        from app.models.company import Company
        company = Company(
            id=uuid.uuid4(), name="No Reach Inc.", country="US", source="test", phone=None,
        )
        db_session.add(company)
        await db_session.flush()
        contact = Contact(
            id=uuid.uuid4(), company_id=company.id, first_name="No",
            last_name="Channel", title="Ghost", source="test",
        )
        db_session.add(contact)
        await db_session.flush()
        seq = await _mk_sequence(db_session)
        enr = await _mk_enrollment(db_session, contact.id, seq.id)
        decision = await get_orchestrator().decide(db_session, enr, use_ai=False)
        assert decision.requires_human is True


class TestOrchestrationRun:
    @pytest.mark.asyncio
    async def test_run_all_autonomous_records_log(self, db_session, sample_contact):
        seq = await _mk_sequence(db_session)
        enr = await _mk_enrollment(db_session, sample_contact.id, seq.id, mode="autonomous")
        results = await get_orchestrator().run_all_autonomous(db_session, limit=10)
        assert len(results) == 1
        refreshed = (
            await db_session.execute(select(OutreachEnrollment).where(OutreachEnrollment.id == enr.id))
        ).scalar_one()
        assert len(refreshed.orchestration_log) == 1
        assert refreshed.orchestration_log[0]["action"] in (ACTION_SEND, ACTION_WAIT, ACTION_STOP)

    @pytest.mark.asyncio
    async def test_manual_enrollments_untouched(self, db_session, sample_contact):
        seq = await _mk_sequence(db_session)
        await _mk_enrollment(db_session, sample_contact.id, seq.id, mode="manual")
        results = await get_orchestrator().run_all_autonomous(db_session, limit=10)
        assert results == []


class TestPolicyRouter:
    @pytest.mark.asyncio
    async def test_policy_crud(self, client):
        resp = await client.post(
            "/api/v1/orchestration/policies",
            json={"name": "Aggressive", "max_daily_touches": 4, "allowed_channels": ["email", "phone"]},
        )
        assert resp.status_code == 201
        pid = resp.json()["id"]
        assert resp.json()["max_daily_touches"] == 4

        resp = await client.get(f"/api/v1/orchestration/policies/{pid}")
        assert resp.status_code == 200

        resp = await client.patch(
            f"/api/v1/orchestration/policies/{pid}", json={"max_total_touches": 20}
        )
        assert resp.status_code == 200
        assert resp.json()["max_total_touches"] == 20

        resp = await client.get("/api/v1/orchestration/policies")
        assert any(p["id"] == pid for p in resp.json())

        resp = await client.delete(f"/api/v1/orchestration/policies/{pid}")
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_policy_rejects_unknown_channel(self, client):
        resp = await client.post(
            "/api/v1/orchestration/policies",
            json={"name": "Bad", "allowed_channels": ["carrier_pigeon"]},
        )
        assert resp.status_code == 422


class TestEnrollmentEndpoints:
    @pytest.mark.asyncio
    async def test_set_mode_and_log(self, client, db_session, sample_contact):
        seq = await _mk_sequence(db_session)
        enr = await _mk_enrollment(db_session, sample_contact.id, seq.id)

        resp = await client.post(
            f"/api/v1/orchestration/enrollments/{enr.id}/mode", json={"mode": "autonomous"}
        )
        assert resp.status_code == 200
        assert resp.json()["orchestration_mode"] == "autonomous"

        resp = await client.post(f"/api/v1/orchestration/enrollments/{enr.id}/decide?use_ai=false")
        assert resp.status_code == 200
        assert resp.json()["decision"]["action"] in (ACTION_SEND, ACTION_WAIT, ACTION_STOP)

        resp = await client.get(f"/api/v1/orchestration/enrollments/{enr.id}/log")
        assert resp.status_code == 200
        assert resp.json()["orchestration_mode"] == "autonomous"

    @pytest.mark.asyncio
    async def test_set_mode_invalid(self, client, db_session, sample_contact):
        seq = await _mk_sequence(db_session)
        enr = await _mk_enrollment(db_session, sample_contact.id, seq.id)
        resp = await client.post(
            f"/api/v1/orchestration/enrollments/{enr.id}/mode", json={"mode": "bogus"}
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_decide_unknown_enrollment_404(self, client):
        resp = await client.post(f"/api/v1/orchestration/enrollments/{uuid.uuid4()}/decide")
        assert resp.status_code == 404
