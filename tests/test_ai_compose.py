"""AI composition of sequence first touches + the human review gate.

The important properties, in order:
  * composition NEVER blocks a send — every failure path falls back to static;
  * a reviewer's decision is terminal for the step (no re-composing, no loops);
  * only the first N per sequence are queued for review.
"""

import uuid

import pytest

from app.config import get_settings
from app.models.ai_email_draft import AiEmailDraft
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence, SequenceStep
from app.services.ai_compose import compose_step

STATIC_SUBJECT = "Quick question about {{company}}"
STATIC_BODY = "<p>Hi there, we do manufacturing software.</p>"

AI_REPLY = {
    "subject": "Your 3-week quote turnaround",
    "body": "<p>Noticed you quote by hand — that is usually where the weeks go.</p>",
    "demo_link": "https://example.com/demo",
    "ai_generated": True,
}


async def _setup(db, company, contact, step_number=1):
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db.add(seq)
    await db.flush()
    step = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=step_number, delay_days=0,
        subject_template=STATIC_SUBJECT, body_template=STATIC_BODY,
    )
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=step_number, status="active",
    )
    db.add_all([step, enr])
    await db.flush()
    return seq, step, enr


@pytest.fixture
def ai_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_compose_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_compose_review_batch_size", 2)
    monkeypatch.setattr(settings, "ai_compose_all_steps", False)
    return settings


def _patch_generator(monkeypatch, response):
    async def fake_generate(lead_data, template_type="first_touch", custom_instructions=None,
                           research_context=""):
        return response

    import app.services.ai_email_generator as gen
    monkeypatch.setattr(gen, "generate_email", fake_generate)


