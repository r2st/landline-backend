"""Bounce ingestion from the mailbox.

Covers DSN parsing (the machine-readable path and the loose fallback), the
guards that stop us suppressing the wrong address, and the dedup ledger that
makes a repeated sweep safe against the non-idempotent bounce handler.
"""

import uuid

import pytest

from app.models.bounce_record import BounceRecord
from app.services.bounce_ingestion import (
    BounceIngestionService,
    get_bounce_ingestion_service,
    parse_bounce,
)

# A conformant RFC 3464 hard bounce (unknown mailbox).
HARD_DSN = """Delivery to the following recipient failed permanently:

    deadlead@gone-corp.com

Reporting-MTA: dns; googlemail.com
Final-Recipient: rfc822; deadlead@gone-corp.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550-5.1.1 The email account that you tried to reach does not exist.
"""

# A transient failure — mailbox full.
SOFT_DSN = """Delivery to the following recipient has been delayed:

Final-Recipient: rfc822; busy@acme.com
Action: failed
Status: 4.2.2
Diagnostic-Code: smtp; 452 4.2.2 The recipient's inbox is out of storage space.
"""

# A provider that emits no DSN part at all.
LOOSE_BOUNCE = """Your message to nomailbox@example.org couldn't be delivered.

550 5.1.1 unknown user
"""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_hard_dsn():
    parsed = parse_bounce(HARD_DSN)
    assert parsed is not None
    assert parsed["email"] == "deadlead@gone-corp.com"
    assert parsed["status_code"] == "5.1.1"
    assert parsed["kind"] == "hard"
    assert "does not exist" in parsed["diagnostic"]


def test_parse_soft_dsn():
    parsed = parse_bounce(SOFT_DSN)
    assert parsed is not None
    assert parsed["email"] == "busy@acme.com"
    assert parsed["status_code"] == "4.2.2"
    assert parsed["kind"] == "soft", "a full mailbox is transient — never suppress on it"


def test_parse_falls_back_without_dsn_part():
    parsed = parse_bounce(LOOSE_BOUNCE)
    assert parsed is not None
    assert parsed["email"] == "nomailbox@example.org"
    assert parsed["kind"] == "hard"


def test_parse_ignores_postmaster_addresses():
    """The bounce sender must never be mistaken for the bounced recipient."""
    body = """From: mailer-daemon@googlemail.com
To: postmaster@oursender.com

Something went wrong. Contact postmaster@googlemail.com.
"""
    assert parse_bounce(body) is None


def test_parse_prefers_final_recipient_over_stray_addresses():
    body = """From: mailer-daemon@googlemail.com

Original message to sales@oursender.com could not be delivered.

Final-Recipient: rfc822; real-target@prospect.com
Status: 5.1.1
"""
    parsed = parse_bounce(body)
    assert parsed["email"] == "real-target@prospect.com"


def test_parse_returns_none_on_empty_or_unrelated():
    assert parse_bounce("") is None
    assert parse_bounce("Just a normal newsletter with no addresses") is None


def test_parse_never_raises_on_garbage():
    for junk in ("\x00\x01", "<<<>>>", "Status: not-a-code", "@@@@"):
        parse_bounce(junk)  # must not raise


# ---------------------------------------------------------------------------
# Message flattening
# ---------------------------------------------------------------------------

def test_message_text_walks_nested_parts():
    import base64

    def _b64(text: str) -> str:
        return base64.urlsafe_b64encode(text.encode()).decode()

    msg = {
        "snippet": "Delivery failed",
        "payload": {
            "parts": [
                {"body": {"data": _b64("Final-Recipient: rfc822; a@b.com")}},
                {"parts": [{"body": {"data": _b64("Status: 5.1.1")}}]},
            ]
        },
    }
    text = BounceIngestionService._message_text(msg)
    assert "Final-Recipient" in text
    assert "Status: 5.1.1" in text
    assert "Delivery failed" in text


def test_message_text_survives_undecodable_parts():
    msg = {"snippet": "x", "payload": {"body": {"data": "!!!not-base64!!!"}}}
    assert "x" in BounceIngestionService._message_text(msg)


# ---------------------------------------------------------------------------
# Dedup ledger
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_already_processed_detects_ledgered_message(db_session):
    service = get_bounce_ingestion_service()
    msg_id = f"msg-{uuid.uuid4()}"

    assert await service._already_processed(db_session, msg_id) is False

    db_session.add(
        BounceRecord(
            id=uuid.uuid4(), gmail_message_id=msg_id,
            recipient_email="x@y.com", bounce_kind="hard", result={},
        )
    )
    await db_session.flush()

    assert await service._already_processed(db_session, msg_id) is True


