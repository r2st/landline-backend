"""Tests for outreach queue prioritization.

The defect these pin: ``process_outreach_queue`` selected active enrollments with
no ``ORDER BY`` and then spent a finite daily quota walking that list. Capacity
went to whichever rows the planner returned first — and because a sequential scan
is stable, the *same* rows lost every day. Not a fairness nit: a contact past the
cap line was never emailed at all.

Two properties are load-bearing and both are tested here:

1. Value ordering — engagement, invested touches, fit and live buyer intent
   decide who goes first.
2. **Waiting always wins eventually.** Value is capped; wait pressure is not. A
   worthless enrollment overdue long enough outranks a perfect one due now,
   which is the only thing standing between "ranked queue" and "starvation with
   a rationale".
"""

import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.intent_signal import IntentSignal
from app.models.lead_score import LeadScore
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services import queue_priority

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 2, 15, 0, tzinfo=timezone.utc)


# ─── Fixtures / builders ─────────────────────────────────────────────────────


async def _sequence(db: AsyncSession, *, delay_days: int = 3) -> OutreachSequence:
    seq = OutreachSequence(id=_uuid.uuid4(), name="CNC — Deal Cracker", is_active=True)
    db.add(seq)
    await db.flush()
    db.add(
        SequenceStep(
            id=_uuid.uuid4(),
            sequence_id=seq.id,
            step_number=1,
            delay_days=delay_days,
            subject_template="s",
            body_template="b",
        )
    )
    await db.flush()
    return seq


async def _company(db: AsyncSession, name: str) -> Company:
    company = Company(
        id=_uuid.uuid4(), name=name, pipeline_stage="outreach", source="test"
    )
    db.add(company)
    await db.flush()
    return company


async def _contact(db: AsyncSession, company: Company, email: str) -> Contact:
    contact = Contact(
        id=_uuid.uuid4(),
        company_id=company.id,
        first_name="Dale",
        email=email,
        source="test",
    )
    db.add(contact)
    await db.flush()
    return contact


async def _enrollment(
    db: AsyncSession,
    seq: OutreachSequence,
    contact: Contact,
    *,
    due_at: datetime | None = None,
    enrolled_at: datetime | None = None,
    current_step: int = 1,
) -> OutreachEnrollment:
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        current_step=current_step,
        status="active",
        enrolled_at=enrolled_at or (NOW - timedelta(days=1)),
        next_send_at=due_at,
    )
    db.add(enrollment)
    await db.flush()
    return enrollment


async def _event(
    db: AsyncSession,
    enrollment: OutreachEnrollment,
    event_type: str,
    *,
    step: int = 1,
    at: datetime | None = None,
) -> None:
    db.add(
        OutreachEvent(
            id=_uuid.uuid4(),
            enrollment_id=enrollment.id,
            step_number=step,
            event_type=event_type,
            occurred_at=at or (NOW - timedelta(days=1)),
        )
    )
    await db.flush()


# ─── Constants ───────────────────────────────────────────────────────────────


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_value_ceiling_matches_its_components():
    """MAX_VALUE_POINTS is the sum of every bounded term, not a guessed number.

    If it drifts, the anti-starvation crossing point silently moves and the
    module's central claim stops being true.
    """
    assert queue_priority.MAX_VALUE_POINTS == (
        queue_priority.CLICK_POINTS
        + queue_priority.MAX_STEP_POINTS
        + queue_priority.MAX_FIT_POINTS
        + queue_priority.MAX_INTENT_POINTS
    )
    # A click must outrank a mere open, or "strongest signal" means nothing.
    assert queue_priority.CLICK_POINTS > queue_priority.OPEN_POINTS
    # And the crossing point has to be reachable — weeks, not years.
    assert 5 < queue_priority.wait_breakeven_days() < 60


# ─── Value ordering ──────────────────────────────────────────────────────────


async def test_engagement_outranks_a_cold_first_touch(db_session: AsyncSession):
    seq = await _sequence(db_session)
    company = await _company(db_session, "Precision Molding")
    cold = await _enrollment(
        db_session, seq, await _contact(db_session, company, "info@cold.com"),
        due_at=NOW - timedelta(hours=1),
    )
    warm = await _enrollment(
        db_session, seq, await _contact(db_session, company, "dale@warm.com"),
        due_at=NOW - timedelta(hours=1),
    )
    await _event(db_session, warm, "clicked")
    await db_session.commit()

    ordered = await queue_priority.rank(db_session, [cold, warm], now=NOW)
    assert [e.id for e in ordered] == [warm.id, cold.id]


