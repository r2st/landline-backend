"""A reply is admitted on the *thread* and was actioned on the *address*.

``EmailHistoryService._is_outreach_reply`` decides what reaches the Dialtone
inbox, and its first and strongest rule is a Gmail thread match: this message
sits in a thread we started, so it is a reply to our outreach. Its docstring
says so — "replies stay in the same thread, so this catches essentially every
genuine reply".

The row it then writes resolves ``contact_id`` a completely different way:
``select(Contact).where(func.lower(Contact.email) == from_email)``. Address
only. So for every reply admitted by the thread rule whose sender we hold no
row for, ``contact_id`` is NULL — and that is not an exotic case, it is what an
alias, a personal address, a shared ``team@`` box, a forwarding mailbox or an
envelope-rewriting gateway looks like from our side.

``route_auto_action`` then had nothing to act on:

* ``unsubscribe`` — no flag, no consent record, **no suppression entry**, and
  the enrollment kept its schedule. The remaining steps went out to the address
  we originally mailed, which is precisely the address the person replying is
  telling us to stop using. Nothing raised; the operator notification said
  "They have been suppressed across all sequences (0 enrollment(s) stopped)".
* ``not_interested`` / ``wrong_person`` / ``out_of_office`` — read
  ``reply.contact_id`` directly rather than the contact the caller had already
  resolved, so they stopped/paused nothing for the same replies.

Two halves to the fix, and they fail differently, so both are pinned here:

1. ``_contact_via_thread`` walks the link the ingest already stored — the
   ``EmailSend`` matched by thread id, or the enrollment carrying that thread id
   (the sequence engine writes no ``EmailSend`` row at all) — back to the person
   we mailed.
2. The unsubscribe branch lists ``reply.from_email`` unconditionally. The
   suppression list is keyed by address and outlives every contact row, so it is
   the only half that can hold for an address we have no row for *yet* — the
   scraper makes rows.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
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
        self.sent = []

    async def notify_reply(self, *a, **kw):
        self.sent.append(kw or a)

    async def _send_notification(self, **kw):
        self.sent.append(kw)


def _result(category: str, **extra) -> dict:
    return {
        "category": category,
        "confidence": 0.99,
        "summary": "…",
        "source": "ai",
        **extra,
    }


async def _mk_contact(db: AsyncSession, company, email: str) -> Contact:
    contact = Contact(
        id=_uuid.uuid4(),
        company_id=company.id,
        first_name="Priya",
        last_name="Raman",
        email=email,
    )
    db.add(contact)
    await db.flush()
    return contact


async def _mk_enrollment(
    db: AsyncSession, contact, *, thread_id: str | None = None, status: str = "active"
) -> OutreachEnrollment:
    seq = OutreachSequence(
        id=_uuid.uuid4(), name=f"seq-{_uuid.uuid4().hex[:6]}", status="active"
    )
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        status=status,
        current_step=2,
        gmail_thread_id=thread_id,
    )
    db.add(enr)
    await db.flush()
    return enr


async def _mk_reply(
    db: AsyncSession,
    from_email: str,
    body: str,
    *,
    thread_id: str | None = None,
    email_send_id=None,
) -> EmailReply:
    """A reply the sync admitted on a thread match and could not name a contact for.

    ``contact_id`` is NULL — the ingest resolves it by ``From:`` address, and the
    whole point of this case is that the address is one we hold no row for.
    """
    reply = EmailReply(
        id=_uuid.uuid4(),
        contact_id=None,
        company_id=None,
        email_send_id=email_send_id,
        gmail_thread_id=thread_id,
        from_email=from_email,
        subject="Re: quick question",
        body_text=body,
        snippet=body[:200],
        received_at=datetime.now(timezone.utc),
    )
    db.add(reply)
    await db.flush()
    return reply


async def _suppression_entry(db: AsyncSession, email: str):
    return (
        await db.execute(
            select(SuppressionEntry).where(SuppressionEntry.email == email)
        )
    ).scalars().first()


# ---------------------------------------------------------------------------
# The finding: an unsubscribe from an alias on our own thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsubscribe_from_an_alias_stops_the_enrollment_it_replied_to(
    db_session: AsyncSession, sample_company
):
    """The sequence path, which is where the volume is.

    ``send_sequence_step`` writes no ``EmailSend`` row; the enrollment carries
    the ``gmail_thread_id``. So the enrollment is the *only* thing linking this
    reply to the person we mailed, and before the fix nothing looked at it.
    """
    thread = f"thread-{_uuid.uuid4().hex[:10]}"
    mailed = await _mk_contact(db_session, sample_company, "priya.raman@acme.com")
    enr = await _mk_enrollment(db_session, mailed, thread_id=thread)

    reply = await _mk_reply(
        db_session, "p.raman@personal.example", "please unsubscribe me", thread_id=thread
    )

    await classify_reply_record(
        db_session, reply, ai_service=FakeAI(_result("unsubscribe")),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(mailed)
    await db_session.refresh(enr)
    assert mailed.suppressed is True, "the person we mail is the one who must stop being mailed"
    assert enr.status == "unsubscribed", "the cadence must stop, not just the flag"

    # Both addresses end up on the list: the one we mailed (via suppress_contact)
    # and the one that actually asked (the reply's From:).
    assert await _suppression_entry(db_session, "priya.raman@acme.com") is not None
    assert await _suppression_entry(db_session, "p.raman@personal.example") is not None


@pytest.mark.asyncio
async def test_unsubscribe_resolves_through_the_matched_email_send(
    db_session: AsyncSession, sample_company
):
    """The other write path. A manual/template send leaves an ``EmailSend`` row,
    and the ingest stores its id on the reply after matching by thread."""
    mailed = await _mk_contact(db_session, sample_company, "dana@acme.com")
    enr = await _mk_enrollment(db_session, mailed)
    send = EmailSend(
        id=_uuid.uuid4(),
        contact_id=mailed.id,
        to_email="dana@acme.com",
        subject="quick question",
        status="sent",
        sent_at=datetime.now(timezone.utc) - timedelta(days=1),
        gmail_thread_id=f"thread-{_uuid.uuid4().hex[:10]}",
    )
    db_session.add(send)
    await db_session.flush()

    reply = await _mk_reply(
        db_session, "assistant@acme.com", "unsubscribe", email_send_id=send.id
    )

    await classify_reply_record(
        db_session, reply, ai_service=FakeAI(_result("unsubscribe")),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(mailed)
    await db_session.refresh(enr)
    assert mailed.suppressed is True
    assert enr.status == "unsubscribed"
    assert await _suppression_entry(db_session, "assistant@acme.com") is not None


@pytest.mark.asyncio
async def test_the_address_that_asked_is_listed_even_with_no_contact_anywhere(
    db_session: AsyncSession, sample_company
):
    """No contact row, no thread, no send — and still a durable record.

    This is the case the flags structurally cannot cover: there is no row to put
    a flag on. The list is keyed by address and is consulted by the send gate for
    rows that do not exist yet, which is exactly what the scraper creates next
    month.
    """
    reply = await _mk_reply(db_session, "stranger@elsewhere.example", "unsubscribe")

    await classify_reply_record(
        db_session, reply, ai_service=FakeAI(_result("unsubscribe")),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    entry = await _suppression_entry(db_session, "stranger@elsewhere.example")
    assert entry is not None, "an opt-out with nowhere to land still has to land"
    assert entry.reason == "unsubscribe"


@pytest.mark.asyncio
async def test_the_operator_is_not_told_a_suppression_that_did_not_happen(
    db_session: AsyncSession, sample_company
):
    """The notification is a claim about what the system did.

    A reply carrying no usable address resolves to no contact and lists nothing,
    and the old body said "They have been suppressed across all sequences"
    regardless — the same class of lie the unsubscribe confirmation page used to
    tell the recipient.
    """
    reply = await _mk_reply(db_session, "", "unsubscribe")
    notify = FakeNotify()

    await classify_reply_record(
        db_session, reply, ai_service=FakeAI(_result("unsubscribe")),
        notification_service=notify,
    )
    await db_session.flush()

    bodies = [n.get("body", "") for n in notify.sent if isinstance(n, dict)]
    assert bodies, "the operator is still told a reply asked to unsubscribe"
    assert any("review this reply by hand" in b for b in bodies)
    assert not any("have been suppressed" in b for b in bodies)


# ---------------------------------------------------------------------------
# The same omission on the other three branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_interested_from_an_alias_completes_the_enrollment(
    db_session: AsyncSession, sample_company
):
    """``not_interested`` read ``reply.contact_id`` — NULL here — so a prospect
    who said "no thanks" from an alias kept receiving the rest of the sequence."""
    thread = f"thread-{_uuid.uuid4().hex[:10]}"
    mailed = await _mk_contact(db_session, sample_company, "sam@acme.com")
    enr = await _mk_enrollment(db_session, mailed, thread_id=thread)
    reply = await _mk_reply(
        db_session, "sam.b@personal.example", "not a fit, thanks", thread_id=thread
    )

    await classify_reply_record(
        db_session, reply, ai_service=FakeAI(_result("not_interested")),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(enr)
    assert enr.status == "completed"
    # A "no thanks" is not an opt-out — it stops the cadence, it does not
    # suppress the address.
    await db_session.refresh(mailed)
    assert mailed.suppressed is False
    assert await _suppression_entry(db_session, "sam.b@personal.example") is None


@pytest.mark.asyncio
async def test_wrong_person_from_an_alias_flags_the_contact_we_mailed(
    db_session: AsyncSession, sample_company
):
    thread = f"thread-{_uuid.uuid4().hex[:10]}"
    mailed = await _mk_contact(db_session, sample_company, "lee@acme.com")
    enr = await _mk_enrollment(db_session, mailed, thread_id=thread)
    reply = await _mk_reply(
        db_session, "reception@acme.com", "Lee left the company", thread_id=thread
    )

    await classify_reply_record(
        db_session, reply, ai_service=FakeAI(_result("wrong_person")),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(mailed)
    await db_session.refresh(enr)
    assert mailed.status == "wrong_person"
    assert enr.status == "completed"


@pytest.mark.asyncio
async def test_out_of_office_from_an_alias_pauses_the_enrollment(
    db_session: AsyncSession, sample_company
):
    """An autoresponder often rewrites the sender. Pausing on
    ``reply.contact_id`` therefore paused nothing on exactly the replies OOO
    handling exists for, and the sequence kept mailing someone on leave."""
    thread = f"thread-{_uuid.uuid4().hex[:10]}"
    mailed = await _mk_contact(db_session, sample_company, "kai@acme.com")
    enr = await _mk_enrollment(db_session, mailed, thread_id=thread)
    reply = await _mk_reply(
        db_session, "no-reply@autoreply.acme.com", "I am away until next month",
        thread_id=thread,
    )

    await classify_reply_record(
        db_session, reply, ai_service=FakeAI(_result("out_of_office", return_date=None)),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(enr)
    assert enr.status == "paused"
    assert enr.pause_reason == "out_of_office"


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unrelated_thread_resolves_to_nobody(
    db_session: AsyncSession, sample_company
):
    """Widening the match must not make it match everything.

    A reply carrying a thread id nothing in the database knows about resolves to
    no contact, so no stranger's enrollment is stopped on its behalf.
    """
    mailed = await _mk_contact(db_session, sample_company, "quiet@acme.com")
    enr = await _mk_enrollment(
        db_session, mailed, thread_id=f"thread-{_uuid.uuid4().hex[:10]}"
    )
    reply = await _mk_reply(
        db_session, "someone@elsewhere.example", "unsubscribe",
        thread_id=f"unknown-{_uuid.uuid4().hex[:10]}",
    )

    await classify_reply_record(
        db_session, reply, ai_service=FakeAI(_result("unsubscribe")),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(mailed)
    await db_session.refresh(enr)
    assert mailed.suppressed is False, "an unrelated thread is not this contact's opt-out"
    assert enr.status == "active"
    # The address that wrote to us is still listed — that part needs no thread.
    assert await _suppression_entry(db_session, "someone@elsewhere.example") is not None


@pytest.mark.asyncio
async def test_the_operator_override_resolves_the_same_way(
    db_session: AsyncSession, sample_company, client
):
    """``POST /dialtone/inbox/{id}/reclassify`` is the human fix for a reply the
    classifier got wrong — so it is reached *most* for the replies that resolved
    to nobody. It routed on the contact ``_load_reply_context`` loads for
    display, which comes from ``reply.contact_id`` alone: the exact column that
    is NULL here. An operator marking this "unsubscribe" by hand changed the
    category and stopped nothing."""
    thread = f"thread-{_uuid.uuid4().hex[:10]}"
    mailed = await _mk_contact(db_session, sample_company, "rosa@acme.com")
    enr = await _mk_enrollment(db_session, mailed, thread_id=thread)
    reply = await _mk_reply(
        db_session, "rosa.g@personal.example", "take me off this list",
        thread_id=thread,
    )
    reply.reply_classification = "unknown"
    reply.classification_confidence = 0.2
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/dialtone/inbox/{reply.id}/reclassify",
        json={"category": "unsubscribe"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["handled_action"] == "unsubscribed"

    await db_session.refresh(mailed)
    await db_session.refresh(enr)
    assert mailed.suppressed is True
    assert enr.status == "unsubscribed"
    assert await _suppression_entry(db_session, "rosa.g@personal.example") is not None


@pytest.mark.asyncio
async def test_a_resolved_contact_id_still_wins(
    db_session: AsyncSession, sample_company
):
    """The control for the resolution order.

    When the ingest already named a contact, the thread fallback must not run and
    must not be able to override it — a shared thread with two enrollments would
    otherwise be able to redirect a named reply onto the wrong person.
    """
    thread = f"thread-{_uuid.uuid4().hex[:10]}"
    named = await _mk_contact(db_session, sample_company, "named@acme.com")
    other = await _mk_contact(db_session, sample_company, "other@acme.com")
    named_enr = await _mk_enrollment(db_session, named)
    other_enr = await _mk_enrollment(db_session, other, thread_id=thread)

    reply = await _mk_reply(db_session, "named@acme.com", "unsubscribe", thread_id=thread)
    reply.contact_id = named.id
    await db_session.flush()

    await classify_reply_record(
        db_session, reply, ai_service=FakeAI(_result("unsubscribe")),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(named)
    await db_session.refresh(other)
    await db_session.refresh(named_enr)
    await db_session.refresh(other_enr)
    assert named.suppressed is True and named_enr.status == "unsubscribed"
    assert other.suppressed is False and other_enr.status == "active"
