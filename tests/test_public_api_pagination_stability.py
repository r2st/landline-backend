"""Paging through the public API returns each row once.

Every list endpoint ordered on a timestamp alone. `OFFSET`/`LIMIT` over a
non-total order is not a paging scheme: `created_at` is a `server_default` of
`now()` — transaction start time in Postgres — so a CSV import or a discovery
run writes hundreds of rows sharing one reading to the microsecond, and below
the timestamp the database orders the tie however it likes. It is under no
obligation to answer page 1 and page 2 the same way, so a client walking the
collection sees some rows twice and never sees others, with nothing on either
page to indicate anything went wrong.

This is the surface where paging the whole collection is the normal way to use
the API, and the bulk-imported org — where *every* row ties — is the ordinary
case, not the edge one.

SQLite happens to be stable here, so these tests do not reproduce the shuffle
directly. What they pin is the property that makes the shuffle impossible: the
emitted `ORDER BY` is total, and the union of the pages is the collection.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import event

from app.models.api_key import ALL_SCOPES, ApiKey
from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.org_context import APPREND_ORG_ID
from app.services.api_key_auth import generate_api_key


@pytest_asyncio.fixture
async def api_headers(db_session):
    raw, key_hash, prefix = generate_api_key()
    db_session.add(ApiKey(
        org_id=APPREND_ORG_ID, name="pagination", key_hash=key_hash,
        key_prefix=prefix, scopes=list(ALL_SCOPES), rate_limit=100000,
    ))
    await db_session.commit()
    return {"X-API-Key": raw}


@pytest.fixture
def sql_log(db_engine):
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(db_engine.sync_engine, "before_cursor_execute", _record)
    yield statements
    event.remove(db_engine.sync_engine, "before_cursor_execute", _record)


async def _tied_contacts(db_session, company, n):
    """`n` contacts written in one transaction — so one shared `created_at`."""
    for i in range(n):
        db_session.add(Contact(
            id=uuid.uuid4(), company_id=company.id, first_name=f"P{i}",
            last_name="Tied", email=f"tied{i}@acmestamp.com", source="import",
        ))
    await db_session.commit()


# ─── The order the database is asked for ─────────────────────────────────────


@pytest.mark.asyncio
async def test_every_paged_list_orders_by_a_unique_column_too(
    client, db_session, sample_company, api_headers, sql_log
):
    db_session.add(Deal(
        id=uuid.uuid4(), company_id=sample_company.id, title="D", value=1,
        status="pending",
    ))
    await db_session.commit()

    for path, table in (
        ("/api/v1/public/contacts", "contacts"),
        ("/api/v1/public/companies", "companies"),
        ("/api/v1/public/deals", "deals"),
        ("/api/v1/public/enrollments", "outreach_enrollments"),
        ("/api/v1/public/sequences", "outreach_sequences"),
    ):
        sql_log.clear()
        resp = await client.get(path, headers=api_headers)
        assert resp.status_code == 200, (path, resp.text)

        ordered = [
            s for s in sql_log
            if f"FROM {table}" in s and "ORDER BY" in s
        ]
        assert ordered, f"{path} issued no ordered read of {table}"
        assert any(f"{table}.id DESC" in s for s in ordered), (
            f"{path} pages on a timestamp with no tie-break: {ordered}"
        )


# ─── The property that buys ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_paging_a_bulk_import_yields_each_contact_exactly_once(
    client, db_session, sample_company, api_headers
):
    await _tied_contacts(db_session, sample_company, 25)

    seen: list[str] = []
    for page in (1, 2, 3):
        resp = await client.get(
            "/api/v1/public/contacts",
            params={"page": page, "page_size": 10},
            headers=api_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        seen.extend(item["id"] for item in body["items"])

    total = body["total"]
    assert len(seen) == total, "a page was short or overlapping"
    assert len(set(seen)) == total, "the same contact came back on two pages"


@pytest.mark.asyncio
async def test_the_same_page_twice_is_the_same_page(
    client, db_session, sample_company, api_headers
):
    await _tied_contacts(db_session, sample_company, 12)

    async def _page(n):
        resp = await client.get(
            "/api/v1/public/contacts",
            params={"page": n, "page_size": 5},
            headers=api_headers,
        )
        return [i["id"] for i in resp.json()["items"]]

    assert await _page(2) == await _page(2)


@pytest.mark.asyncio
async def test_newest_first_still_holds(
    client, db_session, sample_company, api_headers
):
    """The tie-break is a tie-break; it must not become the sort."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    # The *older* row gets the higher id, so an id-first ordering would invert.
    db_session.add(Contact(
        id=uuid.UUID(int=999), company_id=sample_company.id, first_name="Older",
        last_name="X", email="older@acmestamp.com", source="test",
        created_at=now - timedelta(days=5),
    ))
    db_session.add(Contact(
        id=uuid.UUID(int=1), company_id=sample_company.id, first_name="Newer",
        last_name="X", email="newer@acmestamp.com", source="test",
        created_at=now,
    ))
    await db_session.commit()

    resp = await client.get(
        "/api/v1/public/contacts", params={"page_size": 2}, headers=api_headers
    )
    names = [i["first_name"] for i in resp.json()["items"]]
    assert names == ["Newer", "Older"]


@pytest.mark.asyncio
async def test_companies_page_cleanly_too(client, db_session, api_headers):
    for i in range(15):
        db_session.add(Company(
            id=uuid.uuid4(), name=f"Imported {i}", domain=f"imp{i}.example",
            country="US", source="import",
        ))
    await db_session.commit()

    seen: list[str] = []
    for page in (1, 2, 3, 4):
        resp = await client.get(
            "/api/v1/public/companies",
            params={"page": page, "page_size": 5},
            headers=api_headers,
        )
        body = resp.json()
        seen.extend(item["id"] for item in body["items"])

    assert len(seen) == body["total"]
    assert len(set(seen)) == body["total"]
