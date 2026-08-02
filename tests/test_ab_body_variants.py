"""Body A/B tests — the other half of the email.

``generate_body_variants`` had existed in the statistics service since A/B
testing shipped and had no caller anywhere in ``app/``: a step could test what
its subject *said* and never what its body *did*, which is the half that decides
replies.

Body tests are scored on **reply rate** where subject tests are scored on opens.
The subject and the sender name decide whether a message is opened; the body has
almost no say in that and all of the say in whether anyone answers.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.ab_test import AbTest, AbTestVariant
from app.models.sequence import OutreachSequence, SequenceStep
from app.services.ab_test_seed import generate_body_test_for_step


class _FakeAI:
    def __init__(self, variants):
        self._variants = variants
        self.calls = 0
        self.prompts: list[str] = []

    async def _complete_json(self, prompt, max_tokens=None):
        self.calls += 1
        self.prompts.append(prompt)
        return {"variants": self._variants}


async def _step(
    db,
    subject="quick question",
    # Three sentences, so the deterministic fallback has something to cut —
    # otherwise a test meaning to exercise generation silently exercises "there
    # was no distinct variant" instead.
    body=(
        "<p>We watch presses for drift. Most shops find the scrap at inspection. "
        "Worth fifteen minutes?</p>"
    ),
):
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq", industry="cnc_machining")
    db.add(seq)
    await db.flush()
    step = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
        subject_template=subject, body_template=body,
    )
    db.add(step)
    await db.flush()
    return seq, step


@pytest.mark.asyncio
async def test_body_test_is_runnable_and_scored_on_replies(db_session):
    _, step = await _step(db_session)
    ai = _FakeAI([
        "<p>How long does a quote take you today?</p>",
        "<p>Most shops find scrap before the customer does. Sometimes.</p>",
    ])

    result = await generate_body_test_for_step(db_session, step, ai=ai)

    assert ai.calls == 1
    assert len(result["challengers"]) == 2
    assert result["test"].metric == "replied"
    # Reply rates run a few percent, so a body test needs materially more volume
    # than a subject test before it may call a winner.
    assert result["test"].min_sample >= 200


@pytest.mark.asyncio
async def test_control_inherits_the_step_body_and_subject_is_held_constant(db_session):
    _, step = await _step(db_session)
    ai = _FakeAI(["<p>A</p>", "<p>B</p>"])
    await generate_body_test_for_step(db_session, step, ai=ai)

    variants = (
        await db_session.execute(
            select(AbTestVariant).where(AbTestVariant.sequence_step_id == step.id)
        )
    ).scalars().all()
    assert len(variants) == 3

    control = next(v for v in variants if v.is_control)
    assert control.label == "A"
    # Inherits rather than copies, so editing the step keeps the control in sync.
    assert control.body_template is None
    # Body-only: every arm sends the same subject, so what the test measures is
    # the body.
    assert all(v.subject_template is None for v in variants)
    assert all(v.body_template for v in variants if not v.is_control)


@pytest.mark.asyncio
async def test_a_variant_identical_to_the_step_is_not_offered_against_itself(db_session):
    body = "<p>We do process monitoring.</p>"
    _, step = await _step(db_session, body=body)
    ai = _FakeAI([body, "<p>Genuinely different angle.</p>"])

    result = await generate_body_test_for_step(db_session, step, ai=ai)
    assert body not in result["challengers"]
    assert len(result["challengers"]) == 1


@pytest.mark.asyncio
async def test_falls_back_when_no_llm_is_reachable(db_session):
    """No key, no network — still produces a runnable test rather than an error
    the operator has to interpret.

    This is the common case, not the edge one: a deployment whose provider chain
    is exhausted has already degraded every other AI feature to static copy, and
    that is exactly when someone wants to test copy by hand.
    """

    class _DeadAI:
        async def _complete_json(self, prompt, max_tokens=None):
            raise RuntimeError("no provider available")

    _, step = await _step(
        db_session,
        body="<p>We watch presses for drift. Most shops find scrap at inspection. "
             "Worth fifteen minutes?</p>",
    )
    result = await generate_body_test_for_step(db_session, step, ai=_DeadAI())

    assert result["challengers"], "a fallback with no distinct arm is not a test"
    assert result["test"].status == "running"


def test_the_short_fallback_only_removes_text():
    """Length is the one body hypothesis testable without writing copy. Anything
    that generated new sentences here would be inventing claims about someone's
    business with nothing checking them."""
    from app.services.ab_testing_service import _fallback_body_variants

    base = "<p>We watch presses for drift. Most shops find scrap at inspection. Worth a call?</p>"
    variants = _fallback_body_variants(base, 3)

    assert variants[0] == base
    assert len(variants) == 2
    short = variants[1]
    assert "Worth a call?" not in short
    assert "We watch presses for drift." in short
    assert short.startswith("<p>") and short.endswith("</p>")


def test_the_short_fallback_declines_when_there_is_nothing_to_cut():
    """A one-sentence body has no shorter version, and returning it twice would
    be a test whose arms are identical."""
    from app.services.ab_testing_service import _fallback_body_variants

    assert _fallback_body_variants("<p>Worth a call?</p>", 3) == ["<p>Worth a call?</p>"]


@pytest.mark.asyncio
async def test_refuses_a_second_test_on_the_same_step(db_session):
    """One test per step: subject and body are tested in sequence, not at once —
    two variables moving together on cold volume this thin leaves neither
    attributable."""
    _, step = await _step(db_session)
    ai = _FakeAI(["<p>A</p>", "<p>B</p>"])
    await generate_body_test_for_step(db_session, step, ai=ai)

    with pytest.raises(ValueError, match="already exists"):
        await generate_body_test_for_step(db_session, step, ai=ai)


@pytest.mark.asyncio
async def test_rejects_an_unknown_metric_or_strategy(db_session):
    _, step = await _step(db_session)
    ai = _FakeAI(["<p>A</p>", "<p>B</p>"])

    with pytest.raises(ValueError, match="metric"):
        await generate_body_test_for_step(db_session, step, metric="vibes", ai=ai)
    with pytest.raises(ValueError, match="strategy"):
        await generate_body_test_for_step(db_session, step, strategy="coinflip", ai=ai)


@pytest.mark.asyncio
async def test_generate_bodies_endpoint(client, db_session):
    _, step = await _step(db_session)
    await db_session.commit()

    resp = await client.post(f"/api/v1/dialtone/ab-tests/generate-bodies/{step.id}")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["metric"] == "replied"
    assert len(body["variants"]) >= 2

    # One test per step.
    again = await client.post(f"/api/v1/dialtone/ab-tests/generate-bodies/{step.id}")
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_generate_bodies_endpoint_404s_on_an_unknown_step(client):
    resp = await client.post(f"/api/v1/dialtone/ab-tests/generate-bodies/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_generate_bodies_endpoint_validates_the_metric(client, db_session):
    _, step = await _step(db_session)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/dialtone/ab-tests/generate-bodies/{step.id}", params={"metric": "vibes"}
    )
    assert resp.status_code == 400
    # Nothing was created by the rejected request.
    assert (
        await db_session.execute(
            select(AbTest).where(AbTest.sequence_step_id == step.id)
        )
    ).scalars().first() is None
