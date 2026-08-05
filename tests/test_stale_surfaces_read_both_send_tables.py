"""The two read surfaces that answered "have we emailed them?" from one table.

``tasks.followups`` was the third and is covered in
``test_stale_followup_send_sources.py``. These two are the operator-facing half:

* ``GET /follow-ups/stale-contacts`` — "contacts that haven't been emailed in N
  days", which listed every prospect mid-sequence with ``last_emailed_at: null``;
* ``GET /suggestions`` — ``_stale_leads``, which proposed *"In 'outreach' but
  never emailed — re-engage"* about a company whose pipeline stage was set by
  the enrollment that had been mailing it for weeks.

Both now go through ``services.last_contact``. The ``skipped`` case is the
opposite error and is pinned here too: it stamps ``sent_at`` and put nothing on
the wire, so counting it hides a lead that was never actually contacted.
"""

from datetime import datetime, timezone, timedelta

import pytest

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence


async def _company(db, name, stage="outreach"):
    c = Company(name=name, country="US", source="test", stage=stage,
                pipeline_stage=stage)
    db.add(c)
    await db.flush()
    return c


async def _contact(db, company, email):
    c = Contact(company_id=company.id, first_name="X", email=email, source="test")
    db.add(c)
    await db.flush()
    return c


async def _sequence_send(db, contact, *, days_ago):
    seq = OutreachSequence(name=f"S{contact.email}", is_active=True)
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        contact_id=contact.id, sequence_id=seq.id, status="active", current_step=2
    )
    db.add(enr)
    await db.flush()
    db.add(OutreachEvent(
        enrollment_id=enr.id, step_number=1, event_type="sent",
        occurred_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    ))
    await db.flush()


@pytest.mark.asyncio
async def test_stale_contacts_sees_sequence_sends(client, db_session):
    co = await _company(db_session, "Mid Cadence Co")
    mailed = await _contact(db_session, co, "midcadence@a.com")
    quiet = await _contact(db_session, co, "genuinelyquiet@a.com")
    await _sequence_send(db_session, mailed, days_ago=2)
    await db_session.commit()

    rows = (await client.get("/api/v1/follow-ups/stale-contacts?days=30")).json()
    emails = {r["email"] for r in rows}
    assert "midcadence@a.com" not in emails
    assert "genuinelyquiet@a.com" in emails


@pytest.mark.asyncio
async def test_stale_contacts_reports_the_real_last_send(client, db_session):
    """A long-quiet sequence contact is still stale — with a date, not a null."""
    co = await _company(db_session, "Went Quiet Co")
    c = await _contact(db_session, co, "wentquiet@a.com")
    await _sequence_send(db_session, c, days_ago=90)
    await db_session.commit()

    rows = (await client.get("/api/v1/follow-ups/stale-contacts?days=30")).json()
    row = next(r for r in rows if r["email"] == "wentquiet@a.com")
    assert row["last_emailed_at"] is not None
    assert row["days_since_contact"] == 90


@pytest.mark.asyncio
async def test_stale_contacts_ignores_a_skipped_send(client, db_session):
    """A send the cap or the kill switch declined must not hide the lead."""
    co = await _company(db_session, "Held Co")
    held = await _contact(db_session, co, "held@a.com")
    db_session.add(EmailSend(
        contact_id=held.id, to_email=held.email, subject="hi", status="skipped",
        sent_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    await db_session.commit()

    rows = (await client.get("/api/v1/follow-ups/stale-contacts?days=30")).json()
    assert "held@a.com" in {r["email"] for r in rows}


@pytest.mark.asyncio
async def test_suggestions_does_not_call_a_mailed_company_never_emailed(client, db_session):
    co = await _company(db_session, "Being Mailed Co")
    c = await _contact(db_session, co, "seq@b.com")
    await _sequence_send(db_session, c, days_ago=1)
    await db_session.commit()

    body = (await client.get("/api/v1/suggestions")).json()
    stale = [s for s in body["suggestions"] if s["type"] == "stale_lead"]
    assert not [s for s in stale if s["company_name"] == "Being Mailed Co"]


@pytest.mark.asyncio
async def test_suggestions_still_flags_a_company_gone_quiet(client, db_session):
    """The control: reading the second table must not silence the feed."""
    co = await _company(db_session, "Gone Quiet Co")
    c = await _contact(db_session, co, "old@b.com")
    await _sequence_send(db_session, c, days_ago=120)
    await db_session.commit()

    body = (await client.get("/api/v1/suggestions")).json()
    stale = [
        s for s in body["suggestions"]
        if s["type"] == "stale_lead" and s["company_name"] == "Gone Quiet Co"
    ]
    assert len(stale) == 1
    # Dated from the real send rather than claimed as never emailed.
    assert "never emailed" not in stale[0]["reason"]
    assert stale[0]["metadata"]["days_since_contact"] == 120
