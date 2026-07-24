"""Sequence sends must appear in the unified inbox.

The sequence engine (``app.tasks.outreach.send_sequence_step``) records a real
send as ``OutreachEvent(event_type="sent")`` and writes no ``EmailSend`` row.
Every mail surface in :mod:`app.services.email_history` was built on
``EmailSend`` alone, so the outbound half of Dialtone's own outreach never
reached the inbox: the operator saw the mail in Gmail's "Sent" folder and
nothing at all in Landline — and a prospect's reply showed up as a thread whose
opening message was missing.

These tests pin the read-side union: sequence sends surface in the inbox, in the
``sent`` view, in a thread, and on a lead — scoped to the right mailbox, the
right organization, and deduped against any ``EmailSend`` covering the same
Gmail message.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.models.tenant import Organization
from app.org_context import APPREND_ORG_ID, org_scope
from app.services import crypto, google_oauth
from app.services.email_history import EmailHistoryService

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _crypto_key():
    """Give crypto a valid Fernet key for the duration of each test."""
    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


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


async def _make_contact(
    db: AsyncSession, email: str, company_name: str = "Prospect Co"
) -> Contact:
    company = Company(
        id=uuid.uuid4(), name=company_name, country="US", source="test"
    )
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(),
        company_id=company.id,
        first_name="Pat",
        last_name="Prospect",
        email=email,
        source="test",
    )
    db.add(contact)
    await db.flush()
    return contact


async def _make_enrollment(
    db: AsyncSession,
    contact: Contact,
    *,
    gmail_thread_id: str | None = None,
    sender_account_id: uuid.UUID | None = None,
) -> OutreachEnrollment:
    sequence = OutreachSequence(id=uuid.uuid4(), name="Cold outreach")
    db.add(sequence)
    await db.flush()
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=sequence.id,
        current_step=2,
        gmail_thread_id=gmail_thread_id,
        sender_account_id=sender_account_id,
    )
    db.add(enrollment)
    await db.flush()
    return enrollment


async def _make_sequence_send(
    db: AsyncSession,
    enrollment: OutreachEnrollment,
    *,
    step_number: int = 1,
    to_email: str = "pat@prospect.com",
    from_email: str | None = "sales@company.com",
    subject: str = "Quick question about your CNC line",
    body_html: str = "<p>Hi Pat — noticed you run a CNC shop.</p>",
    gmail_thread_id: str | None = None,
    gmail_message_id: str | None = None,
    sender_account_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
    event_type: str = "sent",
) -> OutreachEvent:
    """One send exactly as ``send_sequence_step`` records it."""
    metadata = {
        "status": "sent",
        "transport": "gmail",
        "tracked": True,
        "to_email": to_email,
        "subject": subject,
        "body_html": body_html,
    }
    if from_email is not None:
        metadata["from_email"] = from_email
        metadata["sender"] = from_email
    if gmail_thread_id is not None:
        metadata["gmail_thread_id"] = gmail_thread_id
    if gmail_message_id is not None:
        metadata["gmail_message_id"] = gmail_message_id
    if sender_account_id is not None:
        metadata["sender_account_id"] = str(sender_account_id)

    event = OutreachEvent(
        id=uuid.uuid4(),
        enrollment_id=enrollment.id,
        step_number=step_number,
        event_type=event_type,
        event_metadata=metadata,
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )
    db.add(event)
    await db.flush()
    return event


async def _make_engagement_event(
    db: AsyncSession,
    enrollment: OutreachEnrollment,
    *,
    step_number: int,
    event_type: str,
    occurred_at: datetime | None = None,
) -> OutreachEvent:
    event = OutreachEvent(
        id=uuid.uuid4(),
        enrollment_id=enrollment.id,
        step_number=step_number,
        event_type=event_type,
        event_metadata={},
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )
    db.add(event)
    await db.flush()
    return event


# ---------------------------------------------------------------------------
# The regression: sequence sends reach the inbox at all
# ---------------------------------------------------------------------------

class TestSequenceSendsAppearInInbox:
    """The core bug — mail in Gmail's Sent folder, nothing in Landline."""

    async def test_sequence_send_appears_with_no_email_send_row(self, db_session):
        account = await _make_gmail_account(db_session, "sales@company.com")
        contact = await _make_contact(db_session, "pat@prospect.com", "Prospect Co")
        enrollment = await _make_enrollment(
            db_session, contact, gmail_thread_id="thr-1", sender_account_id=account.id
        )
        await _make_sequence_send(
            db_session,
            enrollment,
            gmail_thread_id="thr-1",
            gmail_message_id="gm-1",
            sender_account_id=account.id,
        )
        await db_session.commit()

        # Nothing was written to email_sends — that is the whole point.
        assert (await db_session.execute(EmailSend.__table__.select())).first() is None

        result = await EmailHistoryService().get_inbox(db_session, filter_type="all")

        assert result["total"] == 1
        message = result["threads"][0]["messages"][0]
        assert message["direction"] == "sent"
        assert message["to_email"] == "pat@prospect.com"
        assert message["from_email"] == "sales@company.com"
        assert message["subject"] == "Quick question about your CNC line"
        assert "CNC shop" in message["body_html"]
        assert message["gmail_thread_id"] == "thr-1"
        assert message["gmail_message_id"] == "gm-1"
        assert message["gmail_account_id"] == account.id
        assert message["gmail_account_email"] == "sales@company.com"
        assert message["is_read"] is True

    async def test_sent_filter_returns_sequence_sends(self, db_session):
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(db_session, contact)
        await _make_sequence_send(db_session, enrollment, gmail_message_id="gm-1")
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(db_session, filter_type="sent")

        assert result["total"] == 1
        assert result["threads"][0]["messages"][0]["direction"] == "sent"

    async def test_received_filter_excludes_sequence_sends(self, db_session):
        """The received view is inbound-only; an outbound send must not leak in."""
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(db_session, contact)
        await _make_sequence_send(db_session, enrollment, gmail_message_id="gm-1")
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(db_session, filter_type="received")

        assert result["total"] == 0

    async def test_only_sent_events_surface(self, db_session):
        """``opened`` / ``bounced`` events are engagement, not messages."""
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(db_session, contact)
        await _make_sequence_send(
            db_session, enrollment, step_number=1, gmail_message_id="gm-1"
        )
        await _make_engagement_event(
            db_session, enrollment, step_number=1, event_type="opened"
        )
        await _make_sequence_send(
            db_session,
            enrollment,
            step_number=2,
            gmail_message_id="gm-2",
            event_type="bounced",
        )
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(db_session, filter_type="sent")

        messages = [m for t in result["threads"] for m in t["messages"]]
        assert len(messages) == 1
        assert messages[0]["gmail_message_id"] == "gm-1"

    async def test_reply_and_its_sequence_send_share_one_thread(self, db_session):
        """The reply used to hang alone under a thread with no opening message."""
        account = await _make_gmail_account(db_session, "sales@company.com")
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(
            db_session, contact, gmail_thread_id="thr-1", sender_account_id=account.id
        )
        sent_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await _make_sequence_send(
            db_session,
            enrollment,
            gmail_thread_id="thr-1",
            gmail_message_id="gm-1",
            sender_account_id=account.id,
            occurred_at=sent_at,
        )
        db_session.add(
            EmailReply(
                id=uuid.uuid4(),
                from_email="pat@prospect.com",
                to_email="sales@company.com",
                subject="Re: Quick question about your CNC line",
                body_text="Sure, let's talk.",
                snippet="Sure, let's talk.",
                gmail_thread_id="thr-1",
                gmail_message_id="gm-2",
                gmail_account_id=account.id,
                contact_id=contact.id,
                received_at=datetime.now(timezone.utc),
                is_read=False,
            )
        )
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(db_session, filter_type="all")

        assert result["total"] == 1
        messages = result["threads"][0]["messages"]
        assert [m["direction"] for m in messages] == ["sent", "received"]

    async def test_needs_reply_sees_the_sequence_send(self, db_session):
        """A thread whose last message is our own send is not awaiting us."""
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(db_session, contact, gmail_thread_id="thr-1")
        await _make_sequence_send(
            db_session,
            enrollment,
            gmail_thread_id="thr-1",
            gmail_message_id="gm-1",
            occurred_at=datetime.now(timezone.utc),
        )
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(
            db_session, filter_type="needs_reply"
        )

        assert result["total"] == 0


