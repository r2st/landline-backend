"""The send-history date filters, at the two boundaries they get wrong.

``GET /email-sends`` and ``GET /email-sends/export-csv`` share
``_apply_email_send_filters``, which turned a ``YYYY-MM-DD`` query param into a
bound on ``EmailSend.sent_at``. Two things were wrong with how it did it, and
neither produces an error — the endpoint returns a window, just not the one that
was asked for.

**The bounds were naive.** ``sent_at`` is ``TIMESTAMP(timezone=True)``. Postgres
does not reject a naive comparand; it coerces it using the session's ``TimeZone``
setting, which nothing in this application configures, so the window slides by
whatever ``initdb`` picked up from the host. A report for "1st to 7th" quietly
becomes "31st 20:00 to 6th 20:00" on a box in New York — with the right number
of rows in it, and the wrong rows.

**The upper bound was inclusive of ``datetime.max.time()``**, i.e.
23:59:59.999999, so it dropped the last microsecond of the final day. Small, and
it is the kind of small that only shows up in a reconciliation against another
report months later.

These tests exercise the *filter* rather than a route, because the shift is a
property of the bound and every caller inherits it. Times are pinned to real UTC
instants either side of midnight — a test that constructs its fixtures the same
way the code does cannot see a timezone bug, because both sides move together.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_template import EmailSend
from app.routers.email_templates import _apply_email_send_filters

DAY = date(2026, 5, 14)


async def _send(db: AsyncSession, when: datetime) -> EmailSend:
    row = EmailSend(
        id=uuid.uuid4(),
        to_email=f"{uuid.uuid4().hex[:8]}@prospect.com",
        subject="Quick question",
        status="sent",
        sent_at=when,
    )
    db.add(row)
    return row


async def _filtered(db: AsyncSession, **kw) -> list[EmailSend]:
    query = _apply_email_send_filters(select(EmailSend), **kw)
    return list((await db.execute(query)).scalars().all())


async def test_the_window_is_the_utc_day_the_caller_named(db_session: AsyncSession):
    """Both edges of 14 May UTC are in; both neighbours are out.

    The four instants are one second inside and one second outside each
    boundary. A naive bound coerced through a non-UTC session timezone moves the
    whole window by whole hours, so at least one of these four lands on the wrong
    side of it.
    """
    day_start = datetime(2026, 5, 14, 0, 0, 0, tzinfo=timezone.utc)
    just_before = day_start - timedelta(seconds=1)
    just_after = day_start + timedelta(days=1)

    await _send(db_session, just_before)
    first = await _send(db_session, day_start)
    last = await _send(db_session, day_start + timedelta(days=1, seconds=-1))
    await _send(db_session, just_after)
    await db_session.commit()

    rows = await _filtered(db_session, date_from=DAY, date_to=DAY)
    assert {r.id for r in rows} == {first.id, last.id}


async def test_the_final_microsecond_of_the_last_day_is_included(
    db_session: AsyncSession,
):
    """``datetime.max.time()`` is 23:59:59.999999, so it has a gap after it.

    An inclusive upper bound built from it excludes anything in the last
    microsecond of the day. The half-open ``< date_to + 1 day`` form has no edge
    to get wrong and does not depend on the column's precision.
    """
    tail = datetime(2026, 5, 14, 23, 59, 59, 999999, tzinfo=timezone.utc)
    row = await _send(db_session, tail)
    await db_session.commit()

    rows = await _filtered(db_session, date_from=DAY, date_to=DAY)
    assert [r.id for r in rows] == [row.id]


async def test_the_upper_bound_does_not_reach_into_the_next_day(
    db_session: AsyncSession,
):
    """The other direction — half-open must not become inclusive-plus-a-day.

    Midnight exactly belongs to the 15th, and a bound written as ``<=`` on
    ``date_to + 1 day`` would hand back a report a day wider than it claims.
    """
    await _send(db_session, datetime(2026, 5, 15, 0, 0, 0, tzinfo=timezone.utc))
    await db_session.commit()

    assert await _filtered(db_session, date_from=DAY, date_to=DAY) == []


async def test_an_open_ended_range_still_filters_the_end_it_was_given(
    db_session: AsyncSession,
):
    """Each bound is independent; supplying one must not imply the other."""
    early = await _send(db_session, datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc))
    late = await _send(db_session, datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc))
    await db_session.commit()

    only_from = await _filtered(db_session, date_from=DAY)
    assert {r.id for r in only_from} == {late.id}

    only_to = await _filtered(db_session, date_to=DAY)
    assert {r.id for r in only_to} == {early.id}