async def test_click_and_open_together_count_once(db_session: AsyncSession):
    """A contact who clicked also opened. Summing would rate one interested
    person above two, so only the strongest signal scores."""
    seq = await _sequence(db_session)
    company = await _company(db_session, "Precision Molding")
    enrollment = await _enrollment(
        db_session, seq, await _contact(db_session, company, "dale@warm.com"),
        due_at=NOW,
    )
    await _event(db_session, enrollment, "opened")
    await _event(db_session, enrollment, "clicked")
    await db_session.commit()

    scores = await queue_priority.score_enrollments(db_session, [enrollment], now=NOW)
    parts = scores[enrollment.id].parts
    assert parts.get("engaged_clicked") == queue_priority.CLICK_POINTS
    assert "engaged_opened" not in parts


async def test_in_flight_touches_score_and_cap(db_session: AsyncSession):
    """Sends already spent on a thread count, but cannot grow without bound —
    a long sequence must not outrank live engagement on length alone."""
    seq = await _sequence(db_session)
    company = await _company(db_session, "Precision Molding")
    enrollment = await _enrollment(
        db_session, seq, await _contact(db_session, company, "dale@deep.com"),
        due_at=NOW, current_step=9,
    )
    for step in range(1, 9):
        await _event(db_session, enrollment, "sent", step=step)
    await db_session.commit()

    scores = await queue_priority.score_enrollments(db_session, [enrollment], now=NOW)
    assert scores[enrollment.id].parts["in_flight"] == queue_priority.MAX_STEP_POINTS


async def test_live_intent_signal_lifts_a_company(db_session: AsyncSession):
    seq = await _sequence(db_session)
    quiet = await _company(db_session, "Quiet Machining")
    hiring = await _company(db_session, "Hiring Machining")
    db_session.add(
        IntentSignal(
            id=_uuid.uuid4(),
            company_id=hiring.id,
            signal_type="hiring",
            title="Hiring a Quality Engineer",
            relevance_score=90,
            urgency_score=80,
            detected_at=NOW - timedelta(days=2),
            expires_at=NOW + timedelta(days=60),
        )
    )
    a = await _enrollment(
        db_session, seq, await _contact(db_session, quiet, "a@quiet.com"), due_at=NOW
    )
    b = await _enrollment(
        db_session, seq, await _contact(db_session, hiring, "b@hiring.com"), due_at=NOW
    )
    await db_session.commit()

    ordered = await queue_priority.rank(db_session, [a, b], now=NOW)
    assert [e.id for e in ordered] == [b.id, a.id]


async def test_expired_intent_signal_scores_nothing(db_session: AsyncSession):
    """``expires_at`` is the buying window an analyst assigned. Past it the
    signal is history, and treating it as intent is how a six-month-old job
    posting keeps a dead lead at the front of the queue."""
    seq = await _sequence(db_session)
    company = await _company(db_session, "Stale Machining")
    db_session.add(
        IntentSignal(
            id=_uuid.uuid4(),
            company_id=company.id,
            signal_type="hiring",
            title="Hired a Quality Engineer, last winter",
            relevance_score=95,
            urgency_score=95,
            detected_at=NOW - timedelta(days=200),
            expires_at=NOW - timedelta(days=1),
        )
    )
    enrollment = await _enrollment(
        db_session, seq, await _contact(db_session, company, "a@stale.com"), due_at=NOW
    )
    await db_session.commit()

    scores = await queue_priority.score_enrollments(db_session, [enrollment], now=NOW)
    assert "intent" not in scores[enrollment.id].parts


async def test_fit_score_orders_two_otherwise_identical_leads(db_session: AsyncSession):
    seq = await _sequence(db_session)
    poor = await _company(db_session, "Poor Fit Co")
    good = await _company(db_session, "Good Fit Co")
    for company, fit in ((poor, 20), (good, 95)):
        db_session.add(
            LeadScore(
                id=_uuid.uuid4(),
                company_id=company.id,
                score_type="rule_based",
                fit_score=fit,
            )
        )
    a = await _enrollment(
        db_session, seq, await _contact(db_session, poor, "a@poor.com"), due_at=NOW
    )
    b = await _enrollment(
        db_session, seq, await _contact(db_session, good, "b@good.com"), due_at=NOW
    )
    await db_session.commit()

    ordered = await queue_priority.rank(db_session, [a, b], now=NOW)
    assert [e.id for e in ordered] == [b.id, a.id]


# ─── The anti-starvation property ────────────────────────────────────────────


