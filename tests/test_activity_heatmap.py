"""The activity heatmap counts the sends that happened, in the window asked for.

Three separate faults, all in one endpoint:

* it selected **every** timestamp in `email_sends`, `interactions` and `tasks` —
  all of history, on every table — and then dropped everything outside the
  window in Python, to colour in at most 120 squares;
* it counted `EmailSend` rows whatever their status, so a `skipped` row (no
  address, suppressed recipient, spam-gate block, warm-up hold, spent cap,
  engaged kill switch) drew activity on a day when nothing left the building —
  and those cluster on precisely the days outreach was *not* happening;
* it read only `email_sends`, so every sequence send was invisible. Those are
  `OutreachEvent(event_type='sent')` rows and they are where the bulk of this
  platform's outreach volume goes, which left the heatmap palest on the busiest
  days.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app.models.email_template import EmailSend
from app.models.interaction import Interaction
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence

NOW = datetime.now(timezone.utc)


def _day(offset_days: int) -> str:
    return (NOW - timedelta(days=offset_days)).date().isoformat()


def _count_on(data, day) -> int:
    return next((p["count"] for p in data if p["date"] == day), 0)


def _send(db, contact, *, status, days_ago):
    db.add(EmailSend(
        id=uuid.uuid4(), contact_id=contact.id, to_email="x@y.com",
        subject="hi", status=status, sent_at=NOW - timedelta(days=days_ago),
    ))


async def _sequence_send(db, contact, *, days_ago):
    seq = OutreachSequence(id=uuid.uuid4(), name=f"Heatmap {uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db.add(enr)
    await db.flush()
    db.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=1, event_type="sent",
        occurred_at=NOW - timedelta(days=days_ago), event_metadata={},
    ))


# ─── What it counts ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_skipped_send_is_not_a_day_of_activity(
    client, db_session, sample_contact
):
    _send(db_session, sample_contact, status="sent", days_ago=1)
    _send(db_session, sample_contact, status="skipped", days_ago=1)
    _send(db_session, sample_contact, status="suppressed", days_ago=1)
    _send(db_session, sample_contact, status="halted", days_ago=1)
    _send(db_session, sample_contact, status="failed", days_ago=1)
    await db_session.commit()

    data = (await client.get("/api/v1/reports/activity-heatmap")).json()
    assert _count_on(data, _day(1)) == 1


@pytest.mark.asyncio
async def test_a_bounce_still_counts(client, db_session, sample_contact):
    """A bounce is mail that went out and was rejected, not mail that stayed home."""
    _send(db_session, sample_contact, status="bounced", days_ago=2)
    _send(db_session, sample_contact, status="opened", days_ago=2)
    await db_session.commit()

    data = (await client.get("/api/v1/reports/activity-heatmap")).json()
    assert _count_on(data, _day(2)) == 2


@pytest.mark.asyncio
async def test_sequence_sends_are_visible(client, db_session, sample_contact):
    """They write no EmailSend row, and they are most of the volume."""
    await _sequence_send(db_session, sample_contact, days_ago=3)
    await _sequence_send(db_session, sample_contact, days_ago=3)
    await db_session.commit()

    data = (await client.get("/api/v1/reports/activity-heatmap")).json()
    assert _count_on(data, _day(3)) == 2


@pytest.mark.asyncio
async def test_a_non_send_outreach_event_is_not_a_send(
    client, db_session, sample_contact
):
    """`outreach_events` holds opens, replies and bounces on the same table."""
    seq = OutreachSequence(id=uuid.uuid4(), name="Heatmap Opens")
    db_session.add(seq)
    await db_session.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db_session.add(enr)
    await db_session.flush()
    for et in ("sent", "opened", "replied"):
        db_session.add(OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enr.id, step_number=1, event_type=et,
            occurred_at=NOW - timedelta(days=4), event_metadata={},
        ))
    await db_session.commit()

    data = (await client.get("/api/v1/reports/activity-heatmap")).json()
    assert _count_on(data, _day(4)) == 1


@pytest.mark.asyncio
async def test_the_two_send_tables_add_up(client, db_session, sample_contact):
    _send(db_session, sample_contact, status="sent", days_ago=5)
    await _sequence_send(db_session, sample_contact, days_ago=5)
    db_session.add(Interaction(
        id=uuid.uuid4(), company_id=sample_contact.company_id,
        interaction_type="call", subject="Ring",
        occurred_at=NOW - timedelta(days=5),
    ))
    await db_session.commit()

    data = (await client.get("/api/v1/reports/activity-heatmap")).json()
    assert _count_on(data, _day(5)) == 3


# ─── What it reads ───────────────────────────────────────────────────────────


@pytest.fixture
def sql_log(db_engine):
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(db_engine.sync_engine, "before_cursor_execute", _record)
    yield statements
    event.remove(db_engine.sync_engine, "before_cursor_execute", _record)


@pytest.mark.asyncio
async def test_the_window_reaches_sql_on_every_table(
    client, db_session, sample_contact, sql_log
):
    """The default window is 120 days; the read used to be all of history.

    Asserted on the emitted SQL rather than the response, because the response
    was never wrong about the window — the rows were dropped, just only after
    all of them had crossed the wire.
    """
    _send(db_session, sample_contact, status="sent", days_ago=1)
    await db_session.commit()

    sql_log.clear()
    resp = await client.get("/api/v1/reports/activity-heatmap")
    assert resp.status_code == 200, resp.text

    for table, column in (
        ("email_sends", "sent_at"),
        ("outreach_events", "occurred_at"),
        ("interactions", "occurred_at"),
        ("tasks", "created_at"),
    ):
        reads = [s for s in sql_log if f"FROM {table}" in s]
        assert reads, f"the heatmap never read {table}"
        assert any(f"{column} >=" in s for s in reads), (
            f"{table} was read without the window bound"
        )


@pytest.mark.asyncio
async def test_an_explicit_window_is_honoured(client, db_session, sample_contact):
    _send(db_session, sample_contact, status="sent", days_ago=2)
    _send(db_session, sample_contact, status="sent", days_ago=200)
    await db_session.commit()

    data = (
        await client.get(
            "/api/v1/reports/activity-heatmap",
            params={"start": _day(7), "end": _day(0)},
        )
    ).json()
    assert _count_on(data, _day(2)) == 1
    assert _count_on(data, _day(200)) == 0


@pytest.mark.asyncio
async def test_the_default_window_excludes_the_archive(
    client, db_session, sample_contact
):
    _send(db_session, sample_contact, status="sent", days_ago=1)
    _send(db_session, sample_contact, status="sent", days_ago=400)
    await db_session.commit()

    data = (await client.get("/api/v1/reports/activity-heatmap")).json()
    assert _count_on(data, _day(1)) == 1
    assert _count_on(data, _day(400)) == 0