# ---------------------------------------------------------------------------
# Dedup against EmailSend
# ---------------------------------------------------------------------------

class TestSequenceSendDedup:

    async def test_same_gmail_message_is_not_shown_twice(self, db_session):
        """If an EmailSend ever covers the same Gmail message, it wins."""
        account = await _make_gmail_account(db_session, "sales@company.com")
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(
            db_session, contact, gmail_thread_id="thr-1", sender_account_id=account.id
        )
        await _make_sequence_send(
            db_session,
            enrollment,
            gmail_thread_id="thr-1",
            gmail_message_id="gm-1",
            sender_account_id=account.id,
        )
        db_session.add(
            EmailSend(
                id=uuid.uuid4(),
                from_email="sales@company.com",
                to_email="pat@prospect.com",
                subject="Quick question about your CNC line",
                body_html="<p>canonical copy</p>",
                status="sent",
                gmail_thread_id="thr-1",
                gmail_message_id="gm-1",
                sender_account_id=account.id,
                contact_id=contact.id,
                sent_at=datetime.now(timezone.utc),
            )
        )
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(db_session, filter_type="all")

        messages = [m for t in result["threads"] for m in t["messages"]]
        assert len(messages) == 1
        assert messages[0]["body_html"] == "<p>canonical copy</p>"

    async def test_thread_view_dedups_too(self, db_session):
        account = await _make_gmail_account(db_session, "sales@company.com")
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(
            db_session, contact, gmail_thread_id="thr-1", sender_account_id=account.id
        )
        await _make_sequence_send(
            db_session,
            enrollment,
            gmail_thread_id="thr-1",
            gmail_message_id="gm-1",
            sender_account_id=account.id,
        )
        db_session.add(
            EmailSend(
                id=uuid.uuid4(),
                from_email="sales@company.com",
                to_email="pat@prospect.com",
                subject="Quick question",
                body_html="<p>canonical copy</p>",
                status="sent",
                gmail_thread_id="thr-1",
                gmail_message_id="gm-1",
                sender_account_id=account.id,
                contact_id=contact.id,
                sent_at=datetime.now(timezone.utc),
            )
        )
        await db_session.commit()

        thread = await EmailHistoryService().get_thread(db_session, "thr-1")

        assert len(thread["messages"]) == 1


