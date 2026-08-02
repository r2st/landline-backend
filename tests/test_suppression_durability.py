"""A do-not-contact decision has to outlive the contact row it was made on.

Round 14 made the unsubscribe link and the reply path write a ``SuppressionEntry``
as well as the per-contact flags, because flags live on a *row* and the scraper
makes rows: opt out today, get rediscovered next month through another source,
and the new row arrives ``suppressed=False``.

Two writers were left setting flags alone.

**GDPR erasure is the severe one, and it is worse than a re-scrape hole.**
``gdpr.erase_subject`` sets ``suppressed``/``unsubscribed`` and then overwrites
``contact.email`` with ``anonymized+<id>@redacted.invalid``. The flags survive;
the address they were about does not. Nothing else mapped that person to "never
contact", so erasure did not merely fail to be durable — it deleted the only
link between the address and the decision, for the one class of subject where
re-contact is a regulatory violation rather than an annoyance.

**The bulk compliance route** — the one actually named compliance — was the last
of the three opt-out paths still recording nothing durable.

Mutation-tested. Each of these fails at least one test below: writing the
erasure entry after the scrub instead of before; gating the erasure entry on a
contact row existing; dropping the erasure entry entirely; dropping the bulk
route's entry; letting un-suppress lift a complaint, an unsubscribe or an
erasure; failing to lift the bulk route's own entry on un-suppress; and allowing
the operator DELETE to remove an erasure entry.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact
from app.models.suppression import SuppressionEntry
from app.services import gdpr, suppression


async def _subject(db, email="erased@subject.co"):
    company = Company(id=uuid.uuid4(), name="Subject Co", country="US", source="test")
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Jo", last_name="Subject",
        email=email, source="test",
    )
    db.add(contact)
    await db.commit()
    return company, contact


async def _entries(db, email):
    return list(
        (
            await db.execute(
                select(SuppressionEntry).where(SuppressionEntry.email == email)
            )
        ).scalars().all()
    )


# --------------------------------------------------------------------------
# GDPR erasure — the headline.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_erasure_records_the_real_address_not_the_placeholder(db_session):
    """The entry has to be written *before* the scrub. Afterwards it is gone."""
    await _subject(db_session, "forget@subject.co")
    await gdpr.erase_subject(db_session, "forget@subject.co")

    entries = await _entries(db_session, "forget@subject.co")
    assert len(entries) == 1, "erasure recorded nothing that outlives the contact row"
    assert entries[0].reason == "erasure"
    assert "redacted.invalid" not in entries[0].email


@pytest.mark.asyncio
async def test_a_rediscovered_erased_subject_is_still_blocked(db_session):
    """The whole point. The scraper finds them again; the send gate must refuse.

    Their address is on their employer's public site — that is where we got it
    the first time — so rediscovery is the expected case, not a corner one.
    """
    await _subject(db_session, "rescrape@subject.co")
    await gdpr.erase_subject(db_session, "rescrape@subject.co")

    # The scraper, next month, knowing nothing about any of this.
    company = Company(id=uuid.uuid4(), name="Subject Co", country="US", source="scrape")
    db_session.add(company)
    await db_session.flush()
    fresh = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Jo",
        email="rescrape@subject.co", source="scrape",
    )
    db_session.add(fresh)
    await db_session.commit()

    assert fresh.suppressed in (False, None), "precondition: a fresh row is unflagged"
    assert await suppression.is_suppressed(db_session, "rescrape@subject.co") is True


@pytest.mark.asyncio
async def test_erasure_for_an_address_we_hold_no_row_for_is_recorded(db_session):
    """An erasure request must be honourable without a contact to hang it on.

    Also covers the subject whose rows a *previous* erasure already anonymised:
    there is nothing left to match, and the request still has to stick.
    """
    result = await gdpr.erase_subject(db_session, "stranger@subject.co")

    assert result["anonymized_contacts"] == 0
    assert result["scrubbed"]["suppression_entry"] == 1
    assert len(await _entries(db_session, "stranger@subject.co")) == 1


@pytest.mark.asyncio
async def test_erasure_is_idempotent(db_session):
    """Re-running must not trip the (org, email) unique constraint."""
    await _subject(db_session, "twice@subject.co")
    await gdpr.erase_subject(db_session, "twice@subject.co")
    await gdpr.erase_subject(db_session, "twice@subject.co")

    assert len(await _entries(db_session, "twice@subject.co")) == 1


@pytest.mark.asyncio
async def test_erasure_still_scrubs_and_flags_the_contact(db_session):
    """The new write must not have displaced what erasure already did."""
    _, contact = await _subject(db_session, "intact@subject.co")
    await gdpr.erase_subject(db_session, "intact@subject.co")

    await db_session.refresh(contact)
    assert contact.first_name is None
    assert contact.anonymized_at is not None
    assert contact.suppressed is True
    assert "redacted.invalid" in contact.email


# --------------------------------------------------------------------------
# The erasure entry is the only record left, so it cannot be clicked away.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_cannot_delete_an_erasure_entry(client, db_session):
    await _subject(db_session, "protected@subject.co")
    await gdpr.erase_subject(db_session, "protected@subject.co")
    entry = (await _entries(db_session, "protected@subject.co"))[0]

    resp = await client.delete(f"/api/v1/dialtone/suppression/{entry.id}")
    assert resp.status_code == 409
    assert len(await _entries(db_session, "protected@subject.co")) == 1


@pytest.mark.asyncio
async def test_operator_can_still_delete_an_ordinary_entry(client, db_session):
    """The guard is narrow — every other reason stays an operator's call."""
    entry, status = await suppression.add_entry(
        db_session, "bounced@subject.co", reason="bounce", source="auto"
    )
    await db_session.commit()
    assert status == "added"

    resp = await client.delete(f"/api/v1/dialtone/suppression/{entry.id}")
    assert resp.status_code == 204
    assert await _entries(db_session, "bounced@subject.co") == []


