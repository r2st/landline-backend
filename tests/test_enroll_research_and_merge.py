"""F5 — research brief at enrollment + richer merge fields.

* Enrolling a contact dispatches a research brief for its company (idempotent,
  best-effort) so the first AI-composed touch is grounded.
* ``{{industry}}`` / ``{{city}}`` are available in the render context and render
  gracefully (Jinja ``{% if %}``) when absent.
"""

import uuid

import pytest

from app.models.sequence import OutreachSequence
from app.routers.sequences import _enroll_into_sequence
from app.services.email_compose import build_merge_context, render
from app.services.outreach_templates import STEP_1_BODY, STEP_2_BODY


class _Recorder:
    """Stands in for a Celery task — records .delay() args."""

    def __init__(self):
        self.calls = []

    def delay(self, *args):
        self.calls.append(args)


@pytest.mark.asyncio
async def test_enroll_dispatches_research_for_each_company(
    db_session, sample_company, sample_contact, monkeypatch
):
    research = _Recorder()
    send = _Recorder()
    import app.tasks.research as research_mod
    import app.tasks.outreach as outreach_mod
    monkeypatch.setattr(research_mod, "research_company", research)
    monkeypatch.setattr(outreach_mod, "send_sequence_step", send)

    seq = OutreachSequence(id=uuid.uuid4(), name="s", is_active=True, status="active")
    db_session.add(seq)
    await db_session.flush()

    result = await _enroll_into_sequence(db_session, seq, [sample_contact.id])
    assert result.enrolled_count == 1
    # Research was warmed up for the contact's company.
    assert research.calls == [(str(sample_company.id),)]
    # And the first send was still dispatched.
    assert len(send.calls) == 1


@pytest.mark.asyncio
async def test_enroll_dedupes_research_by_company(
    db_session, sample_company, sample_contact, monkeypatch
):
    from app.models.contact import Contact

    research = _Recorder()
    send = _Recorder()
    import app.tasks.research as research_mod
    import app.tasks.outreach as outreach_mod
    monkeypatch.setattr(research_mod, "research_company", research)
    monkeypatch.setattr(outreach_mod, "send_sequence_step", send)

    # Two contacts at the SAME company → research dispatched once.
    c2 = Contact(
        id=uuid.uuid4(), company_id=sample_company.id, first_name="Two",
        email="two@acmestamp.com", source="test",
    )
    db_session.add(c2)
    await db_session.flush()

    seq = OutreachSequence(id=uuid.uuid4(), name="s", is_active=True, status="active")
    db_session.add(seq)
    await db_session.flush()

    await _enroll_into_sequence(db_session, seq, [sample_contact.id, c2.id])
    assert research.calls == [(str(sample_company.id),)]


# ── merge fields ───────────────────────────────────────────────────────────────

def test_build_merge_context_includes_industry_and_city(sample_contact, sample_company):
    ctx = build_merge_context(sample_contact, sample_company)
    assert ctx["industry"] == sample_company.industry_segment
    assert ctx["city"] == sample_company.city


def test_templates_render_city_and_industry_when_present():
    ctx = {
        "first_name": "Sam", "company_name": "Acme", "city": "Cleveland",
        "industry": "Metal Stamping", "demo_link": "http://x",
    }
    step1 = render(STEP_1_BODY, ctx)
    assert "Cleveland" in step1
    step2 = render(STEP_2_BODY, ctx)
    assert "Metal Stamping" in step2


def test_templates_degrade_gracefully_when_absent():
    ctx = {
        "first_name": "Sam", "company_name": "Acme", "city": "",
        "industry": "", "demo_link": "http://x",
    }
    step1 = render(STEP_1_BODY, ctx)
    # No dangling "around ," artifact when city is blank.
    assert "around ," not in step1
    assert "around  " not in step1
    step2 = render(STEP_2_BODY, ctx)
    # Falls back to "the industry" phrasing.
    assert "across the industry" in step2
