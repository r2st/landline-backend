"""Tests for Feature 6 — Smart Send-Time Optimization (Dialtone).

Covers the pillars the spec calls out — timezone resolution chain precedence,
optimal-window intersection with the business-hours window, the
``max_smart_delay`` cap, and cohort/best-practice fallback when data is thin —
plus profile bucketing, deterministic A/B arm assignment, the ``should_defer``
scheduling gate, the nightly refresh, and the three Console endpoints.

Every test is ``async`` (even the pure-unit ones) to stay consistent with the
rest of the suite's all-async files: a file that runs synchronous tests ahead of
the async DB tests leaves the session-scoped event loop in a state that breaks
the async engine, so we never mix the two.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.contact_engagement import ContactEngagementProfile
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services.send_time_optimizer import (
    DEFAULT_TZ,
    SendTimeOptimizer,
    dow_sunday0,
    get_send_time_optimizer,
)

UTC = timezone.utc


# The suite's async engine fixtures are function-scoped over a session-scoped
# event loop; a test that never instantiates the engine before a later test that
# does leaves the shared loop in a state that breaks the engine. Force every test
# in this file (including the pure-unit ones) to spin up the engine, matching the
# all-DB files that run cleanly.
@pytest_asyncio.fixture(autouse=True)
async def _engine_per_test(db_engine):
    yield


# ─── Helpers ─────────────────────────────────────────────────────────────────


class _Ev:
    """Lightweight stand-in for an OutreachEvent (event_type + occurred_at)."""

    def __init__(self, event_type, occurred_at):
        self.event_type = event_type
        self.occurred_at = occurred_at


def _contact(**over) -> Contact:
    base = dict(
        id=_uuid.uuid4(),
        company_id=_uuid.uuid4(),
        email="john@acme.com",
        timezone=None,
        state=None,
        country=None,
    )
    base.update(over)
    return Contact(**base)


def _company(**over) -> Company:
    base = dict(
        id=_uuid.uuid4(),
        name="Acme",
        source="test",
        country="US",
        state_province=None,
    )
    base.update(over)
    return Company(**base)


def _next_weekday(target_py_weekday: int, hour: int) -> datetime:
    """A concrete UTC datetime on the next given Python weekday at ``hour``."""
    d = datetime(2026, 7, 14, hour, 0, 0, tzinfo=UTC)
    while d.weekday() != target_py_weekday:
        d += timedelta(days=1)
    return d


async def _make_enrollment(db, contact, *, timing_arm=None, status="active") -> OutreachEnrollment:
    seq = OutreachSequence(id=_uuid.uuid4(), name=f"seq-{_uuid.uuid4().hex[:6]}", status="active")
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        status=status,
        current_step=2,
        timing_arm=timing_arm,
    )
    db.add(enr)
    await db.flush()
    return enr


# ─── Timezone resolution chain precedence ────────────────────────────────────


@pytest.mark.asyncio
async def test_tz_resolution_explicit_wins():
    opt = SendTimeOptimizer()
    c = _contact(timezone="America/Denver", state="CA", country="US")
    tz, source = opt.resolve_timezone(c, _company(state_province="NY", country="US"))
    assert tz == "America/Denver"
    assert source == "explicit"


@pytest.mark.asyncio
async def test_tz_resolution_contact_geo():
    opt = SendTimeOptimizer()
    c = _contact(state="CA", country="US")
    tz, source = opt.resolve_timezone(c, _company(state_province="NY", country="US"))
    assert tz == "America/Los_Angeles"
    assert source == "contact_geo"


@pytest.mark.asyncio
async def test_tz_resolution_contact_geo_full_state_name():
    opt = SendTimeOptimizer()
    c = _contact(state="Texas", country="United States")
    tz, source = opt.resolve_timezone(c, None)
    assert tz == "America/Chicago"
    assert source == "contact_geo"


@pytest.mark.asyncio
async def test_tz_resolution_company_geo():
    opt = SendTimeOptimizer()
    c = _contact()  # no explicit tz, no geo
    tz, source = opt.resolve_timezone(c, _company(state_province="ON", country="CA"))
    assert tz == "America/Toronto"
    assert source == "company_geo"


@pytest.mark.asyncio
async def test_tz_resolution_domain_cctld():
    opt = SendTimeOptimizer()
    c = _contact(email="jane@example.co.uk")
    tz, source = opt.resolve_timezone(c, _company())  # company has no geo
    assert tz == "Europe/London"
    assert source == "domain"


@pytest.mark.asyncio
async def test_tz_resolution_default_fallback():
    opt = SendTimeOptimizer()
    c = _contact(email="jane@example.com")  # .com → no ccTLD hint
    tz, source = opt.resolve_timezone(c, None)
    assert tz == DEFAULT_TZ
    assert source == "default"


# ─── Optimal-window intersection with business hours ─────────────────────────


@pytest.mark.asyncio
async def test_window_personal_clamped_to_business_end():
    opt = SendTimeOptimizer()
    # Personal best at 17:00, 2h window → [17,19); business window ends at 18.
    w = opt.compute_optimal_window(tz="UTC", best_dow=2, best_hour=17, opens_total=10)
    assert w["source"] == "personal"
    assert w["hour_start"] == 17
    assert w["hour_end"] == 18  # clamped to business end
    assert w["hour_start"] >= opt.settings.send_window_start_hour
    assert w["hour_end"] <= opt.settings.send_window_end_hour


@pytest.mark.asyncio
async def test_window_personal_before_business_falls_back():
    opt = SendTimeOptimizer()
    # Personal best at 06:00 → [6,8) is before the 08:00 business start → fall
    # back to the default window, still within business hours.
    w = opt.compute_optimal_window(tz="UTC", best_dow=2, best_hour=6, opens_total=10)
    assert w["source"] == "default"
    assert w["hour_start"] >= opt.settings.send_window_start_hour
    assert w["hour_end"] <= opt.settings.send_window_end_hour
    assert w["hour_start"] < w["hour_end"]


# ─── Cohort / best-practice fallback when data is thin ───────────────────────


@pytest.mark.asyncio
async def test_window_cohort_fallback_thin_data():
    opt = SendTimeOptimizer()
    # Below smart_timing_min_opens (default 3) → blend to cohort default.
    w = opt.compute_optimal_window(tz="UTC", best_dow=5, best_hour=15, opens_total=1)
    assert w["source"] == "cohort"
    assert w["hour_start"] == opt.settings.smart_timing_default_hour_start  # 9
    assert w["hour_end"] == opt.settings.smart_timing_default_hour_end      # 11
    assert w["dow_preference"] == sorted(opt.settings.smart_timing_default_dows)  # Tue-Thu


@pytest.mark.asyncio
async def test_window_best_practice_default_no_data():
    opt = SendTimeOptimizer()
    w = opt.compute_optimal_window(tz="UTC", best_dow=None, best_hour=None, opens_total=0)
    assert w["source"] == "default"
    assert (w["hour_start"], w["hour_end"]) == (9, 11)
    assert w["dow_preference"] == [2, 3, 4]  # Tue, Wed, Thu (Sunday=0)
    assert w["confidence"] == 0.0


# ─── next_send_slot: send-now, defer, and the max_smart_delay cap ────────────


@pytest.mark.asyncio
async def test_next_send_slot_sends_now_when_in_window():
    opt = SendTimeOptimizer()
    now = _next_weekday(1, 10)  # a Tuesday 10:00 UTC
    window = {"dow_preference": [dow_sunday0(now)], "hour_start": 9, "hour_end": 11}
    slot = opt.next_send_slot(tz="UTC", window=window, due_at=now, now=now)
    assert slot == now  # already inside the window → send now


@pytest.mark.asyncio
async def test_next_send_slot_defers_to_window_opening():
    opt = SendTimeOptimizer()
    now = _next_weekday(1, 6)  # a Tuesday 06:00 UTC, before the 9-11 window
    window = {"dow_preference": [dow_sunday0(now)], "hour_start": 9, "hour_end": 11}
    slot = opt.next_send_slot(tz="UTC", window=window, due_at=now, now=now)
    assert slot > now
    assert slot.hour == 9
    assert slot.date() == now.date()  # same day, later


@pytest.mark.asyncio
async def test_max_smart_delay_cap():
    opt = SendTimeOptimizer()
    now = _next_weekday(1, 12)  # a Tuesday
    # Aim at a weekday exactly 3 days out → next occurrence is > 48h away, so the
    # slot must be capped at now + max_smart_delay, never further.
    far_dow = (dow_sunday0(now) + 3) % 7
    window = {"dow_preference": [far_dow], "hour_start": 9, "hour_end": 11}
    slot = opt.next_send_slot(tz="UTC", window=window, due_at=now, now=now)
    cap = now + timedelta(hours=opt.settings.max_smart_delay_hours)
    assert slot == cap
    assert (slot - now) <= timedelta(hours=opt.settings.max_smart_delay_hours)


@pytest.mark.asyncio
async def test_next_send_slot_no_valid_window_returns_cap():
    opt = SendTimeOptimizer()
    now = _next_weekday(1, 12)
    window = {"dow_preference": [], "hour_start": 9, "hour_end": 11}
    slot = opt.next_send_slot(tz="UTC", window=window, due_at=now, now=now)
    assert slot == now + timedelta(hours=opt.settings.max_smart_delay_hours)


# ─── A/B arm assignment ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assign_arm_is_deterministic():
    opt = SendTimeOptimizer()
    eid = _uuid.uuid4()
    assert opt.assign_arm(eid) == opt.assign_arm(eid)
    assert opt.assign_arm(eid) in ("optimized", "control")


@pytest.mark.asyncio
async def test_assign_arm_produces_both_arms():
    opt = SendTimeOptimizer()
    arms = {opt.assign_arm(_uuid.uuid4()) for _ in range(200)}
    assert arms == {"optimized", "control"}


# ─── Engagement bucketing ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_profile_fields_buckets_local_time():
    opt = SendTimeOptimizer()
    # 14:00 UTC → 10:00 America/New_York (EDT). Two opens + one click land there.
    tue_1400 = _next_weekday(1, 14)  # Tuesday 14:00 UTC
    events = [
        _Ev("sent", tue_1400),
        _Ev("opened", tue_1400),
        _Ev("opened", tue_1400 + timedelta(days=7)),  # next Tuesday, same local hour
        _Ev("clicked", tue_1400),
    ]
    fields = opt.build_profile_fields(events, "America/New_York")
    assert fields["opens_total"] == 3  # 2 opens + 1 click
    assert fields["best_hour"] == 10   # 14 UTC → 10 EDT
    local = tue_1400.astimezone(ZoneInfo("America/New_York"))
    assert fields["best_dow"] == dow_sunday0(local)
    assert fields["confidence"] > 0


@pytest.mark.asyncio
async def test_build_profile_fields_no_opens_zero_confidence():
    opt = SendTimeOptimizer()
    fields = opt.build_profile_fields([_Ev("sent", _next_weekday(1, 14))], "UTC")
    assert fields["opens_total"] == 0
    assert fields["best_hour"] is None
    assert fields["confidence"] == 0.0


# ─── should_defer scheduling gate (DB) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_should_defer_control_arm_sends_now(
    db_session: AsyncSession, sample_company, sample_contact
):
    opt = get_send_time_optimizer()
    enr = await _make_enrollment(db_session, sample_contact, timing_arm="control")
    now = _next_weekday(1, 6)
    deferred = await opt.should_defer(db_session, enr, sample_contact, now, now)
    assert deferred is False
    assert enr.scheduled_send_at is None  # control never gets a smart schedule


@pytest.mark.asyncio
async def test_should_defer_optimized_defers_outside_window(
    db_session: AsyncSession, sample_company, sample_contact
):
    opt = get_send_time_optimizer()
    sample_contact.timezone = "UTC"  # explicit → deterministic window (Tue-Thu 9-11)
    enr = await _make_enrollment(db_session, sample_contact, timing_arm="optimized")
    now = _next_weekday(1, 6)  # Tuesday 06:00 UTC — before the 9-11 window
    deferred = await opt.should_defer(db_session, enr, sample_contact, now, now)
    assert deferred is True
    assert enr.scheduled_send_at is not None
    sched = enr.scheduled_send_at
    if sched.tzinfo is None:
        sched = sched.replace(tzinfo=UTC)
    assert sched > now


@pytest.mark.asyncio
async def test_should_defer_optimized_sends_now_in_window(
    db_session: AsyncSession, sample_company, sample_contact
):
    opt = get_send_time_optimizer()
    sample_contact.timezone = "UTC"
    enr = await _make_enrollment(db_session, sample_contact, timing_arm="optimized")
    now = _next_weekday(1, 10)  # Tuesday 10:00 UTC — inside the 9-11 window
    deferred = await opt.should_defer(db_session, enr, sample_contact, now, now)
    assert deferred is False


@pytest.mark.asyncio
async def test_should_defer_assigns_arm_when_missing(
    db_session: AsyncSession, sample_company, sample_contact
):
    opt = get_send_time_optimizer()
    sample_contact.timezone = "UTC"
    enr = await _make_enrollment(db_session, sample_contact, timing_arm=None)
    now = _next_weekday(1, 10)
    await opt.should_defer(db_session, enr, sample_contact, now, now)
    assert enr.timing_arm in ("optimized", "control")


# ─── Nightly refresh ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_profiles_computes_and_backfills_tz(
    db_session: AsyncSession, sample_company
):
    opt = get_send_time_optimizer()
    # Contact in California, no explicit tz → should backfill to Pacific.
    contact = Contact(
        id=_uuid.uuid4(),
        company_id=sample_company.id,
        email="pat@acme.com",
        state="CA",
        country="US",
        source="test",
    )
    db_session.add(contact)
    await db_session.flush()
    enr = await _make_enrollment(db_session, contact)

    opened = _next_weekday(1, 17)  # 17:00 UTC → 10:00 Pacific
    db_session.add_all([
        OutreachEvent(id=_uuid.uuid4(), enrollment_id=enr.id, step_number=1,
                      event_type="sent", occurred_at=opened),
        OutreachEvent(id=_uuid.uuid4(), enrollment_id=enr.id, step_number=1,
                      event_type="opened", occurred_at=opened),
    ])
    await db_session.flush()

    report = await opt.refresh_profiles(db_session)
    await db_session.flush()

    assert report["profiles_updated"] >= 1
    assert report["timezones_backfilled"] >= 1
    await db_session.refresh(contact)
    assert contact.timezone == "America/Los_Angeles"

    profile = (
        await db_session.execute(
            select(ContactEngagementProfile).where(
                ContactEngagementProfile.contact_id == contact.id
            )
        )
    ).scalar_one_or_none()
    assert profile is not None
    assert profile.opens_total == 1
    assert profile.best_hour == 10  # 17 UTC → 10 PDT
    assert profile.tz == "America/Los_Angeles"


# ─── Console endpoints ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_optimal_endpoint(client: AsyncClient):
    resp = await client.get("/api/v1/dialtone/send-timing/optimal")
    assert resp.status_code == 200
    assert "has_data" in resp.json()


@pytest.mark.asyncio
async def test_contact_send_timing_endpoint(
    client: AsyncClient, db_session: AsyncSession, sample_company, sample_contact
):
    resp = await client.get(f"/api/v1/dialtone/send-timing/contacts/{sample_contact.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["contact_id"] == str(sample_contact.id)
    # sample_company is in OH/US → Eastern via company geo.
    assert body["timezone"] == "America/New_York"
    assert body["window"]["hour_start"] < body["window"]["hour_end"]
    assert body["next_send_at"] is not None


@pytest.mark.asyncio
async def test_contact_send_timing_endpoint_404(client: AsyncClient):
    resp = await client.get(f"/api/v1/dialtone/send-timing/contacts/{_uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_experiment_endpoint(
    client: AsyncClient, db_session: AsyncSession, sample_company, sample_contact
):
    # One optimized + one control enrollment, each with a sent + open event.
    for arm in ("optimized", "control"):
        enr = await _make_enrollment(db_session, sample_contact, timing_arm=arm)
        db_session.add_all([
            OutreachEvent(id=_uuid.uuid4(), enrollment_id=enr.id, step_number=1,
                          event_type="sent", occurred_at=datetime.now(UTC)),
            OutreachEvent(id=_uuid.uuid4(), enrollment_id=enr.id, step_number=1,
                          event_type="opened", occurred_at=datetime.now(UTC)),
        ])
    await db_session.flush()

    resp = await client.get("/api/v1/dialtone/send-timing/experiment")
    assert resp.status_code == 200
    body = resp.json()
    assert {a["arm"] for a in body["arms"]} == {"optimized", "control"}
    for a in body["arms"]:
        assert a["sent"] == 1
        assert a["opens"] == 1
        assert a["open_rate"] == 1.0
    assert "confidence_target" in body
