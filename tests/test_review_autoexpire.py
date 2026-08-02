"""F1 — auto-expire stale review drafts + un-park frozen enrollments.

A ``pending_review`` draft parks its enrollment (the outreach queue only
dispatches ``active``). If no human clears it the prospect is frozen forever.
``expire_stale_review_drafts`` is the backstop: after the expiry window it drops
the draft to the static template (a *rejection*, never an unreviewed send) and
un-parks the enrollment. Also covers the config defaults and the stats endpoint.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.models.ai_email_draft import AiEmailDraft
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence, SequenceStep
from app.tasks.outreach import expire_stale_review_drafts


async def _parked(db, contact, company, *, age_hours, step_number=1):
    """A pending draft aged ``age_hours`` old with its enrollment parked."""
    seq = OutreachSequence(id=uuid.uuid4(), name=f"seq-{uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    step = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=step_number, delay_days=0,
        subject_template="S", body_template="B",
    )
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=step_number, status="pending_review",
    )
    db.add_all([step, enr])
    await db.flush()
    draft = AiEmailDraft(
        id=uuid.uuid4(), enrollment_id=enr.id, sequence_id=seq.id,
        contact_id=contact.id, company_id=company.id, step_number=step_number,
        subject="AI subject", body="AI body", static_subject="S", static_body="B",
        ai_generated=True, research_used=False, status="pending",
        created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
    )
    db.add(draft)
    await db.flush()
    return enr, draft


# ── Config defaults (F1 behaviour is pinned as defaults, not left to env) ──────

def test_config_defaults_for_review_gate():
    # AI composition covers every step, not just the first touch. This assertion
    # said ``is False`` and had been wrong since the default flipped: the review
    # gate below is what makes composing all steps safe, so the two ship
    # together. ``test_ai_compose.py::test_config_defaults`` pins the same value.
    assert Settings.model_fields["ai_compose_all_steps"].default is True
    assert Settings.model_fields["ai_compose_review_batch_size"].default == 3
    assert Settings.model_fields["ai_compose_review_expiry_hours"].default == 24


# ── expire_stale_review_drafts ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stale_draft_is_released_to_static_and_enrollment_unparked(
    db_session, sample_company, sample_contact
):
    enr, draft = await _parked(db_session, sample_contact, sample_company, age_hours=48)

    result = await expire_stale_review_drafts(
        db_session, older_than_hours=24, dispatch=False
    )

    assert result == {"expired": 1, "resumed": 1}
    await db_session.refresh(draft)
    await db_session.refresh(enr)
    # Rejected — NOT approved/sent: the unreviewed AI copy must never ship.
    assert draft.status == "rejected"
    assert "auto-expired" in (draft.review_notes or "")
    # Un-parked so the outreach queue picks it up (on the static template).
    assert enr.status == "active"
    assert enr.next_send_at is not None


@pytest.mark.asyncio
async def test_fresh_draft_is_left_alone(db_session, sample_company, sample_contact):
    enr, draft = await _parked(db_session, sample_contact, sample_company, age_hours=2)

    result = await expire_stale_review_drafts(
        db_session, older_than_hours=24, dispatch=False
    )

    assert result == {"expired": 0, "resumed": 0}
    await db_session.refresh(draft)
    await db_session.refresh(enr)
    assert draft.status == "pending"
    assert enr.status == "pending_review"


@pytest.mark.asyncio
async def test_expiry_disabled_when_hours_is_zero(db_session, sample_company, sample_contact):
    await _parked(db_session, sample_contact, sample_company, age_hours=999)
    result = await expire_stale_review_drafts(db_session, older_than_hours=0, dispatch=False)
    assert result == {"expired": 0, "resumed": 0}


@pytest.mark.asyncio
async def test_already_decided_draft_is_not_touched(
    db_session, sample_company, sample_contact
):
    """A draft a human already approved/rejected is not re-expired."""
    enr, draft = await _parked(db_session, sample_contact, sample_company, age_hours=48)
    draft.status = "approved"
    enr.status = "active"
    await db_session.flush()

    result = await expire_stale_review_drafts(
        db_session, older_than_hours=24, dispatch=False
    )
    assert result == {"expired": 0, "resumed": 0}


# ── /ai-drafts/stats endpoint (pending_review backlog surfaced) ────────────────

@pytest.mark.asyncio
async def test_draft_stats_endpoint_surfaces_pending_count(
    client, db_session, sample_company, sample_contact
):
    await _parked(db_session, sample_contact, sample_company, age_hours=30)
    await db_session.commit()

    resp = await client.get("/api/v1/ai-drafts/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pending"] == 1
    assert data["total"] >= 1
    # oldest_pending_age_hours reflects the ~30h-old draft.
    assert data["oldest_pending_age_hours"] is not None
    assert data["oldest_pending_age_hours"] >= 24
