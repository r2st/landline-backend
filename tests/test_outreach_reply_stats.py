"""``/outreach/reply-stats`` reads engagement from timestamps, not from status.

Two defects, both in the direction that makes a healthy campaign look sick:

* ``status`` is a lifecycle column carrying the *furthest* state a send reached.
  A message that was opened and then replied to is ``replied`` and appears under
  no other key, so ``status == 'opened'`` counted only the opens that went
  nowhere — a measure of disengagement flying the name of a measure of
  engagement.
* The denominator was ``sum`` of every status count, so ``pending``, ``skipped``,
  ``error`` and ``scheduled`` all counted as sends. The reply rate therefore
  collapsed fastest exactly when sending was being *prevented* — a suppressed
  recipient, a spam-gate block or a spent daily cap each made the copy look
  worse.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_template import EmailSend

NOW = datetime.now(timezone.utc)


def _send(status: str, **kw) -> EmailSend:
    kw.setdefault("to_email", f"{_uuid.uuid4().hex[:8]}@example.test")
    return EmailSend(
        id=_uuid.uuid4(),
        subject="Hello",
        status=status,
        sent_at=NOW - timedelta(hours=1),
        **kw,
    )


@pytest.mark.asyncio
async def test_opens_count_sends_that_went_further(
    client: AsyncClient, db_session: AsyncSession
):
    """An open followed by a reply is still an open."""
    db_session.add_all(
        [
            _send("opened", opened_at=NOW),
            _send("clicked", opened_at=NOW, clicked_at=NOW),
            _send("replied", opened_at=NOW, clicked_at=NOW, replied_at=NOW),
        ]
    )
    await db_session.commit()

    data = (await client.get("/api/v1/outreach/reply-stats")).json()
    assert data["opened"] == 3
    assert data["clicked"] == 2
    assert data["replied"] == 1


@pytest.mark.asyncio
async def test_unsent_rows_are_not_in_the_denominator(
    client: AsyncClient, db_session: AsyncSession
):
    """Blocked, queued and failed rows never reached the wire."""
    db_session.add_all(
        [
            _send("sent"),
            _send("replied", opened_at=NOW, replied_at=NOW),
            _send("skipped"),
            _send("suppressed"),
            _send("pending"),
            _send("scheduled", scheduled_at=NOW + timedelta(days=3)),
            _send("error", error="smtp refused"),
            _send("failed", error="boom"),
            _send("halted"),
        ]
    )
    await db_session.commit()

    data = (await client.get("/api/v1/outreach/reply-stats")).json()
    assert data["total_sent"] == 2
    # 1 reply out of 2 delivered — not 1 out of 9.
    assert data["reply_rate"] == pytest.approx(50.0, abs=0.1)


@pytest.mark.asyncio
async def test_suppressing_more_recipients_does_not_lower_the_reply_rate(
    client: AsyncClient, db_session: AsyncSession
):
    db_session.add_all(
        [_send("sent"), _send("replied", replied_at=NOW)]
    )
    await db_session.commit()
    before = (await client.get("/api/v1/outreach/reply-stats")).json()["reply_rate"]

    db_session.add_all([_send("skipped") for _ in range(20)])
    await db_session.commit()
    after = (await client.get("/api/v1/outreach/reply-stats")).json()["reply_rate"]

    assert before == after == pytest.approx(50.0, abs=0.1)


@pytest.mark.asyncio
async def test_bounces_are_volume_but_not_an_engagement_denominator(
    client: AsyncClient, db_session: AsyncSession
):
    """A bounce is mail that went out (volume) and was never delivered (rate)."""
    db_session.add_all(
        [
            _send("replied", replied_at=NOW),
            _send("bounced"),
            _send("bounced"),
        ]
    )
    await db_session.commit()

    data = (await client.get("/api/v1/outreach/reply-stats")).json()
    assert data["total_sent"] == 3
    assert data["bounced"] == 2
    # 1 reply / 1 delivered.
    assert data["reply_rate"] == pytest.approx(100.0, abs=0.1)


@pytest.mark.asyncio
async def test_recent_replies_keyed_on_the_reply_timestamp(
    client: AsyncClient, db_session: AsyncSession
):
    """A thread that later bounced still replied."""
    db_session.add(
        _send("bounced", to_email="late@example.test", replied_at=NOW - timedelta(minutes=5))
    )
    db_session.add(
        _send("replied", to_email="first@example.test", replied_at=NOW)
    )
    await db_session.commit()

    data = (await client.get("/api/v1/outreach/reply-stats")).json()
    emails = [r["to_email"] for r in data["recent_replies"]]
    assert emails == ["first@example.test", "late@example.test"]


@pytest.mark.asyncio
async def test_empty_database_reports_zero_not_a_crash(client: AsyncClient):
    data = (await client.get("/api/v1/outreach/reply-stats")).json()
    assert data["total_sent"] == 0
    assert data["reply_rate"] == 0.0
    assert data["recent_replies"] == []
