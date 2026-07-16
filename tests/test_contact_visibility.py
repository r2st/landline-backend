"""Contact details must reach every surface that shows a discovered lead.

Places-discovered companies get a contact with an email and (usually) a phone,
but no name and no ``is_primary`` flag — that is what enrichment actually
produces. These tests pin the shape the dashboard needs against that reality,
not against a hand-curated contact.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.prospecting import ProspectCandidate, ProspectingRun


@pytest_asyncio.fixture
async def places_company(db_session: AsyncSession) -> Company:
    """A company as Google Places discovery leaves it."""
    company = Company(
        id=uuid.uuid4(),
        name="Method Manufacturing CNC",
        domain="method-mfg.com",
        city="Denver",
        state_province="CO",
        country="US",
        source="google_places",
    )
    db_session.add(company)
    await db_session.flush()
    return company


@pytest_asyncio.fixture
async def places_contact(db_session: AsyncSession, places_company: Company) -> Contact:
    """Email + phone, no name, is_primary never set — the real enrichment shape."""
    contact = Contact(
        id=uuid.uuid4(),
        company_id=places_company.id,
        email="jeff@method-mfg.com",
        phone="+17203165579",
    )
    db_session.add(contact)
    await db_session.flush()
    return contact


class TestCompanyList:
    async def test_list_includes_primary_contact_email(
        self, client, places_company, places_contact
    ):
        """The companies list is where the user first looked and saw nothing."""
        resp = await client.get("/api/v1/companies/")
        assert resp.status_code == 200
        item = next(
            c for c in resp.json()["items"] if c["id"] == str(places_company.id)
        )
        assert item["primary_contact"]["email"] == "jeff@method-mfg.com"
        assert item["primary_contact"]["phone"] == "+17203165579"
        assert item["contact_count"] == 1

    async def test_primary_contact_resolves_without_is_primary_flag(
        self, client, places_company, places_contact
    ):
        """No Places contact sets is_primary; keying on it would show nothing."""
        assert places_contact.is_primary is False
        resp = await client.get("/api/v1/companies/")
        item = next(
            c for c in resp.json()["items"] if c["id"] == str(places_company.id)
        )
        assert item["primary_contact"] is not None

    async def test_nameless_contact_has_null_full_name_not_blank(
        self, client, places_company, places_contact
    ):
        """Enrichment often finds an address but no person."""
        resp = await client.get("/api/v1/companies/")
        item = next(
            c for c in resp.json()["items"] if c["id"] == str(places_company.id)
        )
        assert item["primary_contact"]["full_name"] is None

    async def test_list_omits_full_contact_roster(
        self, client, places_company, places_contact
    ):
        """A list row carries one contact, not the company's whole roster.

        Company.contacts is a selectin relationship, so a `contacts` field on
        the list schema would silently serialize every contact of every row.
        """
        resp = await client.get("/api/v1/companies/")
        item = next(
            c for c in resp.json()["items"] if c["id"] == str(places_company.id)
        )
        assert "contacts" not in item

    async def test_company_without_contacts_is_null_not_error(
        self, client, sample_company
    ):
        resp = await client.get("/api/v1/companies/")
        item = next(
            c for c in resp.json()["items"] if c["id"] == str(sample_company.id)
        )
        assert item["primary_contact"] is None
        assert item["contact_count"] == 0


class TestPrimaryResolutionOrder:
    async def test_hand_marked_primary_wins(
        self, client, db_session, places_company, places_contact
    ):
        marked = Contact(
            id=uuid.uuid4(),
            company_id=places_company.id,
            first_name="Dana",
            last_name="Reed",
            title="Owner",
            email="dana@method-mfg.com",
            is_primary=True,
        )
        db_session.add(marked)
        await db_session.flush()

        resp = await client.get("/api/v1/companies/")
        item = next(
            c for c in resp.json()["items"] if c["id"] == str(places_company.id)
        )
        assert item["primary_contact"]["email"] == "dana@method-mfg.com"
        assert item["primary_contact"]["full_name"] == "Dana Reed"
        assert item["primary_contact"]["title"] == "Owner"

    async def test_emailable_contact_beats_one_without_email(
        self, client, db_session, places_company
    ):
        """A name with no address can't be outreached — the address wins."""
        no_email = Contact(
            id=uuid.uuid4(),
            company_id=places_company.id,
            first_name="Pat",
            last_name="Kim",
            created_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        emailable = Contact(
            id=uuid.uuid4(),
            company_id=places_company.id,
            email="shop@method-mfg.com",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([no_email, emailable])
        await db_session.flush()

        resp = await client.get("/api/v1/companies/")
        item = next(
            c for c in resp.json()["items"] if c["id"] == str(places_company.id)
        )
        assert item["primary_contact"]["email"] == "shop@method-mfg.com"

    async def test_emailable_contact_beats_a_primary_with_no_address(
        self, client, db_session, places_company
    ):
        """Match the send path, which selects on email before is_primary —
        otherwise the dashboard names one contact and the sequence emails
        another."""
        primary_no_email = Contact(
            id=uuid.uuid4(),
            company_id=places_company.id,
            first_name="Lee",
            last_name="Ash",
            is_primary=True,
            created_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        emailable = Contact(
            id=uuid.uuid4(),
            company_id=places_company.id,
            email="shop@method-mfg.com",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([primary_no_email, emailable])
        await db_session.flush()

        resp = await client.get("/api/v1/companies/")
        item = next(
            c for c in resp.json()["items"] if c["id"] == str(places_company.id)
        )
        assert item["primary_contact"]["email"] == "shop@method-mfg.com"

    async def test_blank_email_is_not_treated_as_reachable(
        self, client, db_session, places_company
    ):
        blank = Contact(
            id=uuid.uuid4(),
            company_id=places_company.id,
            email="   ",
            created_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        real = Contact(
            id=uuid.uuid4(),
            company_id=places_company.id,
            email="shop@method-mfg.com",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([blank, real])
        await db_session.flush()

        resp = await client.get("/api/v1/companies/")
        item = next(
            c for c in resp.json()["items"] if c["id"] == str(places_company.id)
        )
        assert item["primary_contact"]["email"] == "shop@method-mfg.com"

    async def test_suppressed_contact_loses_to_a_reachable_one(
        self, client, db_session, places_company
    ):
        """Never front a contact we're barred from emailing."""
        suppressed = Contact(
            id=uuid.uuid4(),
            company_id=places_company.id,
            email="optout@method-mfg.com",
            suppressed=True,
            created_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        reachable = Contact(
            id=uuid.uuid4(),
            company_id=places_company.id,
            email="ok@method-mfg.com",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([suppressed, reachable])
        await db_session.flush()

        resp = await client.get("/api/v1/companies/")
        item = next(
            c for c in resp.json()["items"] if c["id"] == str(places_company.id)
        )
        assert item["primary_contact"]["email"] == "ok@method-mfg.com"


class TestCompanyDetail:
    async def test_detail_returns_every_contact(
        self, client, db_session, places_company, places_contact
    ):
        second = Contact(
            id=uuid.uuid4(),
            company_id=places_company.id,
            first_name="Sam",
            last_name="Vale",
            email="sam@method-mfg.com",
            phone="+13035550111",
        )
        db_session.add(second)
        await db_session.flush()

        resp = await client.get(f"/api/v1/companies/{places_company.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["contact_count"] == 2
        emails = {c["email"] for c in body["contacts"]}
        assert emails == {"jeff@method-mfg.com", "sam@method-mfg.com"}
        assert body["primary_contact"]["email"] in emails

    async def test_detail_exposes_phone_numbers(
        self, client, places_company, places_contact
    ):
        resp = await client.get(f"/api/v1/companies/{places_company.id}")
        assert resp.json()["contacts"][0]["phone"] == "+17203165579"


class TestProspectingReviewQueue:
    @pytest_asyncio.fixture
    async def candidate(self, db_session, places_company) -> ProspectCandidate:
        run = ProspectingRun(
            id=uuid.uuid4(),
            status="completed",
            trigger="manual",
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(run)
        await db_session.flush()
        candidate = ProspectCandidate(
            id=uuid.uuid4(),
            run_id=run.id,
            company_id=places_company.id,
            company_name=places_company.name,
            domain=places_company.domain,
            source="google_places",
            fit_score=68,
            qualification={},
            status="review",
        )
        db_session.add(candidate)
        await db_session.flush()
        return candidate

    async def test_queue_shows_the_actual_address(
        self, client, candidate, places_contact
    ):
        """"Yes" doesn't tell a reviewer whether the address is worth enrolling."""
        resp = await client.get("/api/v1/prospecting/review-queue")
        assert resp.status_code == 200
        row = next(c for c in resp.json() if c["id"] == str(candidate.id))
        assert row["contact_email"] == "jeff@method-mfg.com"
        assert row["contact_phone"] == "+17203165579"
        assert row["has_email"] is True

    async def test_queue_without_contact_reports_no_email(self, client, candidate):
        resp = await client.get("/api/v1/prospecting/review-queue")
        row = next(c for c in resp.json() if c["id"] == str(candidate.id))
        assert row["has_email"] is False
        assert row["contact_email"] is None


class TestDialtoneActivityFeed:
    async def test_lead_discovered_carries_the_email(
        self, client, db_session, places_company, places_contact
    ):
        run = ProspectingRun(
            id=uuid.uuid4(),
            status="completed",
            trigger="scheduled",
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(run)
        await db_session.flush()
        db_session.add(ProspectCandidate(
            id=uuid.uuid4(),
            run_id=run.id,
            company_id=places_company.id,
            company_name=places_company.name,
            source="google_places",
            fit_score=68,
            qualification={},
            status="approved",
        ))
        await db_session.flush()

        resp = await client.get("/api/v1/dialtone/activity")
        assert resp.status_code == 200
        event = next(
            e for e in resp.json()["items"] if e["type"] == "lead_discovered"
        )
        assert event["metadata"]["contact_email"] == "jeff@method-mfg.com"
