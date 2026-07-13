"""Tests for adaptive call scripts (generate the next script after a logged
call outcome, incorporating what happened)."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import CallTask
from app.models.company import Company
from app.models.contact import Contact
from app.services import ai_script_generator as gen


# ---------------------------------------------------------------------------
# Fake generator that records the prompt it was asked to run.
# ---------------------------------------------------------------------------


class _CapturingGen:
    def __init__(self, available=True):
        self.available = available
        self.last_prompt = None
        self._result = {
            "opening_hook": "Hey Pat, left you a message the other day — quick follow-up.",
            "personalization_notes": "Adapted to the voicemail from last attempt.",
            "talking_points": ["scrap ROI"],
            "qualifying_questions": ["what scraps most?"],
            "pain_points": ["tool wear"],
            "objection_handling": [{"objection": "we're fine", "rebuttal": "cheap insurance"}],
            "call_to_action": "free 48-hour pilot?",
            "voicemail": "Hi Pat, {rep_name} at Apprend.",
        }

    async def generate_script_json(self, prompt: str) -> dict:
        self.last_prompt = prompt
        return dict(self._result)


def _install(monkeypatch, fake):
    monkeypatch.setattr(gen, "get_script_generator", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# _outcome_adaptation
# ---------------------------------------------------------------------------


class TestOutcomeAdaptation:
    @pytest.mark.parametrize(
        "outcome,needle",
        [
            ("voicemail", "VOICEMAIL"),
            ("no_answer", "NO ANSWER"),
            ("gatekeeper", "GATEKEEPER"),
            ("connected", "CONNECTED"),
            ("callback_scheduled", "CALLBACK"),
        ],
    )
    def test_known_outcomes(self, outcome, needle):
        assert needle in gen._outcome_adaptation(outcome)

    def test_unknown_outcome_empty(self):
        assert gen._outcome_adaptation("teleported") == ""

    def test_none_empty(self):
        assert gen._outcome_adaptation(None) == ""


# ---------------------------------------------------------------------------
# Adaptive follow-up integration
# ---------------------------------------------------------------------------


async def _company_contact(db):
    c = Company(
        id=uuid.uuid4(), name="Maple CNC", industry_segment="CNC Machining",
        country="US", employee_count=80, source="test",
    )
    db.add(c)
    await db.flush()
    ct = Contact(
        id=uuid.uuid4(), company_id=c.id, first_name="Pat", last_name="Lee",
        title="Owner", email="pat@maplecnc.com", source="test",
    )
    db.add(ct)
    await db.flush()
    return c, ct


@pytest.mark.asyncio
async def test_followup_after_voicemail_adapts_prompt(
    db_session: AsyncSession, monkeypatch
):
    fake = _install(monkeypatch, _CapturingGen(available=True))
    company, contact = await _company_contact(db_session)

    resp = await gen.generate_followup_after_outcome(
        db_session, company=company, contact=contact,
        outcome="voicemail", notes="Left a VM about scrap tracking.",
    )
    assert resp.source == "ai"
    assert resp.call_type == "follow_up"
    # The prompt handed to the model carried the voicemail adaptation + notes.
    assert "VOICEMAIL" in fake.last_prompt
    assert "Left a VM about scrap tracking" in fake.last_prompt
    assert "ADAPT TO THE LAST CALL" in fake.last_prompt


@pytest.mark.asyncio
async def test_followup_after_connected_includes_objections(
    db_session: AsyncSession, monkeypatch
):
    fake = _install(monkeypatch, _CapturingGen(available=True))
    company, contact = await _company_contact(db_session)

    await gen.generate_followup_after_outcome(
        db_session, company=company, contact=contact,
        outcome="connected", notes="Talked for 5 min, liked the demo.",
        objections=["no budget this quarter"],
    )
    assert "CONNECTED" in fake.last_prompt
    assert "no budget this quarter" in fake.last_prompt


@pytest.mark.asyncio
async def test_gatekeeper_uses_initial_call_type(
    db_session: AsyncSession, monkeypatch
):
    fake = _install(monkeypatch, _CapturingGen(available=True))
    company, contact = await _company_contact(db_session)

    resp = await gen.generate_followup_after_outcome(
        db_session, company=company, contact=contact, outcome="gatekeeper",
    )
    # Still need to get past reception → initial-cold-call scaffold, adapted.
    assert resp.call_type == "initial"
    assert "GATEKEEPER" in fake.last_prompt


@pytest.mark.asyncio
async def test_initial_script_has_no_adaptation_block(
    db_session: AsyncSession, monkeypatch
):
    fake = _install(monkeypatch, _CapturingGen(available=True))
    company, contact = await _company_contact(db_session)

    # A normal (non-adaptive) generation must not carry the adaptation section.
    await gen.generate_for_contact(
        db_session, company=company, contact=contact, call_type="initial",
    )
    assert "ADAPT TO THE LAST CALL" not in fake.last_prompt


@pytest.mark.asyncio
async def test_followup_for_task_reads_outcome(
    db_session: AsyncSession, monkeypatch
):
    fake = _install(monkeypatch, _CapturingGen(available=True))
    company, contact = await _company_contact(db_session)

    task = CallTask(
        id=uuid.uuid4(),
        company_id=company.id,
        contact_id=contact.id,
        status="completed",
        priority="hot",
        outcome="callback_scheduled",
        notes="Asked me to call back Thursday.",
        objections_encountered=["send info first"],
    )
    db_session.add(task)
    await db_session.flush()

    resp = await gen.generate_followup_for_task(db_session, task)
    assert resp.call_type == "follow_up"
    assert "CALLBACK" in fake.last_prompt
    assert "send info first" in fake.last_prompt


@pytest.mark.asyncio
async def test_followup_for_task_requires_outcome(
    db_session: AsyncSession, monkeypatch
):
    _install(monkeypatch, _CapturingGen(available=True))
    company, contact = await _company_contact(db_session)
    task = CallTask(
        id=uuid.uuid4(), company_id=company.id, contact_id=contact.id,
        status="pending", priority="warm", outcome=None,
    )
    db_session.add(task)
    await db_session.flush()

    with pytest.raises(ValueError):
        await gen.generate_followup_for_task(db_session, task)
