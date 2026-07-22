"""Complaint / feedback-loop (FBL) ingestion from the mailbox.

Covers ARF parsing (the machine-readable Original-Rcpt-To path and the embedded
To: fallback), the guards that stop us suppressing the reporting infrastructure
or our own sender, the suppression path (contact + enrollments + suppression
list), and the dedup ledger that makes a repeated sweep safe.
"""

import base64
import uuid

import pytest
from sqlalchemy import select

from app.models.company import Company
from app.models.complaint_record import ComplaintRecord
from app.models.contact import Contact
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.models.suppression import SuppressionEntry
from app.services.complaint_handler import record_complaint
from app.services.complaint_ingestion import (
    ComplaintIngestionService,
    is_complaint_message,
    parse_complaint,
)

# A conformant RFC 5965 ARF abuse report (Yahoo-style).
ARF_REPORT = """This is an email abuse report for an email message received on 1 Jan.

Feedback-Type: abuse
User-Agent: Yahoo!-Mail-Feedback/2.0
Version: 1
Original-Mail-From: suman@apprend.tech
Original-Rcpt-To: angry@prospect.com
Reporting-MTA: dns; fbl.yahoo.com

--- original message ---
From: Suman <suman@apprend.tech>
To: angry@prospect.com
Subject: Quick question
"""

# A report that only names the recipient in the embedded original message.
ARF_FALLBACK = """We received a spam complaint about a message you sent.

From: Suman <suman@apprend.tech>
To: victim@corp.io
Subject: hello there
"""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_arf_machine_readable():
    parsed = parse_complaint(ARF_REPORT)
    assert parsed is not None
    assert parsed["email"] == "angry@prospect.com"
    assert parsed["feedback_type"] == "abuse"
    assert "fbl.yahoo.com" in (parsed["source"] or "")


def test_parse_arf_falls_back_to_embedded_to():
    parsed = parse_complaint(ARF_FALLBACK)
    assert parsed is not None
    assert parsed["email"] == "victim@corp.io"


def test_parse_ignores_reporting_infra_and_sender():
    # A report where the only address is the FBL/postmaster infra → no recipient.
    text = "Feedback-Type: abuse\nOriginal-Rcpt-To: feedback-loop@yahoo.com\n"
    assert parse_complaint(text) is None


def test_parse_returns_none_without_recipient():
    assert parse_complaint("just some prose, no addresses") is None


def test_unknown_feedback_type_normalised_to_other():
    parsed = parse_complaint(
        "Feedback-Type: something-weird\nOriginal-Rcpt-To: x@y.com\n"
    )
    assert parsed["feedback_type"] == "other"


def test_is_complaint_message_filter():
    assert is_complaint_message("feedback-loop@yahoo.com", "Email Feedback Report")
    assert is_complaint_message("x@y.com", "spam report")
    assert is_complaint_message("x@y.com", "hi", "Feedback-Type: abuse")
    assert not is_complaint_message("prospect@corp.io", "Re: your email", "sounds good")


# ---------------------------------------------------------------------------
# Suppression (record_complaint)
# ---------------------------------------------------------------------------

async def _contact_with_enrollment(db, email="angry@prospect.com"):
    company = Company(id=uuid.uuid4(), name="Acme", source="test", country="US")
    db.add(company)
    await db.flush()
    contact = Contact(id=uuid.uuid4(), company_id=company.id, email=email,
                      first_name="Dana", source="test")
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db.add_all([contact, seq])
    await db.flush()
    enr = OutreachEnrollment(id=uuid.uuid4(), contact_id=contact.id,
                             sequence_id=seq.id, status="active")
    db.add(enr)
    await db.flush()
    return contact, enr


async def test_record_complaint_suppresses_contact_and_lists_address(db_session):
    contact, enr = await _contact_with_enrollment(db_session)
    result = await record_complaint(
        db_session, contact.email, feedback_type="abuse", diagnostic="reported as spam"
    )
    assert result["matched_contacts"] == 1
    assert result["suppressed"] == 1
    assert result["enrollments_stopped"] == 1

    await db_session.refresh(contact)
    await db_session.refresh(enr)
    assert contact.suppressed is True
    assert contact.unsubscribed is True
    assert enr.status == "unsubscribed"

    # Address is on the org suppression list with reason 'complaint'.
    entry = (
        await db_session.execute(
            select(SuppressionEntry).where(SuppressionEntry.email == contact.email)
        )
    ).scalar_one_or_none()
    assert entry is not None
    assert entry.reason == "complaint"


