"""The kill switch and the do-not-contact list reach the paths that bypass dispatch.

``tests/test_send_gate_coverage.py`` is the mechanical half — it checks that
every send site *calls* the gate. This is the behavioural half: that calling it
actually stops the mail, on each path that used to hold a transport directly.

The paths, and what each one cost before:

* **the meeting-booking agent** — autonomous, fires off a 15-minute beat, and
  answered to neither floor. An operator engaging the kill switch because their
  domain was on fire stopped the queue, the sequences and every manual send, and
  this agent kept proposing meetings. A recipient who unsubscribed stayed on the
  list every other path honours and got mail from this one.
* **the smart-reply auto-send** — had the kill switch and four other floors, but
  not suppression, so a reply arriving on an old thread from an address that had
  since unsubscribed or filed a spam complaint was auto-answered.
* **the inbox reply composer** — human-driven, which is not an exemption: a
  person clicking Reply is how a do-not-contact address actually gets mailed.
* **the per-account test send** — a "test" is still mail in someone's inbox when
  the operator types a third-party address into it.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.gmail_account import GmailAccount
from app.models.meeting_booking import BookedMeeting, MeetingBookingAgent
from app.services import email_dispatch, meeting_booking_agent as mba, suppression
from app.services import system_settings

MONDAY_8AM = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)


class _SpySender:
    """Stands in for GmailSender.send_email; records every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, account, **kwargs):
        self.calls.append(kwargs)
        return {"gmail_message_id": "msg-1", "gmail_thread_id": kwargs.get("thread_id") or "t-1"}


async def _free_busy(account, *, start, end):
    return []


async def _mk_account(db: AsyncSession) -> GmailAccount:
    account = GmailAccount(
        id=uuid.uuid4(), email="rep@apprend.co", google_sub=str(uuid.uuid4()),
        display_name="Rep", refresh_token_encrypted="x", status="connected",
    )
    db.add(account)
    await db.flush()
    return account


async def _mk_agent(db: AsyncSession, account: GmailAccount) -> MeetingBookingAgent:
    agent = MeetingBookingAgent(
        is_active=True, calendar_account_id=account.id, timezone="UTC",
        booking_window_days=7,
    )
    db.add(agent)
    await db.flush()
    return agent


async def _mk_meeting(db: AsyncSession, contact: Contact, account: GmailAccount, **overrides):
    fields = {
        "id": uuid.uuid4(), "contact_id": contact.id, "sender_account_id": account.id,
        "title": "Intro call", "timezone": "UTC", "status": "proposed",
        "proposed_slots": [], "conversation_thread": [],
        "gmail_thread_id": "thread-1", "gmail_message_id": "orig-1",
        **overrides,
    }
    meeting = BookedMeeting(**fields)
    db.add(meeting)
    await db.flush()
    return meeting


# ─── the gate itself ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_allows_a_clean_address(db_session):
    assert await email_dispatch.check_send_allowed(db_session, "fresh@example.com") is None


@pytest.mark.asyncio
async def test_gate_blocks_a_suppressed_address(db_session):
    await suppression.add_entry(db_session, "gone@example.com", reason="unsubscribe")
    blocked = await email_dispatch.check_send_allowed(db_session, "gone@example.com")
    assert blocked is not None and blocked["status"] == "suppressed"


@pytest.mark.asyncio
async def test_gate_blocks_on_the_kill_switch(db_session):
    await system_settings.set_sending_enabled(db_session, False, updated_by="test")
    blocked = await email_dispatch.check_send_allowed(db_session, "fine@example.com")
    assert blocked is not None and blocked["status"] == "halted"


@pytest.mark.asyncio
async def test_suppression_outranks_the_kill_switch_bypass(db_session):
    """``bypass_kill_switch`` is an operational override, not a compliance one.

    The distinction is the whole reason the two floors are separate: an internal
    caller may need to send while the platform is paused, and none of them may
    send to someone who asked to be left alone.
    """
    await system_settings.set_sending_enabled(db_session, False, updated_by="test")
    await suppression.add_entry(db_session, "gone@example.com", reason="complaint")

    assert await email_dispatch.check_send_allowed(
        db_session, "fine@example.com", bypass_kill_switch=True
    ) is None
    blocked = await email_dispatch.check_send_allowed(
        db_session, "gone@example.com", bypass_kill_switch=True
    )
    assert blocked is not None and blocked["status"] == "suppressed"


@pytest.mark.asyncio
async def test_check_kill_switch_false_still_enforces_suppression(db_session):
    await system_settings.set_sending_enabled(db_session, False, updated_by="test")
    await suppression.add_entry(db_session, "gone@example.com", reason="bounce")

    assert await email_dispatch.check_send_allowed(
        db_session, "fine@example.com", check_kill_switch=False
    ) is None
    blocked = await email_dispatch.check_send_allowed(
        db_session, "gone@example.com", check_kill_switch=False
    )
    assert blocked is not None and blocked["status"] == "suppressed"


# ─── meeting-booking agent ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_proposal_is_not_sent_to_a_suppressed_contact(db_session, sample_contact):
    account = await _mk_account(db_session)
    agent = await _mk_agent(db_session, account)
    await suppression.add_entry(db_session, sample_contact.email, reason="unsubscribe")
    sender = _SpySender()

    meeting = await mba.propose_meeting(
        db_session, agent=agent, contact=sample_contact, reply=None,
        now=MONDAY_8AM, busy_provider=_free_busy, send_email=sender,
    )

    assert sender.calls == []
    # And no phantom `proposed` row for the sweep to keep polling on.
    assert meeting is None


