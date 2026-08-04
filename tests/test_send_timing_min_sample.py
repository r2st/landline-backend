"""A "best send time" needs a denominator before it means anything.

``get_optimal_times`` ranked days, hours and (day, hour) cells purely by open
rate. Open rates on real traffic sit around 30-60%, so no busy hour ever reaches
100% — which means the top of the recommendation was structurally reserved for
the thinnest cells on the grid. One send that happened to be opened outranked
every hour with real volume behind it, and an operator following the card moved
their sending into the hour with the least evidence for it.

``get_heatmap`` next door already refused to nominate a ``best_cell`` under
three sends. These tests pin the same floor on all three rankings, and pin that
the floor does not quietly become "drop thin data from the totals" — the counts
are how an operator watches the grid fill up.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.services.send_timing import MIN_SAMPLE, get_send_timing

# Tuesday and Wednesday of a fixed week, so ``extract('dow')`` is deterministic.
# 2026-07-07 is a Tuesday (dow 2); 2026-07-08 a Wednesday (dow 3).
TUESDAY = datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc)
WEDNESDAY = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)


async def _event(db: AsyncSession, when: datetime, *, step: int, opened: bool) -> None:
    """One 'sent' event at ``when``, plus its 'opened' event when asked."""
    enrollment = OutreachEnrollment(
        contact_id=uuid.uuid4(), sequence_id=uuid.uuid4(), status="active"
    )
    db.add(enrollment)
    await db.flush()
    db.add(
        OutreachEvent(
            enrollment_id=enrollment.id, step_number=step,
            event_type="sent", occurred_at=when,
        )
    )
    if opened:
        db.add(
            OutreachEvent(
                enrollment_id=enrollment.id, step_number=step,
                event_type="opened", occurred_at=when,
            )
        )
    await db.flush()


async def _seed_thin_vs_thick(db: AsyncSession) -> None:
    """A lucky 1/1 on Wednesday against a well-evidenced 6/10 on Tuesday.

    Wednesday's rate is 100% and Tuesday's is 60%, so rate-only ranking puts
    Wednesday first on every one of the three lists.
    """
    for i in range(10):
        await _event(db, TUESDAY, step=i + 1, opened=i < 6)
    await _event(db, WEDNESDAY, step=99, opened=True)
    await db.commit()


@pytest.mark.asyncio
async def test_a_single_lucky_send_is_not_the_best_day(db_session: AsyncSession):
    await _seed_thin_vs_thick(db_session)

    data = await get_send_timing().get_optimal_times(db_session)

    assert data["has_data"] is True
    assert data["has_ranking"] is True
    days = [d["day"] for d in data["best_days"]]
    assert days[0] == "Tuesday", f"the 1/1 cell was ranked best: {data['best_days']}"
    assert "Wednesday" not in days


@pytest.mark.asyncio
async def test_a_single_lucky_send_is_not_the_best_hour(db_session: AsyncSession):
    await _seed_thin_vs_thick(db_session)

    data = await get_send_timing().get_optimal_times(db_session)

    hours = [h["hour"] for h in data["best_hours"]]
    assert hours[0] == 10, f"the 1/1 hour was ranked best: {data['best_hours']}"
    assert 14 not in hours


@pytest.mark.asyncio
async def test_a_single_lucky_send_is_not_the_best_combination(db_session: AsyncSession):
    await _seed_thin_vs_thick(db_session)

    data = await get_send_timing().get_optimal_times(db_session)

    combos = [(c["day"], c["hour"]) for c in data["best_combinations"]]
    assert combos[0] == ("Tuesday", 10)
    assert ("Wednesday", 14) not in combos


@pytest.mark.asyncio
async def test_thin_cells_still_count_toward_the_totals(db_session: AsyncSession):
    """Barred from being *called* best is not the same as discarded.

    The counts are what show an operator the grid filling up; a floor that
    silently dropped them would make the surface look emptier than the mailbox.
    """
    await _seed_thin_vs_thick(db_session)

    data = await get_send_timing().get_optimal_times(db_session)

    assert data["total_emails_analyzed"] == 11  # 10 Tuesday + 1 Wednesday
    assert data["overall_open_rate"] == pytest.approx(63.6, abs=0.1)  # 7 of 11


@pytest.mark.asyncio
async def test_nothing_thick_enough_reports_itself_rather_than_guessing(
    db_session: AsyncSession,
):
    """Data exists but none of it is rankable — say so, don't recommend noise."""
    await _event(db_session, WEDNESDAY, step=1, opened=True)
    await db_session.commit()

    data = await get_send_timing().get_optimal_times(db_session)

    assert data["has_data"] is True     # a send did happen
    assert data["has_ranking"] is False  # and it cannot support a recommendation
    assert data["best_days"] == []
    assert data["best_hours"] == []
    assert data["best_combinations"] == []
    assert data["min_sample"] == MIN_SAMPLE


@pytest.mark.asyncio
async def test_ties_on_rate_break_toward_the_larger_sample(db_session: AsyncSession):
    """Both qualify; the one with more sends behind it should lead.

    Without the tiebreak the order among equal rates is whatever the grouping
    happened to yield, which makes the top recommendation unstable run to run.
    """
    for i in range(3):          # Wednesday: 3 sends, 2 opens → 66.7%
        await _event(db_session, WEDNESDAY, step=i + 1, opened=i < 2)
    for i in range(9):          # Tuesday: 9 sends, 6 opens → 66.7%
        await _event(db_session, TUESDAY, step=i + 20, opened=i < 6)
    await db_session.commit()

    data = await get_send_timing().get_optimal_times(db_session)

    assert [d["day"] for d in data["best_days"]][:2] == ["Tuesday", "Wednesday"]


@pytest.mark.asyncio
async def test_heatmap_and_optimal_times_share_one_floor(db_session: AsyncSession):
    """The two surfaces read the same events; they must not disagree."""
    await _seed_thin_vs_thick(db_session)

    optimal = await get_send_timing().get_optimal_times(db_session)
    heatmap = await get_send_timing().get_heatmap(db_session)

    assert optimal["min_sample"] == heatmap["min_sample"] == MIN_SAMPLE
    best_cell = heatmap["best_cell"]
    assert best_cell is not None
    assert (best_cell["dow"], best_cell["hour"]) == (2, 10)  # Tuesday 10:00
    assert (optimal["best_combinations"][0]["day"], optimal["best_combinations"][0]["hour"]) == (
        "Tuesday", 10,
    )
