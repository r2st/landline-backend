"""``/dashboard/email-trends`` — volume per day, and what came back from it.

Two failures this endpoint had, both of the "reports a number nobody could act
on" kind:

  * it read ``email_sends`` alone, so it plotted manual and template mail and
    called it the org's email volume — the sequence engine, where the outreach
    actually goes, writes ``outreach_events`` and no ``EmailSend`` row;
  * it returned ``sent`` and nothing else, while the Analytics page's compare
    mode read ``opened`` / ``replied`` off these points to show a
    period-over-period change. Absent fields resolved to 0, so the panel
    reported "no change" whatever had happened.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep


def _today() -> datetime:
    return datetime.now(timezone.utc)


async def _enrollment(db: AsyncSession, contact: Contact) -> OutreachEnrollment:
    seq = OutreachSequence(id=uuid.uuid4(), name="Trend seq")
    db.add(seq)
    await db.flush()
    db.add(SequenceStep(
        sequence_id=seq.id, step_number=1,
        subject_template="s", body_template="b",
    ))
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, current_step=1
    )
    db.add(enr)
    await db.flush()
    return enr


async def _points(client: AsyncClient, days: int = 30) -> list[dict]:
    resp = await client.get(f"/api/v1/dashboard/email-trends?days={days}")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_sequence_sends_are_counted(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    """The bug in one test: a sequence send writes no EmailSend row."""
    enr = await _enrollment(db_session, sample_contact)
    db_session.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, event_type="sent",
        step_number=1, occurred_at=_today(),
    ))
    await db_session.commit()

    points = await _points(client)
    assert sum(p["sent"] for p in points) == 1


@pytest.mark.asyncio
async def test_engagement_from_both_tables_lands_on_the_same_day(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    now = _today()
    enr = await _enrollment(db_session, sample_contact)
    db_session.add_all([
        OutreachEvent(id=uuid.uuid4(), enrollment_id=enr.id, event_type="sent",
                      step_number=1, occurred_at=now),
        OutreachEvent(id=uuid.uuid4(), enrollment_id=enr.id, event_type="opened",
                      step_number=1, occurred_at=now),
        OutreachEvent(id=uuid.uuid4(), enrollment_id=enr.id, event_type="clicked",
                      step_number=1, occurred_at=now),
        OutreachEvent(id=uuid.uuid4(), enrollment_id=enr.id, event_type="bounced",
                      step_number=1, occurred_at=now),
        EmailSend(
            id=uuid.uuid4(), contact_id=sample_contact.id,
            to_email=sample_contact.email or "x@y.com", subject="hi",
            status="sent", sent_at=now, opened_at=now, clicked_at=now,
            replied_at=now,
        ),
    ])
    await db_session.commit()

    today = now.date().isoformat()
    point = next(p for p in await _points(client) if p["date"] == today)
    assert point["sent"] == 2       # one sequence event + one EmailSend
    assert point["opened"] == 2
    assert point["clicked"] == 2
    assert point["replied"] == 1
    assert point["bounced"] == 1


@pytest.mark.asyncio
async def test_an_open_buckets_on_the_day_it_happened_not_the_day_we_sent(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    """Sent Monday, opened Wednesday: one row, two days, different columns."""
    now = _today()
    sent_at = now - timedelta(days=3)
    db_session.add(EmailSend(
        id=uuid.uuid4(), contact_id=sample_contact.id,
        to_email=sample_contact.email or "x@y.com", subject="hi",
        status="sent", sent_at=sent_at, opened_at=now,
    ))
    await db_session.commit()

    points = {p["date"]: p for p in await _points(client)}
    assert points[sent_at.date().isoformat()]["sent"] == 1
    assert points[sent_at.date().isoformat()]["opened"] == 0
    assert points[now.date().isoformat()]["opened"] == 1
    assert points[now.date().isoformat()]["sent"] == 0


@pytest.mark.asyncio
async def test_the_window_is_honoured(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    old = _today() - timedelta(days=60)
    db_session.add(EmailSend(
        id=uuid.uuid4(), contact_id=sample_contact.id,
        to_email=sample_contact.email or "x@y.com", subject="old",
        status="sent", sent_at=old,
    ))
    await db_session.commit()

    assert sum(p["sent"] for p in await _points(client, days=30)) == 0
    assert sum(p["sent"] for p in await _points(client, days=90)) == 1


@pytest.mark.asyncio
async def test_points_are_ordered_oldest_first(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    now = _today()
    for offset in (5, 1, 3):
        db_session.add(EmailSend(
            id=uuid.uuid4(), contact_id=sample_contact.id,
            to_email=sample_contact.email or "x@y.com", subject="hi",
            status="sent", sent_at=now - timedelta(days=offset),
        ))
    await db_session.commit()

    dates = [p["date"] for p in await _points(client)]
    assert dates == sorted(dates)


@pytest.mark.asyncio
async def test_manual_touch_events_are_not_counted_as_sends(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    """A filed call reminder is not a delivered message.

    ``task_*`` events are outside the send vocabulary on purpose — counting one
    would put a touch that may never have been made into every rate's
    denominator.
    """
    enr = await _enrollment(db_session, sample_contact)
    db_session.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, event_type="task_created",
        step_number=1, occurred_at=_today(),
    ))
    await db_session.commit()

    assert sum(p["sent"] for p in await _points(client)) == 0


@pytest.mark.asyncio
async def test_no_mail_returns_an_empty_series_not_a_zero_row(client: AsyncClient):
    assert await _points(client) == []