# ---------------------------------------------------------------------------
# Fail-safe: never block a send
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_returns_static(db_session, sample_company, sample_contact, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_compose_enabled", False)
    _, step, enr = await _setup(db_session, sample_company, sample_contact)

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "send"
    assert result.subject == STATIC_SUBJECT
    assert result.ai_generated is False


@pytest.mark.asyncio
async def test_missing_api_key_returns_static(db_session, sample_company, sample_contact, ai_on, monkeypatch):
    monkeypatch.setattr(ai_on, "anthropic_api_key", "")
    _, step, enr = await _setup(db_session, sample_company, sample_contact)

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "send"
    assert result.subject == STATIC_SUBJECT


@pytest.mark.asyncio
async def test_generator_error_falls_back_to_static(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    async def boom(*a, **k):
        raise RuntimeError("anthropic exploded")

    import app.services.ai_email_generator as gen
    monkeypatch.setattr(gen, "generate_email", boom)
    _, step, enr = await _setup(db_session, sample_company, sample_contact)

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "send"
    assert result.subject == STATIC_SUBJECT
    assert "AI error" in result.reason


@pytest.mark.asyncio
async def test_generator_template_fallback_is_not_used(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    """ai_generated=False means the generator used its own template — no better
    than the static step, and the static step is the one written for this sequence."""
    _patch_generator(monkeypatch, {**AI_REPLY, "ai_generated": False})
    _, step, enr = await _setup(db_session, sample_company, sample_contact)

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "send"
    assert result.subject == STATIC_SUBJECT


@pytest.mark.asyncio
async def test_empty_copy_falls_back_to_static(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    _patch_generator(monkeypatch, {"subject": "  ", "body": "", "ai_generated": True})
    _, step, enr = await _setup(db_session, sample_company, sample_contact)

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "send"
    assert result.subject == STATIC_SUBJECT


@pytest.mark.asyncio
async def test_spammy_copy_is_rejected_for_static(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    """A model writing spam-trigger copy must not burn the sending domain."""
    _patch_generator(monkeypatch, {
        "subject": "FREE!!! ACT NOW!!! LIMITED TIME OFFER!!!",
        "body": "<p>CLICK HERE NOW! 100% FREE! RISK FREE! MAKE MONEY FAST! "
                "GUARANTEED! NO CREDIT CHECK! CASH BONUS! WINNER!</p>",
        "ai_generated": True,
    })
    _, step, enr = await _setup(db_session, sample_company, sample_contact)

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "send"
    assert result.subject == STATIC_SUBJECT
    assert "spam scan" in result.reason


@pytest.mark.asyncio
async def test_later_steps_are_left_to_the_optimizer(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    _patch_generator(monkeypatch, AI_REPLY)
    _, step, enr = await _setup(db_session, sample_company, sample_contact, step_number=3)

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "send"
    assert result.subject == STATIC_SUBJECT
    assert "first touch" in result.reason


# ---------------------------------------------------------------------------
# Review gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_batch_is_queued_for_review(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    from sqlalchemy import select

    _patch_generator(monkeypatch, AI_REPLY)
    _, step, enr = await _setup(db_session, sample_company, sample_contact)

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "review"
    assert result.draft_id is not None
    assert result.ai_generated is True

    draft = (await db_session.execute(
        select(AiEmailDraft).where(AiEmailDraft.id == result.draft_id)
    )).scalar_one()
    assert draft.status == "pending"
    assert draft.subject == AI_REPLY["subject"]
    # The reviewer needs the comparison to judge it.
    assert draft.static_subject == STATIC_SUBJECT


@pytest.mark.asyncio
async def test_sends_unattended_once_quota_is_cleared(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    """Batch size is 2 here; the third composition should send without review."""
    from sqlalchemy import select

    _patch_generator(monkeypatch, AI_REPLY)
    seq, step, enr = await _setup(db_session, sample_company, sample_contact)

    # Two drafts already raised for this sequence = quota met.
    for _ in range(2):
        db_session.add(
            AiEmailDraft(
                id=uuid.uuid4(), enrollment_id=enr.id, sequence_id=seq.id,
                step_number=1, subject="s", body="b", status="approved",
                ai_generated=True,
            )
        )
    await db_session.flush()

    # A different enrollment on the same sequence, so no decided draft applies.
    enr2 = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db_session.add(enr2)
    await db_session.flush()

    result = await compose_step(
        db_session, enr2, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "send"
    assert result.subject == AI_REPLY["subject"]
    assert result.ai_generated is True

    # And no new draft was queued.
    drafts = (await db_session.execute(
        select(AiEmailDraft).where(AiEmailDraft.enrollment_id == enr2.id)
    )).scalars().all()
    assert drafts == []


@pytest.mark.asyncio
async def test_quota_counts_rejected_drafts_too(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    """Rejecting AI copy must still consume review quota, or a sequence whose
    copy is always rejected would queue forever."""
    _patch_generator(monkeypatch, AI_REPLY)
    seq, step, enr = await _setup(db_session, sample_company, sample_contact)

    for _ in range(2):
        db_session.add(
            AiEmailDraft(
                id=uuid.uuid4(), enrollment_id=uuid.uuid4(), sequence_id=seq.id,
                step_number=1, subject="s", body="b", status="rejected", ai_generated=True,
            )
        )
    await db_session.flush()

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "send"


# ---------------------------------------------------------------------------
# A reviewer's decision is terminal — the loop bug
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pending_draft_is_not_duplicated(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    """The queue re-dispatches enrollments; a parked step must not mint drafts."""
    from sqlalchemy import select

    _patch_generator(monkeypatch, AI_REPLY)
    _, step, enr = await _setup(db_session, sample_company, sample_contact)

    first = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    second = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert first.action == second.action == "review"
    assert first.draft_id == second.draft_id

    drafts = (await db_session.execute(
        select(AiEmailDraft).where(AiEmailDraft.enrollment_id == enr.id)
    )).scalars().all()
    assert len(drafts) == 1, "re-composing a parked step must not create a second draft"


@pytest.mark.asyncio
async def test_rejected_draft_sends_static_and_does_not_reloop(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    """The loop bug: reject → enrollment reactivates → compose runs again.

    If composition only recognised *pending* drafts it would generate a fresh one
    and park the step again, forever. A rejection is terminal: send the static.
    """
    from sqlalchemy import select

    _patch_generator(monkeypatch, AI_REPLY)
    _, step, enr = await _setup(db_session, sample_company, sample_contact)

    queued = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    draft = (await db_session.execute(
        select(AiEmailDraft).where(AiEmailDraft.id == queued.draft_id)
    )).scalar_one()
    draft.status = "rejected"
    await db_session.flush()

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "send"
    assert result.subject == STATIC_SUBJECT
    assert "rejected by reviewer" in result.reason

    drafts = (await db_session.execute(
        select(AiEmailDraft).where(AiEmailDraft.enrollment_id == enr.id)
    )).scalars().all()
    assert len(drafts) == 1, "a rejected step must not be re-queued for review"


@pytest.mark.asyncio
async def test_approved_draft_sends_the_approved_copy(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    """Approval must send exactly what the human read — not a regeneration."""
    from sqlalchemy import select

    _patch_generator(monkeypatch, AI_REPLY)
    _, step, enr = await _setup(db_session, sample_company, sample_contact)

    queued = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    draft = (await db_session.execute(
        select(AiEmailDraft).where(AiEmailDraft.id == queued.draft_id)
    )).scalar_one()
    draft.status = "approved"
    await db_session.flush()

    # Generator now returns something different — it must not be consulted.
    _patch_generator(monkeypatch, {**AI_REPLY, "subject": "REGENERATED — must not ship"})

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "send"
    assert result.subject == AI_REPLY["subject"]
    assert result.ai_generated is True
