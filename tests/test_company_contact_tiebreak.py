"""The contact that fronts a company is the same one on every surface.

``contacts.created_at`` is a ``server_default`` of ``now()`` — transaction start
time in Postgres — so every contact one scrape writes for a company shares a
single reading. Ordering on it alone therefore settles nothing, and below it the
database returns rows in whatever order it likes.

Two things read that order and used to disagree because of it:

* ``company_contacts.pick_primary`` names the contact every list view attributes
  the company to;
* ``autonomous_prospector`` picks the contact a sequence actually mails.

So the dashboard could name one person while the mail went to another, and two
discovery runs over the same company could enrol two different contacts. Both
now end on ``(created_at, id)``.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact
from app.services import company_contacts


def _contact(**kw) -> Contact:
    """An in-memory Contact — these tests exercise the sort key, not the ORM."""
    defaults = dict(
        id=uuid.uuid4(),
        first_name="A",
        last_name="B",
        email="a@example.com",
        is_primary=False,
        suppressed=False,
        unsubscribed=False,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return Contact(**defaults)


# ─── The sort key is total ───────────────────────────────────────────────────


def test_contacts_written_in_one_transaction_still_have_an_order():
    """The tie the timestamp cannot break is broken by the id."""
    shared = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = _contact(created_at=shared, id=uuid.UUID(int=1), email="a@x.com")
    b = _contact(created_at=shared, id=uuid.UUID(int=2), email="b@x.com")

    # Whatever order the database hands them back in, the ranking is the same.
    assert company_contacts.rank_contacts([a, b]) == [a, b]
    assert company_contacts.rank_contacts([b, a]) == [a, b]
    assert company_contacts.pick_primary(company_contacts.rank_contacts([b, a])) is a


def test_an_earlier_contact_still_outranks_a_later_one():
    """The id is a tie-break, not the primary key of the ordering."""
    old = _contact(created_at=datetime(2025, 1, 1, tzinfo=timezone.utc), id=uuid.UUID(int=9))
    new = _contact(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), id=uuid.UUID(int=1))
    assert company_contacts.rank_contacts([new, old]) == [old, new]


def test_reachability_and_primary_still_outrank_the_tiebreak():
    """The tie-break is last; it must not reorder the decisions above it."""
    shared = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Lowest id, but suppressed — never fronts a company.
    blocked = _contact(created_at=shared, id=uuid.UUID(int=1), suppressed=True)
    # Highest id, but flagged and reachable.
    flagged = _contact(created_at=shared, id=uuid.UUID(int=3), is_primary=True)
    plain = _contact(created_at=shared, id=uuid.UUID(int=2))

    ordered = company_contacts.rank_contacts([blocked, plain, flagged])
    assert ordered == [flagged, plain, blocked]


def test_an_unflushed_contact_does_not_raise_mid_page_render():
    """``created_at`` is a server default, so a pending row carries ``None``.

    A page that adds a contact and re-renders the company in the same
    transaction used to compare ``None`` against a ``datetime`` and 500.
    """
    persisted = _contact(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    pending = _contact(created_at=None)

    ordered = company_contacts.rank_contacts([persisted, pending])
    assert set(ordered) == {persisted, pending}
    # Two pending rows compare against each other on id alone, also without raising.
    p1 = _contact(created_at=None, id=uuid.UUID(int=1))
    p2 = _contact(created_at=None, id=uuid.UUID(int=2))
    assert company_contacts.rank_contacts([p2, p1]) == [p1, p2]


# ─── The prospector agrees with it ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_prospector_and_dashboard_pick_the_same_contact(db_session):
    """The enrolment query and ``pick_primary`` must not name different people."""
    company = Company(
        id=uuid.uuid4(), name="Tie Co", domain="tie.example", source="test"
    )
    db_session.add(company)
    await db_session.flush()

    # Three unflagged, reachable contacts sharing one timestamp — what a single
    # enrichment pass over one company actually writes.
    shared = datetime.now(timezone.utc) - timedelta(days=1)
    contacts = [
        Contact(
            id=uuid.UUID(int=i),
            company_id=company.id,
            first_name=f"C{i}",
            last_name="X",
            email=f"c{i}@tie.example",
            is_primary=False,
            suppressed=False,
            unsubscribed=False,
            created_at=shared,
        )
        for i in (7, 3, 5)
    ]
    db_session.add_all(contacts)
    await db_session.flush()

    # What the list views name.
    resolved = await company_contacts.resolve_primary_contacts(db_session, [company.id])
    dashboard_pick = resolved[company.id]

    # What the prospector's own candidate query would mail — the same ORDER BY.
    candidates = (
        await db_session.execute(
            select(Contact)
            .where(Contact.company_id == company.id)
            .order_by(
                Contact.is_primary.desc(),
                Contact.created_at.asc(),
                Contact.id.asc(),
            )
        )
    ).scalars().all()

    assert candidates[0].id == dashboard_pick.id
    assert dashboard_pick.id == uuid.UUID(int=3)
