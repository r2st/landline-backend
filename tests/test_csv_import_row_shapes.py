"""A CSV row that omits its trailing cells is an ordinary CSV row.

``csv.DictReader`` fills the columns a short row doesn't reach with ``restval``,
which defaults to ``None``. Excel, Google Sheets and every CRM export drop
trailing empty cells, so ``company_name,domain,city`` followed by
``Acme,acme.com`` is the common shape, not a malformed one.

The importer normalised each row with ``v.strip()``, so every one of those rows
raised ``AttributeError``. Three things made that worse than a crash:

* the loop's blanket ``except Exception`` swallowed it into the row error
  ``'NoneType' object has no attribute 'strip'`` — a message that names neither
  the column nor the problem, for a row that should simply have imported;
* the endpoint returned **200** with that in ``errors``, so the import reads as
  having run;
* at 101 errors the loop **breaks**. A file whose last column is usually blank
  therefore imported its first hundred rows and abandoned the rest — and the
  truncation note said "too many errors", which reads like a summary of a
  finished import rather than "the remaining rows were never looked at".

So the direction is the usual one: a routine input silently produced a partial
import that reported success.
"""

import io

import pytest
from httpx import AsyncClient

from app.routers.import_export import MAX_IMPORT_ERRORS, _normalize_row


def _upload(text: str) -> dict:
    return {"file": ("leads.csv", io.BytesIO(text.encode()), "text/csv")}


# ─── The normaliser, directly ────────────────────────────────────────────────


def test_a_short_row_normalises_to_empty_strings():
    """The `None` DictReader writes for an unreached column is not an error."""
    assert _normalize_row({"company_name": "Acme", "city": None}) == {
        "company_name": "Acme",
        "city": "",
    }


def test_header_names_are_lowercased_and_underscored():
    assert _normalize_row({" Company Name ": " Acme "}) == {"company_name": "Acme"}


def test_fields_past_the_header_are_dropped():
    """A long row puts its extra fields under the `None` key, as a list."""
    assert _normalize_row({"company_name": "Acme", None: ["stray", "extra"]}) == {
        "company_name": "Acme"
    }


# ─── Through the endpoint ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_short_rows_import_instead_of_erroring(client: AsyncClient):
    csv_text = (
        "company_name,domain,city,contact_name,contact_email\n"
        "Acme Fabrication,acme-fab.com,Toledo,Dana Reed,dana@acme-fab.com\n"
        "Beta Molding,beta-molding.com\n"  # short: three cells missing
        "Gamma Tooling\n"  # shorter still: only the required column
    )
    resp = await client.post("/api/v1/import-export/import/csv", files=_upload(csv_text))
    assert resp.status_code == 200
    body = resp.json()
    assert body["errors"] == []
    assert body["imported_companies"] == 3


@pytest.mark.asyncio
async def test_a_short_row_still_carries_the_cells_it_does_have(client: AsyncClient):
    csv_text = (
        "company_name,domain,city,contact_name,contact_email\n"
        "Delta Press,delta-press.com,Akron\n"
    )
    resp = await client.post("/api/v1/import-export/import/csv", files=_upload(csv_text))
    assert resp.status_code == 200
    assert resp.json()["errors"] == []

    listing = await client.get("/api/v1/companies/", params={"limit": 50})
    assert listing.status_code == 200
    rows = listing.json()
    companies = rows["items"] if isinstance(rows, dict) else rows
    delta = next(c for c in companies if c["name"] == "Delta Press")
    assert delta["domain"] == "delta-press.com"
    assert delta["city"] == "Akron"


@pytest.mark.asyncio
async def test_a_hundred_short_rows_no_longer_truncate_the_import(client: AsyncClient):
    """The regression, at the size that used to trip the break."""
    rows = "".join(f"Shop {i},shop{i}.example\n" for i in range(MAX_IMPORT_ERRORS + 20))
    csv_text = "company_name,domain,city\n" + rows

    resp = await client.post("/api/v1/import-export/import/csv", files=_upload(csv_text))
    assert resp.status_code == 200
    body = resp.json()
    assert body["errors"] == []
    assert body["imported_companies"] == MAX_IMPORT_ERRORS + 20


@pytest.mark.asyncio
async def test_giving_up_says_where_it_stopped(client: AsyncClient):
    """A file that really is broken still aborts — and now says so usefully.

    Every row names an unsupported country, which ``ingest_one`` rejects. The
    import is right to stop; what it owes the operator is the row it stopped at
    and the fact that the rest of the file is unread.
    """
    rows = "".join(f"Shop {i},shop{i}.example,XX\n" for i in range(MAX_IMPORT_ERRORS + 50))
    csv_text = "company_name,domain,country\n" + rows

    resp = await client.post("/api/v1/import-export/import/csv", files=_upload(csv_text))
    assert resp.status_code == 200
    errors = resp.json()["errors"]

    assert len(errors) == MAX_IMPORT_ERRORS + 2  # the failures, plus the note
    note = errors[-1]
    assert "Stopped at row" in note
    assert "not imported" in note


@pytest.mark.asyncio
async def test_a_row_error_names_the_exception_type(client: AsyncClient):
    """`str(e)` alone is empty for plenty of exceptions; the type never is."""
    csv_text = "company_name,domain,country\nAcme,acme.example,XX\n"
    resp = await client.post("/api/v1/import-export/import/csv", files=_upload(csv_text))
    assert resp.status_code == 200
    errors = resp.json()["errors"]
    assert len(errors) == 1
    assert errors[0].startswith("Row 2:")
    assert errors[0] != "Row 2: "
