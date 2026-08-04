"""The funnel report reads the window, not the archive.

``GET /reports/outreach-funnel`` used to open with ``select(OutreachEvent)`` —
the whole table, as full ORM objects — and apply the date window afterwards, in
Python. That is the single most expensive read in the codebase, because
``outreach_events.event_metadata`` is where the sequence engine persists each
message's ``body_html``: one request for a 120-day report dragged every email
the org had ever sent, in full, through the session. The three lookup tables
around it (enrollments, A/B assignments) were unbounded reads too.

The behaviour must not change — these tests pin both halves:

* what the endpoint *reads*: the window and the event-type filter reach SQL, and
  metadata is fetched only for the enrollments that appear in the answer;
* what it *reports*: dedup, the sequence filter, and — the one that is easy to
  break while narrowing a query — a subject line inherited across the window
  boundary, where an open counted this month belongs to a send from last month.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep

SUBJ = "the scrap math your CFO hasn't run yet"


async def _seq(db, name):
    seq = OutreachSequence(id=uuid.uuid4(), name=name)
    db.add(seq)
    await db.flush()
    db.add(SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
        subject_template=SUBJ, body_template="B",
    ))
    await db.flush()
    return seq


async def _contact(db, company, email):
    c = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="X", last_name="Y",
        email=email, source="test",
    )
    db.add(c)
    await db.flush()
    return c


async def _enroll(db, contact, seq):
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db.add(enr)
    await db.flush()
    return enr


def _event(db, enr, event_type, when, *, subject=None, body=None, step=1):
    meta = {}
    if subject is not None:
        meta["subject"] = subject
    if body is not None:
        meta["body_html"] = body
    db.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=step,
        event_type=event_type, occurred_at=when, event_metadata=meta,
    ))


@pytest.fixture
def sql_log(db_engine):
    """Capture every statement the endpoint issues, with its parameters."""
    statements: list[tuple[str, object]] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append((" ".join(statement.split()), parameters))

    event.listen(db_engine.sync_engine, "before_cursor_execute", _record)
    yield statements
    event.remove(db_engine.sync_engine, "before_cursor_execute", _record)


def _event_reads(statements):
    return [s for s, _ in statements if "FROM outreach_events" in s]


# ─── What it reads ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_query_reads_the_whole_events_table(
    client, db_session, sample_company, sql_log
):
    seq = await _seq(db_session, "Volume Seq")
    enr = await _enroll(db_session, await _contact(db_session, sample_company, "a@x.com"), seq)
    now = datetime.now(timezone.utc)
    _event(db_session, enr, "sent", now, subject=SUBJ, body="<p>body</p>")
    await db_session.commit()

    sql_log.clear()
    resp = await client.get("/api/v1/reports/outreach-funnel")
    assert resp.status_code == 200, resp.text

    reads = _event_reads(sql_log)
    assert reads, "the endpoint issued no read of outreach_events at all"
    for stmt in reads:
        # Every read narrows to the outcomes the funnel counts, or to the
        # enrollments already in the answer. A `WHERE` alone proves nothing —
        # the org filter supplies one on every statement in this codebase.
        assert (
            "event_type IN" in stmt or "enrollment_id IN" in stmt
        ), f"unbounded scan of outreach_events: {stmt}"


@pytest.mark.asyncio
async def test_the_date_window_reaches_sql(
    client, db_session, sample_company, sql_log
):
    """The window used to be applied in Python, after every row was in memory."""
    seq = await _seq(db_session, "Window Seq")
    enr = await _enroll(db_session, await _contact(db_session, sample_company, "b@x.com"), seq)
    now = datetime.now(timezone.utc)
    _event(db_session, enr, "sent", now, subject=SUBJ)
    await db_session.commit()

    sql_log.clear()
    resp = await client.get(
        "/api/v1/reports/outreach-funnel",
        params={"start_date": (now - timedelta(days=7)).isoformat()},
    )
    assert resp.status_code == 200, resp.text

    windowed = [s for s in _event_reads(sql_log) if "occurred_at >=" in s]
    assert windowed, "the start bound never reached the database"


@pytest.mark.asyncio
async def test_metadata_is_not_read_for_enrollments_outside_the_answer(
    client, db_session, sample_company, sql_log
):
    """`event_metadata` carries the full `body_html`; it is the expensive column.

    An enrollment whose only events fall outside the window contributes nothing
    to the report, so its copy must never be fetched.
    """
    seq = await _seq(db_session, "Metadata Seq")
    now = datetime.now(timezone.utc)

    recent = await _enroll(db_session, await _contact(db_session, sample_company, "r@x.com"), seq)
    _event(db_session, recent, "sent", now - timedelta(days=1), subject=SUBJ, body="<p>recent</p>")

    ancient = await _enroll(db_session, await _contact(db_session, sample_company, "o@x.com"), seq)
    _event(db_session, ancient, "sent", now - timedelta(days=900), subject="old", body="<p>old</p>")
    await db_session.commit()

    sql_log.clear()
    resp = await client.get(
        "/api/v1/reports/outreach-funnel",
        params={"start_date": (now - timedelta(days=30)).isoformat()},
    )
    assert resp.status_code == 200, resp.text

    # The attribute is `event_metadata`; the column it maps to is `metadata`.
    meta_reads = [
        (s, p) for s, p in sql_log
        if "FROM outreach_events" in s and "outreach_events.metadata" in s
    ]
    assert meta_reads, "the subject line is resolved from metadata; nothing read it"

    bound = str([p for _, p in meta_reads])
    assert str(recent.id) in bound
    assert str(ancient.id) not in bound, (
        "the archived enrollment's email body was read to draw a 30-day report"
    )


# ─── What it reports ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_subject_is_inherited_across_the_window_boundary(
    client, db_session, sample_company
):
    """The subject harvest is deliberately *not* windowed.

    Narrowing it to the window would be the natural way to make this query
    cheap, and it would silently relabel every open whose send predates the
    window as ``(unknown subject)`` — on the report whose whole point is which
    subject line works.
    """
    seq = await _seq(db_session, "Boundary Seq")
    enr = await _enroll(db_session, await _contact(db_session, sample_company, "c@x.com"), seq)
    now = datetime.now(timezone.utc)

    _event(db_session, enr, "sent", now - timedelta(days=60), subject=SUBJ)
    _event(db_session, enr, "opened", now - timedelta(days=1))
    await db_session.commit()

    resp = await client.get(
        "/api/v1/reports/outreach-funnel",
        params={"start_date": (now - timedelta(days=7)).isoformat()},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["overall"]["sent"] == 0, "the out-of-window send must not be counted"
    assert data["overall"]["opened"] == 1
    assert [s["subject"] for s in data["by_subject"]] == [SUBJ]


@pytest.mark.asyncio
async def test_the_window_drops_an_older_open_on_another_step(
    client, db_session, sample_company
):
    """An open outside the window is not counted, however many steps ran.

    This used to plant five opens on one step to prove the funnel collapsed
    them. Migration 117 makes that impossible — `(enrollment, step, 'opened')`
    is unique — so the surviving question is the windowing one: a step opened
    inside the window counts, an older step opened long before it does not.
    """
    seq = await _seq(db_session, "Dedup Seq")
    enr = await _enroll(db_session, await _contact(db_session, sample_company, "d@x.com"), seq)
    now = datetime.now(timezone.utc)

    _event(db_session, enr, "sent", now - timedelta(days=2), subject=SUBJ)
    _event(db_session, enr, "opened", now - timedelta(days=1))
    _event(db_session, enr, "opened", now - timedelta(days=400), step=2)
    await db_session.commit()

    resp = await client.get(
        "/api/v1/reports/outreach-funnel",
        params={"start_date": (now - timedelta(days=30)).isoformat()},
    )
    data = resp.json()
    assert data["overall"]["sent"] == 1
    assert data["overall"]["opened"] == 1


@pytest.mark.asyncio
async def test_the_sequence_filter_still_excludes_other_sequences(
    client, db_session, sample_company
):
    seq_a = await _seq(db_session, "Seq A")
    seq_b = await _seq(db_session, "Seq B")
    now = datetime.now(timezone.utc)

    enr_a = await _enroll(db_session, await _contact(db_session, sample_company, "f@x.com"), seq_a)
    enr_b = await _enroll(db_session, await _contact(db_session, sample_company, "g@x.com"), seq_b)
    _event(db_session, enr_a, "sent", now, subject="A subj")
    _event(db_session, enr_b, "sent", now, subject="B subj")
    await db_session.commit()

    resp = await client.get(f"/api/v1/reports/outreach-funnel?sequence_id={seq_a.id}")
    data = resp.json()
    assert data["overall"]["sent"] == 1
    assert [s["subject"] for s in data["by_subject"]] == ["A subj"]
