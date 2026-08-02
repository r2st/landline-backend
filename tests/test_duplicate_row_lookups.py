"""Two more ``scalar_one_or_none`` assertions the schema does not back.

CLAUDE.md records this bug class costing twice already — duplicate contacts
broke every unsubscribe link, duplicate group names broke all three auto-group
endpoints — and asks for a constraint check before writing one. These are the
remaining sites where the check fails, found by diffing every
``scalar_one_or_none`` call against the unique constraints actually declared on
the model it queries:

- ``contacts.quick_add_contact`` finds-or-creates a Company by name.
  ``companies`` is unique on ``domain`` and on nothing else; two firms sharing a
  name is ordinary B2B data, and the scrapers create companies from several
  sources. The second one makes quick-add return 500 for **every** contact
  entered against that name — the business-card scanner's whole job.
- ``EmailHistoryService`` matches inbound mail to a contact by address while
  syncing an inbox. ``contacts`` has *no* unique constraint at all and the
  scraper re-creates rows, which is exactly the duplicate the opt-out handler
  was already fixed for. Here one duplicate aborts the sync, so the mailbox
  stops ingesting replies — on the mail path, silence looks like "no replies
  yet" rather than like a failure.

Both fixes are the prescribed shape: order by something stable, take the first.
"""

import uuid

from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact


async def _make_company(db, name: str, **kwargs) -> Company:
    kwargs.setdefault("source", "test")
    company = Company(id=uuid.uuid4(), name=name, **kwargs)
    db.add(company)
    await db.flush()
    return company


async def _make_contact(db, email: str, company: Company, **kwargs) -> Contact:
    contact = Contact(
        id=uuid.uuid4(), email=email, company_id=company.id, **kwargs
    )
    db.add(contact)
    await db.flush()
    return contact


# --------------------------------------------------------------------------
# quick-add: two companies with the same name
# --------------------------------------------------------------------------


async def test_quick_add_survives_two_companies_of_the_same_name(client, db_session):
    """The regression: a duplicate name 500'd the business-card scanner."""
    await _make_company(db_session, "Acme Manufacturing", domain="acme-tx.com")
    await _make_company(db_session, "Acme Manufacturing", domain="acme-oh.com")
    await db_session.commit()

    response = await client.post(
        "/api/v1/contacts/quick-add",
        json={
            "first_name": "Dana",
            "last_name": "Reed",
            "email": "dana@acme-tx.com",
            "company_name": "Acme Manufacturing",
        },
    )

    assert response.status_code in (200, 201), response.text
    assert response.json()["company_created"] is False


async def test_quick_add_matches_the_name_case_insensitively_and_stably(
    client, db_session
):
    """Repeat adds must all land on the same company, not scatter between them.

    Scattering is the failure the duplicate-group fix called out: work spread
    across two rows instead of accumulating in one. Here it would file a
    company's contacts under two different records.
    """
    await _make_company(db_session, "Acme Manufacturing", domain="acme-1.com")
    await _make_company(db_session, "ACME MANUFACTURING", domain="acme-2.com")
    await db_session.commit()

    company_ids = set()
    for i in range(3):
        response = await client.post(
            "/api/v1/contacts/quick-add",
            json={
                "first_name": f"Person{i}",
                "email": f"person{i}@acme.com",
                "company_name": "acme manufacturing",
            },
        )
        assert response.status_code in (200, 201), response.text
        company_ids.add(response.json()["company_id"])

    assert len(company_ids) == 1, f"contacts scattered across companies: {company_ids}"


async def test_quick_add_still_creates_a_company_when_none_matches(client):
    """The find-or-create half is unchanged."""
    response = await client.post(
        "/api/v1/contacts/quick-add",
        json={
            "first_name": "Jo",
            "email": "jo@brand-new-co.com",
            "company_name": "Brand New Co",
        },
    )

    assert response.status_code in (200, 201), response.text
    assert response.json()["company_created"] is True


# --------------------------------------------------------------------------
# inbox sync: two contacts with the same address
# --------------------------------------------------------------------------


def _sync_contact_lookup(address: str):
    """The lookup the inbox-sync loop performs, as fixed."""
    from sqlalchemy import func

    return (
        select(Contact)
        .where(func.lower(Contact.email) == address)
        .order_by(Contact.created_at.asc(), Contact.id.asc())
        .limit(1)
    )


async def test_inbox_sync_matches_a_duplicated_contact_address(db_session):
    """A contact the scraper found twice must not abort the mailbox sync.

    The addresses differ only in case, which is the duplicate this actually
    produces — ``contacts.email`` has no unique constraint *and* no case
    folding, so the same person arrives as two rows from two sources.
    """
    from datetime import datetime, timedelta, timezone

    company = await _make_company(db_session, "Dup Co", domain="dup.example")
    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = await _make_contact(
        db_session, "buyer@dup.example", company, created_at=earlier
    )
    await _make_contact(
        db_session,
        "BUYER@dup.example",
        company,
        created_at=earlier + timedelta(days=30),
    )
    await db_session.flush()

    matched = (
        await db_session.execute(_sync_contact_lookup("buyer@dup.example"))
    ).scalars().first()

    assert matched is not None, "the duplicate must still match something"
    assert matched.id == first.id, "the earliest-created row is the canonical one"


async def test_the_match_is_stable_when_the_timestamps_tie(db_session):
    """Rows written in the same tick share a ``created_at``, so the id breaks it.

    This is the common case rather than the exotic one: ``created_at`` is a
    ``server_default``, and an import that writes both copies in one transaction
    gives them the same reading. Without the second sort key the database is
    free to return either, and a reply would attach to a different contact on
    each sync — which is the scattering the duplicate-group fix called out.
    """
    company = await _make_company(db_session, "Tie Co", domain="tie.example")
    for _ in range(4):
        await _make_contact(db_session, "same@tie.example", company)
    await db_session.flush()

    picks = set()
    for _ in range(5):
        matched = (
            await db_session.execute(_sync_contact_lookup("same@tie.example"))
        ).scalars().first()
        picks.add(matched.id)

    assert len(picks) == 1, f"the contact match is not deterministic: {picks}"


async def test_the_sync_lookup_is_not_a_single_row_assertion():
    """Pin the shape, so the assertion cannot be reintroduced by a later edit.

    The behavioural test above exercises the query in isolation, because
    driving a real Gmail sync needs the whole Google client mocked. This one
    holds the production source to the same shape.
    """
    import inspect

    from app.services import email_history

    source = inspect.getsource(email_history.EmailHistoryService)
    marker = "# Match to a contact by email"
    assert marker in source, "the contact-matching block moved; recheck this test"

    # Take the statement itself, not a fixed character window — the explanatory
    # comment above it is longer than the code and would otherwise be most of
    # what gets inspected.
    after = source.split(marker, 1)[1]
    statement = after.split("contact = (", 1)[1][:500]

    assert "scalar_one_or_none" not in statement, (
        "the inbound-mail contact lookup asserts a uniqueness `contacts` does "
        "not have — one duplicate address aborts the whole inbox sync"
    )
    assert ".limit(1)" in statement and "order_by" in statement
