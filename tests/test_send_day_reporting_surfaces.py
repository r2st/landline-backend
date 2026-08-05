"""Everything that says "today" about sending says the same "today".

``app.services.send_day`` moved the day boundary off 00:00 UTC and onto the
sender's local midnight, because the send window is evaluated in each *contact's*
timezone and a UTC boundary therefore falls **inside** the sending day — 00:00
UTC is 17:00 in Los Angeles, an hour before that contact's window closes with
the shipped 08:00–18:00 defaults. The cap, its row-count seed and the warm-up
counter all moved.

Two surfaces did not, and they are the two shapes a leftover boundary takes:

* **a display** — the dashboard's "emails sent today" tiles, which sit beside a
  cap readout that counts against the new boundary. Between 20:00 and midnight
  in Toronto the tile read zero while the cap reported the day's allowance
  spent, and sends made in the last hour of a US-Pacific window appeared here as
  *today's* while the cap booked them against yesterday. A half-migrated read
  surface is worse than an unmigrated one: the two numbers disagree, and neither
  is labelled with which day it means.

* **a decision** — ``OrchestrationPolicy.max_daily_touches``. That one is not a
  reporting inconsistency but the same doubling the warm-up ramp had: the
  per-contact touch cap refilled at 17:00 local for a West-Coast prospect, so a
  cadence could put twice the policy's touches into one of their business days.

Both are pinned against the *default* ``America/Toronto`` configuration rather
than a monkeypatched zone, because the bug is that the shipped defaults are
misaligned — a test that picks a convenient timezone proves nothing about them.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.config import get_settings
from app.services import send_day


TORONTO = ZoneInfo("America/Toronto")

# 21:00 EDT on 2026-07-15 — a summer evening, after the Toronto send window has
# closed and before local midnight. In UTC that is 01:00 on the *16th*, so the
# UTC date has already rolled and the sending date has not. Every assertion
# below hangs off that one instant.
EVENING = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)


def test_the_default_configuration_is_the_one_with_the_gap():
    """A control: the shipped settings are what make these two dates differ."""
    assert get_settings().send_window_timezone == "America/Toronto"
    assert EVENING.astimezone(TORONTO).date().isoformat() == "2026-07-15"
    assert EVENING.date().isoformat() == "2026-07-16"
    # The sending day started the previous local midnight, which is *before* the
    # UTC day started — so a UTC-keyed count drops the whole evening.
    assert send_day.day_start(EVENING) < EVENING.replace(
        hour=0, minute=0, second=0, microsecond=0
    )


# --- The dashboard tiles ------------------------------------------------------


# UTC+14. Local midnight is 10:00 UTC on the *previous* calendar day, so the
# sending-day boundary is never 00:00 UTC whatever time this suite runs at — and
# two events placed either side of it always land on the *same* UTC date. That
# makes the mutation control hold at every hour: a tile counting the UTC day
# sees both events or neither (2 or 0), and never the correct 1.
FAR_EAST = "Pacific/Kiritimati"


@pytest.fixture
def eastern_sending_day(monkeypatch):
    """Anchor the sending day well away from the UTC one, and return its start."""
    settings = get_settings()
    monkeypatch.setattr(settings, "send_window_timezone", FAR_EAST, raising=False)
    start = send_day.day_start()
    assert start != datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start


async def _sent_event(db_session, enrollment, at: datetime):
    import uuid as _uuid

    from app.models.outreach import OutreachEvent

    db_session.add(
        OutreachEvent(
            id=_uuid.uuid4(),
            enrollment_id=enrollment.id,
            step_number=1,
            event_type="sent",
            occurred_at=at,
        )
    )


async def _an_enrollment(db_session):
    import uuid as _uuid

    from app.models.company import Company
    from app.models.contact import Contact
    from app.models.outreach import OutreachEnrollment
    from app.models.sequence import OutreachSequence

    company = Company(id=_uuid.uuid4(), name="SendDayCo", country="US", source="test")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(
        id=_uuid.uuid4(),
        company_id=company.id,
        first_name="Sam",
        last_name="Day",
        email="sam@sendday.test",
    )
    seq = OutreachSequence(id=_uuid.uuid4(), name="Send-day seq")
    db_session.add_all([contact, seq])
    await db_session.flush()
    enr = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id
    )
    db_session.add(enr)
    await db_session.flush()
    return enr


@pytest.mark.asyncio
async def test_kpi_emails_sent_today_counts_the_sending_day(
    client, db_session, eastern_sending_day
):
    """One send inside the sending day, one just outside it.

    Mutation control: with ``now.replace(hour=0, ...)`` both events sit on the
    same UTC date, so the tile reports 2 or 0 — never the 1 that is actually
    today's.
    """
    enr = await _an_enrollment(db_session)
    await _sent_event(db_session, enr, eastern_sending_day + timedelta(seconds=1))
    await _sent_event(db_session, enr, eastern_sending_day - timedelta(seconds=1))
    await db_session.commit()

    data = (await client.get("/api/v1/dashboard/kpis")).json()
    assert data["emails_sent_today"] == 1


@pytest.mark.asyncio
async def test_overview_emails_sent_today_counts_the_same_day(
    client, db_session, eastern_sending_day
):
    """The overview tile and the KPI tile must not disagree about "today"."""
    enr = await _an_enrollment(db_session)
    await _sent_event(db_session, enr, eastern_sending_day + timedelta(seconds=1))
    await _sent_event(db_session, enr, eastern_sending_day - timedelta(seconds=1))
    await db_session.commit()

    overview = (await client.get("/api/v1/dashboard/overview")).json()
    kpis = (await client.get("/api/v1/dashboard/kpis")).json()
    assert overview["emails_sent_today"] == 1
    assert overview["emails_sent_today"] == kpis["emails_sent_today"]


def test_dashboard_module_uses_send_day_for_today():
    """The tiles resolve their window through ``send_day``, not ``replace()``.

    Read as a source check because both call sites live inside handlers that
    build a dozen unrelated tiles from a live session. What matters is that
    neither one reintroduces a second definition of "today" — which is exactly
    what a reviewer cannot see, since ``now.replace(hour=0, ...)`` is the
    obvious and wrong shape.
    """
    import inspect

    from app.routers import dashboard

    source = inspect.getsource(dashboard)
    assert "send_day.day_start(now)" in source
    assert source.count("send_day.day_start(now)") == 2, (
        "both `emails_sent_today` tiles must resolve the same boundary"
    )
    # No surviving UTC-midnight "today" in the send counters.
    for line in source.splitlines():
        if "replace(hour=0" in line:
            assert "day=1" in line, f"UTC-midnight day boundary left in: {line.strip()}"


# --- The per-contact touch cap ------------------------------------------------


def _send_event(at: datetime):
    return SimpleNamespace(
        event_type="sent", occurred_at=at, event_metadata={"channel": "email"}
    )


def _touches_today(events, now):
    """The guardrail's own counter, over a hand-made event list."""
    from app.services.orchestrator import count_touches_today

    return count_touches_today([e for e in events if e.event_type == "sent"], now)


