"""The Gmail reply sync must not run its HTTP calls on the event loop.

``POST /api/v1/emails/sync`` is a request-path entry into
``EmailHistoryService.sync_replies_for_account``, which talks to the Gmail API:
one ``messages().list`` plus one ``messages().get`` per message, up to 50 per
mailbox, across every connected account the caller owns. The google-api-python
client is synchronous, so every one of those round trips was made from inside
the coroutine — blocking the whole FastAPI worker, for every tenant, for as long
as the sweep took.

``smart_reply`` already offloads the identical calls (``_gexec``); this sync did
not. These tests pin the behaviour rather than the implementation: the Gmail
calls must happen on some other thread, and the loop must stay live while they
do.
"""

import asyncio
import base64
import threading
import time
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.services import crypto, google_oauth
from app.services.email_history import EmailHistoryService


@pytest.fixture(autouse=True)
def _crypto_key():
    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


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
    """A Gmail request whose ``execute`` records where — and how long — it ran."""

    def __init__(self, result, idents: list, delay: float):
        self._result = result
        self._idents = idents
        self._delay = delay

    def execute(self):
        self._idents.append(threading.get_ident())
        if self._delay:
            # Stands in for the network round trip. A blocking call made on the
            # loop thread stops every other coroutine for exactly this long.
            time.sleep(self._delay)
        return self._result


class _Messages:
    def __init__(self, by_id, idents, delay):
        self._by_id = by_id
        self._idents = idents
        self._delay = delay

    def list(self, **kwargs):
        return _Exec(
            {"messages": [{"id": mid} for mid in self._by_id]},
            self._idents, self._delay,
        )

    def get(self, userId=None, id=None, format=None, **kwargs):
        return _Exec(self._by_id[id], self._idents, self._delay)


class _Users:
    def __init__(self, by_id, idents, delay):
        self._messages = _Messages(by_id, idents, delay)

    def messages(self):
        return self._messages


class _FakeService:
    def __init__(self, by_id, idents, delay):
        self._users = _Users(by_id, idents, delay)

    def users(self):
        return self._users


async def _account(db: AsyncSession) -> GmailAccount:
    acct = GmailAccount(
        id=uuid.uuid4(),
        email="sender@apprend.tech",
        google_sub="sub-offload",
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


async def _known_contact(db: AsyncSession, email: str) -> Contact:
    """A contact we have enrolled, so the sync keeps their reply."""
    company = Company(
        id=uuid.uuid4(), name="Prospect Co", domain="prospect.co",
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


def _install(monkeypatch, messages, idents, delay=0.0):
    monkeypatch.setattr(
        EmailHistoryService,
        "_build_gmail_service",
        lambda self, acct: _FakeService(messages, idents, delay),
    )


@pytest.mark.asyncio
async def test_gmail_calls_run_off_the_event_loop(
    db_session: AsyncSession, monkeypatch
):
    """Every Gmail round trip happens on a worker thread, not the loop thread."""
    account = await _account(db_session)
    await _known_contact(db_session, "pat@prospect.co")
    await db_session.commit()

    messages = {
        "m-1": _msg("m-1", "Pat Prospect <pat@prospect.co>"),
        "m-2": _msg("m-2", "Pat Prospect <pat@prospect.co>"),
    }
    idents: list[int] = []
    _install(monkeypatch, messages, idents)

    loop_thread = threading.get_ident()
    result = await EmailHistoryService().sync_replies_for_account(
        db_session, account
    )

    assert result["synced"] == 2, result
    # list + one get per message.
    assert len(idents) == 3, idents
    assert loop_thread not in idents, (
        "a Gmail HTTP call ran on the event loop thread — the whole worker "
        f"stalls for its duration (loop={loop_thread}, calls={idents})"
    )


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_during_sync(
    db_session: AsyncSession, monkeypatch
):
    """Other coroutines keep running while a mailbox is being swept.

    Note the threshold cannot be 1. A blocked sweep still yields at each of its
    ``await db.execute(...)`` points, so the loop advances a handful of times
    even in the broken case — "made some progress" does not distinguish the two.
    What does is the *rate*: ~0.45s of network with the loop free services ~45
    of these timers, and with the loop held it services one per DB round trip.
    """
    account = await _account(db_session)
    await _known_contact(db_session, "pat@prospect.co")
    await db_session.commit()

    messages = {
        "m-1": _msg("m-1", "Pat Prospect <pat@prospect.co>"),
        "m-2": _msg("m-2", "Pat Prospect <pat@prospect.co>"),
    }
    _install(monkeypatch, messages, [], delay=0.15)  # ~0.45s of "network"

    ticks = 0
    done = False

    async def _ticker():
        nonlocal ticks
        while not done:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    await EmailHistoryService().sync_replies_for_account(db_session, account)
    done = True
    await ticker

    assert ticks >= 15, (
        f"the loop advanced only {ticks} time(s) during a ~0.45s sweep — it was "
        "blocked by the Gmail client"
    )


@pytest.mark.asyncio
async def test_sync_still_persists_replies(db_session: AsyncSession, monkeypatch):
    """Offloading changes where the call runs, not what the sweep writes."""
    account = await _account(db_session)
    contact = await _known_contact(db_session, "pat@prospect.co")
    await db_session.commit()

    _install(monkeypatch, {"m-1": _msg("m-1", "Pat Prospect <pat@prospect.co>")}, [])

    await EmailHistoryService().sync_replies_for_account(db_session, account)

    reply = (
        await db_session.execute(
            select(EmailReply).where(EmailReply.gmail_message_id == "m-1")
        )
    ).scalar_one()
    assert reply.contact_id == contact.id
    assert reply.gmail_account_id == account.id
    assert reply.from_email == "pat@prospect.co"