# --------------------------------------------------------------------------
# The bulk compliance route.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_suppress_records_something_that_outlives_the_row(
    client, db_session
):
    _, contact = await _subject(db_session, "bulk@subject.co")

    resp = await client.post(
        "/api/v1/contacts/bulk-suppress",
        json={"contact_ids": [str(contact.id)], "suppressed": True,
              "reason": "bought list"},
    )
    assert resp.status_code == 200, resp.text
    assert len(await _entries(db_session, "bulk@subject.co")) == 1
    assert await suppression.is_suppressed(db_session, "bulk@subject.co") is True


@pytest.mark.asyncio
async def test_unsuppressing_lifts_this_route_s_own_entry(client, db_session):
    """Otherwise un-suppress is silent and inert: flag off, list entry on, no mail."""
    _, contact = await _subject(db_session, "reversible@subject.co")
    await client.post(
        "/api/v1/contacts/bulk-suppress",
        json={"contact_ids": [str(contact.id)], "suppressed": True},
    )
    assert await suppression.is_suppressed(db_session, "reversible@subject.co") is True

    resp = await client.post(
        "/api/v1/contacts/bulk-suppress",
        json={"contact_ids": [str(contact.id)], "suppressed": False},
    )
    assert resp.status_code == 200
    assert await suppression.is_suppressed(db_session, "reversible@subject.co") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["complaint", "bounce", "unsubscribe", "erasure"])
async def test_unsuppressing_cannot_lift_what_it_did_not_place(
    client, db_session, reason
):
    """A checkbox round-trip must not become a way to erase a complaint record.

    Someone reported us as spam, the address bounced, its owner opted out, or its
    owner invoked erasure. None of those become untrue because an operator
    un-ticked a box on one contact row.
    """
    _, contact = await _subject(db_session, f"{reason}@subject.co")
    await suppression.add_entry(
        db_session, f"{reason}@subject.co", reason=reason, source="auto"
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/contacts/bulk-suppress",
        json={"contact_ids": [str(contact.id)], "suppressed": False},
    )
    assert resp.status_code == 200

    remaining = await _entries(db_session, f"{reason}@subject.co")
    assert len(remaining) == 1, f"un-suppress deleted a {reason} entry"
    assert remaining[0].reason == reason
