"""Outreach performance over time, not as one lifetime average.

The gap these cover: every Dialtone analytics surface was a point-in-time
aggregate. A sequence sending since March with a 2.1% lifetime reply rate might
be at 4% this month and 1% last, and nothing in the product could tell you
which — so every change made to outreach was evaluated against an average that
mixed the weeks before it into the weeks after.

Two things are easy to get wrong here and are pinned below: the newest bucket is
almost always incomplete, so its counts must not be presented as comparable; and
a "trend" taken from two noisy endpoints is not a trend, so the direction is
pooled over halves and refuses to answer when there is too little to say.
"""

import uuid as _uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services import outreach_trends

pytestmark = pytest.mark.asyncio


# --- fixtures ---------------------------------------------------------------


async def _sequence(db: AsyncSession, contact: Contact, name: str = "CNC Q3"):
    seq = OutreachSequence(id=_uuid.uuid4(), name=name, is_active=True)
    db.add(seq)
    await db.flush()
    db.add(SequenceStep(
        id=_uuid.uuid4(),
        sequence_id=seq.id,
        step_number=1,
        delay_days=0,
        channel="email",
        subject_template="Quote turnaround",
        body_template="<p>Hi.</p>",
    ))
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        current_step=1,
        status="active",
    )
    db.add(enrollment)
    await db.flush()
    return seq, enrollment


async def _events(
    db: AsyncSession, enrollment, *, days_ago: int, sent: int = 0, replied: int = 0,
    opened: int = 0, bounced: int = 0, task_created: int = 0,
):
    """Write ``n`` events of each type, dated ``days_ago`` days back."""
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    for event_type, count in (
        ("sent", sent), ("opened", opened), ("replied", replied),
        ("bounced", bounced), ("task_created", task_created),
    ):
        for _ in range(count):
            db.add(OutreachEvent(
                id=_uuid.uuid4(),
                enrollment_id=enrollment.id,
                step_number=1,
                event_type=event_type,
                occurred_at=when,
            ))
    await db.flush()


# --- bucketing --------------------------------------------------------------


