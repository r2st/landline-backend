"""Pulling a book of business in from a CRM.

The properties that matter, in the order they can hurt:

  * an import **never enrolls** — bringing your customers in must not be the
    event that starts cold-emailing them;
  * it dedups against what we already have, and the domain fallback that makes
    that work must not merge every gmail.com address into one company;
  * a page that fails stops the loop with a resumable cursor rather than losing
    the pages already ingested.
"""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.services.crm_import import (
    CrmImporter,
    domain_from_email,
    to_candidate_record,
)
from app.services.integrations.base import ConnectorResult
from app.services.integrations.providers import HubSpotConnector, SalesforceConnector


class FakeConnector:
    """A connector that serves canned pages and records what it was asked."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def execute(self, action, params, config, credentials):
        self.calls.append((action, dict(params)))
        if not self.pages:
            return ConnectorResult.success("empty", records=[], cursor=None)
        page = self.pages.pop(0)
        if isinstance(page, ConnectorResult):
            return page
        records, cursor = page
        return ConnectorResult.success("ok", records=records, cursor=cursor)


def _row(**over) -> dict:
    row = {
        "external_id": "1",
        "email": "jane@acme-machining.com",
        "first_name": "Jane",
        "last_name": "Diaz",
        "title": "Plant Manager",
        "phone": "555-0100",
        "company": "Acme Machining",
        "website": None,
        "city": "Akron",
        "state": "OH",
        "country": "US",
    }
    row.update(over)
    return row


# ─── Mapping ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "email,expected",
    [
        ("jane@acme-machining.com", "acme-machining.com"),
        ("jane@WWW.Acme-Machining.com", "acme-machining.com"),
        ("jane@gmail.com", None),
        ("jane@outlook.com", None),
        ("not-an-email", None),
        ("", None),
        (None, None),
    ],
)
def test_domain_from_email(email, expected):
    assert domain_from_email(email) == expected


def test_a_free_mail_contact_still_imports_under_its_company_name():
    """We must not invent 'gmail.com Inc', but the person is still a lead."""
    record = to_candidate_record(
        _row(email="owner@gmail.com", website=None), provider="hubspot"
    )
    assert record is not None
    assert record.name == "Acme Machining"
    assert record.domain is None
    assert record.contacts[0].email == "owner@gmail.com"


def test_a_row_with_no_company_and_no_work_domain_is_unusable():
    assert to_candidate_record(
        _row(company=None, website=None, email="someone@gmail.com"), provider="hubspot"
    ) is None


def test_the_email_domain_names_the_company_when_the_crm_has_no_company_field():
    record = to_candidate_record(_row(company=None, website=None), provider="hubspot")
    assert record is not None
    assert record.name == "acme-machining.com"
    assert record.domain == "acme-machining.com"


def test_website_beats_the_email_domain():
    record = to_candidate_record(
        _row(website="https://www.acme-corp.com/about"), provider="salesforce"
    )
    assert record.domain == "acme-corp.com"


def test_an_unsupported_crm_country_does_not_reject_the_row():
    """A CRM export is full of country values our market policy rejects.

    The ingester raises for an unsupported country rather than relabelling it,
    which would drop the row. The CRM's value rides along in ``raw`` instead.
    """
    record = to_candidate_record(_row(country="Deutschland"), provider="hubspot")
    assert record.country is None
    assert record.raw["crm_country"] == "Deutschland"


def test_the_source_names_the_crm_it_came_from():
    record = to_candidate_record(_row(), provider="hubspot")
    assert record.source == "crm_hubspot"
    assert record.contacts[0].source == "crm_hubspot"
    assert record.contacts[0].raw["external_id"] == "1"


# ─── Importing ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_creates_the_company_and_the_person(db_session: AsyncSession):
    connector = FakeConnector([([_row()], None)])
    summary = await CrmImporter().import_contacts(
        db_session, connector, {}, {}, provider="hubspot"
    )
    await db_session.flush()

    assert summary.fetched == 1
    assert summary.companies_created == 1
    assert summary.contacts_created == 1

    company = (
        await db_session.execute(
            select(Company).where(Company.domain == "acme-machining.com")
        )
    ).scalar_one()
    assert company.source == "crm_hubspot"
    contact = (
        await db_session.execute(
            select(Contact).where(Contact.email == "jane@acme-machining.com")
        )
    ).scalar_one()
    assert contact.company_id == company.id
    assert contact.title == "Plant Manager"


@pytest.mark.asyncio
async def test_an_import_never_enrolls_anyone(db_session: AsyncSession):
    """The headline safety property. Importing is not prospecting."""
    before = (
        await db_session.execute(select(func.count()).select_from(OutreachEnrollment))
    ).scalar_one()

    connector = FakeConnector([([_row(), _row(external_id="2", email="bob@acme-machining.com")], None)])
    await CrmImporter().import_contacts(
        db_session, connector, {}, {}, provider="hubspot"
    )
    await db_session.flush()

    after = (
        await db_session.execute(select(func.count()).select_from(OutreachEnrollment))
    ).scalar_one()
    assert after == before


@pytest.mark.asyncio
async def test_two_people_at_one_firm_produce_one_company(db_session: AsyncSession):
    connector = FakeConnector([([
        _row(),
        _row(external_id="2", email="bob@acme-machining.com", first_name="Bob"),
    ], None)])
    summary = await CrmImporter().import_contacts(
        db_session, connector, {}, {}, provider="hubspot"
    )
    assert summary.companies_created == 1
    assert summary.companies_matched == 1
    assert summary.contacts_created == 2


@pytest.mark.asyncio
async def test_a_company_we_already_know_is_matched_not_duplicated(
    db_session: AsyncSession,
):
    existing = Company(
        id=uuid.uuid4(), name="Acme Machining Inc",
        domain="acme-machining.com", source="places",
    )
    db_session.add(existing)
    await db_session.flush()

    connector = FakeConnector([([_row()], None)])
    summary = await CrmImporter().import_contacts(
        db_session, connector, {}, {}, provider="hubspot"
    )
    assert summary.companies_created == 0
    assert summary.companies_matched == 1

    count = (
        await db_session.execute(
            select(func.count()).select_from(Company)
            .where(Company.domain == "acme-machining.com")
        )
    ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_a_contact_we_already_hold_is_not_duplicated(db_session: AsyncSession):
    connector = FakeConnector([([_row()], None)])
    await CrmImporter().import_contacts(db_session, connector, {}, {}, provider="hubspot")

    again = FakeConnector([([_row()], None)])
    summary = await CrmImporter().import_contacts(
        db_session, again, {}, {}, provider="hubspot"
    )
    assert summary.contacts_created == 0
    assert summary.contacts_skipped == 1


@pytest.mark.asyncio
async def test_dry_run_answers_the_question_without_writing(db_session: AsyncSession):
    connector = FakeConnector([([_row()], None)])
    summary = await CrmImporter().import_contacts(
        db_session, connector, {}, {}, provider="hubspot", dry_run=True
    )
    assert summary.dry_run is True
    assert summary.fetched == 1

    count = (
        await db_session.execute(
            select(func.count()).select_from(Company)
            .where(Company.domain == "acme-machining.com")
        )
    ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_paging_follows_the_cursor_and_stops_at_the_budget(
    db_session: AsyncSession,
):
    pages = [
        ([_row(external_id="1", email="a@one-corp.com", company="One")], "cur-1"),
        ([_row(external_id="2", email="b@two-corp.com", company="Two")], "cur-2"),
        ([_row(external_id="3", email="c@three-corp.com", company="Three")], "cur-3"),
    ]
    connector = FakeConnector(pages)
    summary = await CrmImporter().import_contacts(
        db_session, connector, {}, {}, provider="hubspot", max_records=2, page_size=1
    )
    assert summary.fetched == 2
    assert summary.pages == 2
    # The second page's cursor comes back so the rest is resumable.
    assert summary.cursor == "cur-2"
    assert connector.calls[1][1]["cursor"] == "cur-1"


@pytest.mark.asyncio
async def test_the_last_page_ends_the_loop(db_session: AsyncSession):
    connector = FakeConnector([
        ([_row(email="a@one-corp.com", company="One")], "cur-1"),
        ([_row(email="b@two-corp.com", company="Two")], None),
    ])
    summary = await CrmImporter().import_contacts(
        db_session, connector, {}, {}, provider="hubspot", max_records=100
    )
    assert summary.pages == 2
    assert summary.cursor is None
    assert summary.companies_created == 2


@pytest.mark.asyncio
async def test_a_failing_page_keeps_what_was_already_ingested(
    db_session: AsyncSession,
):
    connector = FakeConnector([
        ([_row(email="a@one-corp.com", company="One")], "cur-1"),
        ConnectorResult.failure("HubSpot returned 429"),
    ])
    summary = await CrmImporter().import_contacts(
        db_session, connector, {}, {}, provider="hubspot", max_records=100
    )
    assert summary.companies_created == 1
    assert summary.errors == ["HubSpot returned 429"]
    # Resumable from where it stopped.
    assert summary.cursor == "cur-1"


@pytest.mark.asyncio
async def test_unusable_rows_are_counted_not_silently_dropped(
    db_session: AsyncSession,
):
    connector = FakeConnector([([
        _row(),
        _row(external_id="2", company=None, website=None, email="nobody@gmail.com"),
    ], None)])
    summary = await CrmImporter().import_contacts(
        db_session, connector, {}, {}, provider="hubspot"
    )
    assert summary.fetched == 2
    assert summary.skipped_unusable == 1
    assert summary.companies_created == 1


# ─── Connector wire format ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hubspot_fetch_maps_properties_and_cursor(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "results": [{
                    "id": "701",
                    "properties": {
                        "email": "jane@acme.com", "firstname": "Jane",
                        "lastname": "Diaz", "jobtitle": "Owner",
                        "company": "Acme", "phone": "555", "city": "Akron",
                    },
                }],
                "paging": {"next": {"after": "NTI1Cg%3D%3D"}},
            }

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            captured["url"] = url
            captured["params"] = params
            return _Resp()

    monkeypatch.setattr("app.services.integrations.providers.httpx.AsyncClient", _Client)

    result = await HubSpotConnector().execute(
        "fetch_contacts", {"limit": 100}, {}, {"access_token": "tok"}
    )
    assert result.ok
    assert result.data["cursor"] == "NTI1Cg%3D%3D"
    row = result.data["records"][0]
    assert row["email"] == "jane@acme.com"
    assert row["title"] == "Owner"
    assert row["external_id"] == "701"
    # Without an explicit property list HubSpot returns no email at all.
    assert "email" in captured["params"]["properties"]


@pytest.mark.asyncio
async def test_salesforce_fetch_maps_lead_fields_and_next_records_url(monkeypatch):
    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "records": [{
                    "Id": "00Q1", "FirstName": "Jane", "LastName": "Diaz",
                    "Email": "jane@acme.com", "Title": "Owner",
                    "Company": "Acme", "NumberOfEmployees": 42,
                }],
                "nextRecordsUrl": "/services/data/v59.0/query/01g400",
            }

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            return _Resp()

    monkeypatch.setattr("app.services.integrations.providers.httpx.AsyncClient", _Client)

    result = await SalesforceConnector().execute(
        "fetch_contacts", {},
        {"instance_url": "https://na1.salesforce.com"},
        {"access_token": "tok"},
    )
    assert result.ok
    assert result.data["cursor"] == "/services/data/v59.0/query/01g400"
    assert result.data["records"][0]["employee_count"] == 42


@pytest.mark.asyncio
async def test_fetch_without_credentials_fails_rather_than_raising():
    hub = await HubSpotConnector().execute("fetch_contacts", {}, {}, {})
    assert hub.ok is False
    sf = await SalesforceConnector().execute("fetch_contacts", {}, {}, {"access_token": "t"})
    assert sf.ok is False  # no instance_url


def test_both_crm_connectors_advertise_the_pull_action():
    assert "fetch_contacts" in HubSpotConnector.catalog_entry()["supported_actions"]
    assert "fetch_contacts" in SalesforceConnector.catalog_entry()["supported_actions"]
