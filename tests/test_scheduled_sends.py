""""Send later" scheduled a message and then nothing ever sent it.

``POST /email-templates/compose`` takes a ``scheduled_at`` — the Compose tab's
"Send later" mode posts one — and wrote an ``EmailSend`` with
``status='scheduled'``. Nothing in the codebase read that status back: no beat
entry, no sweep, no query. The row sat there permanently while the endpoint had
already answered "scheduled for later" and counted it in the batch's ``sent``
total, so the operator's own confirmation said the mail was handled.

The first test here is the one that would have caught it, and it is deliberately
not a test of the new sweep: it asks the beat schedule whether *anything* ever
reads the status the endpoint writes. A feature that is only reachable from a
Celery entry has that entry as part of its implementation, and the failure mode
when it's missing is silence.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.tasks import scheduled_sends
from app.tasks.celery_app import celery_app

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def test_something_in_the_beat_schedule_sends_scheduled_mail():
    """The status the compose endpoint writes has a reader.

    ``scheduled`` is a terminal state unless a scheduled job moves it, so the
    beat entry is not configuration around the feature — it is the half of the
    feature that puts mail on the wire.
    """
    tasks = {e["task"] for e in celery_app.conf.beat_schedule.values()}
    assert "app.tasks.scheduled_sends.send_due_scheduled_emails" in tasks
    assert "app.tasks.scheduled_sends.send_due_scheduled_emails" in celery_app.tasks


def _row(*, due: datetime, status: str = "scheduled", body="<p>Hi</p>") -> EmailSend:
    return EmailSend(
        id=uuid.uuid4(),
        to_email=f"{uuid.uuid4().hex[:8]}@prospect.com",
        subject="Quick question",
        status=status,
        sent_at=NOW - timedelta(days=7),
        scheduled_at=due,
        body_html=body,
    )


@pytest.fixture
def dispatched(monkeypatch):
    """Capture dispatcher calls; every send lands as ``sent``."""
    calls = []

    async def _fake(db, **kwargs):
        calls.append(kwargs)
        return {"status": "sent", "message_id": "m-1", "from_email": "us@apprend.tech"}

    monkeypatch.setattr(scheduled_sends, "send_outreach_email", _fake)
    return calls


@pytest.mark.asyncio
async def test_a_due_row_is_sent_and_marked(
    db_session: AsyncSession, dispatched: list,
):
    row = _row(due=NOW - timedelta(minutes=1))
    db_session.add(row)
    await db_session.commit()

    result = await scheduled_sends.send_due_scheduled_for_session(db_session, now=NOW)

    assert result == {"due": 1, "sent": 1, "failed": 0, "abandoned": 0}
    assert [c["to_email"] for c in dispatched] == [row.to_email]
    assert dispatched[0]["body_html"] == "<p>Hi</p>"
    await db_session.refresh(row)
    assert row.status == "sent"


@pytest.mark.asyncio
async def test_a_row_that_is_not_due_yet_is_left_alone(
    db_session: AsyncSession, dispatched: list,
):
    """The bound is the point of the feature — early is as wrong as never."""
    row = _row(due=NOW + timedelta(hours=1))
    db_session.add(row)
    await db_session.commit()

    result = await scheduled_sends.send_due_scheduled_for_session(db_session, now=NOW)

    assert result["due"] == 0
    assert dispatched == []
    await db_session.refresh(row)
    assert row.status == "scheduled"


@pytest.mark.asyncio
async def test_the_send_time_is_when_it_went_not_when_it_was_composed(
    db_session: AsyncSession, dispatched: list,
):
    """``sent_at`` is stamped at compose time by a NOT NULL server default.

    Left alone, a message composed on the 8th and sent on the 15th reports as an
    8th send — it lands in the wrong reporting window, and on the wrong side of
    any date filter the operator uses to check the batch went out.
    """
    row = _row(due=NOW - timedelta(minutes=1))
    db_session.add(row)
    await db_session.commit()

    await scheduled_sends.send_due_scheduled_for_session(db_session, now=NOW)

    await db_session.refresh(row)
    # SQLite drops tzinfo on a stored TIMESTAMP; the column is UTC either way.
    stamped = row.sent_at.replace(tzinfo=row.sent_at.tzinfo or timezone.utc)
    assert stamped > NOW - timedelta(days=1)


@pytest.mark.asyncio
async def test_a_row_is_claimed_before_it_is_sent(
    db_session: AsyncSession, monkeypatch,
):
    """The status leaves ``scheduled`` before the transport is touched.

    Same interlock as ``send_claim`` for sequence steps: the gap between "the
    transport accepted it" and "the row saying so committed" is where duplicate
    cold email comes from. Asserted by looking at the row from inside the
    dispatcher — if the claim came afterwards, a crash here would leave a row
    still eligible for the next pass and the prospect gets it twice.
    """
    row = _row(due=NOW - timedelta(minutes=1))
    db_session.add(row)
    await db_session.commit()
    seen = {}

    async def _fake(db, **kwargs):
        seen["status"] = (
            await db.execute(select(EmailSend.status).where(EmailSend.id == row.id))
        ).scalar_one()
        return {"status": "sent"}

    monkeypatch.setattr(scheduled_sends, "send_outreach_email", _fake)
    await scheduled_sends.send_due_scheduled_for_session(db_session, now=NOW)

    assert seen["status"] != "scheduled"


@pytest.mark.asyncio
async def test_a_refusal_from_the_dispatcher_is_recorded_as_itself(
    db_session: AsyncSession, monkeypatch,
):
    """A scheduled send clears the same gates as an immediate one.

    A message queued on Monday and due on Thursday must still see Wednesday's
    unsubscribe, so the sweep goes through ``send_outreach_email`` rather than
    holding a transport. The refusal it returns is recorded verbatim — writing
    ``sent`` here would put the compliance failure back where it can't be seen.
    """
    row = _row(due=NOW - timedelta(minutes=1))
    db_session.add(row)
    await db_session.commit()

    async def _fake(db, **kwargs):
        return {"status": "suppressed", "reason": "on the do-not-contact list"}

    monkeypatch.setattr(scheduled_sends, "send_outreach_email", _fake)
    result = await scheduled_sends.send_due_scheduled_for_session(db_session, now=NOW)

    assert result["sent"] == 0 and result["failed"] == 1
    await db_session.refresh(row)
    assert row.status == "suppressed"
    assert row.error == "on the do-not-contact list"


@pytest.mark.asyncio
async def test_a_transport_exception_does_not_stop_the_batch(
    db_session: AsyncSession, monkeypatch,
):
    first = _row(due=NOW - timedelta(minutes=2))
    second = _row(due=NOW - timedelta(minutes=1))
    db_session.add_all([first, second])
    await db_session.commit()

    async def _fake(db, *, to_email, **kwargs):
        if to_email == first.to_email:
            raise RuntimeError("smtp host is down")
        return {"status": "sent"}

    monkeypatch.setattr(scheduled_sends, "send_outreach_email", _fake)
    result = await scheduled_sends.send_due_scheduled_for_session(db_session, now=NOW)

    assert result["sent"] == 1 and result["failed"] == 1
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.status == "error" and "smtp host is down" in (first.error or "")
    assert second.status == "sent"


@pytest.mark.asyncio
async def test_a_legacy_row_with_no_stored_body_is_retired_not_retried(
    db_session: AsyncSession, dispatched: list,
):
    """Rows scheduled before the body was persisted cannot be recovered.

    The compose endpoint used to ``continue`` before storing the rendered copy,
    so those rows carry a subject and nothing else, and the merge context that
    produced the body is gone. Left ``scheduled`` they would be re-read by every
    pass forever; sending them empty is worse.
    """
    row = _row(due=NOW - timedelta(minutes=1), body=None)
    db_session.add(row)
    await db_session.commit()

    result = await scheduled_sends.send_due_scheduled_for_session(db_session, now=NOW)

    assert result["abandoned"] == 1
    assert dispatched == []
    await db_session.refresh(row)
    assert row.status == "error"

    # And the next pass doesn't see it again.
    assert (await scheduled_sends.send_due_scheduled_for_session(
        db_session, now=NOW
    ))["due"] == 0


@pytest.mark.asyncio
async def test_compose_stores_the_rendered_copy_it_will_need_later(
    client, sample_contact, db_session: AsyncSession,
):
    """The other half: what the endpoint has to leave behind for the sweep.

    The merge context is built from the contact and the company *as they are at
    compose time* and nothing reconstructs it later, so the rendered body has to
    be persisted with the row. It wasn't — the scheduled branch returned before
    the assignment — which is the second reason those rows could never have gone
    out even with a sweep in place.
    """
    await client.post("/api/v1/email-templates", json={
        "name": "Later", "subject": "Hi {{first_name}}",
        "body": "<p>About {{company}}</p>",
    })
    due = (NOW + timedelta(days=3)).isoformat()

    resp = await client.post("/api/v1/email-compose", json={
        "contact_ids": [str(sample_contact.id)], "scheduled_at": due,
    })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Not "sent" — nothing left the building. Reporting it as a send is how the
    # operator was told a batch had gone out that never would.
    assert body["scheduled"] == 1
    assert body["sent"] == 0

    row = (
        await db_session.execute(
            select(EmailSend).where(EmailSend.to_email == sample_contact.email)
        )
    ).scalars().first()
    assert row is not None and row.status == "scheduled"
    assert row.body_html and sample_contact.first_name in (row.subject or "")


@pytest.mark.asyncio
async def test_one_pass_is_bounded(db_session: AsyncSession, dispatched: list):
    """A blast scheduled for one minute is not one unbounded Celery task."""
    for i in range(scheduled_sends.BATCH_LIMIT + 5):
        db_session.add(_row(due=NOW - timedelta(minutes=i + 1)))
    await db_session.commit()

    result = await scheduled_sends.send_due_scheduled_for_session(db_session, now=NOW)

    assert result["due"] == scheduled_sends.BATCH_LIMIT


@pytest.fixture
def _fernet_key():
    """A real encryption key — ``GmailAccount`` tokens are encrypted at rest."""
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


@pytest.mark.asyncio
async def test_a_scheduled_row_pins_the_mailbox_it_was_composed_as(
    _fernet_key,
    client,
    sample_contact,
    db_session: AsyncSession,
):
    """Whose mailbox a "send later" batch goes out from is decided at compose.

    An immediate send never had to record this: ``payload.sender_account_id``
    reaches the dispatcher inside the same call. A scheduled row is re-resolved
    by the sweep days later, and it left ``sender_account_id`` NULL — which
    ``resolve_sender_info`` does not read as "the default account". It reads as
    "no preference", runs ``get_next_rotation_account``, and picks whichever
    mailbox is *least recently used at send time*.

    So the operator picks their mailbox in the Compose tab, the response
    confirms that address back to them as ``sender``, and on Thursday the
    batch goes out from a colleague's — with the warm-up ramp, the daily
    counter and the sending reputation all landing on the wrong account.
    """
    from app.services import crypto, google_oauth

    chosen = GmailAccount(
        id=uuid.uuid4(), email="picked@apprend.tech", google_sub="sub-picked",
        display_name="Picked", refresh_token_encrypted=crypto.encrypt("r"),
        access_token_encrypted=crypto.encrypt("a"),
        scopes=" ".join(google_oauth.SCOPES), is_active=False, status="connected",
    )
    # The account rotation would hand back instead: never used, so it sorts
    # first on the least-recently-used key.
    other = GmailAccount(
        id=uuid.uuid4(), email="rotation@apprend.tech", google_sub="sub-rotation",
        display_name="Rotation", refresh_token_encrypted=crypto.encrypt("r"),
        access_token_encrypted=crypto.encrypt("a"),
        scopes=" ".join(google_oauth.SCOPES), is_active=True, status="connected",
    )
    db_session.add_all([chosen, other])
    await db_session.commit()

    await client.post("/api/v1/email-templates", json={
        "name": "Later", "subject": "Hi {{first_name}}", "body": "<p>Hello</p>",
    })

    resp = await client.post("/api/v1/email-compose", json={
        "contact_ids": [str(sample_contact.id)],
        "sender_account_id": str(chosen.id),
        "scheduled_at": (NOW + timedelta(days=3)).isoformat(),
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["sender"] == chosen.email

    row = (
        await db_session.execute(
            select(EmailSend).where(EmailSend.to_email == sample_contact.email)
        )
    ).scalars().first()
    assert row is not None and row.status == "scheduled"
    # The promise the response already made, written down where the sweep reads.
    assert row.sender_account_id == chosen.id


@pytest.mark.asyncio
async def test_the_sweep_sends_from_the_pinned_mailbox(
    db_session: AsyncSession, dispatched: list,
):
    """And the pin is what reaches the dispatcher, not a fresh resolution."""
    account_id = uuid.uuid4()
    row = _row(due=NOW - timedelta(minutes=1))
    row.sender_account_id = account_id
    db_session.add(row)
    await db_session.commit()

    await scheduled_sends.send_due_scheduled_for_session(db_session, now=NOW)

    assert dispatched[0]["sender_account_id"] == account_id
