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


async def _setup(db, company, contact, step_number=1, tone=None):
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq", **({"tone": tone} if tone else {}))
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
    """Stub the copywriter and record what it was called with.

    The signature must track ``generate_email``'s for real: compose_step wraps the
    call in ``except Exception -> static``, so a stub that rejects a newly-added
    kwarg doesn't fail loudly — it silently turns every AI assertion in this file
    into a static fallback that still "passes" the fail-safe tests. Returns the
    call-capture dict so tests can assert on what was passed.
    """
    calls: dict = {}

    async def fake_generate(lead_data, template_type="first_touch", custom_instructions=None,
                            research_context="", tone=None):
        calls.update(
            lead_data=lead_data, template_type=template_type,
            custom_instructions=custom_instructions, research_context=research_context,
            tone=tone,
        )
        return response

    import app.services.ai_email_generator as gen
    monkeypatch.setattr(gen, "generate_email", fake_generate)
    return calls


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
async def test_later_steps_are_left_to_the_optimizer_when_all_steps_is_off(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    """``ai_compose_all_steps`` off still means step 1 only.

    The flag ships on, but it is the off-switch for the whole feature's blast
    radius, so the path it selects has to keep working.
    """
    monkeypatch.setattr(ai_on, "ai_compose_all_steps", False)
    _patch_generator(monkeypatch, AI_REPLY)
    _, step, enr = await _setup(db_session, sample_company, sample_contact, step_number=3)

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "send"
    assert result.subject == STATIC_SUBJECT
    assert "first touch" in result.reason


# ---------------------------------------------------------------------------
# Every step writes as itself
# ---------------------------------------------------------------------------

def test_all_steps_composition_is_on_by_default():
    """The whole sequence speaks in the sequence's voice, not just step 1.

    Pinned as a default rather than left to the env: with this off, ``tone`` is
    a column that shapes one email in four and silently does nothing to the
    other three.
    """
    from app.config import Settings

    assert Settings.model_fields["ai_compose_all_steps"].default is True


def test_every_step_template_type_is_a_real_prompt_hint():
    """The map and the copywriter's hints cannot drift apart.

    A key ``TEMPLATE_HINTS`` lacks doesn't raise — ``generate_email`` silently
    falls back to the first_touch hint — so a typo here would quietly reintroduce
    the exact bug this map exists to fix, with nothing in the logs to say so.
    """
    from app.services.ai_compose import LATER_STEP_TEMPLATE_TYPE, STEP_TEMPLATE_TYPES
    from app.services.ai_email_generator import TEMPLATE_HINTS

    for step_number, template_type in STEP_TEMPLATE_TYPES.items():
        assert template_type in TEMPLATE_HINTS, f"step {step_number} -> unknown hint {template_type!r}"
    assert LATER_STEP_TEMPLATE_TYPE in TEMPLATE_HINTS


@pytest.mark.parametrize(
    "step_number, expected",
    [(1, "first_touch"), (2, "scrap_math"), (3, "follow_up_2"), (4, "free_pilot")],
)
@pytest.mark.asyncio
async def test_each_step_is_composed_under_its_own_hint(
    db_session, sample_company, sample_contact, ai_on, monkeypatch, step_number, expected
):
    """The sequence arc survives composition: stakes -> cost -> pattern -> pilot."""
    monkeypatch.setattr(ai_on, "ai_compose_all_steps", True)
    calls = _patch_generator(monkeypatch, AI_REPLY)
    _, step, enr = await _setup(
        db_session, sample_company, sample_contact, step_number=step_number
    )

    await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert calls["template_type"] == expected


@pytest.mark.parametrize("step_number", [2, 3, 4])
@pytest.mark.asyncio
async def test_a_follow_up_is_never_composed_as_a_first_touch(
    db_session, sample_company, sample_contact, ai_on, monkeypatch, step_number
):
    """The regression that turning the flag on would otherwise have shipped.

    ``compose_step`` passed a hardcoded "first_touch" because it only ever ran on
    step 1. Enabling every step without this map means each follow-up opens by
    introducing a stranger the recipient has already heard from — and step 4
    drops the pilot offer it exists to make.
    """
    monkeypatch.setattr(ai_on, "ai_compose_all_steps", True)
    calls = _patch_generator(monkeypatch, AI_REPLY)
    _, step, enr = await _setup(
        db_session, sample_company, sample_contact, step_number=step_number
    )

    await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert calls["template_type"] != "first_touch"


def test_a_step_past_the_arc_degrades_to_a_generic_follow_up():
    """A longer sequence than the seeded four must not break, or say goodbye early."""
    from app.services.ai_compose import template_type_for

    assert template_type_for(7) == "follow_up_2"
    assert template_type_for(0) == "follow_up_2"
    # Never the breakup: a step with emails after it cannot un-say goodbye.
    assert template_type_for(9) != "follow_up_3"


@pytest.mark.parametrize("step_number", [1, 2, 3, 4])
@pytest.mark.asyncio
async def test_tone_reaches_every_step_not_just_the_first(
    db_session, sample_company, sample_contact, ai_on, monkeypatch, step_number
):
    """The point of the change: one voice across the whole sequence."""
    monkeypatch.setattr(ai_on, "ai_compose_all_steps", True)
    calls = _patch_generator(monkeypatch, AI_REPLY)
    _, step, enr = await _setup(
        db_session, sample_company, sample_contact, step_number=step_number, tone="empathetic"
    )

    await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert calls["tone"] == "empathetic"


# ---------------------------------------------------------------------------
# Tone reaches the copywriter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sequence_tone_is_passed_to_the_generator(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    """The whole point of the column: the sequence's voice reaches the prompt."""
    calls = _patch_generator(monkeypatch, AI_REPLY)
    _, step, enr = await _setup(db_session, sample_company, sample_contact, tone="empathetic")

    await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert calls["tone"] == "empathetic"


@pytest.mark.asyncio
async def test_default_sequence_tone_is_conversational(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    """A sequence created without an opinion gets the house voice, not None."""
    calls = _patch_generator(monkeypatch, AI_REPLY)
    _, step, enr = await _setup(db_session, sample_company, sample_contact)

    await compose_step(
        db_session, enr, sample_contact, sample_company, step, STATIC_SUBJECT, STATIC_BODY
    )
    assert calls["tone"] == "conversational"


@pytest.mark.asyncio
async def test_tone_of_a_missing_sequence_is_none(db_session):
    """No row, no opinion — and no exception."""
    from app.services.ai_compose import _sequence_tone

    assert await _sequence_tone(db_session, uuid.uuid4()) is None


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
async def test_quota_is_counted_per_step_not_per_sequence(
    db_session, sample_company, sample_contact, ai_on, monkeypatch
):
    """A step-1 quota must not buy autonomy for step 2.

    Step 1 runs on every enrollment and it runs first, so a per-sequence quota is
    one the first touch always exhausts alone. Composition now covers every step,
    and each writes a different email under a different hint — so without this,
    the model's follow-ups would send from the very first one with no human
    having ever read one.
    """
    monkeypatch.setattr(ai_on, "ai_compose_all_steps", True)
    _patch_generator(monkeypatch, AI_REPLY)
    seq, _, enr = await _setup(db_session, sample_company, sample_contact)

    # Quota (2 here) fully consumed by step 1.
    for _ in range(2):
        db_session.add(
            AiEmailDraft(
                id=uuid.uuid4(), enrollment_id=enr.id, sequence_id=seq.id,
                step_number=1, subject="s", body="b", status="approved",
                ai_generated=True,
            )
        )
    step2 = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=2, delay_days=3,
        subject_template=STATIC_SUBJECT, body_template=STATIC_BODY,
    )
    enr2 = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=2, status="active",
    )
    db_session.add_all([step2, enr2])
    await db_session.flush()

    result = await compose_step(
        db_session, enr2, sample_contact, sample_company, step2, STATIC_SUBJECT, STATIC_BODY
    )
    assert result.action == "review", "step 2's first AI copy sent without a human reading it"
    assert result.draft_id is not None


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


# ---------------------------------------------------------------------------
# Non-DB async tests LAST — this suite shares a session-scoped event_loop and
# an async test that never touches the DB, defined before one that does, has
# historically dragged the whole file down with it. See
# tests/README or the ordering note in test_frontend_contract.py.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tone_lookup_fails_soft_to_the_house_voice():
    """Tone shapes copy; it does not gate a send. A lookup that blows up must
    return "no opinion" (-> house voice), never propagate and drop the touch."""
    from app.services.ai_compose import _sequence_tone

    class BrokenDB:
        async def execute(self, *a, **k):
            raise RuntimeError("db went away")

    assert await _sequence_tone(BrokenDB(), uuid.uuid4()) is None