async def test_record_complaint_lists_address_without_contact(db_session):
    # No contact row — the address must STILL be suppressed on the list so a
    # future import can never mail a known complainer.
    result = await record_complaint(db_session, "nobody@nowhere.com", feedback_type="abuse")
    assert result["matched_contacts"] == 0
    entry = (
        await db_session.execute(
            select(SuppressionEntry).where(SuppressionEntry.email == "nobody@nowhere.com")
        )
    ).scalar_one_or_none()
    assert entry is not None


# ---------------------------------------------------------------------------
# Ingestion sweep + dedup ledger
# ---------------------------------------------------------------------------

class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        return self._val


class _FakeMessages:
    def __init__(self, listing, msg):
        self._listing = listing
        self._msg = msg

    def list(self, userId, q, maxResults):
        return _Exec(self._listing)

    def get(self, userId, id, format=None):
        return _Exec(self._msg)


class _FakeUsers:
    def __init__(self, listing, msg):
        self._listing = listing
        self._msg = msg

    def messages(self):
        return _FakeMessages(self._listing, self._msg)


class _FakeGmail:
    def __init__(self, listing, msg):
        self._listing = listing
        self._msg = msg

    def users(self):
        return _FakeUsers(self._listing, self._msg)


def _arf_message(msg_id="arf-1", report=ARF_REPORT):
    data = base64.urlsafe_b64encode(report.encode()).decode()
    return {
        "id": msg_id,
        "snippet": "abuse report",
        "payload": {"mimeType": "text/plain", "body": {"data": data}},
    }


@pytest.fixture
def _patch_gmail(monkeypatch):
    """Patch the service's credential build + Gmail client to a fake."""
    listing = {"messages": [{"id": "arf-1"}]}
    msg = _arf_message()

    def _install(service):
        monkeypatch.setattr(service, "_build_credentials", lambda account: object())
        import app.services.complaint_ingestion as mod

        monkeypatch.setattr(mod, "build", lambda *a, **k: _FakeGmail(listing, msg))

    return _install


async def test_ingest_account_suppresses_and_ledgers(db_session, _patch_gmail):
    contact, enr = await _contact_with_enrollment(db_session)
    account = GmailAccount(
        id=uuid.uuid4(), email="rep@apprend.tech", google_sub=uuid.uuid4().hex,
        refresh_token_encrypted="x", status="connected", is_active=True,
    )
    db_session.add(account)
    await db_session.flush()

    service = ComplaintIngestionService()
    _patch_gmail(service)

    processed = await service.ingest_account(db_session, account, lookback_days=3)
    assert len(processed) == 1
    assert processed[0]["email"] == "angry@prospect.com"

    await db_session.refresh(contact)
    assert contact.suppressed is True

    # Ledger row written for dedup.
    rec = (
        await db_session.execute(
            select(ComplaintRecord).where(ComplaintRecord.gmail_message_id == "arf-1")
        )
    ).scalar_one_or_none()
    assert rec is not None
    assert rec.recipient_email == "angry@prospect.com"


async def test_ingest_is_idempotent_via_ledger(db_session, _patch_gmail):
    await _contact_with_enrollment(db_session)
    account = GmailAccount(
        id=uuid.uuid4(), email="rep2@apprend.tech", google_sub=uuid.uuid4().hex,
        refresh_token_encrypted="x", status="connected", is_active=True,
    )
    db_session.add(account)
    await db_session.flush()

    service = ComplaintIngestionService()
    _patch_gmail(service)

    first = await service.ingest_account(db_session, account, lookback_days=3)
    assert len(first) == 1
    # Second sweep sees the same message id and skips it — no double-processing.
    second = await service.ingest_account(db_session, account, lookback_days=3)
    assert second == []

    count = (
        await db_session.execute(
            select(ComplaintRecord).where(ComplaintRecord.gmail_message_id == "arf-1")
        )
    ).scalars().all()
    assert len(count) == 1
