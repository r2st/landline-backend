"""Step cadence: what "wait N" actually means, and where the follow-up lands.

``services.step_delay`` is the one resolver every scheduler goes through, so the
properties worth pinning are the ones that used to differ between the five
inline copies of this arithmetic:

  * a day-granular step keeps its exact current schedule, adaptive clamp and all
    — this is the compatibility guarantee that let sub-day delays ship at all;
  * hours are real hours, not rounded up to a day;
  * business-day counting skips the weekend, so a stated cadence is the
    delivered one;
  * jitter is *stable* — recomputing a due date never moves it, or a step would
    flicker in and out of "due" between polls of the queue sweep.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services import step_delay
from app.services.adaptive_delay import MAX_DELAY_DAYS, effective_delay_days


class _Step:
    """A step stub. ``step_delay`` reads its inputs with ``getattr`` defaults so
    it can serve ORM rows, rows loaded before migration 104, and these."""

    def __init__(self, **kw):
        self.step_number = kw.pop("step_number", 2)
        self.delay_days = kw.pop("delay_days", 0)
        self.delay_hours = kw.pop("delay_hours", 0)
        self.delay_business_days = kw.pop("delay_business_days", False)
        self.delay_jitter_minutes = kw.pop("delay_jitter_minutes", 0)
        assert not kw, f"unexpected: {kw}"


# A Wednesday, so "+2 days" stays inside one week unless a test wants otherwise.
WED = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)


# ── Base delay ────────────────────────────────────────────────────────────────

def test_base_delay_sums_days_and_hours():
    assert step_delay.base_delay(_Step(delay_days=2, delay_hours=4)) == timedelta(days=2, hours=4)


def test_base_delay_of_an_immediate_step_is_zero():
    assert step_delay.base_delay(_Step()) == timedelta(0)


def test_base_delay_tolerates_a_step_from_before_the_columns_existed():
    """A row loaded without the 104 columns is a day-granular step, not a crash."""

    class _Legacy:
        step_number = 1
        delay_days = 3

    assert step_delay.base_delay(_Legacy()) == timedelta(days=3)


def test_negative_and_null_delays_read_as_zero():
    assert step_delay.base_delay(_Step(delay_days=-5, delay_hours=-1)) == timedelta(0)
    s = _Step()
    s.delay_days = None
    s.delay_hours = None
    assert step_delay.base_delay(s) == timedelta(0)


# ── The compatibility guarantee ───────────────────────────────────────────────

@pytest.mark.parametrize("days", [1, 2, 3, 5, 7, 14, 30])
@pytest.mark.parametrize("factor", [0.25, 0.5, 1.0, 1.5, 3.0])
def test_day_granular_steps_keep_their_exact_existing_schedule(days, factor):
    """No ``delay_hours`` → the old whole-day arithmetic, unchanged.

    This is the guarantee the whole change rests on: every sequence already in
    the database must land on exactly the timestamp it would have landed on
    before. Anything else silently reschedules live outreach.
    """
    step = _Step(delay_days=days)
    assert step_delay.effective_delay(step, factor) == timedelta(
        days=effective_delay_days(days, factor)
    )
    assert step_delay.due_at(step, WED, factor=factor) == WED + timedelta(
        days=effective_delay_days(days, factor)
    )


def test_an_immediate_step_stays_immediate_under_any_factor():
    """The factor reshapes real waits; it must not invent one for a "send now"
    step, or a same-moment touch would drift by whatever the contact did last."""
    step = _Step(delay_days=0)
    for factor in (0.25, 1.0, 3.0):
        assert step_delay.due_at(step, WED, factor=factor) == WED


# ── Sub-day delays ────────────────────────────────────────────────────────────

def test_hours_are_not_rounded_up_to_a_day():
    """The point of the feature. The day-granular clamp has a 1-day floor, which
    would turn a deliberate 4-hour nudge into a next-day follow-up."""
    assert step_delay.due_at(_Step(delay_hours=4), WED) == WED + timedelta(hours=4)


def test_days_and_hours_compose():
    assert step_delay.due_at(_Step(delay_days=1, delay_hours=6), WED) == WED + timedelta(
        days=1, hours=6
    )


def test_the_adaptive_factor_scales_a_sub_day_delay_proportionally():
    step = _Step(delay_hours=8)
    assert step_delay.due_at(step, WED, factor=0.5) == WED + timedelta(hours=4)
    assert step_delay.due_at(step, WED, factor=1.5) == WED + timedelta(hours=12)


def test_a_shortened_sub_day_delay_is_floored_not_collapsed():
    """A tiny delay times a small factor must not become a burst."""
    delay = step_delay.effective_delay(_Step(delay_hours=1), 0.05)
    assert delay == step_delay.MIN_SUBDAY_DELAY


def test_an_extended_sub_day_delay_is_capped_at_the_day_ceiling():
    delay = step_delay.effective_delay(_Step(delay_days=20, delay_hours=1), 3.0)
    assert delay == timedelta(days=MAX_DELAY_DAYS)


# ── Business days ─────────────────────────────────────────────────────────────

def test_business_days_skip_the_weekend():
    """Thursday + 3 business days is Tuesday, not Sunday.

    Sunday is the answer plain arithmetic gives, and the send window then holds
    it to Monday — which is also where a Friday +2 lands. Two different intended
    spacings, one delivered one.
    """
    thursday = datetime(2026, 8, 6, 14, 0, tzinfo=timezone.utc)
    due = step_delay.due_at(_Step(delay_days=3, delay_business_days=True), thursday)
    assert due.weekday() == 1  # Tuesday
    assert due.date() == datetime(2026, 8, 11).date()


def test_business_and_calendar_days_diverge_across_a_weekend():
    thursday = datetime(2026, 8, 6, 14, 0, tzinfo=timezone.utc)
    calendar = step_delay.due_at(_Step(delay_days=3), thursday)
    business = step_delay.due_at(_Step(delay_days=3, delay_business_days=True), thursday)
    assert business - calendar == timedelta(days=2)


def test_business_days_agree_with_calendar_days_inside_one_week():
    """Monday + 2 crosses no weekend, so the two must not differ — business-day
    counting has to be a no-op whenever the weekend isn't in the way."""
    monday = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    assert step_delay.due_at(_Step(delay_days=2, delay_business_days=True), monday) == \
        step_delay.due_at(_Step(delay_days=2), monday)


