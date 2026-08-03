"""The two activity feeds truncate to ``cap`` rows; the window has to come first.

``dialtone._collect_events`` and ``lead_metrics.recent_events`` both assemble a
feed by pulling the newest ``cap`` rows out of each source table and merging
them. Both then applied the caller's date window to the *merged events*, in
Python, which reads as equivalent and is not: ``cap`` had already thrown away
everything but the newest ``cap`` rows in the table, so the window could only
ever match inside that slice. Ask for a window older than the slice — any
"last month" tile on a tenant past ``cap`` sends — and every source returns
rows that all postdate the window, the filter drops all of them, and the
endpoint answers ``total: 0``.

There is no error and nothing in the response says the feed was truncated
before it was filtered; an empty month looks exactly like a quiet one. The
customers who hit it first are the ones with the most data.

These tests drive the two collectors directly with a small ``cap`` rather than
writing 5001 rows through the routes. ``cap`` is a parameter for this reason,
and the bug is a property of the collector — every route that reaches it
inherits the behaviour either way.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.prospecting import ProspectCandidate, ProspectingRun
from app.models.sequence import OutreachSequence
from app.routers.dialtone import _collect_events
from app.services import lead_metrics

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
# The window under test: a fortnight, three months behind "now".
WINDOW_START = NOW - timedelta(days=100)
WINDOW_END = NOW - timedelta(days=86)
IN_WINDOW = WINDOW_START + timedelta(days=7)


async def _send(db: AsyncSession, when: datetime, subject: str) -> EmailSend:
    row = EmailSend(
        id=uuid.uuid4(),
        to_email=f"{uuid.uuid4().hex[:8]}@prospect.com",
        subject=subject,
        status="sent",
        sent_at=when,
    )
    db.add(row)
    return row


async def _seed_sends(db: AsyncSession, *, recent: int) -> EmailSend:
    """One send inside the window, buried under ``recent`` newer ones."""
    wanted = await _send(db, IN_WINDOW, "the one in the window")
    for i in range(recent):
        await _send(db, NOW - timedelta(hours=i + 1), f"noise {i}")
    await db.commit()
    return wanted


@pytest.mark.asyncio
async def test_dialtone_activity_finds_a_send_older_than_the_cap(
    db_session: AsyncSession,
):
    """The window is asked of the database, not of the newest ``cap`` rows.

    With ``cap=3`` and four newer sends, an unfiltered pull cannot reach the
    in-window row at all — so a collector that filters afterwards returns
    nothing here while one that filters in SQL returns the row.
    """
    wanted = await _seed_sends(db_session, recent=4)

    items = await _collect_events(
        db_session, start=WINDOW_START, end=WINDOW_END,
        contact_id=None, q=None, cap=3,
    )

    assert [i.id for i in items] == [f"email_sent:{wanted.id}"]


@pytest.mark.asyncio
async def test_leads_recent_finds_a_send_older_than_the_cap(
    db_session: AsyncSession,
):
    """Same defect, same shape, in the ``GET /leads/recent`` collector."""
    wanted = await _seed_sends(db_session, recent=4)

    items = await lead_metrics.recent_events(
        db_session, start=WINDOW_START, end=WINDOW_END, cap=3,
    )

    assert [i.id for i in items] == [f"email:email_sent:{wanted.id}"]


@pytest.mark.asyncio
async def test_engagement_on_an_old_send_belongs_to_the_engagement_window(
    db_session: AsyncSession,
):
    """A row qualifies on *any* of its timestamps, not just ``sent_at``.

    One ``EmailSend`` emits up to four events at four different times. A send
    that went out long before the window and was replied to inside it belongs to
    the window on ``replied_at`` alone — a SQL predicate written against
    ``sent_at`` only would drop it, which is the pushdown reintroducing the bug
    it was meant to fix.
    """
    old = await _send(db_session, NOW - timedelta(days=300), "sent long ago")
    old.replied_at = IN_WINDOW
    for i in range(4):
        await _send(db_session, NOW - timedelta(hours=i + 1), f"noise {i}")
    await db_session.commit()

    items = await _collect_events(
        db_session, start=WINDOW_START, end=WINDOW_END,
        contact_id=None, q=None, cap=3,
    )

    # The reply is in the window; the send that carried it is not.
    assert [i.id for i in items] == [f"reply:{old.id}"]


@pytest.mark.asyncio
async def test_sequence_events_and_discoveries_are_filtered_in_sql_too(
    db_session: AsyncSession, sample_contact: Contact,
):
    """The other two sources ``/leads/recent`` unions carry the same defect."""
    sequence = OutreachSequence(id=uuid.uuid4(), name="Cold outreach")
    db_session.add(sequence)
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(),
        sequence_id=sequence.id,
        contact_id=sample_contact.id,
        status="active",
        current_step=1,
    )
    db_session.add(enrollment)

    def event(when: datetime) -> OutreachEvent:
        row = OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
            event_type="sent", occurred_at=when,
        )
        db_session.add(row)
        return row

    run = ProspectingRun(id=uuid.uuid4())
    db_session.add(run)

    def candidate(when: datetime) -> ProspectCandidate:
        row = ProspectCandidate(
            id=uuid.uuid4(), run_id=run.id,
            company_name=f"Acme {uuid.uuid4().hex[:6]}",
            fit_score=70, status="new", created_at=when,
        )
        db_session.add(row)
        return row

    wanted_event = event(IN_WINDOW)
    wanted_cand = candidate(IN_WINDOW)
    for i in range(4):
        event(NOW - timedelta(hours=i + 1))
        candidate(NOW - timedelta(hours=i + 1))
    await db_session.commit()

    items = await lead_metrics.recent_events(
        db_session, start=WINDOW_START, end=WINDOW_END, cap=3,
    )

    assert {i.id for i in items} == {
        f"outreach:{wanted_event.id}",
        f"lead_discovered:{wanted_cand.id}",
    }


@pytest.mark.asyncio
async def test_an_unbounded_request_still_returns_the_newest_cap_rows(
    db_session: AsyncSession,
):
    """No window means no predicate — ``cap`` alone still bounds the response.

    The pushdown must not turn "full history" into "nothing": with both bounds
    open there is no ``WHERE`` to add, and the cap keeps doing the job it was
    added for.
    """
    await _seed_sends(db_session, recent=4)

    items = await lead_metrics.recent_events(db_session, start=None, end=None, cap=3)

    assert len(items) == 3