async def test_events_land_in_the_period_they_happened_in(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    _, enrollment = await _sequence(db_session, sample_contact)
    await _events(db_session, enrollment, days_ago=2, sent=10, replied=1)
    await _events(db_session, enrollment, days_ago=9, sent=20, replied=4)
    await db_session.commit()

    result = await outreach_trends.timeline(db_session, days=28, interval="week")

    assert result["interval"] == "week"
    assert result["total_sent"] == 30
    assert result["total_replied"] == 5

    # Oldest first, and the two weeks are separate buckets.
    non_empty = [b for b in result["buckets"] if b["sent"]]
    assert [b["sent"] for b in non_empty] == [20, 10]
    assert non_empty[0]["reply_rate"] == 20.0
    assert non_empty[1]["reply_rate"] == 10.0


async def test_the_newest_bucket_is_flagged_partial(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """A half-finished week plotted next to full ones reads as a volume collapse."""
    _, enrollment = await _sequence(db_session, sample_contact)
    await _events(db_session, enrollment, days_ago=0, sent=5)
    await db_session.commit()

    result = await outreach_trends.timeline(db_session, days=28, interval="week")

    assert result["buckets"][-1]["partial"] is True
    assert all(not b["partial"] for b in result["buckets"][:-1])


async def test_buckets_are_contiguous_and_oldest_first(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """A gap or an overlap would double-count or drop a day's sends."""
    _, enrollment = await _sequence(db_session, sample_contact)
    await db_session.commit()

    result = await outreach_trends.timeline(db_session, days=28, interval="week")
    buckets = result["buckets"]

    assert len(buckets) == 4
    for earlier, later in zip(buckets, buckets[1:]):
        assert earlier["period_start"] < later["period_start"]
        assert later["period_start"] == earlier["period_end"] + timedelta(days=1)
    # UTC, not ``date.today()``. ``timeline`` builds its buckets from
    # ``datetime.now(timezone.utc)``, and a bare ``date.today()`` is the *local*
    # calendar day — so on any machine west of UTC this assertion is off by one
    # for the last hours of the local day (and east of UTC, the first hours).
    # The suite passes for most of the day and fails for the rest of it, which
    # is the shape that gets a red build ignored rather than fixed.
    assert buckets[-1]["period_end"] == datetime.now(timezone.utc).date()


async def test_a_day_interval_gives_a_bucket_per_day(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    _, enrollment = await _sequence(db_session, sample_contact)
    await _events(db_session, enrollment, days_ago=1, sent=3)
    await db_session.commit()

    result = await outreach_trends.timeline(db_session, days=7, interval="day")

    assert len(result["buckets"]) == 7
    assert sum(b["sent"] for b in result["buckets"]) == 3


async def test_an_unknown_interval_falls_back_to_week(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """A bad query param degrades rather than 500s."""
    result = await outreach_trends.timeline(
        db_session, days=28, interval="fortnight"
    )
    assert result["interval"] == "week"


async def test_events_outside_the_window_are_excluded(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    _, enrollment = await _sequence(db_session, sample_contact)
    await _events(db_session, enrollment, days_ago=3, sent=5)
    await _events(db_session, enrollment, days_ago=200, sent=999)
    await db_session.commit()

    result = await outreach_trends.timeline(db_session, days=28, interval="week")
    assert result["total_sent"] == 5


async def test_an_event_on_the_oldest_bucket_boundary_is_counted(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """The query window and the bucket edges must be the same edge.

    They differed by a few hours, so events in the gap were read out of the
    database, landed in no bucket, and disappeared — a query reporting less than
    it fetched.
    """
    _, enrollment = await _sequence(db_session, sample_contact)
    # 27 days back is the first day of the oldest bucket in a 28-day window.
    await _events(db_session, enrollment, days_ago=27, sent=6)
    await db_session.commit()

    result = await outreach_trends.timeline(db_session, days=28, interval="week")

    assert result["total_sent"] == 6
    assert result["buckets"][0]["sent"] == 6


async def test_manual_touches_stay_out_of_the_email_rates(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """A filed call reminder is not a send.

    Counting one would make a multi-channel sequence show a falling reply rate
    every time a human step ran.
    """
    _, enrollment = await _sequence(db_session, sample_contact)
    await _events(db_session, enrollment, days_ago=2, sent=10, replied=1, task_created=50)
    await db_session.commit()

    result = await outreach_trends.timeline(db_session, days=28, interval="week")

    assert result["total_sent"] == 10
    assert result["overall_reply_rate"] == 10.0


async def test_a_sequence_filter_narrows_to_that_sequence(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    seq_a, enr_a = await _sequence(db_session, sample_contact, name="A")
    seq_b, enr_b = await _sequence(db_session, sample_contact, name="B")
    await _events(db_session, enr_a, days_ago=2, sent=10)
    await _events(db_session, enr_b, days_ago=2, sent=7)
    await db_session.commit()

    everything = await outreach_trends.timeline(db_session, days=28)
    just_a = await outreach_trends.timeline(db_session, days=28, sequence_id=seq_a.id)

    assert everything["total_sent"] == 17
    assert just_a["total_sent"] == 10


# --- the trend itself -------------------------------------------------------


async def test_an_improving_reply_rate_is_reported_as_improving(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """The whole point: a lifetime average cannot say this."""
    _, enrollment = await _sequence(db_session, sample_contact)
    # Six complete weeks: 1% early, 5% late.
    for weeks_ago in (6, 5, 4):
        await _events(
            db_session, enrollment, days_ago=weeks_ago * 7 + 1, sent=100, replied=1
        )
    for weeks_ago in (3, 2, 1):
        await _events(
            db_session, enrollment, days_ago=weeks_ago * 7 + 1, sent=100, replied=5
        )
    await db_session.commit()

    result = await outreach_trends.timeline(db_session, days=56, interval="week")

    assert result["direction"] == "improving"
    # Not pinned to an exact figure: where the halves split depends on how many
    # complete buckets the window holds, and asserting the arithmetic of one
    # particular alignment would break on a change that is not a regression.
    # What must hold is that a 1% period followed by a 5% one reads as a clear
    # multi-point rise.
    assert result["reply_rate_trend"] >= 2.0


async def test_a_declining_reply_rate_is_reported_as_declining(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    _, enrollment = await _sequence(db_session, sample_contact)
    for weeks_ago in (6, 5, 4):
        await _events(
            db_session, enrollment, days_ago=weeks_ago * 7 + 1, sent=100, replied=6
        )
    for weeks_ago in (3, 2, 1):
        await _events(
            db_session, enrollment, days_ago=weeks_ago * 7 + 1, sent=100, replied=1
        )
    await db_session.commit()

    result = await outreach_trends.timeline(db_session, days=56, interval="week")
    assert result["direction"] == "declining"
    assert result["reply_rate_trend"] < 0


async def test_too_few_complete_periods_refuses_to_call_a_trend(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """Two endpoints on a noisy series is not a trend, and saying so is honest."""
    _, enrollment = await _sequence(db_session, sample_contact)
    await _events(db_session, enrollment, days_ago=2, sent=100, replied=9)
    await db_session.commit()

    result = await outreach_trends.timeline(db_session, days=14, interval="week")

    assert result["direction"] == "insufficient_data"
    assert result["reply_rate_trend"] is None


async def test_a_window_with_no_sends_reports_no_trend_rather_than_zero(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """0.0% vs 0.0% is not "flat performance", it is no data."""
    result = await outreach_trends.timeline(db_session, days=56, interval="week")

    assert result["total_sent"] == 0
    assert result["direction"] == "insufficient_data"
    assert result["reply_rate_trend"] is None


async def test_the_trend_is_pooled_not_averaged_across_buckets(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """A week of 4 sends must not outvote a week of 400.

    Averaging the per-bucket rates would let one tiny week with a lucky reply
    (25%) drag the half it sits in, and cold outreach has plenty of tiny weeks.
    """
    _, enrollment = await _sequence(db_session, sample_contact)
    # Older half: high volume, 2% reply.
    for weeks_ago in (6, 5, 4):
        await _events(
            db_session, enrollment, days_ago=weeks_ago * 7 + 1, sent=400, replied=8
        )
    # Newer half: two tiny lucky weeks and one real one, pooling to ~2%.
    await _events(db_session, enrollment, days_ago=3 * 7 + 1, sent=4, replied=1)
    await _events(db_session, enrollment, days_ago=2 * 7 + 1, sent=4, replied=1)
    await _events(db_session, enrollment, days_ago=1 * 7 + 1, sent=400, replied=6)
    await db_session.commit()

    result = await outreach_trends.timeline(db_session, days=56, interval="week")

    # Pooled: 8/1200 = 0.7% vs 8/408 = 2.0%. An average of the bucket rates
    # would have read 0.7% vs (25 + 25 + 1.5)/3 = 17%.
    assert result["reply_rate_trend"] < 3.0


async def test_bucket_deltas_compare_against_the_previous_bucket(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    _, enrollment = await _sequence(db_session, sample_contact)
    await _events(db_session, enrollment, days_ago=9, sent=100, replied=2)
    await _events(db_session, enrollment, days_ago=2, sent=100, replied=5)
    await db_session.commit()

    result = await outreach_trends.timeline(db_session, days=28, interval="week")
    buckets = result["buckets"]

    # The oldest has nothing to compare against.
    assert buckets[0]["reply_rate_change"] is None
    latest = buckets[-1]
    assert latest["reply_rate"] == 5.0
    assert latest["reply_rate_change"] == pytest.approx(3.0, abs=0.1)


# --- the endpoint -----------------------------------------------------------


async def test_the_trends_endpoint_returns_the_window(
    client, db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    _, enrollment = await _sequence(db_session, sample_contact)
    await _events(db_session, enrollment, days_ago=2, sent=10, replied=1, opened=4)
    await db_session.commit()

    response = await client.get("/api/v1/dialtone/analytics/trends?days=28")
    assert response.status_code == 200
    body = response.json()

    assert body["interval"] == "week"
    assert body["total_sent"] == 10
    assert body["total_replied"] == 1
    assert body["overall_reply_rate"] == 10.0
    assert len(body["buckets"]) == 4
    assert body["buckets"][-1]["partial"] is True


async def test_the_endpoint_rejects_an_out_of_range_window(client):
    """Bounded so a caller cannot ask for a decade of buckets."""
    response = await client.get("/api/v1/dialtone/analytics/trends?days=99999")
    assert response.status_code == 422


async def test_the_endpoint_can_be_scoped_to_one_sequence(
    client, db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    seq_a, enr_a = await _sequence(db_session, sample_contact, name="A")
    seq_b, enr_b = await _sequence(db_session, sample_contact, name="B")
    await _events(db_session, enr_a, days_ago=2, sent=10)
    await _events(db_session, enr_b, days_ago=2, sent=7)
    await db_session.commit()

    response = await client.get(
        f"/api/v1/dialtone/analytics/trends?days=28&sequence_id={seq_a.id}"
    )
    assert response.status_code == 200
    assert response.json()["total_sent"] == 10