def test_business_days_from_a_weekend_anchor_land_on_a_weekday():
    saturday = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
    due = step_delay.due_at(_Step(delay_days=1, delay_business_days=True), saturday)
    assert due.weekday() < 5


def test_hours_are_added_after_the_business_day_hop_and_do_not_skip_anything():
    """"2 business days + 4 hours" from Friday: the days skip the weekend, the
    hours don't, because four hours means four hours."""
    friday = datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc)
    due = step_delay.due_at(
        _Step(delay_days=2, delay_hours=4, delay_business_days=True), friday
    )
    assert due.weekday() == 1  # Tuesday
    assert due - datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc) == timedelta(hours=4)


def test_business_day_counting_is_off_by_default():
    assert step_delay.due_at(_Step(delay_days=3), WED) == WED + timedelta(days=3)


# ── Jitter ────────────────────────────────────────────────────────────────────

def test_jitter_is_stable_across_recomputation():
    """The correctness property, not a nicety.

    The queue sweep recomputes the due date on every pass for enrollments with
    no ``next_send_at``. A freshly-drawn random offset would move the deadline
    each time it was checked, so a step could read due at 10:00, not due at
    10:05, and due again at 10:10.
    """
    step = _Step(delay_days=1, delay_jitter_minutes=90)
    enrollment_id = uuid.uuid4()
    first = step_delay.due_at(step, WED, enrollment_id=enrollment_id)
    for _ in range(20):
        assert step_delay.due_at(step, WED, enrollment_id=enrollment_id) == first


def test_jitter_spreads_a_batch_across_the_window():
    step = _Step(delay_days=1, delay_jitter_minutes=120)
    due = {
        step_delay.due_at(step, WED, enrollment_id=uuid.uuid4()) for _ in range(60)
    }
    assert len(due) > 30, "a batch enrolled together should not come due in one block"


def test_jitter_stays_inside_its_window_and_never_pulls_a_send_forward():
    step = _Step(delay_days=1, delay_jitter_minutes=45)
    base = WED + timedelta(days=1)
    for _ in range(100):
        due = step_delay.due_at(step, WED, enrollment_id=uuid.uuid4())
        assert base <= due < base + timedelta(minutes=45)


