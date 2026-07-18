"""Tests for the Meeting Booking Agent service.

Split into:
* Pure slot-generation / selection / intent / copy logic (offline, deterministic).
* Orchestration (propose → confirm → reminder) against the DB with injected
  calendar + email transports, so no network is touched.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.gmail_account import GmailAccount
from app.models.meeting_booking import BookedMeeting, MeetingBookingAgent
from app.services import meeting_booking_agent as mba
from app.services.meeting_booking_agent import SlotConfig

# A Monday 08:00 UTC anchor keeps every generated slot on a predictable weekday.
MONDAY_8AM = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)


# ─── intent classification (keyword path, no AI) ─────────────────────────────


class _NoAI:
    is_configured = False


@pytest.mark.asyncio
async def test_intent_meeting_keyword():
    r = await mba.classify_reply_intent("Sure, can we hop on a call next week?", ai_service=_NoAI())
    assert r["intent"] == mba.INTENT_POSITIVE_MEETING


@pytest.mark.asyncio
async def test_intent_unsubscribe_wins_over_meeting():
    r = await mba.classify_reply_intent(
        "please remove me, not looking for a call", ai_service=_NoAI()
    )
    assert r["intent"] == mba.INTENT_UNSUBSCRIBE


@pytest.mark.asyncio
async def test_intent_negative():
    r = await mba.classify_reply_intent("No thanks, not interested.", ai_service=_NoAI())
    assert r["intent"] == mba.INTENT_NEGATIVE


@pytest.mark.asyncio
async def test_intent_info_first():
    r = await mba.classify_reply_intent("Can you send me some pricing first?", ai_service=_NoAI())
    assert r["intent"] == mba.INTENT_POSITIVE_INFO


@pytest.mark.asyncio
async def test_intent_uses_ai_when_configured():
    class FakeAI:
        is_configured = True

        async def complete_json(self, prompt, max_tokens=200):
            return {"intent": "positive_meeting", "confidence": 0.9, "reason": "wants a demo"}

    r = await mba.classify_reply_intent("I'd love to see it in action.", ai_service=FakeAI())
    assert r["intent"] == mba.INTENT_POSITIVE_MEETING
    assert r["source"] == "ai"
    assert r["confidence"] == 0.9


# ─── slot generation (pure) ──────────────────────────────────────────────────


def test_slots_respect_working_hours_and_weekdays():
    cfg = SlotConfig(duration_minutes=30, buffer_minutes=15, working_start="09:00",
                     working_end="17:00", timezone="UTC", window_days=7, max_daily_meetings=5)
    slots = mba.generate_slots(cfg, now=MONDAY_8AM, busy=[])
    assert slots, "expected at least one slot"
    for s in slots:
        start = datetime.fromisoformat(s["start"])
        assert start.weekday() < 5  # no weekends
        assert 9 <= start.hour < 17  # inside working hours
        assert start >= MONDAY_8AM + timedelta(minutes=mba.MIN_LEAD_MINUTES)


def test_slots_capped_and_per_day_limited():
    cfg = SlotConfig(timezone="UTC", window_days=7)
    slots = mba.generate_slots(cfg, now=MONDAY_8AM, busy=[])
    assert len(slots) <= mba.MAX_OFFERED_SLOTS
    # No more than OFFERED_PER_DAY on any single day.
    by_day = {}
    for s in slots:
        d = datetime.fromisoformat(s["start"]).date()
        by_day[d] = by_day.get(d, 0) + 1
    assert all(v <= mba.OFFERED_PER_DAY for v in by_day.values())


def test_slots_exclude_busy_intervals_with_buffer():
    cfg = SlotConfig(duration_minutes=30, buffer_minutes=15, working_start="09:00",
                     working_end="17:00", timezone="UTC", window_days=2, max_daily_meetings=5)
    # Block the whole Monday working window.
    busy = [(datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
             datetime(2026, 7, 20, 17, 0, tzinfo=timezone.utc))]
    slots = mba.generate_slots(cfg, now=MONDAY_8AM, busy=busy)
    # Nothing should be offered on the fully-blocked Monday.
    monday = datetime(2026, 7, 20).date()
    assert all(datetime.fromisoformat(s["start"]).date() != monday for s in slots)


def test_slots_skip_days_at_daily_cap():
    cfg = SlotConfig(timezone="UTC", window_days=3, max_daily_meetings=2)
    tuesday = datetime(2026, 7, 21).date()
    slots = mba.generate_slots(cfg, now=MONDAY_8AM, busy=[], daily_counts={tuesday: 2})
    assert all(datetime.fromisoformat(s["start"]).date() != tuesday for s in slots)


def test_slot_label_is_human_readable():
    cfg = SlotConfig(timezone="UTC", window_days=2)
    slots = mba.generate_slots(cfg, now=MONDAY_8AM, busy=[])
    assert " · " in slots[0]["label"]


# ─── slot selection (pure keyword path) ──────────────────────────────────────


def test_select_slot_by_option_number():
    slots = [{"start": "s1", "label": "Mon, Jul 21 · 9:00 AM"},
             {"start": "s2", "label": "Tue, Jul 22 · 9:00 AM"}]
    assert mba.select_slot_from_reply("Option 2 works great", slots) == 1


def test_select_slot_by_ordinal_word():
    slots = [{"start": "s1", "label": "a"}, {"start": "s2", "label": "b"}]
    assert mba.select_slot_from_reply("the first one please", slots) == 0


def test_select_slot_by_unique_day_name():
    slots = [{"start": "s1", "label": "Mon, Jul 20 · 9:00 AM"},
             {"start": "s2", "label": "Wed, Jul 22 · 9:00 AM"}]
    assert mba.select_slot_from_reply("Wednesday is best", slots) == 1


def test_select_slot_returns_none_when_ambiguous():
    slots = [{"start": "s1", "label": "a"}, {"start": "s2", "label": "b"}]
    assert mba.select_slot_from_reply("sounds good, talk soon", slots) is None


@pytest.mark.asyncio
async def test_handle_slot_selection_ai_choice():
    class FakeAI:
        is_configured = True

        async def complete_json(self, prompt, max_tokens=200):
            return {"choice": 2, "wants_different_time": False, "reason": "picked 2"}

    slots = [{"start": "s1", "label": "a"}, {"start": "s2", "label": "b"}]
    r = await mba.handle_slot_selection("the tuesday slot", slots, ai_service=FakeAI())
    assert r["selected_index"] == 1
    assert r["selected_slot"]["start"] == "s2"


@pytest.mark.asyncio
async def test_handle_slot_selection_wants_reschedule():
    class FakeAI:
        is_configured = True

        async def complete_json(self, prompt, max_tokens=200):
            return {"choice": 0, "wants_different_time": True, "reason": "none work"}

    slots = [{"start": "s1", "label": "a"}]
    r = await mba.handle_slot_selection("none of those work, how about friday?", slots, ai_service=FakeAI())
    assert r["selected_slot"] is None
    assert r["wants_reschedule"] is True


# ─── copy builders ───────────────────────────────────────────────────────────


def test_proposal_text_lists_all_slots():
    # SimpleNamespace stand-ins keep these pure tests from instantiating a mapped
    # model before the SQLite type-patch runs (which would freeze UUID handling
    # for the db tests in the same file).
    slots = [{"start": "s1", "label": "Mon 9am"}, {"start": "s2", "label": "Tue 10am"}]
    contact = SimpleNamespace(first_name="Dana", last_name="Lee")
    text, html = mba.build_proposal_text(contact, slots, rep_name="Sam")
    assert "Dana" in text
    assert "Mon 9am" in text and "Tue 10am" in text
    assert "<p>" in html


def test_confirmation_template_substitution():
    contact = SimpleNamespace(first_name="Dana")
    meeting = SimpleNamespace(
        title="Intro call", timezone="UTC",
        start_time=datetime(2026, 7, 21, 14, 30, tzinfo=timezone.utc),
    )
    text, _ = mba.build_confirmation_text(
        contact, meeting, template="Hi {name}, confirmed for {when}. — {rep_name}", rep_name="Sam"
    )
    assert text.startswith("Hi Dana, confirmed for")
    assert "— Sam" in text


# ─── orchestration (DB + injected transports) ────────────────────────────────


class _SpySender:
    """Captures GmailSender.send_email calls and returns a threaded id shape."""

    def __init__(self):
        self.calls = []

    def __call__(self, account, **kwargs):
        self.calls.append(kwargs)
        return {"gmail_message_id": "msg-123", "gmail_thread_id": kwargs.get("thread_id") or "thread-1"}


async def _free_busy(account, *, start, end):
    return []  # calendar entirely free


async def _mk_agent(db: AsyncSession, account: GmailAccount, **overrides) -> MeetingBookingAgent:
    agent = MeetingBookingAgent(
        is_active=True, calendar_account_id=account.id, timezone="UTC",
        booking_window_days=7, **overrides,
    )
    db.add(agent)
    await db.flush()
    return agent


async def _mk_account(db: AsyncSession) -> GmailAccount:
    account = GmailAccount(
        id=uuid.uuid4(), email="rep@apprend.co", google_sub=str(uuid.uuid4()),
        display_name="Rep", refresh_token_encrypted="x",
    )
    db.add(account)
    await db.flush()
    return account


@pytest.mark.asyncio
async def test_propose_meeting_creates_proposed_row_and_sends(db_session, sample_contact):
    account = await _mk_account(db_session)
    agent = await _mk_agent(db_session, account)
    sender = _SpySender()
    reply = EmailReply(
        id=uuid.uuid4(), from_email=sample_contact.email, subject="Interested!",
        body_text="Yes, let's set up a call.", received_at=MONDAY_8AM,
        gmail_thread_id="thread-1", gmail_message_id="orig-1",
    )
    db_session.add(reply)
    await db_session.flush()

    meeting = await mba.propose_meeting(
        db_session, agent=agent, contact=sample_contact, reply=reply,
        now=MONDAY_8AM, ai_service=_NoAI(), busy_provider=_free_busy, send_email=sender,
    )
    assert meeting is not None
    assert meeting.status == "proposed"
    assert len(meeting.proposed_slots) >= 1
    assert sender.calls, "a proposal email should have been sent"
    # Sent in-thread as a reply.
    assert sender.calls[0]["thread_id"] == "thread-1"
    assert sender.calls[0]["in_reply_to"] == "orig-1"
    assert meeting.gmail_thread_id == "thread-1"


@pytest.mark.asyncio
async def test_propose_meeting_skips_without_calendar(db_session, sample_contact):
    agent = MeetingBookingAgent(is_active=True, calendar_account_id=None, timezone="UTC")
    db_session.add(agent)
    await db_session.flush()
    meeting = await mba.propose_meeting(
        db_session, agent=agent, contact=sample_contact, reply=None,
        now=MONDAY_8AM, ai_service=_NoAI(), busy_provider=_free_busy, send_email=_SpySender(),
    )
    assert meeting is None


@pytest.mark.asyncio
async def test_confirm_meeting_sets_times_and_creates_event(db_session, sample_contact):
    account = await _mk_account(db_session)
    agent = await _mk_agent(db_session, account)
    meeting = BookedMeeting(
        id=uuid.uuid4(), contact_id=sample_contact.id, sender_account_id=account.id,
        title="Intro call", timezone="UTC", status="proposed",
        proposed_slots=[], conversation_thread=[], gmail_thread_id="thread-1",
        gmail_message_id="orig-1",
    )
    db_session.add(meeting)
    await db_session.flush()

    slot = {"start": "2026-07-21T14:00:00+00:00", "end": "2026-07-21T14:30:00+00:00", "label": "Tue 2pm"}
    events = []

    def fake_create_event(account, **kwargs):
        events.append(kwargs)
        return {"event_id": "evt-1", "html_link": "http://cal/evt-1"}

    sender = _SpySender()
    await mba.confirm_meeting(
        db_session, meeting=meeting, agent=agent, slot=slot, contact=sample_contact,
        now=MONDAY_8AM, create_event=fake_create_event, send_email=sender,
    )
    assert meeting.status == "confirmed"
    assert meeting.calendar_event_id == "evt-1"
    assert meeting.start_time == datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc)
    assert events[0]["attendee_emails"] == [sample_contact.email]
    assert any("Confirmed" in c["subject"] for c in sender.calls)


@pytest.mark.asyncio
async def test_send_reminder_stamps_and_sends(db_session, sample_contact):
    account = await _mk_account(db_session)
    agent = await _mk_agent(db_session, account)
    meeting = BookedMeeting(
        id=uuid.uuid4(), contact_id=sample_contact.id, sender_account_id=account.id,
        title="Intro call", timezone="UTC", status="confirmed",
        start_time=MONDAY_8AM + timedelta(hours=1), end_time=MONDAY_8AM + timedelta(hours=1, minutes=30),
        proposed_slots=[], conversation_thread=[], gmail_thread_id="thread-1",
    )
    db_session.add(meeting)
    await db_session.flush()
    sender = _SpySender()
    sent = await mba.send_reminder(
        db_session, meeting=meeting, agent=agent, contact=sample_contact,
        now=MONDAY_8AM, send_email=sender,
    )
    assert sent is True
    assert meeting.reminder_sent_at is not None
    assert any("Reminder" in c["subject"] for c in sender.calls)


@pytest.mark.asyncio
async def test_on_interested_reply_proposes_when_active(db_session, sample_contact):
    account = await _mk_account(db_session)
    await _mk_agent(db_session, account)
    reply = EmailReply(
        id=uuid.uuid4(), from_email=sample_contact.email, contact_id=sample_contact.id,
        subject="Re: hi", body_text="Yes! Can we schedule a demo?", received_at=MONDAY_8AM,
        gmail_thread_id="thread-1", gmail_message_id="orig-1",
    )
    db_session.add(reply)
    await db_session.flush()

    meeting = await mba.on_interested_reply(
        db_session, reply=reply, contact=sample_contact, company=None,
        ai_service=_NoAI(), now=MONDAY_8AM, busy_provider=_free_busy, send_email=_SpySender(),
    )
    assert meeting is not None
    assert meeting.status == "proposed"


@pytest.mark.asyncio
async def test_on_interested_reply_noop_when_inactive(db_session, sample_contact):
    account = await _mk_account(db_session)
    agent = MeetingBookingAgent(is_active=False, calendar_account_id=account.id, timezone="UTC")
    db_session.add(agent)
    await db_session.flush()
    reply = EmailReply(
        id=uuid.uuid4(), from_email=sample_contact.email, contact_id=sample_contact.id,
        body_text="Yes, let's schedule a call!", received_at=MONDAY_8AM,
    )
    db_session.add(reply)
    await db_session.flush()
    meeting = await mba.on_interested_reply(
        db_session, reply=reply, contact=sample_contact, ai_service=_NoAI(),
        now=MONDAY_8AM, busy_provider=_free_busy, send_email=_SpySender(),
    )
    assert meeting is None


@pytest.mark.asyncio
async def test_on_interested_reply_noop_for_info_intent(db_session, sample_contact):
    account = await _mk_account(db_session)
    await _mk_agent(db_session, account)
    reply = EmailReply(
        id=uuid.uuid4(), from_email=sample_contact.email, contact_id=sample_contact.id,
        body_text="Interesting — can you send me pricing details first?", received_at=MONDAY_8AM,
    )
    db_session.add(reply)
    await db_session.flush()
    meeting = await mba.on_interested_reply(
        db_session, reply=reply, contact=sample_contact, ai_service=_NoAI(),
        now=MONDAY_8AM, busy_provider=_free_busy, send_email=_SpySender(),
    )
    # Info-first is left for the human — no meeting proposed.
    assert meeting is None