# ---------------------------------------------------------------------------
# Mailbox scoping
# ---------------------------------------------------------------------------

class TestSequenceSendScoping:

    async def test_scoped_to_the_users_own_mailboxes(self, db_session):
        mine = await _make_gmail_account(db_session, "mine@company.com")
        theirs = await _make_gmail_account(db_session, "theirs@company.com")
        contact = await _make_contact(db_session, "pat@prospect.com")

        my_enrollment = await _make_enrollment(
            db_session, contact, sender_account_id=mine.id
        )
        await _make_sequence_send(
            db_session,
            my_enrollment,
            from_email="mine@company.com",
            gmail_message_id="gm-mine",
            sender_account_id=mine.id,
        )
        their_enrollment = await _make_enrollment(
            db_session, contact, sender_account_id=theirs.id
        )
        await _make_sequence_send(
            db_session,
            their_enrollment,
            from_email="theirs@company.com",
            gmail_message_id="gm-theirs",
            sender_account_id=theirs.id,
        )
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(
            db_session, filter_type="all", user_gmail_emails={"mine@company.com"}
        )

        messages = [m for t in result["threads"] for m in t["messages"]]
        assert [m["gmail_message_id"] for m in messages] == ["gm-mine"]

    async def test_account_filter_scopes_sequence_sends(self, db_session):
        mine = await _make_gmail_account(db_session, "mine@company.com")
        theirs = await _make_gmail_account(db_session, "theirs@company.com")
        contact = await _make_contact(db_session, "pat@prospect.com")

        for account, message_id in ((mine, "gm-mine"), (theirs, "gm-theirs")):
            enrollment = await _make_enrollment(
                db_session, contact, sender_account_id=account.id
            )
            await _make_sequence_send(
                db_session,
                enrollment,
                from_email=account.email,
                gmail_message_id=message_id,
                sender_account_id=account.id,
            )
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(
            db_session, filter_type="all", gmail_account_id=mine.id
        )

        messages = [m for t in result["threads"] for m in t["messages"]]
        assert [m["gmail_message_id"] for m in messages] == ["gm-mine"]

    async def test_account_email_filter_scopes_sequence_sends(self, db_session):
        mine = await _make_gmail_account(db_session, "mine@company.com")
        theirs = await _make_gmail_account(db_session, "theirs@company.com")
        contact = await _make_contact(db_session, "pat@prospect.com")

        for account, message_id in ((mine, "gm-mine"), (theirs, "gm-theirs")):
            enrollment = await _make_enrollment(
                db_session, contact, sender_account_id=account.id
            )
            await _make_sequence_send(
                db_session,
                enrollment,
                from_email=account.email,
                gmail_message_id=message_id,
                sender_account_id=account.id,
            )
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(
            db_session, filter_type="all", account_email="mine@company.com"
        )

        messages = [m for t in result["threads"] for m in t["messages"]]
        assert [m["gmail_message_id"] for m in messages] == ["gm-mine"]

    async def test_rotated_sender_is_scoped_by_the_events_own_account(self, db_session):
        """Rotation can move a later step to another mailbox.

        The enrollment column only remembers the most recent sender, so scoping
        on it alone would file step 1 under whoever sent step 2. The event's own
        metadata is authoritative.
        """
        first = await _make_gmail_account(db_session, "first@company.com")
        second = await _make_gmail_account(db_session, "second@company.com")
        contact = await _make_contact(db_session, "pat@prospect.com")
        # Enrollment now points at ``second`` — the account that sent step 2.
        enrollment = await _make_enrollment(
            db_session, contact, sender_account_id=second.id
        )
        await _make_sequence_send(
            db_session,
            enrollment,
            step_number=1,
            from_email="first@company.com",
            gmail_message_id="gm-step1",
            sender_account_id=first.id,
        )
        await _make_sequence_send(
            db_session,
            enrollment,
            step_number=2,
            from_email="second@company.com",
            gmail_message_id="gm-step2",
            sender_account_id=second.id,
        )
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(
            db_session, filter_type="all", user_gmail_emails={"first@company.com"}
        )

        messages = [m for t in result["threads"] for m in t["messages"]]
        assert [m["gmail_message_id"] for m in messages] == ["gm-step1"]

    async def test_smtp_send_without_account_id_is_scoped_by_address(self, db_session):
        """SMTP sends carry no gmail_accounts FK — the address is all there is."""
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(db_session, contact)
        await _make_sequence_send(
            db_session,
            enrollment,
            from_email="smtp@company.com",
            gmail_message_id="gm-smtp",
        )
        await db_session.commit()

        service = EmailHistoryService()
        mine = await service.get_inbox(
            db_session, filter_type="all", user_gmail_emails={"smtp@company.com"}
        )
        assert mine["total"] == 1

        theirs = await service.get_inbox(
            db_session, filter_type="all", user_gmail_emails={"other@company.com"}
        )
        assert theirs["total"] == 0

    async def test_smtp_sender_id_is_not_published_as_a_gmail_account(self, db_session):
        """The dispatcher reports Gmail and SMTP account ids under one key.

        Publishing an SMTP id as ``gmail_account_id`` would have the frontend
        match it against Gmail accounts; the address is still carried.
        """
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(db_session, contact)
        await _make_sequence_send(
            db_session,
            enrollment,
            from_email="smtp@company.com",
            gmail_message_id="gm-smtp",
            # An id that belongs to no connected Gmail account.
            sender_account_id=uuid.uuid4(),
        )
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(db_session, filter_type="all")

        message = result["threads"][0]["messages"][0]
        assert message["gmail_account_id"] is None
        assert message["gmail_account_email"] == "smtp@company.com"
        assert message["from_email"] == "smtp@company.com"