async def test_long_wait_eventually_beats_maximum_value(db_session: AsyncSession):
    """The property the module exists for.

    ``best`` has everything: a click, a full sequence behind it, a perfect fit
    score and a red-hot intent signal. ``forgotten`` has nothing but time. Past
    the crossing point, time wins — otherwise the low-value rows lose every
    single day forever, which is the exact bug a ranked queue was supposed to
    fix rather than formalize.
    """
    seq = await _sequence(db_session)
    hot = await _company(db_session, "Hot Prospect Inc")
    nobody = await _company(db_session, "Nobody Machining")
    db_session.add(
        LeadScore(
            id=_uuid.uuid4(), company_id=hot.id, score_type="ai", fit_score=100
        )
    )
    db_session.add(
        IntentSignal(
            id=_uuid.uuid4(),
            company_id=hot.id,
            signal_type="funding",
            title="Series B",
            relevance_score=100,
            urgency_score=100,
            detected_at=NOW,
            expires_at=NOW + timedelta(days=90),
        )
    )
    best = await _enrollment(
        db_session, seq, await _contact(db_session, hot, "vp@hot.com"),
        due_at=NOW - timedelta(minutes=1), current_step=4,
    )
    for step in range(1, 4):
        await _event(db_session, best, "sent", step=step)
    await _event(db_session, best, "clicked", step=3)

    overdue_days = queue_priority.wait_breakeven_days() + 2
    forgotten = await _enrollment(
        db_session, seq, await _contact(db_session, nobody, "info@nobody.com"),
        due_at=NOW - timedelta(days=overdue_days),
        enrolled_at=NOW - timedelta(days=overdue_days + 1),
    )
    await db_session.commit()

    ordered = await queue_priority.rank(db_session, [best, forgotten], now=NOW)
    assert [e.id for e in ordered] == [forgotten.id, best.id]

    # ...but only *past* the crossing point. A day overdue must not be enough to
    # displace a hot lead, or the ranking is just FIFO wearing a scoring module.
    forgotten.next_send_at = NOW - timedelta(days=1)
    await db_session.commit()
    ordered = await queue_priority.rank(db_session, [best, forgotten], now=NOW)
    assert [e.id for e in ordered] == [best.id, forgotten.id]


async def test_waiting_falls_back_to_last_send_then_enrollment(db_session: AsyncSession):
    """With no ``next_send_at`` the wait anchors on the last send (legacy due-date
    path), or on the enrollment for an untouched first touch."""
    seq = await _sequence(db_session)
    company = await _company(db_session, "Legacy Co")
    legacy = await _enrollment(
        db_session, seq, await _contact(db_session, company, "a@legacy.com"),
        due_at=None, enrolled_at=NOW - timedelta(days=30), current_step=2,
    )
    await _event(db_session, legacy, "sent", at=NOW - timedelta(days=2))
    fresh = await _enrollment(
        db_session, seq, await _contact(db_session, company, "b@legacy.com"),
        due_at=None, enrolled_at=NOW - timedelta(days=10),
    )
    await db_session.commit()

    scores = await queue_priority.score_enrollments(
        db_session, [legacy, fresh], now=NOW
    )
    # Legacy waits from its last send (2 days), not its enrollment (30) — its
    # sequence was moving until recently.
    assert scores[legacy.id].parts["waiting"] == pytest.approx(
        queue_priority.WAIT_POINTS_PER_DAY * 2, abs=0.5
    )
    assert scores[fresh.id].parts["waiting"] == pytest.approx(
        queue_priority.WAIT_POINTS_PER_DAY * 10, abs=0.5
    )


# ─── Contract: rank never changes the set ────────────────────────────────────


async def test_rank_preserves_every_enrollment(db_session: AsyncSession):
    """Ordering only. Dropping one row here silently stops a contact's sequence,
    which is far worse than the unordered sweep this replaces."""
    seq = await _sequence(db_session)
    company = await _company(db_session, "Bulk Co")
    enrollments = []
    for i in range(25):
        contact = await _contact(db_session, company, f"c{i}@bulk.com")
        enrollments.append(
            await _enrollment(
                db_session, seq, contact, due_at=NOW - timedelta(hours=i)
            )
        )
    await db_session.commit()

    ordered = await queue_priority.rank(db_session, enrollments, now=NOW)
    assert len(ordered) == len(enrollments)
    assert {e.id for e in ordered} == {e.id for e in enrollments}


async def test_ties_break_deterministically(db_session: AsyncSession):
    """Two enrollments that genuinely tie must not swap between sweeps, or the
    pair takes turns losing the last slot and neither ever completes."""
    seq = await _sequence(db_session)
    company = await _company(db_session, "Tied Co")
    a = await _enrollment(
        db_session, seq, await _contact(db_session, company, "a@tied.com"),
        due_at=NOW, enrolled_at=NOW - timedelta(days=1),
    )
    b = await _enrollment(
        db_session, seq, await _contact(db_session, company, "b@tied.com"),
        due_at=NOW, enrolled_at=NOW - timedelta(days=1),
    )
    await db_session.commit()

    first = [e.id for e in await queue_priority.rank(db_session, [a, b], now=NOW)]
    second = [e.id for e in await queue_priority.rank(db_session, [b, a], now=NOW)]
    assert first == second


