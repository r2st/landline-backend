"""One bad inbound message must not take a mailbox — or the sweep — with it.

Two failures share a cause: the sync built one transaction for a whole mailbox
and had no way back from a statement that failed inside it.

* **The unbounded header.** ``_is_outreach_reply`` matched an inbound message's
  ``In-Reply-To`` / ``References`` against ids we recorded when sending, by
  putting every token into two ``IN`` clauses. Those headers are written by the
  sender of the inbound message — attacker-controlled, arriving from the open
  internet, and bounded by nothing between the mail server and here. Each token
  becomes several bind parameters, and every database driver refuses a statement
  past its own parameter ceiling. ``reply_matching`` was capped for exactly this
  reason; this second parser was not.

* **The cascade.** A failed statement leaves the session in a state where every
  later statement raises until someone rolls back. Nothing did — so the message
  after the bad one failed, and the one after that, and the ``commit`` at the
  end, and then *the next mailbox in the sweep*, and the caller's session after
  that. A crafted reply to one mailbox stopped reply ingestion for the whole
  org, and "no replies at all" is indistinguishable from "no replies yet".

A mailbox that stops ingesting looks exactly like a quiet mailbox, which is why
these are pinned rather than left to reading.
"""

import base64
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.services import crypto, google_oauth
from app.services.email_history import EmailHistoryService, _parse_message_ids
from app.services.reply_matching import MAX_MSGID_TOKENS


@pytest.fixture(autouse=True)
def _crypto_key():
    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


# --------------------------------------------------------------------------- #
# The header parser's bound
# --------------------------------------------------------------------------- #

def test_a_huge_references_header_cannot_grow_the_query():
    """Ten thousand ids in, a bounded number of bind parameters out."""
    references = " ".join(f"<junk-{i}@spam.example>" for i in range(10_000))

    ids = _parse_message_ids(None, references)

    # Two forms per token (raw and bracket-stripped), so the ceiling is 2x.
    assert len(ids) <= 2 * MAX_MSGID_TOKENS, len(ids)


def test_the_direct_parent_survives_the_trim():
    """``In-Reply-To`` is the id most likely to be one of ours — never dropped.

    Callers pass it first precisely because it is the *direct parent*. A plain
    tail-slice of the combined token list would discard exactly the token worth
    keeping, so each header is trimmed on its own.
    """
    references = " ".join(f"<junk-{i}@spam.example>" for i in range(10_000))

    ids = _parse_message_ids("<the-send@apprend.tech>", references)

    assert "<the-send@apprend.tech>" in ids
    assert "the-send@apprend.tech" in ids


def test_the_newest_references_entries_are_the_ones_kept():
    """A References chain runs oldest to newest; our send is near the end."""
    references = " ".join(f"<r{i}@spam.example>" for i in range(200))

    ids = _parse_message_ids(None, references)

    assert "<r199@spam.example>" in ids
    assert "<r0@spam.example>" not in ids


def test_an_ordinary_thread_is_untouched():
    """The cap costs a real conversation nothing."""
    ids = _parse_message_ids(
        "<b@apprend.tech>", "<a@prospect.co> <b@apprend.tech>"
    )
    assert ids == {
        "<a@prospect.co>", "a@prospect.co",
        "<b@apprend.tech>", "b@apprend.tech",
    }


@pytest.mark.asyncio
async def test_outreach_reply_gate_matches_through_a_flooded_references(
    db_session: AsyncSession,
):
    """The gate still recognises our own send, and the statement still runs."""
    db_session.add(EmailSend(
        id=uuid.uuid4(), from_email="sender@apprend.tech", to_email="lead@acme.com",
        subject="Intro", status="sent", gmail_thread_id="thread-acme",
        gmail_message_id="apisend-1", provider_message_id="<rfc-send-1@apprend.tech>",
    ))
    await db_session.commit()

    flood = " ".join(f"<junk-{i}@spam.example>" for i in range(10_000))
    assert await EmailHistoryService()._is_outreach_reply(
        db_session,
        "stranger@spam.example",
        None,
        "<rfc-send-1@apprend.tech>",
        flood,
    ) is True


# --------------------------------------------------------------------------- #
# The cascade
# --------------------------------------------------------------------------- #

def _b64(text_: str) -> str:
    return base64.urlsafe_b64encode(text_.encode()).decode().rstrip("=")


def _msg(msg_id: str, from_email: str) -> dict:
    return {
        "id": msg_id,
        "threadId": f"thread-{msg_id}",
        "snippet": "Re: hello",
        "internalDate": "1700000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": from_email},
                {"name": "To", "value": "sender@apprend.tech"},
                {"name": "Subject", "value": "Re: hello"},
            ],
            "body": {"data": _b64("Sounds good.")},
        },
    }


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Messages:
    def __init__(self, by_id):
        self._by_id = by_id

    def list(self, **kwargs):
        return _Exec({"messages": [{"id": mid} for mid in self._by_id]})

    def get(self, userId=None, id=None, format=None, **kwargs):
        return _Exec(self._by_id[id])


class _Users:
    def __init__(self, by_id):
        self._messages = _Messages(by_id)

    def messages(self):
        return self._messages


class _FakeService:
    def __init__(self, by_id):
        self._users = _Users(by_id)

    def users(self):
        return self._users