# ---------------------------------------------------------------------------
# Multi-tenant isolation
# ---------------------------------------------------------------------------

class TestSequenceSendOrgIsolation:

    async def test_another_orgs_sequence_sends_are_invisible(self, db_session):
        """The global org filter must cover the new union arm too."""
        other_org = Organization(
            id=uuid.uuid4(), name="Other Co", slug="other-co", is_active=True
        )
        db_session.add(other_org)
        await db_session.flush()

        contact = await _make_contact(db_session, "pat@prospect.com")
        mine = await _make_enrollment(db_session, contact)
        await _make_sequence_send(db_session, mine, gmail_message_id="gm-mine")

        async with org_scope(other_org.id):
            other_contact = await _make_contact(db_session, "other@prospect.com")
            theirs = await _make_enrollment(db_session, other_contact)
            await _make_sequence_send(
                db_session, theirs, gmail_message_id="gm-theirs"
            )
        await db_session.commit()

        async with org_scope(APPREND_ORG_ID):
            result = await EmailHistoryService().get_inbox(
                db_session, filter_type="all"
            )

        messages = [m for t in result["threads"] for m in t["messages"]]
        assert [m["gmail_message_id"] for m in messages] == ["gm-mine"]


# ---------------------------------------------------------------------------
# Engagement, search, date bounds
# ---------------------------------------------------------------------------