@pytest.mark.asyncio
async def test_bounce_record_message_id_is_unique(db_session):
    """The UNIQUE constraint is the safety mechanism, not just an index.

    record_bounce increments contacts.bounce_count, so re-processing one DSN
    would eventually suppress a live contact purely from polling frequency.
    """
    from sqlalchemy.exc import IntegrityError

    msg_id = f"msg-{uuid.uuid4()}"
    db_session.add(
        BounceRecord(id=uuid.uuid4(), gmail_message_id=msg_id,
                     recipient_email="a@b.com", bounce_kind="hard", result={})
    )
    await db_session.flush()

    db_session.add(
        BounceRecord(id=uuid.uuid4(), gmail_message_id=msg_id,
                     recipient_email="a@b.com", bounce_kind="hard", result={})
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Ingestion → suppression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hard_bounce_suppresses_the_contact(db_session, sample_contact):
    """End-to-end: the loop that was previously never closed automatically.

    Note the field: the bounce handler sets ``unsubscribed``, not ``suppressed``.
    Contact keeps those separate on purpose — ``suppressed`` is a reply-based
    opt-out, ``unsubscribed`` is a link/compliance block — and ``check_consent``
    blocks a send on either, so a bounced address stops receiving mail via the
    consent gate rather than the suppression gate.
    """
    from app.services.bounce_handler import record_bounce
    from app.services.consent import check_consent

    parsed = parse_bounce(HARD_DSN.replace("deadlead@gone-corp.com", sample_contact.email))
    assert parsed["kind"] == "hard"

    result = await record_bounce(
        db_session, sample_contact.email,
        status_code=parsed["status_code"], diagnostic=parsed["diagnostic"],
        kind=parsed["kind"],
    )
    await db_session.refresh(sample_contact)

    assert result["matched_contacts"] == 1
    assert result["suppressed"] == 1
    assert sample_contact.unsubscribed is True
    assert sample_contact.last_bounce_type == "hard"

    # The point of all of it: this address can no longer be emailed.
    company = await _company_for(db_session, sample_contact)
    verdict = await check_consent(db_session, sample_contact, company)
    assert verdict.can_email is False


async def _company_for(db, contact):
    from sqlalchemy import select

    from app.models.company import Company

    return (
        await db.execute(select(Company).where(Company.id == contact.company_id))
    ).scalar_one()


@pytest.mark.asyncio
async def test_soft_bounce_does_not_suppress_on_first_occurrence(db_session, sample_contact):
    from app.services.bounce_handler import record_bounce

    parsed = parse_bounce(SOFT_DSN)
    result = await record_bounce(
        db_session, sample_contact.email,
        status_code=parsed["status_code"], diagnostic=parsed["diagnostic"],
        kind=parsed["kind"],
    )
    await db_session.refresh(sample_contact)

    assert result["suppressed"] == 0
    assert sample_contact.unsubscribed is False
    assert sample_contact.bounce_count == 1


# --- Credentials ----------------------------------------------------------


@pytest.fixture
def crypto_key():
    """A valid Fernet key for the duration of one test.

    Deliberately not autouse: the DSN-parsing tests above are pure and must keep
    passing without crypto configured, the way they do in CI today.
    """
    from cryptography.fernet import Fernet

    from app.config import get_settings
    from app.services import crypto

    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()

# The sweep read `account.access_token` / `account.refresh_token`. Those
# attributes do not exist — GmailAccount stores `*_encrypted` — so every sweep
# raised AttributeError, which `_scan_account` caught, logged and turned into
# "0 bounces". Gmail has no bounce webhook, so this sweep is the only automatic
# path to the bounce handler: it silently ingested nothing, forever.


@pytest.mark.asyncio
async def test_build_credentials_decrypts_the_stored_tokens(db_session, crypto_key):
    from app.models.gmail_account import GmailAccount
    from app.services import crypto

    account = GmailAccount(
        id=uuid.uuid4(),
        email="sender@apprend.tech",
        google_sub=f"sub-{uuid.uuid4()}",
        refresh_token_encrypted=crypto.encrypt("refresh-abc"),
        access_token_encrypted=crypto.encrypt("access-xyz"),
        is_active=True,
    )
    db_session.add(account)
    await db_session.flush()

    creds = get_bounce_ingestion_service()._build_credentials(account)

    # Plaintext reaches Google, not ciphertext — and not an AttributeError.
    assert creds.refresh_token == "refresh-abc"
    assert creds.token == "access-xyz"


@pytest.mark.asyncio
async def test_build_credentials_works_without_a_stored_access_token(db_session, crypto_key):
    """access_token_encrypted is nullable; the refresh token mints a new one."""
    from app.models.gmail_account import GmailAccount
    from app.services import crypto

    account = GmailAccount(
        id=uuid.uuid4(),
        email="sender2@apprend.tech",
        google_sub=f"sub-{uuid.uuid4()}",
        refresh_token_encrypted=crypto.encrypt("refresh-only"),
        access_token_encrypted=None,
        is_active=True,
    )
    db_session.add(account)
    await db_session.flush()

    creds = get_bounce_ingestion_service()._build_credentials(account)

    assert creds.refresh_token == "refresh-only"
    assert creds.token is None


@pytest.mark.asyncio
async def test_build_credentials_survives_an_undecryptable_access_token(db_session, crypto_key):
    """A key rotation leaves old access tokens unreadable.

    That must not take out bounce ingestion: the refresh token still works, and
    a dead sweep means we keep mailing addresses that are already bouncing.
    """
    from app.models.gmail_account import GmailAccount
    from app.services import crypto

    account = GmailAccount(
        id=uuid.uuid4(),
        email="sender3@apprend.tech",
        google_sub=f"sub-{uuid.uuid4()}",
        refresh_token_encrypted=crypto.encrypt("refresh-good"),
        access_token_encrypted="not-decryptable-ciphertext",
        is_active=True,
    )
    db_session.add(account)
    await db_session.flush()

    creds = get_bounce_ingestion_service()._build_credentials(account)

    assert creds.refresh_token == "refresh-good"
    assert creds.token is None


@pytest.mark.asyncio
async def test_build_credentials_does_not_read_plaintext_attributes(db_session):
    """Pin the regression by name.

    GmailAccount has no `access_token`/`refresh_token`. If either name comes
    back, someone has reintroduced the plaintext columns — at which point the
    encryption-at-rest guarantee is gone and this file's fix is moot.
    """
    from app.models.gmail_account import GmailAccount

    assert not hasattr(GmailAccount, "access_token")
    assert not hasattr(GmailAccount, "refresh_token")
