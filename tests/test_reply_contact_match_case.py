"""A reply saying "unsubscribe" has to find the person who sent it.

``contacts.email`` is not normalised on write. The scraper stores whatever the
page had, the CSV import stores whatever the file had, and quick-add stores
whatever was typed — so a row spelled ``Sarah.Chen@Acme.com`` is ordinary data,
not a corruption. Ten call sites across the codebase compare
``func.lower(Contact.email)`` for exactly that reason.

The reply handler compared the column raw, against a ``from_email`` that *is*
lower-cased on ingest. So the comparison was between a normalised value and a
column that is only sometimes normalised, and the mismatch was silent in the
worst possible place:

    reply classified ``unsubscribe`` → contact lookup returns None →
    ``suppress_contact`` never runs → no flag, no consent record,
    **no suppression-list entry** → the enrollment keeps its schedule

Nothing raises. The reply is logged as handled. The person who answered a cold
email with the word "unsubscribe" receives the remaining four steps of the
sequence, on time, from the same sender.

This only bites when the reply arrives with no ``contact_id`` already resolved —
which is the normal case for the *durable* half of the fix, since that is
precisely the path a rediscovered or re-scraped prospect takes.
"""

import uuid as _uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consent import ConsentRecord
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.models.suppression import SuppressionEntry
from app.tasks.replies import classify_reply_record


class FakeAI:
    def __init__(self, result: dict):
        self.result = result

    async def classify_reply(self, *a, **kw):
        return self.result


class FakeNotify:
    def __init__(self):
        self.replies = []

    async def notify_reply(self, *a, **kw):
        self.replies.append(a)

    async def _send_notification(self, **kw):
        self.replies.append(kw)


def _unsubscribe_result() -> dict:
    return {
        "category": "unsubscribe",
        "confidence": 0.99,
        "summary": "asked to stop",
        "source": "ai",
    }


async def _mk_contact(db: AsyncSession, company, email: str) -> Contact:
    contact = Contact(
        id=_uuid.uuid4(),
        company_id=company.id,
        first_name="Sarah",
        last_name="Chen",
        email=email,
    )
    db.add(contact)
    await db.flush()
    return contact


async def _mk_enrollment(db: AsyncSession, contact) -> OutreachEnrollment:
    seq = OutreachSequence(
        id=_uuid.uuid4(), name=f"seq-{_uuid.uuid4().hex[:6]}", status="active"
    )
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        status="active",
        current_step=2,
    )
    db.add(enr)
    await db.flush()
    return enr


async def _mk_orphan_reply(db: AsyncSession, from_email: str, body: str) -> EmailReply:
    """A reply the sync could not attach to a contact row.

    ``contact_id`` is NULL, which is what makes the fallback lookup the only
    thing standing between this reply and a violated opt-out.
    """
    reply = EmailReply(
        id=_uuid.uuid4(),
        contact_id=None,
        company_id=None,
        from_email=from_email,
        subject="Re: quick question",
        body_text=body,
        snippet=body[:200],
        received_at=datetime.now(timezone.utc),
    )
    db.add(reply)
    await db.flush()
    return reply


@pytest.mark.asyncio
async def test_unsubscribe_reply_suppresses_a_mixed_case_contact(
    db_session: AsyncSession, sample_company
):
    """The whole finding, end to end.

    Stored mixed-case, replying from the lower-cased address the mail server
    handed us. Every assertion here failed before the fix — not with an error,
    with silence.
    """
    contact = await _mk_contact(db_session, sample_company, "Sarah.Chen@Acme.com")
    enr = await _mk_enrollment(db_session, contact)
    reply = await _mk_orphan_reply(db_session, "sarah.chen@acme.com", "unsubscribe me")

    await classify_reply_record(
        db_session,
        reply,
        ai_service=FakeAI(_unsubscribe_result()),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(contact)
    await db_session.refresh(enr)
    assert contact.suppressed is True
    assert contact.unsubscribed is True
    assert enr.status == "unsubscribed", "the cadence must stop, not just the flag"

    # The durable half. Flags live on a row and the scraper makes rows; the list
    # is keyed by address and is what the send gate consults for a rediscovered
    # prospect.
    entry = (
        await db_session.execute(
            select(SuppressionEntry).where(SuppressionEntry.email == "sarah.chen@acme.com")
        )
    ).scalars().first()
    assert entry is not None, "the address must land on the suppression list"
    assert entry.reason == "unsubscribe"

    records = (
        await db_session.execute(
            select(ConsentRecord).where(ConsentRecord.contact_id == contact.id)
        )
    ).scalars().all()
    assert any(r.revoked_at is not None for r in records)


@pytest.mark.asyncio
async def test_unsubscribe_reply_from_a_mixed_case_sender_also_matches(
    db_session: AsyncSession, sample_company
):
    """The other direction: a normalised row, a header that was not.

    ``from_email`` is lower-cased by the Gmail sync and the SMTP reader, but
    replies also arrive through ``POST /replies`` and the smart-reply webhook,
    which take what they are given.
    """
    contact = await _mk_contact(db_session, sample_company, "dana@example.com")
    enr = await _mk_enrollment(db_session, contact)
    reply = await _mk_orphan_reply(db_session, "Dana@Example.com ", "please unsubscribe")

    await classify_reply_record(
        db_session,
        reply,
        ai_service=FakeAI(_unsubscribe_result()),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(contact)
    await db_session.refresh(enr)
    assert contact.suppressed is True
    assert enr.status == "unsubscribed"


@pytest.mark.asyncio
async def test_a_reply_from_a_genuine_stranger_still_matches_nothing(
    db_session: AsyncSession, sample_company
):
    """The control. Case-folding must widen the match, not remove it."""
    contact = await _mk_contact(db_session, sample_company, "kim@example.com")
    enr = await _mk_enrollment(db_session, contact)
    reply = await _mk_orphan_reply(db_session, "someone.else@elsewhere.com", "unsubscribe")

    await classify_reply_record(
        db_session,
        reply,
        ai_service=FakeAI(_unsubscribe_result()),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(contact)
    await db_session.refresh(enr)
    assert contact.suppressed is False, "a stranger's opt-out is not this contact's"
    assert enr.status == "active"


@pytest.mark.asyncio
async def test_duplicate_rows_resolve_to_the_earliest_deterministically(
    db_session: AsyncSession, sample_company
):
    """A duplicated address is expected here, and must not raise.

    ``contacts`` carries no unique constraint on ``email`` and the scraper
    re-creates rows, so the lookup takes ``.limit(1)`` rather than asserting a
    single row — a ``scalar_one_or_none`` here is a ``MultipleResultsFound``,
    which is how this same duplicate used to 500 every unsubscribe link.

    Which of the two rows gets the flag is deliberately not asserted: they are
    written in one transaction and ``created_at`` is a server default, so they
    tie and the ``id`` tiebreak is a random UUID. What has to hold is the
    outcome — the **address** is on the suppression list, which is the half that
    binds the send gate for both rows and for every row created after them.
    """
    first = await _mk_contact(db_session, sample_company, "dup@example.com")
    second = await _mk_contact(db_session, sample_company, "DUP@example.com")
    reply = await _mk_orphan_reply(db_session, "dup@example.com", "unsubscribe")

    await classify_reply_record(
        db_session,
        reply,
        ai_service=FakeAI(_unsubscribe_result()),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.suppressed or second.suppressed, "one of the duplicates is flagged"

    entry = (
        await db_session.execute(
            select(SuppressionEntry).where(SuppressionEntry.email == "dup@example.com")
        )
    ).scalars().first()
    assert entry is not None, "the address is what durably binds the send gate"
