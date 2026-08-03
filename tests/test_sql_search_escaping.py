"""A search term is text the user typed, not a pattern language.

Every search filter in this codebase was ``column.ilike(f"%{q}%")``. That is safe
from SQL injection — ``q`` is bound, never concatenated — and it hands the user
the ``LIKE`` wildcards ``%`` and ``_`` anyway, because those live *inside* the
parameter. The consequences are a correctness one and a cost one:

* ``q=%`` matches every row. On the suppression list that is a screen answering
  "is this address on the do-not-contact list?" with "yes" for an address nobody
  suppressed.
* ``q=%a%a%a%a%a%a%a%a%a%z`` turns each row's comparison from a linear scan into
  a backtracking one, multiplied by the table. A leading-wildcard ``ILIKE`` is
  already a sequential scan, so there is no index to save it — that is an
  authenticated caller holding a worker and a connection for as long as they
  like, from a query parameter.

These tests pin the escaping at the helper and at the endpoints, and they carry a
control in each case: a search for ordinary text must still match, or an escaper
that broke every search would pass a test that only checks the wildcard case.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.suppression import SuppressionEntry
from app.services import suppression
from app.services.sql_search import (
    LIKE_ESCAPE,
    contains_pattern,
    endswith_pattern,
    escape_like,
    startswith_pattern,
)


# ─── The helper ──────────────────────────────────────────────────────────────


def test_escape_like_neutralises_wildcards():
    assert escape_like("50%") == r"50\%"
    assert escape_like("a_b") == r"a\_b"
    assert escape_like("plain text") == "plain text"


def test_escape_like_escapes_the_escape_character_first():
    """``\\`` must be doubled before the wildcards are escaped.

    Doing it in the other order would escape the backslashes this function had
    just inserted, turning ``%`` back into a live wildcard preceded by a literal
    backslash.
    """
    assert escape_like("\\") == "\\\\"
    assert escape_like("\\%") == r"\\\%"


def test_pattern_helpers_wrap_the_escaped_term():
    assert contains_pattern("50%") == r"%50\%%"
    assert endswith_pattern("@a_b.com") == r"%@a\_b.com"
    assert startswith_pattern("50%") == r"50\%%"


# ─── Endpoints ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suppression_search_treats_percent_literally(
    client: AsyncClient, db_session: AsyncSession
):
    db_session.add_all([
        SuppressionEntry(email="alice@example.com", reason="manual", source="manual"),
        SuppressionEntry(email="bob@example.com", reason="bounce", source="auto"),
    ])
    await db_session.commit()

    # Control: an ordinary substring search still works.
    hit = await client.get("/api/v1/dialtone/suppression", params={"q": "alice"})
    assert hit.status_code == 200
    assert hit.json()["total"] == 1

    # A bare wildcard is a literal, so it matches nothing — it must not return
    # the whole list and report every address as suppressed.
    wild = await client.get("/api/v1/dialtone/suppression", params={"q": "%"})
    assert wild.status_code == 200
    body = wild.json()
    assert body["total"] == 0
    assert body["items"] == []

    # ``_`` is the single-character wildcard and gets the same treatment: this
    # would otherwise match "alice@example.com" via "alice@exampleXcom".
    underscore = await client.get(
        "/api/v1/dialtone/suppression", params={"q": "alice@example_com"}
    )
    assert underscore.json()["total"] == 0


@pytest.mark.asyncio
async def test_suppression_count_and_page_agree_under_wildcards(
    client: AsyncClient, db_session: AsyncSession
):
    """``total`` and ``items`` are two queries and must apply the same filter.

    They are built in different modules (``suppression.count_entries`` and the
    router), which is exactly the shape that drifts — an escaper applied to one
    and not the other reports "3 results" above an empty table.
    """
    db_session.add_all([
        SuppressionEntry(email=f"user{i}@example.com", reason="manual", source="manual")
        for i in range(3)
    ])
    await db_session.commit()

    for term in ("%", "_", "user", "user1"):
        resp = await client.get("/api/v1/dialtone/suppression", params={"q": term})
        body = resp.json()
        assert body["total"] == len(body["items"]), f"{term}: {body['total']} vs items"


@pytest.mark.asyncio
async def test_company_search_treats_wildcards_literally(
    client: AsyncClient, db_session: AsyncSession
):
    db_session.add_all([
        Company(id=uuid.uuid4(), name="Acme Manufacturing", domain="acme.com", source="test"),
        Company(
            id=uuid.uuid4(), name="100% Plastics", domain="hundred.example", source="test"
        ),
    ])
    await db_session.commit()

    # Control.
    hit = await client.get("/api/v1/companies/", params={"q": "Acme"})
    assert hit.status_code == 200
    assert [c["name"] for c in hit.json()["items"]] == ["Acme Manufacturing"]

    # The literal "100%" finds the company actually named that, and nothing else
    # — before escaping, the trailing "%" made this match every row.
    literal = await client.get("/api/v1/companies/", params={"q": "100%"})
    assert [c["name"] for c in literal.json()["items"]] == ["100% Plastics"]

    # And a bare "%" is not a match-all: it finds the one company whose name
    # actually contains a percent sign, which is the whole point — the character
    # is data now, so it both stops matching everything *and* starts matching
    # the row it names.
    wild = await client.get("/api/v1/companies/", params={"q": "%"})
    assert [c["name"] for c in wild.json()["items"]] == ["100% Plastics"]


@pytest.mark.asyncio
async def test_suppression_service_count_escapes(db_session: AsyncSession):
    """The service-level count is escaped too, not only the router's page query."""
    await suppression.add_entry(db_session, "someone@example.com")
    await db_session.commit()

    assert await suppression.count_entries(db_session, "someone") == 1
    assert await suppression.count_entries(db_session, "%") == 0
    assert await suppression.count_entries(db_session, "_") == 0


def test_like_escape_is_a_single_backslash():
    """The constant every call site passes as ``escape=``.

    ``LIKE`` has no default escape character in the SQL standard — it must be
    named on the statement — so a pattern built by the helpers above is only
    escaped if the query also passes this.
    """
    assert LIKE_ESCAPE == "\\"
    assert len(LIKE_ESCAPE) == 1