class TestSequenceSendEngagement:

    async def test_open_and_click_stamps_come_from_sibling_events(self, db_session):
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(db_session, contact)
        await _make_sequence_send(
            db_session, enrollment, step_number=1, gmail_message_id="gm-1"
        )
        opened_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        await _make_engagement_event(
            db_session,
            enrollment,
            step_number=1,
            event_type="opened",
            occurred_at=opened_at,
        )
        await _make_engagement_event(
            db_session, enrollment, step_number=1, event_type="clicked"
        )
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(db_session, filter_type="all")

        message = result["threads"][0]["messages"][0]
        assert message["opened_at"] is not None
        assert message["clicked_at"] is not None

    async def test_engagement_does_not_bleed_across_steps(self, db_session):
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(db_session, contact)
        await _make_sequence_send(
            db_session, enrollment, step_number=1, gmail_message_id="gm-1"
        )
        await _make_sequence_send(
            db_session, enrollment, step_number=2, gmail_message_id="gm-2"
        )
        await _make_engagement_event(
            db_session, enrollment, step_number=2, event_type="opened"
        )
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(db_session, filter_type="all")

        by_message_id = {
            m["gmail_message_id"]: m
            for t in result["threads"]
            for m in t["messages"]
        }
        assert by_message_id["gm-1"]["opened_at"] is None
        assert by_message_id["gm-2"]["opened_at"] is not None

    async def test_opened_filter_keeps_only_opened_sequence_sends(self, db_session):
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(db_session, contact)
        await _make_sequence_send(
            db_session, enrollment, step_number=1, gmail_message_id="gm-1"
        )
        await _make_sequence_send(
            db_session, enrollment, step_number=2, gmail_message_id="gm-2"
        )
        await _make_engagement_event(
            db_session, enrollment, step_number=2, event_type="opened"
        )
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(db_session, filter_type="opened")

        messages = [m for t in result["threads"] for m in t["messages"]]
        assert [m["gmail_message_id"] for m in messages] == ["gm-2"]


