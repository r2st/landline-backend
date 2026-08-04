"""The console's landing numbers read the window, not the archive.

``GET /dialtone/status`` is the first thing an operator loads, and every one of
its counters came out of ``_period_counts``, which read **every row ever
written** to ``email_sends``, ``outreach_events`` and ``prospect_candidates`` and
then discarded all but the asked-for window in Python.

The cost was therefore unrelated to the window: the default view is "today", a
few hundred rows, and it paid for the whole send history to produce them. So the
console got monotonically slower for the life of an account, and got slowest for
exactly the tenants sending most — the same shape as the 120-day funnel that read
every email ever sent to draw four months (see
``tests/test_outreach_funnel_read_volume.py``, whose harness this mirrors).

Two things are pinned here, and the second is the one that makes the first safe:

* the window bound reaches SQL for all three source tables;
* the arithmetic is unchanged. The prefilter is a *superset* — it keeps any row
  any window could count and drops only rows that were always going to be
  discarded — and ``all_time``, which has no bounds, still reads everything,
  because that is the honest answer to "count everything".
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.prospecting import ProspectCandidate, ProspectingRun
from app.models.sequence import OutreachSequence, SequenceStep

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sql_log(db_engine):
    """Capture every statement the endpoint issues."""
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(db_engine.sync_engine, "before_cursor_execute", _record)
    yield statements
    event.remove(db_engine.sync_engine, "before_cursor_execute", _record)


def _reads(statements, table):
    return [s for s in statements if f"FROM {table}" in s]


async def _enrollment(db, contact):
    seq = OutreachSequence(id=uuid.uuid4(), name="Console Seq", is_active=True)
    db.add(seq)
    await db.flush()
    db.add(SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
        subject_template="S", body_template="B",
    ))
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db.add(enr)
    await db.flush()
    return enr


async def _history(db, company, contact):
    """One row in each source table, today and long ago."""
    now = datetime.now(timezone.utc)
    enr = await _enrollment(db, contact)
    run = ProspectingRun(id=uuid.uuid4())
    db.add(run)
    await db.flush()

    for step, when in ((1, now), (2, now - timedelta(days=400))):
        db.add(OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enr.id, step_number=step,
            event_type="sent", occurred_at=when, event_metadata={},
        ))
    for when in (now, now - timedelta(days=400)):
        db.add(EmailSend(
            id=uuid.uuid4(), contact_id=contact.id, to_email=contact.email,
            subject="Manual", status="sent", sent_at=when,
        ))
        db.add(ProspectCandidate(
            id=uuid.uuid4(), run_id=run.id,
            company_name=f"Lead {uuid.uuid4().hex[:6]}",
            source="test", created_at=when,
        ))
    await db.commit()
    return enr


# ─── What it reads ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "table",
    ["email_sends", "outreach_events", "prospect_candidates"],
)
async def test_the_window_reaches_sql_for_every_source_table(
    client, db_session, sample_company, sample_contact, sql_log, table,
):
    """The regression, one table at a time.

    A ``WHERE`` alone proves nothing — the org filter puts one on every statement
    in this codebase — so the assertion is on a timestamp bound specifically.
    """
    await _history(db_session, sample_company, sample_contact)

    sql_log.clear()
    resp = await client.get("/api/v1/dialtone/console", params={"timeframe": "today"})
    assert resp.status_code == 200, resp.text

    reads = _reads(sql_log, table)
    assert reads, f"the console issued no read of {table} at all"
    assert any(">=" in s and "<=" in s for s in reads), (
        f"unbounded scan of {table}: {reads}"
    )


async def test_all_time_still_reads_everything(
    client, db_session, sample_company, sample_contact, sql_log,
):
    """The control, and the reason the span opens on a single open bound.

    ``all_time`` has no bounds. A prefilter that invented one would silently
    turn "count everything" into "count the last N days" — a number that looks
    entirely plausible and is wrong by the whole archive.
    """
    await _history(db_session, sample_company, sample_contact)

    resp = await client.get("/api/v1/dialtone/console", params={"timeframe": "all_time"})
    assert resp.status_code == 200, resp.text
    counts = resp.json()["today"]
    assert counts["emails_sent"] == 4, counts   # 2 events + 2 EmailSend rows
    assert counts["leads_discovered"] == 2, counts


# ─── What it reports ─────────────────────────────────────────────────────────


async def test_the_narrowed_read_reports_the_same_numbers(
    client, db_session, sample_company, sample_contact,
):
    """Today's counters see today's rows and none of the archive's."""
    await _history(db_session, sample_company, sample_contact)

    resp = await client.get("/api/v1/dialtone/console", params={"timeframe": "today"})
    assert resp.status_code == 200, resp.text
    counts = resp.json()["today"]

    assert counts["emails_sent"] == 2, counts   # 1 event + 1 EmailSend, both today
    assert counts["leads_discovered"] == 1, counts


async def test_a_row_engaged_this_period_but_sent_last_still_counts_its_open(
    client, db_session, sample_company, sample_contact,
):
    """The row the prefilter is easiest to get wrong on.

    One ``EmailSend`` carries four timestamps and each is counted separately. A
    send from last year that was opened today belongs to today's ``opens`` — so
    the prefilter has to keep a row when *any* of its columns lands in the span,
    not just the send column. Narrowing on ``sent_at`` alone would drop this open
    with nothing to notice.
    """
    now = datetime.now(timezone.utc)
    db_session.add(EmailSend(
        id=uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="Old send, fresh open", status="opened",
        sent_at=now - timedelta(days=400), opened_at=now,
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/console", params={"timeframe": "today"})
    counts = resp.json()["today"]

    assert counts["opens"] == 1, counts
    assert counts["emails_sent"] == 0, "the send itself is out of the window"


async def test_the_previous_period_is_still_tallied(
    client, db_session, sample_company, sample_contact,
):
    """The span covers both windows, so the delta must survive the prefilter.

    The console asks for a period *and* the one before it in one pass. A span
    taken from the current window alone would clip every comparison to zero and
    report every metric as newly arrived.
    """
    now = datetime.now(timezone.utc)
    contact = Contact(
        id=uuid.uuid4(), company_id=sample_company.id, first_name="Prev",
        email="prev@acme.test", source="test",
    )
    db_session.add(contact)
    await db_session.flush()
    enr = await _enrollment(db_session, contact)

    # This month and last month, so "this_month" carries a real previous window.
    db_session.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=1, event_type="sent",
        occurred_at=now, event_metadata={},
    ))
    db_session.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=2, event_type="sent",
        occurred_at=(now.replace(day=1) - timedelta(days=2)), event_metadata={},
    ))
    await db_session.commit()

    resp = await client.get(
        "/api/v1/dialtone/console", params={"timeframe": "this_month"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["today"]["emails_sent"] == 1
    assert body["previous"] is not None
    assert body["previous"]["emails_sent"] == 1