def test_jitter_differs_per_step_for_one_enrollment():
    """Otherwise every step of an enrollment carries the same offset, which is a
    per-enrollment constant rather than a spread."""
    enrollment_id = uuid.uuid4()
    offsets = {
        step_delay.jitter_for(
            _Step(step_number=n, delay_jitter_minutes=120), enrollment_id
        )
        for n in range(1, 8)
    }
    assert len(offsets) > 1


def test_no_jitter_without_an_enrollment_to_key_it_on():
    """A preview with no enrollment gets the unjittered time rather than a
    random one it can't reproduce."""
    step = _Step(delay_days=1, delay_jitter_minutes=120)
    assert step_delay.due_at(step, WED) == WED + timedelta(days=1)


def test_jitter_is_capped():
    step = _Step(delay_days=1, delay_jitter_minutes=99999)
    for _ in range(50):
        offset = step_delay.jitter_for(step, uuid.uuid4())
        assert offset < timedelta(minutes=step_delay.MAX_JITTER_MINUTES)


def test_jitter_off_by_default():
    assert step_delay.jitter_for(_Step(delay_days=1), uuid.uuid4()) == timedelta(0)


# ── is_due ────────────────────────────────────────────────────────────────────

def test_is_due_is_false_before_and_true_at_the_due_moment():
    step = _Step(delay_days=2)
    due_now, due = step_delay.is_due(step, WED, now=WED + timedelta(days=1))
    assert not due_now
    assert due == WED + timedelta(days=2)

    due_now, _ = step_delay.is_due(step, WED, now=WED + timedelta(days=2))
    assert due_now


def test_is_due_agrees_with_due_at_under_every_option():
    """The sweep asks ``is_due`` and the executor asks ``due_at``; if they ever
    disagreed an enrollment would be dispatched and immediately deferred."""
    step = _Step(delay_days=2, delay_hours=3, delay_business_days=True, delay_jitter_minutes=60)
    enrollment_id = uuid.uuid4()
    due = step_delay.due_at(step, WED, factor=1.5, enrollment_id=enrollment_id)
    _, from_is_due = step_delay.is_due(
        step, WED, now=WED, factor=1.5, enrollment_id=enrollment_id
    )
    assert due == from_is_due


# ── Naive anchors ─────────────────────────────────────────────────────────────

def test_a_naive_anchor_reads_as_utc():
    """SQLite hands back naive datetimes; scheduling must not raise on them."""
    naive = datetime(2026, 8, 5, 14, 0)
    assert step_delay.due_at(_Step(delay_days=1), naive) == WED + timedelta(days=1)


def test_the_returned_due_date_is_always_aware():
    for step in (
        _Step(delay_days=1),
        _Step(delay_hours=5),
        _Step(delay_days=2, delay_business_days=True),
    ):
        assert step_delay.due_at(step, datetime(2026, 8, 5, 14, 0)).tzinfo is not None


# ── describe ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kw,expected", [
    ({}, "immediately"),
    ({"delay_days": 1}, "1 day"),
    ({"delay_days": 3}, "3 days"),
    ({"delay_hours": 4}, "4 hours"),
    ({"delay_hours": 1}, "1 hour"),
    ({"delay_days": 2, "delay_hours": 6}, "2 days and 6 hours"),
    ({"delay_days": 3, "delay_business_days": True}, "3 business days"),
    ({"delay_days": 1, "delay_jitter_minutes": 30}, "1 day (±30m spread)"),
])
def test_describe(kw, expected):
    assert step_delay.describe(_Step(**kw)) == expected


def test_describe_reports_the_capped_jitter_not_the_configured_one():
    assert "±720m" in step_delay.describe(_Step(delay_days=1, delay_jitter_minutes=5000))


# ── Timezone fallback ─────────────────────────────────────────────────────────

def test_an_unresolvable_send_window_timezone_degrades_to_utc(monkeypatch):
    """A bad tz name must not take scheduling down — a weekend boundary a few
    hours off is a far smaller failure than a scheduler that raises."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "send_window_timezone", "Not/AZone", raising=False)
    thursday = datetime(2026, 8, 6, 14, 0, tzinfo=timezone.utc)
    due = step_delay.due_at(_Step(delay_days=3, delay_business_days=True), thursday)
    assert due.date() == datetime(2026, 8, 11).date()
