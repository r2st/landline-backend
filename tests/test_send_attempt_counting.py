"""How many times did we put a step on the wire?

``sent`` and ``bounced`` OutreachEvents cannot answer that by row count, because
a ``bounced`` row means two different things depending on who wrote it:

* ``send_sequence_step`` writes ``"sent" if status == "sent" else "bounced"`` —
  one row per attempt, never both, so a synchronous failure has *no* ``sent``
  row;
* ``bounce_handler._suppress_contact`` writes one when the DSN comes back hours
  after a send that already logged its ``sent`` row — a *second* row about the
  *same* attempt.

Every consumer picked a different wrong answer to that. ``sent + bounced`` rows
(``danger_monitor``) inflates the denominator in the common async case, so a
domain at a true 10% bounce rate reads 9.1% and the auto-pause holds off.
``sent`` alone (``funnel_analytics``) leaves a run where every attempt failed
synchronously dividing by zero and reporting 0.0 — healthy.

``AttemptTally`` and ``count_attempts`` are the one answer, in Python and in SQL
respectively. The two must agree, so every case below is asserted against both.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services.outreach_events import AttemptTally, count_attempts


async def _enrollment(db: AsyncSession) -> OutreachEnrollment:
    company = Company(id=uuid.uuid4(), name=f"Co {uuid.uuid4()}", source="test", country="US")
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, email=f"{uuid.uuid4()}@acme.com",
        first_name="Dana", source="test",
    )
    seq = OutreachSequence(id=uuid.uuid4(), name=f"Seq {uuid.uuid4()}")
    db.add_all([contact, seq])
    await db.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status="active",
    )
    db.add(enr)
    await db.flush()
    return enr


async def _event(db: AsyncSession, enr, step: int, event_type: str) -> None:
    db.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=step,
        event_type=event_type, occurred_at=datetime.now(timezone.utc),
    ))
    await db.flush()


def _tally(events) -> tuple[int, int]:
    t = AttemptTally()
    for event_type, enrollment_id, step in events:
        t.add(event_type, enrollment_id, step)
    return t.attempts, t.bounces


# --- the Python tally, in isolation ------------------------------------------


def test_a_clean_send_is_one_attempt_and_no_bounce():
    assert _tally([("sent", "e1", 1)]) == (1, 0)


def test_an_async_dsn_is_the_same_attempt():
    """The DSN row sits on top of the send's own row; together they are one."""
    assert _tally([("sent", "e1", 1), ("bounced", "e1", 1)]) == (1, 1)


def test_a_synchronous_failure_is_an_attempt_with_no_send_row():
    """Nothing was accepted, but we did try — the denominator has to hold it.

    Otherwise the worst possible run, where every attempt fails at SMTP time,
    divides by zero and reports a bounce rate of 0.0.
    """
    assert _tally([("bounced", "e1", 1)]) == (1, 1)


def test_a_redelivered_dsn_is_not_a_second_bounce():
    events = [("sent", "e1", 1)] + [("bounced", "e1", 1)] * 4
    assert _tally(events) == (1, 1)


def test_steps_of_one_enrollment_are_separate_attempts():
    """The key is (enrollment, step) — a five-step sequence is five attempts."""
    events = [("sent", "e1", n) for n in range(1, 6)]
    events.append(("bounced", "e1", 3))
    assert _tally(events) == (5, 1)


def test_other_event_types_are_ignored():
    events = [("sent", "e1", 1), ("opened", "e1", 1), ("replied", "e1", 1)]
    assert _tally(events) == (1, 0)


def test_duplicate_sent_rows_are_counted_rather_than_collapsed():
    """The conservative direction when the send-claim invariant is violated.

    Collapsing them would shrink the denominator and inflate every rate the
    danger rail acts on, which is the wrong way to be wrong about a brake.
    """
    assert _tally([("sent", "e1", 1), ("sent", "e1", 1)]) == (2, 0)


def test_a_bounce_filed_against_the_wrong_step_reads_as_a_second_attempt():
    """Why the ``last_sent_step`` fix in ``bounce_handler`` is load-bearing.

    Rows written before it carry ``current_step`` — the step queued *next* — so
    they never pair with the send they describe. Pinned so the coupling between
    the two fixes is visible rather than folklore.
    """
    assert _tally([("sent", "e1", 1), ("bounced", "e1", 2)]) == (2, 1)


# --- the SQL counterpart must agree ------------------------------------------


@pytest.mark.asyncio
async def test_sql_counts_an_async_dsn_as_one_attempt(db_session: AsyncSession):
    enr = await _enrollment(db_session)
    await _event(db_session, enr, 1, "sent")
    await _event(db_session, enr, 1, "bounced")

    assert await count_attempts(db_session) == (1, 1)
    assert _tally([("sent", enr.id, 1), ("bounced", enr.id, 1)]) == (1, 1)


@pytest.mark.asyncio
async def test_sql_counts_a_synchronous_failure_as_an_attempt(db_session: AsyncSession):
    enr = await _enrollment(db_session)
    await _event(db_session, enr, 1, "bounced")

    assert await count_attempts(db_session) == (1, 1)


@pytest.mark.asyncio
async def test_sql_dedupes_a_redelivered_dsn(db_session: AsyncSession):
    enr = await _enrollment(db_session)
    await _event(db_session, enr, 1, "sent")
    for _ in range(3):
        await _event(db_session, enr, 1, "bounced")

    assert await count_attempts(db_session) == (1, 1)


@pytest.mark.asyncio
async def test_sql_matches_the_python_tally_on_a_mixed_population(db_session: AsyncSession):
    """Nine clean sends, one async DSN, one synchronous failure → 10 attempts."""
    events: list[tuple] = []
    for _ in range(9):
        enr = await _enrollment(db_session)
        await _event(db_session, enr, 1, "sent")
        events.append(("sent", enr.id, 1))
    dsn = await _enrollment(db_session)
    await _event(db_session, dsn, 1, "sent")
    await _event(db_session, dsn, 1, "bounced")
    events += [("sent", dsn.id, 1), ("bounced", dsn.id, 1)]
    sync = await _enrollment(db_session)
    await _event(db_session, sync, 1, "bounced")
    events.append(("bounced", sync.id, 1))

    sql = await count_attempts(db_session)
    assert sql == (11, 2)
    assert _tally(events) == sql
