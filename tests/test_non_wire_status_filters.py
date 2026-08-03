"""`status != "skipped"` is not the same question as "did this go out?".

`NON_WIRE_STATUSES` names seven statuses meaning nothing left the building, and
it exists so that a new status has to be classified by whoever adds it rather
than defaulting into whichever check is written first. Six queries predated it
and still asked `!= "skipped"`, which lets four other non-sends through:

* `suppressed` — the send gate matched a do-not-contact entry;
* `halted` — the kill switch was engaged;
* `pending` / `scheduled` — the row is queued, and `sent_at` is a NOT NULL
  `server_default`, so it carries a plausible timestamp from the moment it is
  written.

The direction is what makes it worth a test. Every one of those is a *non-send*
counted as a send, and two of them are the system refusing to send — so the
count grew precisely when sending stopped. On `danger_monitor` and
`deliverability_optimizer` that count is a **denominator**: engaging the kill
switch during a bad afternoon diluted the bounce rate that had just tripped it,
and a blast queued for next Tuesday diluted today's rate the moment it was
composed. A brake reading low is the one direction a brake must not err in.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import NON_WIRE_STATUS_VALUES, EmailSend
from app.models.gmail_account import GmailAccount
from app.services.danger_monitor import get_danger_monitor
from app.services.deliverability_optimizer import get_deliverability_optimizer


def test_the_ordered_tuple_matches_the_set():
    from app.models.email_template import NON_WIRE_STATUSES

    assert set(NON_WIRE_STATUS_VALUES) == set(NON_WIRE_STATUSES)
    assert NON_WIRE_STATUS_VALUES == tuple(sorted(NON_WIRE_STATUS_VALUES)), (
        "a stable order keeps the emitted IN (...) list identical per process"
    )


def test_every_refusal_and_queue_state_is_classified_as_a_non_send():
    """The four the old filter let through, named explicitly."""
    for status in ("suppressed", "halted", "pending", "scheduled"):
        assert status in NON_WIRE_STATUS_VALUES


async def _contact(db: AsyncSession) -> Contact:
    company = Company(id=uuid.uuid4(), name="Acme", source="test", country="US")
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, email="lead@acme.com",
        first_name="Dana", source="test",
    )
    db.add(contact)
    await db.flush()
    return contact


async def _send(db: AsyncSession, contact, status: str, *, account=None, when=None):
    db.add(EmailSend(
        id=uuid.uuid4(), contact_id=contact.id, to_email=contact.email,
        subject=f"S {status}", status=status,
        sent_at=when or datetime.now(timezone.utc),
        sender_account_id=account.id if account else None,
        from_email=account.email if account else None,
    ))
    await db.flush()


@pytest.mark.asyncio
async def test_the_kill_switch_does_not_dilute_the_bounce_rate(db_session: AsyncSession):
    """Twenty real sends, one bounce, then a hundred refusals.

    Engaging the kill switch writes a `halted` row per blocked send. Counting
    those as sends took a 5% bounce rate to 0.83% — the rail talking itself down
    from a breach with the evidence of its own intervention.
    """
    contact = await _contact(db_session)
    for _ in range(19):
        await _send(db_session, contact, "sent")
    await _send(db_session, contact, "bounced")
    await db_session.commit()

    before = await get_danger_monitor()._window_metrics(db_session)
    assert before["sent"] == 20
    assert before["bounce_rate"] == 0.05

    for _ in range(100):
        await _send(db_session, contact, "halted")
    await db_session.commit()

    after = await get_danger_monitor()._window_metrics(db_session)
    assert after["sent"] == 20, "a refused send is not a send"
    assert after["bounce_rate"] == 0.05


@pytest.mark.asyncio
async def test_a_scheduled_batch_does_not_dilute_it_either(db_session: AsyncSession):
    """`sent_at` has a NOT NULL server default, so a queued row looks sent."""
    contact = await _contact(db_session)
    for _ in range(19):
        await _send(db_session, contact, "sent")
    await _send(db_session, contact, "bounced")
    for _ in range(200):
        await _send(db_session, contact, "scheduled")
    for _ in range(50):
        await _send(db_session, contact, "suppressed")
    await db_session.commit()

    m = await get_danger_monitor()._window_metrics(db_session)
    assert m["sent"] == 20
    assert m["bounce_rate"] == 0.05


@pytest.mark.asyncio
async def test_the_sender_reputation_denominator_agrees(db_session: AsyncSession):
    """`deliverability_optimizer` divides the same way, over 7 days."""
    account = GmailAccount(
        id=uuid.uuid4(), email="sender@apprend.tech", google_sub=f"sub-{uuid.uuid4()}",
        refresh_token_encrypted="x", is_active=True,
    )
    db_session.add(account)
    await db_session.flush()
    contact = await _contact(db_session)

    when = datetime.now(timezone.utc) - timedelta(days=1)
    for _ in range(9):
        await _send(db_session, contact, "sent", account=account, when=when)
    await _send(db_session, contact, "bounced", account=account, when=when)
    for _ in range(90):
        await _send(db_session, contact, "halted", account=account, when=when)
    await db_session.commit()

    metrics = await get_deliverability_optimizer()._account_metrics(db_session, account)
    assert metrics["sent_7d"] == 10
    assert metrics["bounce_rate_7d"] == 0.1