def test_touch_cap_does_not_refill_while_the_window_is_still_open():
    """A West-Coast contact's evening touches count against the same day.

    Mutation control: with the UTC-date comparison this returns 1 — the 22:00 UTC
    touch lands on the previous UTC date — so a policy of ``max_daily_touches=2``
    would permit two more touches at 17:00 Los Angeles time, inside the window
    the first two were sent in.
    """
    # 18:00 and 22:00 UTC on the 15th, then 00:30 UTC on the 16th: 14:00, 18:00
    # and 20:30 Toronto. All one sending day; two different UTC days.
    events = [
        _send_event(datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)),
        _send_event(datetime(2026, 7, 15, 22, 0, tzinfo=timezone.utc)),
        _send_event(datetime(2026, 7, 16, 0, 30, tzinfo=timezone.utc)),
    ]
    assert _touches_today(events, EVENING) == 3


def test_touch_cap_still_forgets_yesterday():
    """The counter must roll — it just has to roll at the right instant."""
    yesterday = [
        # 20:00 Toronto on the 14th — before the 15th's local midnight.
        _send_event(datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)),
    ]
    assert _touches_today(yesterday, EVENING) == 0


def test_touch_cap_counts_nothing_with_no_sends():
    assert _touches_today([], EVENING) == 0


def test_touch_cap_ignores_non_send_events():
    """Opens and replies are not touches; only what we put on the wire is."""
    events = [
        SimpleNamespace(
            event_type="opened",
            occurred_at=datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc),
            event_metadata={},
        ),
        _send_event(datetime(2026, 7, 15, 18, 5, tzinfo=timezone.utc)),
    ]
    assert _touches_today(events, EVENING) == 1


def test_touch_cap_tolerates_a_naive_timestamp():
    """Columns read back naive must not raise inside a guardrail."""
    events = [_send_event(datetime(2026, 7, 15, 18, 0))]
    assert _touches_today(events, EVENING) == 1


def test_touch_cap_rolls_over_at_local_midnight_exactly():
    """One second either side of the boundary lands on different days."""
    boundary = send_day.day_start(EVENING)
    just_before = _send_event(boundary - timedelta(seconds=1))
    just_after = _send_event(boundary)
    assert _touches_today([just_before], EVENING) == 0
    assert _touches_today([just_after], EVENING) == 1
