"""The stale-lead sweep must read every source of "we already emailed them".

Three ways the old query got the answer wrong, all of them in
``generate_stale_lead_followups_for_session``:

* it read ``EmailSend`` alone, so the sequence engine's sends — the bulk of
  outreach volume, recorded as ``OutreachEvent(event_type='sent')`` and nothing
  else — were invisible, and a prospect mid-cadence was filed as never emailed;
* it counted every ``EmailSend`` row whatever its status, so a ``skipped`` one
  (no address, suppressed recipient, spam-gate block, warm-up hold, spent cap,
  engaged kill switch) read as a recent contact and held the lead out;
* it read the two contact flags and not the org do-not-contact list, so it put
  "reach out to this person" on a human's task list for an address nothing is
  allowed to mail.

The first two are opposite errors from the same omission, which is why both are
pinned here rather than one standing in for the other.
"""

from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.follow_up import FollowUp
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services import suppression
from app.tasks.followups import generate_stale_lead_followups_for_session


async def _company(db, name="Chase Co", stage="contacted"):
    c = Company(name=name, country="US", source="test", stage=stage)
    db.add(c)
    await db.flush()
    return c


async def _contact(db, company, email):
    c = Contact(company_id=company.id, first_name="X", email=email, source="test")
    db.add(c)
    await db.flush()
    return c


async def _sequence(db, name="Seq"):
    s = OutreachSequence(name=name, is_active=True)
    db.add(s)
    await db.flush()
    return s


async def _sequence_send(db, contact, sequence, *, days_ago: int):
    """A send the way the sequence engine records it: an event, no ``EmailSend``."""
    enr = OutreachEnrollment(
        contact_id=contact.id,
        sequence_id=sequence.id,
        status="active",
        current_step=2,
    )
    db.add(enr)
    await db.flush()
    db.add(OutreachEvent(
        enrollment_id=enr.id,
        step_number=1,
        event_type="sent",
        occurred_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    ))
    await db.flush()
    return enr


async def _flagged(db) -> set:
    rows = await db.execute(select(FollowUp.contact_id))
    return {r[0] for r in rows.all()}


@pytest.mark.asyncio
async def test_recent_sequence_send_is_not_a_stale_lead(db_session):
    """The regression: mid-cadence prospects filed as "never been emailed".

    ``recent`` was mailed by the sequence engine yesterday and has no
    ``EmailSend`` row, because that path never writes one.
    """
    co = await _company(db_session)
    seq = await _sequence(db_session)
    recent = await _contact(db_session, co, "midcadence@a.com")
    await _sequence_send(db_session, recent, seq, days_ago=1)

    result = await generate_stale_lead_followups_for_session(db_session)
    await db_session.flush()

    assert result["created"] == 0
    assert recent.id not in await _flagged(db_session)


@pytest.mark.asyncio
async def test_old_sequence_send_still_goes_stale(db_session):
    """Reading the second table must not make the contact permanently fresh."""
    co = await _company(db_session)
    seq = await _sequence(db_session)
    cold = await _contact(db_session, co, "wentquiet@a.com")
    await _sequence_send(db_session, cold, seq, days_ago=90)

    result = await generate_stale_lead_followups_for_session(db_session)
    await db_session.flush()

    assert result["created"] == 1
    assert cold.id in await _flagged(db_session)
    fu = (await db_session.execute(select(FollowUp))).scalars().first()
    # Dated from the real send, not reported as a never-contacted lead.
    assert "never been emailed" not in fu.reason
    assert "90 days since last email" in fu.reason


@pytest.mark.asyncio
async def test_the_newest_send_wins_across_both_tables(db_session):
    """An old direct send and a recent sequence send is still a recent contact."""
    co = await _company(db_session)
    seq = await _sequence(db_session)
    c = await _contact(db_session, co, "both@a.com")
    db_session.add(EmailSend(
        contact_id=c.id, to_email=c.email, subject="hi", status="sent",
        sent_at=datetime.now(timezone.utc) - timedelta(days=120),
    ))
    await _sequence_send(db_session, c, seq, days_ago=2)

    result = await generate_stale_lead_followups_for_session(db_session)
    assert result["created"] == 0