@pytest.mark.asyncio
async def test_proposal_is_not_sent_while_the_kill_switch_is_engaged(db_session, sample_contact):
    account = await _mk_account(db_session)
    agent = await _mk_agent(db_session, account)
    await system_settings.set_sending_enabled(db_session, False, updated_by="test")
    sender = _SpySender()

    meeting = await mba.propose_meeting(
        db_session, agent=agent, contact=sample_contact, reply=None,
        now=MONDAY_8AM, busy_provider=_free_busy, send_email=sender,
    )

    assert sender.calls == []
    assert meeting is None


@pytest.mark.asyncio
async def test_proposal_still_sends_for_a_clean_contact(db_session, sample_contact):
    """The gate has to let the normal case through — a block-everything gate passes
    every test above and ships an agent that never works."""
    account = await _mk_account(db_session)
    agent = await _mk_agent(db_session, account)
    sender = _SpySender()

    meeting = await mba.propose_meeting(
        db_session, agent=agent, contact=sample_contact, reply=None,
        now=MONDAY_8AM, busy_provider=_free_busy, send_email=sender,
    )

    assert meeting is not None
    assert len(sender.calls) == 1
    assert sender.calls[0]["to_email"] == sample_contact.email


@pytest.mark.asyncio
async def test_reminder_is_not_sent_to_a_suppressed_contact(db_session, sample_contact):
    account = await _mk_account(db_session)
    agent = await _mk_agent(db_session, account)
    meeting = await _mk_meeting(
        db_session, sample_contact, account, status="confirmed",
        start_time=MONDAY_8AM + timedelta(hours=1),
        end_time=MONDAY_8AM + timedelta(hours=1, minutes=30),
    )
    await suppression.add_entry(db_session, sample_contact.email, reason="complaint")
    sender = _SpySender()

    sent = await mba.send_reminder(
        db_session, meeting=meeting, agent=agent, contact=sample_contact,
        now=MONDAY_8AM, send_email=sender,
    )

    assert sent is False
    assert sender.calls == []


@pytest.mark.asyncio
async def test_confirmation_is_not_sent_to_a_suppressed_contact(db_session, sample_contact):
    """A confirmation claims no transactional exemption, on purpose.

    The list does not record which mail a recipient meant to escape, and a
    complaint is the entry most likely to be sitting there. Overriding it is an
    operator removing the entry — a decision with a record — not a branch here.
    """
    account = await _mk_account(db_session)
    agent = await _mk_agent(db_session, account)
    meeting = await _mk_meeting(db_session, sample_contact, account)
    await suppression.add_entry(db_session, sample_contact.email, reason="complaint")
    sender = _SpySender()
    slot = {
        "start": "2026-07-21T14:00:00+00:00",
        "end": "2026-07-21T14:30:00+00:00",
        "label": "Tue 2pm",
    }

    confirmed = await mba.confirm_meeting(
        db_session, meeting=meeting, agent=agent, slot=slot, contact=sample_contact,
        now=MONDAY_8AM, send_email=sender, create_event=lambda *a, **k: None,
    )

    assert sender.calls == []
    # The meeting itself is still confirmed — they picked a slot, and dropping
    # the booking because we can't email about it would be the worse failure.
    assert confirmed is not None and confirmed.status == "confirmed"


# ─── smart-reply auto-send ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_smart_reply_gate_reports_suppression_separately(db_session):
    """The auto-send decision distinguishes its floors in the ops trail.

    A collapsed "blocked" reason is how an operator ends up looking for a paused
    kill switch when what actually happened is that the recipient unsubscribed.
    """
    await suppression.add_entry(db_session, "gone@example.com", reason="unsubscribe")
    blocked = await email_dispatch.check_send_allowed(
        db_session, "gone@example.com", check_kill_switch=False
    )
    assert blocked is not None
    assert blocked["status"] == "suppressed"
    assert blocked["status"] != "halted"


# ─── per-account test send ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gmail_test_send_refuses_a_suppressed_third_party(client, db_session):
    account = GmailAccount(
        id=uuid.uuid4(), email="rep@apprend.co", google_sub=str(uuid.uuid4()),
        display_name="Rep", refresh_token_encrypted="x", status="connected",
    )
    db_session.add(account)
    await suppression.add_entry(db_session, "gone@example.com", reason="unsubscribe")
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/gmail/accounts/{account.id}/test",
        json={"to_email": "gone@example.com"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "suppressed"


@pytest.mark.asyncio
async def test_gmail_test_send_to_own_address_skips_the_list(client, db_session, monkeypatch):
    """A self-test is a connectivity check, not mail to a contact.

    Suppressing your own mailbox — it hard-bounced once during an outage — must
    not take away the button you use to find out whether it can send again.
    """
    account = GmailAccount(
        id=uuid.uuid4(), email="rep@apprend.co", google_sub=str(uuid.uuid4()),
        display_name="Rep", refresh_token_encrypted="x", status="connected",
    )
    db_session.add(account)
    await suppression.add_entry(db_session, "rep@apprend.co", reason="bounce")
    await db_session.commit()

    sent = {}

    class _FakeGmail:
        def __init__(self, acct):
            self.acct = acct

        def send_email(self, **kwargs):
            sent.update(kwargs)
            return {"status": "sent", "gmail_message_id": "m-1"}

    monkeypatch.setattr("app.routers.gmail.GmailSender", _FakeGmail)

    resp = await client.post(f"/api/v1/gmail/accounts/{account.id}/test", json={})

    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"
    assert sent["to_email"] == "rep@apprend.co"