async def _account(db: AsyncSession, email: str, sub: str) -> GmailAccount:
    acct = GmailAccount(
        id=uuid.uuid4(),
        email=email,
        google_sub=sub,
        display_name="Sender",
        refresh_token_encrypted=crypto.encrypt("refresh-xyz"),
        access_token_encrypted=crypto.encrypt("access-xyz"),
        scopes=" ".join(google_oauth.SCOPES),
        is_active=True,
        status="connected",
    )
    db.add(acct)
    await db.flush()
    return acct


async def _poison_session(db: AsyncSession) -> None:
    """Fail one statement the way a real one fails, and leave the debris.

    The point of these two tests is the *session state a database error leaves
    behind*, so the failure has to produce that state and not merely raise. A
    failed **flush** is what does it, in every driver: SQLAlchemy deactivates
    the transaction and every statement after it raises ``PendingRollbackError``
    until someone rolls back.

    A failed ``SELECT`` — the obvious way to write this — does *not*, on SQLite.
    Postgres aborts the whole transaction on any error, so in production a bad
    SELECT cascades exactly like this; under the test engine it doesn't, and a
    test built on one passes with the rollback deleted. Which is the trap worth
    naming: a resilience test that cannot fail is indistinguishable from one
    that holds.
    """
    dup = (await db.execute(select(Company.domain).limit(1))).scalar_one()
    db.add(Company(
        id=uuid.uuid4(), name="Collides", domain=dup, country="US", source="test",
    ))
    await db.flush()


async def _known_contact(db: AsyncSession, email: str) -> Contact:
    # One company per contact, and ``companies.domain`` is unique — so the
    # domain is derived from the local part too. Two contacts at one domain is
    # the ordinary case here (pat@ and boom@ are colleagues).
    local, _, host = email.partition("@")
    company = Company(
        id=uuid.uuid4(), name=f"Co {email}", domain=f"{local}.{host}",
        country="US", source="test",
    )
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Pat",
        last_name="Prospect", email=email, source="test",
    )
    db.add(contact)
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq", status="active")
    db.add(seq)
    await db.flush()
    db.add(OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status="active",
    ))
    return contact


@pytest.mark.asyncio
async def test_one_failing_message_does_not_lose_the_rest_of_the_batch(
    db_session: AsyncSession, monkeypatch
):
    """A statement that fails on message 2 costs message 2 and nothing else."""
    account = await _account(db_session, "sender@apprend.tech", "sub-a")
    await _known_contact(db_session, "pat@prospect.co")
    await _known_contact(db_session, "boom@prospect.co")
    await db_session.commit()

    messages = {
        "m-1": _msg("m-1", "pat@prospect.co"),
        "m-2": _msg("m-2", "boom@prospect.co"),
        "m-3": _msg("m-3", "pat@prospect.co"),
    }
    monkeypatch.setattr(
        EmailHistoryService, "_build_gmail_service",
        lambda self, acct: _FakeService(messages),
    )

    original = EmailHistoryService._is_outreach_reply

    async def _poisoning(self, db, from_email, thread_id, in_reply_to, references):
        if from_email == "boom@prospect.co":
            await _poison_session(db)
        return await original(self, db, from_email, thread_id, in_reply_to, references)

    monkeypatch.setattr(EmailHistoryService, "_is_outreach_reply", _poisoning)

    result = await EmailHistoryService().sync_replies_for_account(
        db_session, account
    )

    stored = set(
        (
            await db_session.execute(select(EmailReply.gmail_message_id))
        ).scalars().all()
    )
    assert stored == {"m-1", "m-3"}, stored
    assert result["errors"] == 1, result


@pytest.mark.asyncio
async def test_a_failing_mailbox_does_not_stop_the_sweep(
    db_session: AsyncSession, monkeypatch
):
    """The account after the broken one still syncs on the same session."""
    broken = await _account(db_session, "broken@apprend.tech", "sub-broken")
    await _account(db_session, "healthy@apprend.tech", "sub-healthy")
    await _known_contact(db_session, "pat@prospect.co")
    await db_session.commit()
    # Held as a plain value: a rollback expires every loaded instance, and this
    # id is read again on the iteration *after* the one that rolls back.
    broken_id = broken.id

    messages = {"m-ok": _msg("m-ok", "pat@prospect.co")}
    monkeypatch.setattr(
        EmailHistoryService, "_build_gmail_service",
        lambda self, acct: _FakeService(messages),
    )

    original = EmailHistoryService.sync_replies_for_account

    async def _maybe_break(self, db, account):
        if account.id == broken_id:
            await _poison_session(db)
        return await original(self, db, account)

    monkeypatch.setattr(
        EmailHistoryService, "sync_replies_for_account", _maybe_break
    )

    result = await EmailHistoryService().sync_all_accounts(db_session)

    assert result["errors"] >= 1, result
    assert result["synced"] == 1, (
        "the healthy mailbox synced nothing — the broken one left the session "
        f"unusable ({result})"
    )
    stored = (
        await db_session.execute(select(EmailReply.gmail_message_id))
    ).scalars().all()
    assert list(stored) == ["m-ok"], stored

    # And the broken mailbox is not left marked as anything — a failed
    # statement is not a revoked token.
    still_connected = (
        await db_session.execute(
            select(GmailAccount.status).where(GmailAccount.id == broken_id)
        )
    ).scalar_one()
    assert still_connected == "connected"
