"""Anything answering "how much did we send?" must read both send tables.

There is no single sent-emails table (see CLAUDE.md). Manual, template and
reply sends write ``EmailSend``; the sequence engine writes
``OutreachEvent(event_type='sent')`` and no ``EmailSend`` row at all — and the
sequence engine is where the bulk of outreach volume goes.

``get_email_trends`` already unioned the two. Two surfaces did not: the
dashboard's emails-sent tiles and the inbox's ``sent_count`` / ``daily_volume``.

The dashboard case is the sharp one. It takes ``emails_opened`` from
``OutreachEvent`` while taking ``emails_sent`` from ``EmailSend``, so an open
rate assembled from the two tiles divides sequence opens by non-sequence sends
and can exceed 100%.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services import cache, crypto, google_oauth
from app.services.email_history import EmailHistoryService

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _crypto_key():
    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


@pytest.fixture(autouse=True)
def _no_response_cache(monkeypatch):
    """The dashboard is short-TTL cached; a stale entry would mask the fix.

    Bypassed rather than flushed — these tests are about what the builder
    computes, and Redis may not be up under pytest anyway.
    """
    async def _straight_through(key, builder, ttl=None):
        return await builder()

    monkeypatch.setattr(cache, "cached", _straight_through)


async def _make_gmail_account(db: AsyncSession, email: str) -> GmailAccount:
    account = GmailAccount(
        id=uuid.uuid4(),
        email=email,
        google_sub=f"sub-{email}",
        display_name=email.split("@")[0].title(),
        refresh_token_encrypted=crypto.encrypt("refresh-xyz"),
        access_token_encrypted=crypto.encrypt("access-xyz"),
        scopes=" ".join(google_oauth.SCOPES),
        is_active=True,
        status="connected",
    )
    db.add(account)
    await db.flush()
    return account


async def _make_email_send(db: AsyncSession, *, sent_at: datetime) -> EmailSend:
    send = EmailSend(
        id=uuid.uuid4(),
        from_email="us@company.com",
        to_email="lead@prospect.com",
        subject="Manual note",
        body_html="<p>hi</p>",
        status="sent",
        gmail_thread_id=f"t-{uuid.uuid4().hex[:8]}",
        gmail_message_id=f"m-{uuid.uuid4().hex[:8]}",
        sent_at=sent_at,
    )
    db.add(send)
    await db.flush()
    return send


async def _make_sequence_sends(
    db: AsyncSession, count: int, *, occurred_at: datetime, from_email="us@company.com"
) -> list[OutreachEvent]:
    """``count`` sends exactly as ``send_sequence_step`` records them."""
    company = Company(
        id=uuid.uuid4(), name="Seq Co", country="US", source="test"
    )
    db.add(company)
    contact = Contact(
        id=uuid.uuid4(),
        company_id=company.id,
        first_name="Pat",
        email="pat@prospect.com",
        source="test",
    )
    sequence = OutreachSequence(id=uuid.uuid4(), name="Cold outreach")
    db.add_all([contact, sequence])
    await db.flush()
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=sequence.id
    )
    db.add(enrollment)
    await db.flush()

    events = []
    for i in range(count):
        event = OutreachEvent(
            id=uuid.uuid4(),
            enrollment_id=enrollment.id,
            step_number=i + 1,
            event_type="sent",
            event_metadata={
                "status": "sent",
                "transport": "gmail",
                "to_email": "pat@prospect.com",
                "from_email": from_email,
                "sender": from_email,
                "subject": f"Step {i + 1}",
                "body_html": "<p>Hi Pat</p>",
                "gmail_thread_id": f"seq-t-{i}",
                "gmail_message_id": f"seq-m-{i}",
            },
            occurred_at=occurred_at,
        )
        db.add(event)
        events.append(event)
    await db.flush()
    return events


# --- Dashboard ---------------------------------------------------------------


async def test_dashboard_emails_sent_counts_sequence_sends(
    client: AsyncClient, db_session: AsyncSession
):
    """One manual send + three sequence sends is four, not one."""
    now = datetime.now(timezone.utc)
    await _make_email_send(db_session, sent_at=now - timedelta(hours=2))
    await _make_sequence_sends(db_session, 3, occurred_at=now - timedelta(hours=1))
    await db_session.commit()

    resp = await client.get("/api/v1/dashboard/overview")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["emails_sent"] == 4
    assert body["emails_sent_today"] == 4


async def test_a_skipped_send_is_not_an_email_sent(
    client: AsyncClient, db_session: AsyncSession
):
    """``skipped`` rows record mail that never left, and were counted as sent.

    The status set was ``("sent", "skipped")`` on the theory that skipped meant
    "SES creds aren't configured — still a successful compose". It never meant
    only that: the template router writes it for a contact with no address, one
    who unsubscribed and one on the suppression list, and the dispatcher returns
    it for a spam-gate block, a warm-up hold and a spent daily cap. The SES
    branch itself logs "email not sent".

    Same direction as the union bug this file is about, and the same reason it
    is expensive: ``emails_opened`` comes from ``OutreachEvent``, so padding the
    denominator with mail that never went out flatters every rate built from
    these tiles — and it pads *most* on the day a cap or a kill switch holds a
    blast, which is the day the dashboard should look smallest.
    """
    now = datetime.now(timezone.utc)
    await _make_email_send(db_session, sent_at=now - timedelta(hours=2))
    for reason in ("no_email_address", "suppressed", "Daily limit reached (5/5)."):
        db_session.add(EmailSend(
            id=uuid.uuid4(), to_email="lead@prospect.com", subject="Blast",
            status="skipped", error=reason, sent_at=now - timedelta(hours=1),
        ))
    await db_session.commit()

    resp = await client.get("/api/v1/dashboard/overview")
    assert resp.status_code == 200, resp.text
    assert resp.json()["emails_sent"] == 1, "only the one that went out"


async def test_a_held_send_does_not_claim_the_contact_was_emailed(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """An ``Interaction`` is a claim on the contact's own timeline.

    The compose loop wrote one for ``sent`` *or* ``skipped``, on the same "still
    a successful compose" theory. So the contact whose entire point is that we
    did not email them — suppressed, unsubscribed, or held by the daily cap —
    got a timeline entry saying we did, indistinguishable from a real one to the
    rep reading the history.
    """
    from sqlalchemy import func, select as sa_select

    from app.models.interaction import Interaction
    from app.services import email_dispatch

    async def _held(db, **kwargs):
        return {"status": "skipped", "reason": "Daily limit reached (5/5)."}

    monkeypatch.setattr(email_dispatch, "send_outreach_email", _held)
    monkeypatch.setattr(
        "app.routers.email_templates.send_outreach_email", _held
    )

    company = Company(id=uuid.uuid4(), name="Held Co", country="US", source="test")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Pat",
        email="pat@prospect.com", source="test",
    )
    db_session.add(contact)
    await db_session.commit()

    await client.post("/api/v1/email-templates", json={
        "name": "T", "subject": "Hi {{first_name}}", "body": "<p>{{company}}</p>",
    })
    resp = await client.post("/api/v1/email-compose", json={
        "contact_ids": [str(contact.id)],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["skipped"] == 1

    count = (await db_session.execute(
        sa_select(func.count(Interaction.id)).where(
            Interaction.contact_id == contact.id,
            Interaction.interaction_type == "email",
        )
    )).scalar()
    assert count == 0, "no timeline entry for mail that never went out"


async def test_a_held_template_test_is_not_reported_as_sent(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """"Did that reach the inbox?" answered yes for mail that was never sent.

    ``skipped`` is what the dispatcher returns for every soft decline — spam
    gate, warm-up ramp, daily cap, suppression list — and this endpoint mapped
    all of them to ``sent``. The operator was told yes most often for exactly
    the reasons they most needed to hear about.
    """
    async def _held(db, **kwargs):
        return {"status": "skipped", "reason": "Recipient is on the do-not-contact suppression list."}

    monkeypatch.setattr(
        "app.routers.email_templates.send_outreach_email", _held
    )

    created = await client.post("/api/v1/email-templates", json={
        "name": "T", "subject": "Hi", "body": "<p>hello</p>",
    })
    template_id = created.json()["id"]

    resp = await client.post("/api/v1/email-templates/send-test", json={
        "template_id": template_id, "test_email": "gone@example.com",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert "suppression" in (body["detail"] or "")


async def test_open_rate_denominator_can_no_longer_exceed_its_numerator(
    client: AsyncClient, db_session: AsyncSession
):
    """The reason this mattered: opens were counted, the sends behind them weren't.

    With only sequence traffic, ``emails_sent`` read zero while ``emails_opened``
    read the real number — an open rate of opens-over-nothing.
    """
    now = datetime.now(timezone.utc)
    events = await _make_sequence_sends(
        db_session, 4, occurred_at=now - timedelta(hours=3)
    )
    # Two of those four were opened.
    for e in events[:2]:
        db_session.add(
            OutreachEvent(
                id=uuid.uuid4(),
                enrollment_id=e.enrollment_id,
                step_number=e.step_number,
                event_type="opened",
                occurred_at=now - timedelta(hours=2),
            )
        )
    await db_session.commit()

    body = (await client.get("/api/v1/dashboard/overview")).json()

    assert body["emails_opened"] == 2
    assert body["emails_sent"] == 4
    assert body["emails_opened"] <= body["emails_sent"]


async def test_previous_week_window_counts_both_tables(
    client: AsyncClient, db_session: AsyncSession
):
    """Both halves take the same window, so the comparison stays a comparison."""
    now = datetime.now(timezone.utc)
    last_week = now - timedelta(days=10)
    await _make_email_send(db_session, sent_at=last_week)
    await _make_sequence_sends(db_session, 2, occurred_at=last_week)
    # This week — must NOT land in the previous-week bucket.
    await _make_sequence_sends(db_session, 5, occurred_at=now - timedelta(hours=1))
    await db_session.commit()

    body = (await client.get("/api/v1/dashboard/overview")).json()
    assert body["emails_sent_prev_week"] == 3


# --- Inbox analytics ---------------------------------------------------------


async def test_inbox_analytics_sent_count_includes_sequence_sends(
    db_session: AsyncSession,
):
    now = datetime.now(timezone.utc)
    await _make_gmail_account(db_session, "us@company.com")
    await _make_email_send(db_session, sent_at=now - timedelta(days=1))
    await _make_sequence_sends(db_session, 3, occurred_at=now - timedelta(days=1))
    await db_session.commit()

    analytics = await EmailHistoryService().get_inbox_analytics(db_session, days=30)
    assert analytics["sent_count"] == 4


async def test_inbox_analytics_sparkline_includes_sequence_sends(
    db_session: AsyncSession,
):
    """``daily_volume`` is built from the same message list, so it moves too."""
    now = datetime.now(timezone.utc)
    await _make_gmail_account(db_session, "us@company.com")
    await _make_sequence_sends(db_session, 2, occurred_at=now - timedelta(days=1))
    await db_session.commit()

    analytics = await EmailHistoryService().get_inbox_analytics(db_session, days=30)
    assert sum(p["sent"] for p in analytics["daily_volume"]) == 2


async def test_inbox_analytics_window_still_excludes_old_sequence_sends(
    db_session: AsyncSession,
):
    """The union must not smuggle in traffic from outside the window."""
    now = datetime.now(timezone.utc)
    await _make_gmail_account(db_session, "us@company.com")
    await _make_sequence_sends(db_session, 3, occurred_at=now - timedelta(days=45))
    await _make_sequence_sends(db_session, 1, occurred_at=now - timedelta(days=2))
    await db_session.commit()

    analytics = await EmailHistoryService().get_inbox_analytics(db_session, days=30)
    assert analytics["sent_count"] == 1


async def test_inbox_analytics_respects_the_caller_mailbox_scope(
    db_session: AsyncSession,
):
    """A user's own-mailbox view must not gain another mailbox's sequence sends."""
    now = datetime.now(timezone.utc)
    await _make_gmail_account(db_session, "mine@company.com")
    await _make_gmail_account(db_session, "theirs@company.com")
    await _make_sequence_sends(
        db_session, 2, occurred_at=now - timedelta(days=1), from_email="mine@company.com"
    )
    await _make_sequence_sends(
        db_session, 5, occurred_at=now - timedelta(days=1), from_email="theirs@company.com"
    )
    await db_session.commit()

    service = EmailHistoryService()
    mine = await service.get_inbox_analytics(
        db_session, user_gmail_emails={"mine@company.com"}, days=30
    )
    assert mine["sent_count"] == 2

    everything = await service.get_inbox_analytics(db_session, days=30)
    assert everything["sent_count"] == 7