class TestSequenceSendSearchAndDates:

    async def test_search_matches_subject_and_body(self, db_session):
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(db_session, contact)
        await _make_sequence_send(
            db_session,
            enrollment,
            step_number=1,
            subject="Tooling for your CNC line",
            body_html="<p>about swarf management</p>",
            gmail_message_id="gm-1",
        )
        await _make_sequence_send(
            db_session,
            enrollment,
            step_number=2,
            subject="Following up",
            body_html="<p>unrelated copy</p>",
            gmail_message_id="gm-2",
        )
        await db_session.commit()

        service = EmailHistoryService()

        by_subject = await service.get_inbox(
            db_session, filter_type="all", search="CNC"
        )
        assert [
            m["gmail_message_id"] for t in by_subject["threads"] for m in t["messages"]
        ] == ["gm-1"]

        by_body = await service.get_inbox(
            db_session, filter_type="all", search="swarf"
        )
        assert [
            m["gmail_message_id"] for t in by_body["threads"] for m in t["messages"]
        ] == ["gm-1"]

        # Both sends went to the same prospect, and neither carries a thread id,
        # so a recipient search returns each as its own thread.
        by_recipient = await service.get_inbox(
            db_session, filter_type="all", search="pat@prospect"
        )
        assert by_recipient["total"] == 2

    async def test_date_bounds_apply(self, db_session):
        contact = await _make_contact(db_session, "pat@prospect.com")
        enrollment = await _make_enrollment(db_session, contact)
        now = datetime.now(timezone.utc)
        await _make_sequence_send(
            db_session,
            enrollment,
            step_number=1,
            gmail_message_id="gm-old",
            occurred_at=now - timedelta(days=10),
        )
        await _make_sequence_send(
            db_session,
            enrollment,
            step_number=2,
            gmail_message_id="gm-new",
            occurred_at=now - timedelta(hours=1),
        )
        await db_session.commit()

        result = await EmailHistoryService().get_inbox(
            db_session, filter_type="all", date_from=now - timedelta(days=1)
        )

        messages = [m for t in result["threads"] for m in t["messages"]]
        assert [m["gmail_message_id"] for m in messages] == ["gm-new"]


# ---------------------------------------------------------------------------
# Thread + lead surfaces
# ---------------------------------------------------------------------------

class TestSequenceSendsOnOtherSurfaces:

    async def test_thread_view_includes_the_sequence_send(self, db_session):
        account = await _make_gmail_account(db_session, "sales@company.com")
        contact = await _make_contact(db_session, "pat@prospect.com", "Prospect Co")
        enrollment = await _make_enrollment(
            db_session, contact, gmail_thread_id="thr-1", sender_account_id=account.id
        )
        await _make_sequence_send(
            db_session,
            enrollment,
            gmail_thread_id="thr-1",
            gmail_message_id="gm-1",
            sender_account_id=account.id,
            occurred_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        db_session.add(
            EmailReply(
                id=uuid.uuid4(),
                from_email="pat@prospect.com",
                to_email="sales@company.com",
                subject="Re: Quick question",
                body_text="Interested.",
                snippet="Interested.",
                gmail_thread_id="thr-1",
                gmail_message_id="gm-2",
                gmail_account_id=account.id,
                contact_id=contact.id,
                received_at=datetime.now(timezone.utc),
                is_read=False,
            )
        )
        await db_session.commit()

        thread = await EmailHistoryService().get_thread(db_session, "thr-1")

        assert [m["direction"] for m in thread["messages"]] == ["sent", "received"]
        assert thread["lead_name"] == "Prospect Co"

    async def test_lead_view_includes_the_sequence_send(self, db_session):
        contact = await _make_contact(db_session, "pat@prospect.com", "Prospect Co")
        enrollment = await _make_enrollment(db_session, contact, gmail_thread_id="thr-1")
        await _make_sequence_send(
            db_session,
            enrollment,
            gmail_thread_id="thr-1",
            gmail_message_id="gm-1",
        )
        await db_session.commit()

        result = await EmailHistoryService().get_lead_emails(
            db_session, contact.company_id
        )

        assert result["total_messages"] == 1
        message = result["threads"][0]["messages"][0]
        assert message["gmail_message_id"] == "gm-1"
        assert message["company_name"] == "Prospect Co"
