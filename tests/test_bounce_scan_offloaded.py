"""The bounce sweep must not run its Gmail calls on the event loop.

``BounceIngestionService.ingest_account`` and
``ComplaintIngestionService.ingest_account`` are the same function twice — the
complaint sweep's own docstring says it mirrors this one — and only one of them
offloaded. The bounce half made every round trip inline: a ``messages().list``
plus one ``messages().get`` per DSN, up to 100 per mailbox, across every
connected mailbox, with the loop held for all of it. ``ingest_smtp_account``
sitting directly below it wraps its IMAP reads in ``asyncio.to_thread``, which
is what makes the omission a slip rather than a decision.

These run on the beat worker, so what stalls is every other scheduled task and
coroutine sharing it — including the send queue.

Pinned as behaviour, not call shape: the Gmail calls must land on a thread that
is not the loop's, and the loop must keep servicing timers while they run.
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
from app.models.bounce_record import BounceRecord
from app.models.gmail_account import GmailAccount
from app.services import crypto, google_oauth
from app.services.bounce_ingestion import BounceIngestionService
from app.services.complaint_ingestion import ComplaintIngestionService


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


_DSN = """\
Your message could not be delivered.

Final-Recipient: rfc822; {email}
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 User unknown
"""


def _bounce_msg(msg_id: str, email: str) -> dict:
    return {
        "id": msg_id,
        "threadId": f"thread-{msg_id}",
        "snippet": "Delivery Status Notification (Failure)",
        "internalDate": "1700000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "mailer-daemon@googlemail.com"},
                {"name": "Subject", "value": "Delivery Status Notification (Failure)"},
            ],
            "body": {"data": _b64(_DSN.format(email=email))},
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


async def _account(db: AsyncSession, email: str = "sender@apprend.tech") -> GmailAccount:
    acct = GmailAccount(
        id=uuid.uuid4(),
        email=email,
        google_sub=f"sub-{uuid.uuid4()}",
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


def _install(monkeypatch, module, messages, idents, delay=0.0):
    """Replace ``build`` where the service under test looks it up.

    The service calls the module-level ``build`` through ``asyncio.to_thread``,
    so patching the name in that module covers both the offloaded and the
    inline shape — the test cannot pass merely because of *how* it patched.
    """
    monkeypatch.setattr(
        module, "build",
        lambda *a, **kw: _FakeService(messages, idents, delay),
    )


@pytest.mark.asyncio
async def test_bounce_scan_runs_gmail_calls_off_the_event_loop(
    db_session: AsyncSession, monkeypatch
):
    """Every Gmail round trip happens on a worker thread, not the loop thread."""
    import app.services.bounce_ingestion as mod

    account = await _account(db_session)
    await db_session.commit()

    messages = {
        "b-1": _bounce_msg("b-1", "dead1@prospect.co"),
        "b-2": _bounce_msg("b-2", "dead2@prospect.co"),
    }
    idents: list[int] = []
    _install(monkeypatch, mod, messages, idents)

    loop_thread = threading.get_ident()
    processed = await BounceIngestionService().ingest_account(db_session, account)

    assert len(processed) == 2, processed
    # list + one get per DSN.
    assert len(idents) == 3, idents
    assert loop_thread not in idents, (
        "a Gmail HTTP call ran on the event loop thread — the whole beat "
        f"worker stalls for its duration (loop={loop_thread}, calls={idents})"
    )


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_during_a_bounce_scan(
    db_session: AsyncSession, monkeypatch
):
    """Other coroutines keep running while a mailbox is being scanned.

    The threshold cannot be 1. A blocked scan still yields at each of its
    ``await db.execute(...)`` / ``await db.commit()`` points, so the loop
    advances a handful of times even in the broken case — "made some progress"
    does not separate the two. The *rate* does.
    """
    import app.services.bounce_ingestion as mod

    account = await _account(db_session)
    await db_session.commit()

    messages = {
        "b-1": _bounce_msg("b-1", "dead1@prospect.co"),
        "b-2": _bounce_msg("b-2", "dead2@prospect.co"),
    }
    _install(monkeypatch, mod, messages, [], delay=0.15)  # ~0.45s of "network"

    ticks = 0
    done = False

    async def _ticker():
        nonlocal ticks
        while not done:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    await BounceIngestionService().ingest_account(db_session, account)
    done = True
    await ticker

    assert ticks >= 15, (
        f"the loop advanced only {ticks} time(s) during a ~0.45s scan — it was "
        "blocked by the Gmail client"
    )


@pytest.mark.asyncio
async def test_bounce_scan_still_records_what_it_finds(
    db_session: AsyncSession, monkeypatch
):
    """Offloading changes where the call runs, not what the sweep writes."""
    import app.services.bounce_ingestion as mod

    account = await _account(db_session)
    await db_session.commit()

    _install(monkeypatch, mod, {"b-1": _bounce_msg("b-1", "dead@prospect.co")}, [])

    await BounceIngestionService().ingest_account(db_session, account)

    record = (
        await db_session.execute(
            select(BounceRecord).where(BounceRecord.gmail_message_id == "b-1")
        )
    ).scalar_one()
    assert record.recipient_email == "dead@prospect.co"
    assert record.gmail_account_id == account.id
    assert record.bounce_kind == "hard"


@pytest.mark.asyncio
async def test_the_complaint_sweep_it_mirrors_is_offloaded_too(
    db_session: AsyncSession, monkeypatch
):
    """The control: the sibling that was already correct, checked the same way.

    Without it this file only proves the bounce sweep changed, not that the two
    now agree — and "these two functions are the same function twice" is the
    fact that made the gap a slip in the first place.
    """
    import app.services.complaint_ingestion as mod

    account = await _account(db_session, "sender2@apprend.tech")
    await db_session.commit()

    idents: list[int] = []
    _install(monkeypatch, mod, {"c-1": _bounce_msg("c-1", "angry@prospect.co")}, idents)

    loop_thread = threading.get_ident()
    await ComplaintIngestionService().ingest_account(db_session, account)

    assert idents, "the complaint scan made no Gmail calls — the fake was not reached"
    assert loop_thread not in idents, idents