@pytest.mark.asyncio
async def test_skipped_send_is_not_a_contact(db_session):
    """The opposite error: a send that never reached the wire holding a lead back.

    ``skipped`` stamps ``sent_at`` like any other row. Counting it means the day
    a spent cap or an engaged kill switch holds a blast is the day this stops
    chasing the leads it held.
    """
    co = await _company(db_session)
    held = await _contact(db_session, co, "held@a.com")
    db_session.add(EmailSend(
        contact_id=held.id, to_email=held.email, subject="hi", status="skipped",
        sent_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    await db_session.flush()

    result = await generate_stale_lead_followups_for_session(db_session)
    await db_session.flush()

    assert result["created"] == 1
    assert held.id in await _flagged(db_session)


@pytest.mark.asyncio
async def test_bounced_send_still_counts_as_contact(db_session):
    """``bounced`` is mail that went out. It is a wire status, not a non-send."""
    co = await _company(db_session)
    c = await _contact(db_session, co, "bounced@a.com")
    db_session.add(EmailSend(
        contact_id=c.id, to_email=c.email, subject="hi", status="bounced",
        sent_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    await db_session.flush()

    assert (await generate_stale_lead_followups_for_session(db_session))["created"] == 0


@pytest.mark.asyncio
async def test_engaged_send_still_counts_as_contact(db_session):
    """``EmailSend.status`` is a lifecycle column — an opened send is still a send."""
    co = await _company(db_session)
    c = await _contact(db_session, co, "opened@a.com")
    db_session.add(EmailSend(
        contact_id=c.id, to_email=c.email, subject="hi", status="replied",
        sent_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    await db_session.flush()

    assert (await generate_stale_lead_followups_for_session(db_session))["created"] == 0


@pytest.mark.asyncio
async def test_address_on_the_do_not_contact_list_is_never_chased(db_session):
    """A reminder is not a send, so no downstream gate refuses it.

    The contact row is clean — which is the point: ``add_entry`` does not touch
    ``Contact.suppressed``, so a compliance upload, a bounce against a
    re-scraped duplicate and an erasure whose subject was rediscovered all look
    exactly like this.
    """
    co = await _company(db_session)
    listed = await _contact(db_session, co, "OptedOut@a.com")
    clear = await _contact(db_session, co, "fine@a.com")
    await suppression.add_entry(db_session, "optedout@a.com", reason="manual")
    await db_session.flush()

    result = await generate_stale_lead_followups_for_session(db_session)
    await db_session.flush()

    flagged = await _flagged(db_session)
    assert listed.id not in flagged
    assert clear.id in flagged
    assert result["created"] == 1


@pytest.mark.asyncio
async def test_listed_candidates_do_not_consume_the_batch_limit(db_session, monkeypatch):
    """Filtered before the cap is spent, or they starve the real leads forever.

    A suppressed candidate dropped from an already-capped batch would take a
    slot on every run and never yield it, because nothing about it changes.
    """
    from app.tasks import followups as _followups

    monkeypatch.setattr(_followups.settings, "auto_followup_batch_limit", 2)
    co = await _company(db_session)
    # Ordering is not guaranteed, so suppress enough that a post-cap filter
    # could not produce two survivors by luck.
    for i in range(4):
        await _contact(db_session, co, f"listed{i}@a.com")
        await suppression.add_entry(db_session, f"listed{i}@a.com", reason="manual")
    real = [await _contact(db_session, co, f"real{i}@a.com") for i in range(2)]
    await db_session.flush()

    result = await generate_stale_lead_followups_for_session(db_session)
    await db_session.flush()

    assert result["created"] == 2
    assert {c.id for c in real} == await _flagged(db_session)
