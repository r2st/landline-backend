"""``/dialtone/analytics/senders`` — which mailbox is producing, not which is trusted.

`/deliverability/senders` compares mailboxes on bounce rate, complaint rate and
reputation: is this sender in trouble. Nothing answered whether a sender that
delivers perfectly ever gets a reply — and while it doesn't, it is spending
warm-up capacity the rest of the fleet could use.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep


async def _sequence(db: AsyncSession) -> OutreachSequence:
    seq = OutreachSequence(id=uuid.uuid4(), name="Sender perf seq")
    db.add(seq)
    await db.flush()
    db.add(SequenceStep(
        sequence_id=seq.id, step_number=1,
        subject_template="s", body_template="b",
    ))
    await db.flush()
    return seq


async def _account(db: AsyncSession, email: str) -> GmailAccount:
    acct = GmailAccount(
        id=uuid.uuid4(), email=email, google_sub=str(uuid.uuid4()),
        refresh_token_encrypted="x", status="connected",
    )
    db.add(acct)
    await db.flush()
    return acct


async def _enrollment(
    db: AsyncSession, seq: OutreachSequence, contact: Contact, sender_id=None
) -> OutreachEnrollment:
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, sender_account_id=sender_id,
    )
    db.add(enr)
    await db.flush()
    return enr


def _event(enr, event_type, when=None) -> OutreachEvent:
    return OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=1,
        event_type=event_type,
        occurred_at=when or datetime.now(timezone.utc),
    )


async def _report(client: AsyncClient, days: int = 30) -> dict:
    resp = await client.get(f"/api/v1/dialtone/analytics/senders?days={days}")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_each_mailbox_gets_its_own_funnel(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    seq = await _sequence(db_session)
    good = await _account(db_session, "good@apprend.tech")
    quiet = await _account(db_session, "quiet@apprend.tech")

    good_enr = await _enrollment(db_session, seq, sample_contact, good.id)
    quiet_enr = await _enrollment(db_session, seq, sample_contact, quiet.id)
    db_session.add_all([
        _event(good_enr, "sent"), _event(good_enr, "opened"),
        _event(good_enr, "clicked"), _event(good_enr, "replied"),
        _event(quiet_enr, "sent"), _event(quiet_enr, "sent"),
        _event(quiet_enr, "opened"),
    ])
    await db_session.commit()

    report = await _report(client)
    assert report["total_sent"] == 3
    by_email = {s["account_email"]: s for s in report["senders"]}

    assert by_email["good@apprend.tech"]["sent"] == 1
    assert by_email["good@apprend.tech"]["reply_rate"] == 1.0
    assert by_email["good@apprend.tech"]["click_rate"] == 1.0

    assert by_email["quiet@apprend.tech"]["sent"] == 2
    assert by_email["quiet@apprend.tech"]["replied"] == 0
    assert by_email["quiet@apprend.tech"]["reply_rate"] == 0.0
    assert by_email["quiet@apprend.tech"]["open_rate"] == 0.5


@pytest.mark.asyncio
async def test_best_reply_rate_first_with_volume_breaking_the_tie(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    """A mailbox with 1 send and 1 reply must not outrank 100 of each."""
    seq = await _sequence(db_session)
    small = await _account(db_session, "small@apprend.tech")
    big = await _account(db_session, "big@apprend.tech")

    small_enr = await _enrollment(db_session, seq, sample_contact, small.id)
    big_enr = await _enrollment(db_session, seq, sample_contact, big.id)
    db_session.add_all([_event(small_enr, "sent"), _event(small_enr, "replied")])
    for _ in range(4):
        db_session.add_all([_event(big_enr, "sent"), _event(big_enr, "replied")])
    await db_session.commit()

    senders = (await _report(client))["senders"]
    assert senders[0]["account_email"] == "big@apprend.tech"


@pytest.mark.asyncio
async def test_unpinned_sends_are_reported_not_dropped(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    """A table whose rows don't sum to the total is a table people stop trusting."""
    seq = await _sequence(db_session)
    acct = await _account(db_session, "pinned@apprend.tech")
    pinned = await _enrollment(db_session, seq, sample_contact, acct.id)
    orphan = await _enrollment(db_session, seq, sample_contact, None)
    db_session.add_all([_event(pinned, "sent"), _event(orphan, "sent")])
    await db_session.commit()

    report = await _report(client)
    assert report["total_sent"] == 2
    unattributed = [s for s in report["senders"] if s["unattributed"]]
    assert len(unattributed) == 1
    assert unattributed[0]["sent"] == 1
    assert unattributed[0]["account_id"] is None


@pytest.mark.asyncio
async def test_manual_touches_are_not_sends(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    seq = await _sequence(db_session)
    acct = await _account(db_session, "caller@apprend.tech")
    enr = await _enrollment(db_session, seq, sample_contact, acct.id)
    db_session.add_all([
        _event(enr, "task_created"), _event(enr, "task_completed"),
    ])
    await db_session.commit()

    assert (await _report(client))["total_sent"] == 0


@pytest.mark.asyncio
async def test_the_window_is_honoured(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    seq = await _sequence(db_session)
    acct = await _account(db_session, "old@apprend.tech")
    enr = await _enrollment(db_session, seq, sample_contact, acct.id)
    db_session.add(_event(
        enr, "sent", when=datetime.now(timezone.utc) - timedelta(days=45)
    ))
    await db_session.commit()

    assert (await _report(client, days=30))["total_sent"] == 0
    assert (await _report(client, days=90))["total_sent"] == 1


@pytest.mark.asyncio
async def test_no_outreach_reports_an_empty_fleet(client: AsyncClient):
    report = await _report(client)
    assert report["senders"] == []
    assert report["total_sent"] == 0
