"""The API can build the cadence the send path already knows how to run.

``SequenceStep`` has carried ``channel``, ``send_condition`` and
``task_timeout_hours`` since the multi-channel and conditional-branching work
landed, and ``app.tasks.outreach`` honours all three on every dispatch. No
request schema accepted them, so the only cadence reachable through the API was
unconditional single-channel email — the capability existed and could not be
used, which from outside is the same as not having it.

The other half of this is what replaced ``cadence_pattern``: a sequence now
reports the channels it actually touches, read off its steps, so the answer
cannot drift from the cadence the way a stored label did.
"""

import uuid as _uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sequence import OutreachSequence, SequenceStep


def _step(n: int, **over) -> dict:
    body = {
        "step_number": n,
        "delay_days": n - 1,
        "subject_template": f"Subject {n}",
        "body_template": f"Body {n}",
    }
    body.update(over)
    return body


@pytest.mark.asyncio
async def test_create_sequence_persists_channel_and_condition(
    client: AsyncClient, db_session: AsyncSession
):
    resp = await client.post(
        "/api/v1/outreach/sequences",
        json={
            "name": "Call then email",
            "steps": [
                _step(1, channel="call", task_timeout_hours=48),
                _step(2, channel="email", send_condition="if_not_opened"),
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    seq_id = resp.json()["id"]

    rows = (
        await db_session.execute(
            select(SequenceStep)
            .where(SequenceStep.sequence_id == _uuid.UUID(seq_id))
            .order_by(SequenceStep.step_number)
        )
    ).scalars().all()
    assert [s.channel for s in rows] == ["call", "email"]
    assert rows[0].task_timeout_hours == 48
    assert rows[1].send_condition == "if_not_opened"


@pytest.mark.asyncio
async def test_created_sequence_reports_its_real_channel_mix(client: AsyncClient):
    resp = await client.post(
        "/api/v1/outreach/sequences",
        json={
            "name": "Combo",
            "steps": [
                _step(1, channel="call"),
                _step(2, channel="email"),
                _step(3, channel="linkedin"),
                _step(4, channel="email"),
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    # Ordered by the cadence, deduplicated: the first touch is a call.
    assert resp.json()["channels"] == ["call", "email", "linkedin"]


@pytest.mark.asyncio
async def test_defaults_are_unchanged_for_a_plain_email_sequence(client: AsyncClient):
    """Omitting the new fields must produce exactly what it produced before."""
    resp = await client.post(
        "/api/v1/outreach/sequences",
        json={"name": "Plain", "steps": [_step(1), _step(2)]},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["channels"] == ["email"]
    for step in body["steps"]:
        assert step["channel"] == "email"
        assert step["send_condition"] == "always"
        assert step["task_timeout_hours"] is None


@pytest.mark.asyncio
async def test_add_step_accepts_a_manual_channel(
    client: AsyncClient, db_session: AsyncSession
):
    seq = OutreachSequence(id=_uuid.uuid4(), name="Grow me")
    db_session.add(seq)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/outreach/sequences/{seq.id}/steps",
        json=_step(1, channel="direct_mail", task_timeout_hours=72),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["channel"] == "direct_mail"

    fetched = await client.get(f"/api/v1/outreach/sequences/{seq.id}")
    assert fetched.json()["channels"] == ["direct_mail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("channel", "carrier_pigeon"),
        ("send_condition", "if_they_feel_like_it"),
        ("task_timeout_hours", 0),
    ],
)
async def test_unknown_vocabulary_is_rejected(client: AsyncClient, field, value):
    """A bad channel must 422, not persist and quietly never dispatch.

    ``channel`` routes the step to a dispatcher; an unrecognised value would
    take the non-email branch and file a human task forever.
    """
    resp = await client.post(
        "/api/v1/outreach/sequences",
        json={"name": "Bad", "steps": [_step(1, **{field: value})]},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_public_api_reports_channels(db_session: AsyncSession):
    """The public payload lost ``cadence_pattern`` and gained the true answer."""
    seq = OutreachSequence(id=_uuid.uuid4(), name="Public")
    db_session.add(seq)
    await db_session.flush()
    db_session.add_all([
        SequenceStep(
            sequence_id=seq.id, step_number=1, channel="email",
            subject_template="s", body_template="b",
        ),
        SequenceStep(
            sequence_id=seq.id, step_number=2, channel="call",
            subject_template="s", body_template="b",
        ),
    ])
    await db_session.commit()
    await db_session.refresh(seq)

    assert seq.channels == ["email", "call"]
    # The dropped column is gone from the model entirely — a reader of the
    # public schema can no longer be told a call cadence is "single_channel".
    assert not hasattr(seq, "cadence_pattern")
