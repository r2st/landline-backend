"""A refused send is not a send, on the two surfaces that count them.

``EmailSend.status`` classifies rows into three lists on the model —
``WIRE_STATUSES`` (it reached the wire), ``DELIVERED_STATUSES``, and
``NON_WIRE_STATUSES`` (nothing left the building). The note above them says why
the third list is written out rather than inferred: so a status added later has
to be classified by whoever adds it "rather than defaulting into whichever list
is checked first".

Both send tallies defaulted. ``lead_metrics.tally_by_org`` (behind
``GET /leads/stats``) and ``dialtone._period_counts`` (behind
``GET /dialtone/status``) excluded ``skipped`` by name and let an ``else`` sweep
up everything else — so four non-wire statuses were counted as emails sent:

- ``suppressed`` — the send gate matched a do-not-contact entry and refused;
- ``halted`` — the global kill switch was engaged and refused;
- ``pending`` / ``scheduled`` — the row exists, the message hasn't gone. These
  are counted at once rather than eventually, because ``sent_at`` is a NOT NULL
  ``server_default``: a row queued for next Tuesday is stamped now.

The first two are the ones worth naming. They are the system *declining to
send*, and counting them inverts the metric at the exact moment an operator is
reading it: engage the kill switch to stop a blast and the console reports the
biggest send day on record. Nothing errors, and a fixture whose sends all
succeed cannot tell the two definitions apart — which is why the existing tests
were green.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_template import NON_WIRE_STATUSES, WIRE_STATUSES, EmailSend
from app.routers.dialtone import _period_counts
from app.services.lead_metrics import tally_by_org

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
WINDOW = (NOW - timedelta(days=1), NOW + timedelta(days=1))

# Every status the model says never reached the wire, minus the two the tallies
# deliberately report as errors instead of dropping.
REFUSED_OR_QUEUED = sorted(NON_WIRE_STATUSES - {"error", "failed"})


async def _send(db: AsyncSession, status: str) -> EmailSend:
    row = EmailSend(
        id=uuid.uuid4(),
        to_email=f"{uuid.uuid4().hex[:8]}@prospect.com",
        subject="Quick question",
        status=status,
        sent_at=NOW,
    )
    db.add(row)
    return row


@pytest.mark.parametrize("status", REFUSED_OR_QUEUED)
@pytest.mark.asyncio
async def test_leads_stats_does_not_count_a_send_that_never_went(
    db_session: AsyncSession, status: str,
):
    await _send(db_session, status)
    await db_session.commit()

    tallies = await tally_by_org(db_session, *WINDOW)

    assert [c["emails_sent"] for c in tallies.values()] in ([], [0]), status


@pytest.mark.parametrize("status", REFUSED_OR_QUEUED)
@pytest.mark.asyncio
async def test_dialtone_console_does_not_count_a_send_that_never_went(
    db_session: AsyncSession, status: str,
):
    await _send(db_session, status)
    await db_session.commit()

    counts = (await _period_counts(db_session, [WINDOW]))[0]

    assert counts.emails_sent == 0, status
    assert counts.errors == 0, status


@pytest.mark.asyncio
async def test_the_kill_switch_does_not_inflate_the_send_count(
    db_session: AsyncSession,
):
    """The case that motivated this: a halted blast beside one real send.

    An operator engages the kill switch mid-blast. Every held row lands as
    ``halted``; exactly one message got out before the switch took. The console
    must report one send, not twenty-one.
    """
    await _send(db_session, "sent")
    for _ in range(20):
        await _send(db_session, "halted")
    await db_session.commit()

    counts = (await _period_counts(db_session, [WINDOW]))[0]

    assert counts.emails_sent == 1


@pytest.mark.asyncio
async def test_statuses_that_did_reach_the_wire_are_still_counted(
    db_session: AsyncSession,
):
    """The control. Narrowing what counts must not narrow it past the truth.

    ``status`` is a lifecycle column that climbs ``sent → opened → clicked →
    replied`` as the recipient engages, and ``bounced`` is mail that went out and
    was rejected afterwards — so every one of these is a message this mailbox
    actually sent. A tally that dropped them would repeat the mistake this file
    exists for, in the other direction and on the more valuable rows.
    """
    for status in WIRE_STATUSES:
        await _send(db_session, status)
    await db_session.commit()

    counts = (await _period_counts(db_session, [WINDOW]))[0]

    # ``bounced`` is booked as a bounce rather than a plain send; the rest count.
    assert counts.emails_sent == len(WIRE_STATUSES) - 1
    assert counts.bounces == 1


@pytest.mark.asyncio
async def test_a_failed_send_is_still_reported_as_an_error(
    db_session: AsyncSession,
):
    """``error``/``failed`` are non-wire too, and must not be silently dropped.

    They are the one part of ``NON_WIRE_STATUSES`` the tallies have their own
    bucket for. Folding the whole list into "ignore" would take the error count
    to zero — a transport that is failing every send would report a quiet day.
    """
    await _send(db_session, "error")
    await _send(db_session, "failed")
    await db_session.commit()

    counts = (await _period_counts(db_session, [WINDOW]))[0]

    assert counts.emails_sent == 0
    assert counts.errors == 2
