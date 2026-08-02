"""Tests for the Meeting Booking Agent API (``/meeting-agent``) + Celery sweeps."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gmail_account import GmailAccount
from app.models.meeting_booking import BookedMeeting, MeetingBookingAgent
from app.models.user import UserRole

NOW = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)


async def _mk_account(db: AsyncSession) -> GmailAccount:
    account = GmailAccount(
        id=uuid.uuid4(), email=f"rep-{uuid.uuid4().hex[:6]}@apprend.co",
        google_sub=str(uuid.uuid4()), display_name="Rep", refresh_token_encrypted="x",
    )
    db.add(account)
    await db.flush()
    return account


# ─── config ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_config_lazily_creates_defaults(client: AsyncClient):
    resp = await client.get("/api/v1/meeting-agent/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_active"] is False
    assert body["default_meeting_duration_minutes"] == 30
    assert body["buffer_between_meetings_minutes"] == 15
    assert body["timezone"] == "Asia/Kolkata"


@pytest.mark.asyncio
async def test_update_config_requires_org_admin(client: AsyncClient, make_user, auth_headers):
    agent_user = await make_user("agent2@landline.app", role=UserRole.AGENT.value)
    resp = await client.put(
        "/api/v1/meeting-agent/config",
        json={"default_meeting_duration_minutes": 45},
        headers=auth_headers(agent_user),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_config_as_admin(client: AsyncClient, db_session, admin_user, auth_headers):
    account = await _mk_account(db_session)
    await db_session.commit()
    resp = await client.put(
        "/api/v1/meeting-agent/config",
        json={
            "is_active": True,
            "calendar_account_id": str(account.id),
            "default_meeting_duration_minutes": 45,
            "working_hours_start": "10:00",
        },
        headers=auth_headers(admin_user),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_active"] is True
    assert body["default_meeting_duration_minutes"] == 45
    assert body["working_hours_start"] == "10:00"


@pytest.mark.asyncio
async def test_cannot_activate_without_calendar(client: AsyncClient, admin_user, auth_headers):
    resp = await client.put(
        "/api/v1/meeting-agent/config",
        json={"is_active": True},
        headers=auth_headers(admin_user),
    )
    assert resp.status_code == 400
    assert "calendar" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_config_rejects_out_of_range_duration(client: AsyncClient, admin_user, auth_headers):
    resp = await client.put(
        "/api/v1/meeting-agent/config",
        json={"default_meeting_duration_minutes": 5000},
        headers=auth_headers(admin_user),
    )
    assert resp.status_code == 422


# ─── meetings list / detail / cancel ─────────────────────────────────────────


async def _mk_meeting(db: AsyncSession, contact_id, *, status="proposed", start=None, **kw) -> BookedMeeting:
    m = BookedMeeting(
        id=uuid.uuid4(), contact_id=contact_id, title="Intro call", timezone="UTC",
        status=status, start_time=start,
        end_time=(start + timedelta(minutes=30)) if start else None,
        proposed_slots=[{"start": "2026-07-21T14:00:00+00:00", "label": "Tue 2pm"}],
        conversation_thread=[{"role": "agent", "text": "here are some times", "at": NOW.isoformat()}],
        **kw,
    )
    db.add(m)
    await db.flush()
    return m


@pytest.mark.asyncio
async def test_list_meetings_and_filters(client: AsyncClient, db_session, sample_contact):
    # The router filters against real wall-clock now, so anchor the fixtures to it.
    real_now = datetime.now(timezone.utc)
    await _mk_meeting(db_session, sample_contact.id, status="proposed")
    await _mk_meeting(db_session, sample_contact.id, status="confirmed", start=real_now + timedelta(days=1))
    await _mk_meeting(db_session, sample_contact.id, status="confirmed", start=real_now - timedelta(days=1))
    await db_session.commit()

    all_resp = await client.get("/api/v1/meeting-agent/meetings")
    assert all_resp.status_code == 200
    assert all_resp.json()["total"] == 3

    upcoming = await client.get("/api/v1/meeting-agent/meetings", params={"filter": "upcoming"})
    items = upcoming.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "confirmed"
    # Denormalized contact fields present.
    assert items[0]["contact_email"] == sample_contact.email

    past = await client.get("/api/v1/meeting-agent/meetings", params={"filter": "past"})
    assert past.json()["total"] == 1

    proposed = await client.get("/api/v1/meeting-agent/meetings", params={"status": "proposed"})
    assert proposed.json()["total"] == 1


@pytest.mark.asyncio
async def test_meeting_detail_includes_thread(client: AsyncClient, db_session, sample_contact):
    m = await _mk_meeting(db_session, sample_contact.id)
    await db_session.commit()
    resp = await client.get(f"/api/v1/meeting-agent/meetings/{m.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["proposed_slots"][0]["label"] == "Tue 2pm"
    assert body["conversation_thread"][0]["role"] == "agent"


@pytest.mark.asyncio
async def test_meeting_detail_404(client: AsyncClient):
    resp = await client.get(f"/api/v1/meeting-agent/meetings/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_meeting(client: AsyncClient, db_session, sample_contact):
    m = await _mk_meeting(db_session, sample_contact.id, status="confirmed", start=NOW + timedelta(days=1))
    await db_session.commit()
    resp = await client.post(f"/api/v1/meeting-agent/meetings/{m.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cannot_cancel_completed(client: AsyncClient, db_session, sample_contact):
    m = await _mk_meeting(db_session, sample_contact.id, status="completed", start=NOW - timedelta(days=1))
    await db_session.commit()
    resp = await client.post(f"/api/v1/meeting-agent/meetings/{m.id}/cancel")
    assert resp.status_code == 409


# ─── stats ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_computes_rates(client: AsyncClient, db_session, sample_contact):
    real_now = datetime.now(timezone.utc)
    await _mk_meeting(db_session, sample_contact.id, status="proposed")
    await _mk_meeting(db_session, sample_contact.id, status="confirmed", start=real_now + timedelta(days=1))
    await _mk_meeting(db_session, sample_contact.id, status="completed", start=real_now - timedelta(days=2))
    await _mk_meeting(db_session, sample_contact.id, status="no_show", start=real_now - timedelta(days=2))
    await db_session.commit()

    resp = await client.get("/api/v1/meeting-agent/stats")
    assert resp.status_code == 200
    s = resp.json()
    assert s["total_meetings"] == 4
    assert s["confirmed"] == 1
    assert s["completed"] == 1
    # booking_rate = (confirmed+completed)/total = 2/4
    assert s["booking_rate"] == 0.5
    # show_rate = completed/(completed+no_show) = 1/2
    assert s["show_rate"] == 0.5
    assert s["upcoming"] == 1


@pytest.mark.asyncio
async def test_meeting_endpoints_require_auth(unauth_client: AsyncClient):
    for path in ("/api/v1/meeting-agent/config", "/api/v1/meeting-agent/meetings",
                 "/api/v1/meeting-agent/stats"):
        resp = await unauth_client.get(path)
        assert resp.status_code == 401, path


# ─── Celery sweeps ───────────────────────────────────────────────────────────


class _SpySender:
    def __init__(self):
        self.calls = []

    def __call__(self, account, **kwargs):
        self.calls.append(kwargs)
        return {"gmail_message_id": "m", "gmail_thread_id": kwargs.get("thread_id") or "t"}


@pytest.mark.asyncio
async def test_check_booking_replies_confirms_on_selection(db_session, sample_contact, monkeypatch):
    from app.models.email_reply import EmailReply
    from app.services import meeting_booking_agent as mba
    from app.tasks import meeting_booking as tasks

    account = await _mk_account(db_session)
    agent = MeetingBookingAgent(is_active=True, calendar_account_id=account.id, timezone="UTC")
    db_session.add(agent)
    meeting = await _mk_meeting(
        db_session, sample_contact.id, status="proposed",
        gmail_thread_id="thread-9", gmail_message_id="orig-9", sender_account_id=account.id,
        # Pin created_at to test time. Otherwise the server_default (func.now())
        # stamps the real wall-clock, which is later than the fixed-NOW reply, and
        # the reply-freshness guard (received > created_at) discards the reply.
        created_at=NOW,
    )
    # A reply picking option 1, arriving after the proposal.
    reply = EmailReply(
        id=uuid.uuid4(), from_email=sample_contact.email, contact_id=sample_contact.id,
        body_text="Option 1 works for me!", received_at=NOW + timedelta(hours=1),
        gmail_thread_id="thread-9", gmail_message_id="reply-9",
    )
    db_session.add(reply)
    await db_session.flush()

    # No calendar/email network: stub the event creator and gmail send.
    monkeypatch.setattr(mba.google_calendar, "create_event", lambda *a, **k: {"event_id": "evt-9"})
    monkeypatch.setattr(mba, "_default_gmail_send", lambda account, **k: {"gmail_message_id": "m", "gmail_thread_id": "t"})

    # Force the deterministic keyword slot-parse path (no live LLM call). The test
    # env may carry an anthropic_api_key from .env, which flips the cached AI
    # service to "configured" and would send this reply to a real API. We are
    # asserting the "Option 1" keyword selection is honoured.
    class _NoAI:
        is_configured = False

    monkeypatch.setattr(mba, "get_ai_service", lambda: _NoAI())

    report = await tasks.check_booking_replies_async(db_session, now=NOW + timedelta(hours=2))
    assert report["confirmed"] == 1
    # Assert on the in-session object (a refresh would discard the un-committed
    # mutation, since the async helper leaves the commit to the Celery wrapper).
    assert meeting.status == "confirmed"
    assert meeting.calendar_event_id == "evt-9"


@pytest.mark.asyncio
async def test_send_meeting_reminders_and_completion(db_session, sample_contact, monkeypatch):
    from app.services import meeting_booking_agent as mba
    from app.tasks import meeting_booking as tasks

    account = await _mk_account(db_session)
    agent = MeetingBookingAgent(is_active=True, calendar_account_id=account.id, timezone="UTC")
    db_session.add(agent)
    # One meeting due for a reminder (starts in 45 min), one already elapsed.
    due = await _mk_meeting(db_session, sample_contact.id, status="confirmed", start=NOW + timedelta(minutes=45))
    elapsed = await _mk_meeting(db_session, sample_contact.id, status="confirmed", start=NOW - timedelta(hours=2))
    await db_session.flush()

    monkeypatch.setattr(mba, "_default_gmail_send", lambda account, **k: {"status": "sent"})

    report = await tasks.send_meeting_reminders_async(db_session, now=NOW)
    assert report["reminded"] == 1
    assert report["completed"] == 1
    # In-session assertions (a refresh would discard the un-committed mutations).
    assert due.reminder_sent_at is not None
    assert elapsed.status == "completed"


@pytest.mark.asyncio
async def test_reminders_report_what_was_sent_not_what_was_attempted(
    db_session, sample_contact, monkeypatch
):
    """``reminder_sent_at`` is stamped before the send, so nothing corrects this.

    Stamping first is right — one missed reminder beats mailing someone every
    15 minutes — and it is precisely why the count has to come from the return
    value. Counting the attempt made the sweep report ``reminded: 1`` on a run
    where a dead Gmail token meant the attendee was told nothing, and no later
    sweep will ever revisit it.
    """
    from app.services import meeting_booking_agent as mba
    from app.tasks import meeting_booking as tasks

    account = await _mk_account(db_session)
    agent = MeetingBookingAgent(is_active=True, calendar_account_id=account.id, timezone="UTC")
    db_session.add(agent)
    due = await _mk_meeting(
        db_session, sample_contact.id, status="confirmed", start=NOW + timedelta(minutes=45),
    )
    await db_session.flush()

    monkeypatch.setattr(
        mba, "_default_gmail_send",
        lambda account, **k: {"status": "error", "detail": "invalid_grant"},
    )

    report = await tasks.send_meeting_reminders_async(db_session, now=NOW)
    assert report["reminded"] == 0
    # Still stamped: the retry loop is the worse failure of the two.
    assert due.reminder_sent_at is not None


@pytest.mark.asyncio
async def test_check_booking_replies_idempotent_no_new_reply(db_session, sample_contact):
    from app.tasks import meeting_booking as tasks

    account = await _mk_account(db_session)
    agent = MeetingBookingAgent(is_active=True, calendar_account_id=account.id, timezone="UTC")
    db_session.add(agent)
    await _mk_meeting(db_session, sample_contact.id, status="proposed", gmail_thread_id="thread-x")
    await db_session.flush()
    # No replies in the thread → nothing processed.
    report = await tasks.check_booking_replies_async(db_session, now=NOW + timedelta(hours=1))
    assert report["processed"] == 0