async def test_rank_falls_back_to_input_order_on_failure(
    db_session: AsyncSession, monkeypatch
):
    """An unordered sweep is the status quo. A sweep that raises is an outage."""
    seq = await _sequence(db_session)
    company = await _company(db_session, "Fragile Co")
    a = await _enrollment(
        db_session, seq, await _contact(db_session, company, "a@fragile.com"),
        due_at=NOW - timedelta(days=9),
    )
    b = await _enrollment(
        db_session, seq, await _contact(db_session, company, "b@fragile.com"),
        due_at=NOW,
    )
    await db_session.commit()

    async def _boom(*args, **kwargs):
        raise RuntimeError("lead_scores table is on fire")

    monkeypatch.setattr(queue_priority, "score_enrollments", _boom)
    ordered = await queue_priority.rank(db_session, [b, a], now=NOW)
    assert [e.id for e in ordered] == [b.id, a.id]


# ─── The sweep actually spends its quota on the ranked order ─────────────────


async def test_capped_sweep_spends_its_quota_on_the_best_enrollments(
    db_session: AsyncSession, monkeypatch
):
    """End to end: with room for one send and two enrollments due, the engaged
    one gets it. Against the unordered sweep this fails whenever the planner
    hands back the cold enrollment first — which it does, forever, once it does
    it once.
    """
    import app.tasks.outreach as outreach_tasks
    from app.services import system_settings
    from app.services.send_limiter import SendLimiter

    # Real wall-clock, not the frozen ``NOW``: the sweep reads the clock itself
    # to decide what is due, so a fixture dated in the future is never dispatched.
    real_now = datetime.now(timezone.utc)
    seq = await _sequence(db_session, delay_days=0)
    company = await _company(db_session, "Capped Co")
    cold = await _enrollment(
        db_session, seq, await _contact(db_session, company, "info@cold.com"),
        due_at=real_now - timedelta(hours=2), enrolled_at=real_now - timedelta(days=1),
    )
    warm = await _enrollment(
        db_session, seq, await _contact(db_session, company, "dale@warm.com"),
        due_at=real_now - timedelta(hours=2), enrolled_at=real_now - timedelta(days=1),
    )
    await _event(db_session, warm, "clicked", at=real_now - timedelta(hours=3))
    await db_session.commit()

    monkeypatch.setattr(outreach_tasks, "_run_async", lambda coro: coro)

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(outreach_tasks, "celery_session", _session)

    async def _one_org(body, *, label=None):
        return [await body(db_session)]

    monkeypatch.setattr(outreach_tasks, "for_each_org", _one_org)

    async def _enabled(db):
        return True

    monkeypatch.setattr(system_settings, "is_sending_enabled", _enabled)

    # Capacity for exactly one send, then the quota latches shut.
    #
    # Patched on the class, not an instance: ``get_send_limiter`` constructs a
    # fresh ``SendLimiter`` per call, so the one this test holds is not the one
    # the sweep asks.
    budget = {"left": 1}

    async def _can_send_today(self, db):
        if budget["left"] <= 0:
            return False, "daily limit reached (1/1)"
        return True, "ok"

    async def _can_send_to_contact(self, db, email):
        return True, "ok"

    monkeypatch.setattr(SendLimiter, "can_send_today", _can_send_today)
    monkeypatch.setattr(SendLimiter, "can_send_to_contact", _can_send_to_contact)
    monkeypatch.setattr(SendLimiter, "within_send_window", lambda self, tz: (True, "ok"))

    optimizer = outreach_tasks.get_send_time_optimizer()

    async def _no_defer(db, enrollment, contact, now, due_at):
        return False

    monkeypatch.setattr(optimizer, "should_defer", _no_defer)

    dispatched: list[str] = []

    class _Task:
        @staticmethod
        def delay(enrollment_id):
            dispatched.append(enrollment_id)
            budget["left"] -= 1

    monkeypatch.setattr(outreach_tasks, "send_sequence_step", _Task)

    result = await outreach_tasks.process_outreach_queue()

    assert dispatched == [str(warm.id)]
    assert str(cold.id) not in dispatched
    # And the sweep reports the demand it could not serve, so a binding cap is
    # visible rather than looking like an idle queue.
    assert result[0]["skipped_daily_quota"] == 1
