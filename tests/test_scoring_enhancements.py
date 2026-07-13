"""Tests for the lead-scoring quick-win enhancements:

- Engagement velocity as a *multiplier* (not a flat bonus).
- Score change history (per-score delta + timeline helpers).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.services.scoring import (
    velocity_multiplier,
    score_company_rule_based,
    score_history,
    score_change_since,
    _VELOCITY_NEUTRAL,
)


# ---------------------------------------------------------------------------
# velocity_multiplier — pure function
# ---------------------------------------------------------------------------


class TestVelocityMultiplier:
    def test_zero_signals_is_neutral(self):
        assert velocity_multiplier(0) == _VELOCITY_NEUTRAL == 1.0

    def test_one_signal_is_neutral(self):
        assert velocity_multiplier(1) == 1.0

    def test_two_signals_boosts(self):
        assert velocity_multiplier(2) == 1.25

    def test_three_signals_jumps(self):
        # A cluster of 3 events in 24h nearly doubles the engagement weight.
        assert velocity_multiplier(3) == 1.6

    def test_burst_caps_at_max(self):
        assert velocity_multiplier(6) == 2.5
        assert velocity_multiplier(20) == 2.5

    def test_monotonically_non_decreasing(self):
        vals = [velocity_multiplier(n) for n in range(0, 12)]
        assert vals == sorted(vals)


# ---------------------------------------------------------------------------
# Velocity as a multiplier — integration
# ---------------------------------------------------------------------------


async def _add_send(db, contact, *, opened=None, clicked=None, replied=None):
    db.add(
        EmailSend(
            contact_id=contact.id,
            to_email=contact.email,
            subject="hi",
            status="sent",
            opened_at=opened,
            clicked_at=clicked,
            replied_at=replied,
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_engagement_counts_open_and_reply(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """Regression: a recent open+reply must register (naive SQLite datetimes
    must not raise, and must be counted)."""
    now = datetime.now(timezone.utc)
    await _add_send(db_session, sample_contact, opened=now, replied=now)

    score = await score_company_rule_based(db_session, sample_company)
    eng = score.factors["engagement"]
    # replied(12) + opened(3) = 15
    assert eng["score"] == 15


@pytest.mark.asyncio
async def test_velocity_multiplies_engagement_signal(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """A burst of distinct events in 24h multiplies the engagement signal."""
    now = datetime.now(timezone.utc)
    # 3 distinct recent signals → velocity multiplier of 1.6.
    await _add_send(db_session, sample_contact, opened=now)
    await _add_send(db_session, sample_contact, clicked=now)
    await _add_send(db_session, sample_contact, replied=now)

    score = await score_company_rule_based(db_session, sample_company)
    vel = score.factors["engagement_velocity"]

    assert vel["signals_in_24h"] == 3
    assert vel["multiplier"] == 1.6
    # boosted = round(signal * 1.6); velocity actually added points.
    assert vel["boosted_signal"] == round(vel["engagement_signal"] * 1.6)
    assert vel["points_added"] > 0
    # The stored engagement column = completeness + boosted engagement signal.
    assert score.engagement_score == (
        score.factors["completeness"]["score"] + vel["boosted_signal"]
    )


@pytest.mark.asyncio
async def test_no_velocity_when_signals_stale(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """Events older than 24h don't trigger the velocity multiplier."""
    old = datetime.now(timezone.utc) - timedelta(days=3)
    await _add_send(db_session, sample_contact, opened=old, replied=old)

    score = await score_company_rule_based(db_session, sample_company)
    vel = score.factors["engagement_velocity"]
    assert vel["signals_in_24h"] == 0
    assert vel["multiplier"] == 1.0
    assert vel["points_added"] == 0


@pytest.mark.asyncio
async def test_hot_burst_scores_higher_than_single_stale_open(
    db_session: AsyncSession,
):
    """Two identical companies: the one with a fresh burst scores higher.

    Uses a lower-fit firmographic profile so both leave headroom under the
    100-point cap and the velocity boost is observable.
    """
    async def _company(name):
        c = Company(
            name=name, domain=f"{name}.com", country="US",
            employee_count=30, naics_code="4231",  # small + related industry
            source="test",
        )
        db_session.add(c)
        await db_session.flush()
        ct = Contact(
            company_id=c.id, first_name="A", last_name="B",
            email=f"a@{name}.com", source="test",
        )
        db_session.add(ct)
        await db_session.flush()
        return c, ct

    now = datetime.now(timezone.utc)
    cold_co, cold_ct = await _company("coldco")
    await _add_send(db_session, cold_ct, opened=now)  # single open

    hot_co, hot_ct = await _company("hotco")
    await _add_send(db_session, hot_ct, opened=now)
    await _add_send(db_session, hot_ct, clicked=now)
    await _add_send(db_session, hot_ct, replied=now)

    cold = await score_company_rule_based(db_session, cold_co)
    hot = await score_company_rule_based(db_session, hot_co)
    assert hot.fit_score > cold.fit_score


# ---------------------------------------------------------------------------
# Score change history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_score_is_initial(
    db_session: AsyncSession, sample_company: Company
):
    score = await score_company_rule_based(db_session, sample_company)
    change = score.factors["score_change"]
    assert change["direction"] == "initial"
    assert change["previous_fit_score"] is None
    assert change["delta"] == 0


@pytest.mark.asyncio
async def test_rescore_records_delta(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """A second score records how much it moved from the first."""
    first = await score_company_rule_based(db_session, sample_company)

    # Add fresh engagement so the re-score is higher.
    now = datetime.now(timezone.utc)
    await _add_send(db_session, sample_contact, opened=now, clicked=now, replied=now)

    second = await score_company_rule_based(db_session, sample_company)
    change = second.factors["score_change"]
    assert change["previous_fit_score"] == first.fit_score
    assert change["delta"] == second.fit_score - first.fit_score
    assert change["direction"] in ("up", "flat", "down")
    if second.fit_score > first.fit_score:
        assert change["direction"] == "up"
        assert change["delta"] > 0


@pytest.mark.asyncio
async def test_score_history_timeline(
    db_session: AsyncSession, sample_company: Company
):
    await score_company_rule_based(db_session, sample_company)
    await score_company_rule_based(db_session, sample_company)
    await score_company_rule_based(db_session, sample_company)

    history = await score_history(db_session, sample_company.id, limit=10)
    assert len(history) == 3
    # newest first; oldest entry has no delta.
    assert history[-1]["delta"] is None
    assert all("fit_score" in h and "scored_at" in h for h in history)


@pytest.mark.asyncio
async def test_score_change_since_window(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """score_change_since compares the latest score to the one at a window start."""
    # Baseline score (treated as "a week ago").
    baseline = await score_company_rule_based(db_session, sample_company)
    # Backdate it so it predates the window.
    baseline.scored_at = datetime.now(timezone.utc) - timedelta(days=8)
    await db_session.flush()

    # Fresh engagement + a new score "now".
    now = datetime.now(timezone.utc)
    await _add_send(db_session, sample_contact, opened=now, clicked=now, replied=now)
    latest = await score_company_rule_based(db_session, sample_company)

    since = datetime.now(timezone.utc) - timedelta(days=7)
    result = await score_change_since(db_session, sample_company.id, since)
    assert result["current"] == latest.fit_score
    assert result["baseline"] == baseline.fit_score
    assert result["delta"] == latest.fit_score - baseline.fit_score
