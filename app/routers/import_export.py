"""CSV import and export endpoints."""

import csv
import io
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.csv_export import CSV_FORMULA_PREFIXES, csv_safe
from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.schemas.dashboard import CSVImportResult
from app.services import uploads
from app.services.ingestion import (
    CandidateRecord,
    ContactRecord,
    IngestionError,
    get_ingestion_service,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/import-export", tags=["import/export"])

# Largest CSV body accepted, enforced during the read (see app.services.uploads).
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

# Largest number of data rows one import may carry.
#
# The byte cap alone does not bound this endpoint's *work*. A well-formed 10 MB
# CSV of the minimum viable row ("Acme\n") is two million rows, and the loop
# below awaits ``ingest_one`` — several queries and possible inserts — for each
# one, inline in the request. Nothing stops it early: the ``len(errors) > 100``
# break only trips on rows that *fail*, so the flawless file is precisely the one
# that runs longest. The result is a request that holds a worker and a database
# connection for many minutes, which is a self-inflicted outage from a single
# upload that passed every check.
#
# 50k rows is far above any real hand-assembled prospect list and still finishes
# in a time a request can survive. Anything larger is a bulk job, and should
# arrive as several files rather than one that silently wedges the API.
MAX_IMPORT_ROWS = 50_000

# How many failing rows are reported before the import gives up on the file.
MAX_IMPORT_ERRORS = 100

# Formula-injection defusing now lives in ``app.csv_export`` so every export in
# the codebase shares it. It was private to this module, which is exactly why
# the send-history export in ``routers.email_templates`` — whose ``error``
# column is a string a *remote mail server* chose — never got it. Aliased rather
# than replaced at the call sites because the name reads correctly here.
_CSV_FORMULA_PREFIXES = CSV_FORMULA_PREFIXES
_csv_safe = csv_safe


def _normalize_row(row: dict) -> dict[str, str]:
    """Lower-case/underscore the header names and give every cell a string.

    Two shapes of a perfectly ordinary CSV reach this and neither is an error:

    * **A short row.** ``csv.DictReader`` fills the columns a row doesn't reach
      with ``restval``, which defaults to ``None`` — so ``a,b,c`` followed by
      ``Acme,acme.com`` yields ``{"city": None}``. Excel and every CRM export
      omit trailing empty cells, so this is the common file, not the malformed
      one. The previous ``v.strip()`` raised ``AttributeError`` on it, which the
      loop's blanket ``except Exception`` turned into the row error
      ``'NoneType' object has no attribute 'strip'`` — a message that names
      neither the column nor the file, for a row that should simply have
      imported. Worse, the 101st such row *breaks the loop*, so a file whose
      last column is usually blank imported its first hundred rows and stopped.
    * **A long row.** Fields past the header land under the ``None`` key as a
      *list*. Dropped, hence the ``if k`` — an unnamed column has no meaning to
      map onto.
    """
    out: dict[str, str] = {}
    for key, value in row.items():
        if not key:
            continue
        out[key.strip().lower().replace(" ", "_")] = (value or "").strip()
    return out


def _trigger_verification():
    """Kick the Hunter verification sweep for freshly imported contacts (P3).

    Import deliberately does NOT block on a paid per-address API call — a 10 MB
    CSV would hold the request worker open for the whole batch. The sweep runs
    every 15 minutes regardless and is idempotent + budget-gated; firing it here
    just means an imported contact is verified *before* its sequence's first send
    instead of up to 15 minutes later.

    The sweep is oldest-first, so a large unverified backlog (or an exhausted
    daily budget) can still defer this import's contacts to a later run — the
    pre-send gate in ``app.tasks.outreach`` is the backstop for that case.
    """
    try:
        from app.tasks.verification import verify_pending_contacts
        verify_pending_contacts.delay()
        logger.info("Dispatched email verification sweep for imported contacts")
    except Exception as e:
        logger.error("Failed to dispatch verification after import: %s", str(e))


def _trigger_scoring_for_companies(company_ids: list[str]):
    """Dispatch Celery enrichment and scoring tasks for newly imported companies."""
    try:
        from app.tasks.enrichment import enrich_company, auto_score_leads
        for cid in company_ids:
            enrich_company.delay(cid)
        # Also trigger a scoring pass
        auto_score_leads.delay()
        logger.info("Dispatched enrichment for %d imported companies + scoring pass", len(company_ids))
    except Exception as e:
        logger.error("Failed to dispatch post-import tasks: %s", str(e))


@router.post("/import/csv", response_model=CSVImportResult)
async def import_csv(
    file: UploadFile = File(...),
    source: str = "csv_import",
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
):
    """Import companies and contacts from a CSV file.

    Expected columns: company_name, domain, city, state, country,
    employee_count, contact_name, contact_email, contact_title

    All columns are optional except company_name.
    """
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a .csv")

    content = await uploads.read_capped(file, MAX_UPLOAD_SIZE)
    try:
        text = content.decode("utf-8-sig")  # Handle BOM
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    # Count before importing anything, so an oversized file is refused whole
    # rather than half-applied. ``csv.reader`` over a generator keeps this a
    # streaming count — materializing the rows to call ``len`` would reintroduce
    # the memory blow-up on the exact input this guard exists for.
    data_rows = max(0, sum(1 for _ in csv.reader(io.StringIO(text))) - 1)
    if data_rows > MAX_IMPORT_ROWS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Too many rows ({data_rows:,}). Maximum {MAX_IMPORT_ROWS:,} per "
                f"import — split the file and upload it in parts."
            ),
        )

    reader = csv.DictReader(io.StringIO(text))

    ingestion = get_ingestion_service()
    imported_companies = 0
    imported_contacts = 0
    skipped = 0
    errors = []
    new_company_ids = []  # Track newly created companies for auto-scoring

    for row_num, row in enumerate(reader, start=2):  # Row 1 is header
        try:
            row = _normalize_row(row)

            company_name = row.get("company_name", "").strip()
            if not company_name:
                skipped += 1
                continue

            # Parse employee count
            emp_str = row.get("employee_count", "").strip()
            employee_count = None
            if emp_str:
                try:
                    employee_count = int(emp_str.replace(",", ""))
                except ValueError:
                    pass

            contact_name = row.get("contact_name", "").strip()
            contact_email = row.get("contact_email", "").strip()
            contact_title = row.get("contact_title", "").strip()
            contacts = []
            if contact_name or contact_email:
                first_name = last_name = None
                if contact_name:
                    parts = contact_name.split(None, 1)
                    first_name = parts[0] if parts else None
                    last_name = parts[1] if len(parts) > 1 else None
                contacts.append(ContactRecord(
                    first_name=first_name,
                    last_name=last_name,
                    email=contact_email or None,
                    title=contact_title or None,
                    source=source,
                ))

            record = CandidateRecord(
                name=company_name,
                domain=row.get("domain") or None,
                city=row.get("city", "").strip() or None,
                state=row.get("state", "").strip() or None,
                # Passed through verbatim. An unsupported country now raises an
                # IngestionError and fails this row, instead of being quietly
                # relabelled "US" — which turned every non-NA list into
                # plausible-looking corrupt data.
                country=row.get("country", "").strip() or None,
                employee_count=employee_count,
                contacts=contacts,
                source=source,
            )

            # One shared dedup rule. The old path matched company names
            # **case-sensitively** here and case-insensitively everywhere else,
            # so a CSV happily created the "Acme Corp" that the prospector would
            # have matched to the existing "ACME CORP".
            outcome = await ingestion.ingest_one(db, record)
            if outcome.created:
                imported_companies += 1
                if outcome.company_id:
                    new_company_ids.append(str(outcome.company_id))
            else:
                skipped += 1
            imported_contacts += outcome.contacts_created

        except IngestionError as e:
            errors.append(f"Row {row_num}: {e}")
        except Exception as e:  # noqa: BLE001 — one bad row must not fail the file
            logger.exception("CSV import row %d failed", row_num)
            errors.append(f"Row {row_num}: {type(e).__name__}: {e}")

        # Abandoning the file after 100 failures is right — a header mismatch
        # produces one error per row and there is no point reading 50k of them.
        # Saying *where* it stopped is what was missing: "too many errors" reads
        # like a summary of a completed import, and the rows after this one were
        # never looked at.
        if len(errors) > MAX_IMPORT_ERRORS:
            errors.append(
                f"Stopped at row {row_num} after {MAX_IMPORT_ERRORS} errors — "
                f"the rest of the file was not imported. Fix the errors above "
                f"and re-upload."
            )
            break

    await db.flush()

    # Auto-enrich and auto-score newly imported companies. Kept as a Celery
    # dispatch rather than the pipeline's inline scorer: a large list would pay
    # for thousands of synchronous scoring passes inside the request.
    if new_company_ids:
        background_tasks.add_task(_trigger_scoring_for_companies, new_company_ids)

    # Verify imported addresses before they can enter a sequence (P3).
    if imported_contacts and get_settings().hunter_verify_on_import:
        background_tasks.add_task(_trigger_verification)

    logger.info(
        "CSV import: %d companies, %d contacts, %d skipped, %d errors",
        imported_companies, imported_contacts, skipped, len(errors),
    )
    return CSVImportResult(
        imported_companies=imported_companies,
        imported_contacts=imported_contacts,
        skipped=skipped,
        errors=errors,
    )


@router.get("/export/companies/csv")
async def export_companies_csv(
    limit: Optional[int] = Query(None, ge=1, le=100000, description="Max rows (omit = all)"),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Export companies as a CSV file. Unbounded by default; ``limit``/``offset``
    let a caller page a very large table instead of streaming it all at once."""
    query = select(Company).order_by(Company.created_at.desc()).offset(offset)
    if limit is not None:
        query = query.limit(limit)
    result = await db.execute(query)
    companies = list(result.scalars().all())

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "name", "domain", "city", "state_province", "country",
        "employee_count", "naics_code", "naics_description",
        "industry_segment", "website", "phone", "stage", "source",
        "tags", "created_at",
    ])

    for c in companies:
        writer.writerow([_csv_safe(v) for v in (
            str(c.id), c.name, c.domain or "", c.city or "",
            c.state_province or "", c.country, c.employee_count or "",
            c.naics_code or "", c.naics_description or "",
            c.industry_segment or "", c.website or "", c.phone or "",
            c.stage, c.source, ",".join(c.tags or []),
            c.created_at.isoformat() if c.created_at else "",
        )])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=landline_companies_export.csv"},
    )


@router.get("/export/contacts/csv")
async def export_contacts_csv(
    limit: Optional[int] = Query(None, ge=1, le=100000, description="Max rows (omit = all)"),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Export contacts as a CSV file. Unbounded by default; ``limit``/``offset``
    let a caller page a very large table instead of streaming it all at once."""
    query = (
        select(Contact, Company.name)
        .outerjoin(Company, Contact.company_id == Company.id)
        .order_by(Contact.created_at.desc())
        .offset(offset)
    )
    if limit is not None:
        query = query.limit(limit)
    result = await db.execute(query)
    rows = result.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "company_name", "first_name", "last_name", "title",
        "email", "phone", "linkedin_url", "is_primary", "source", "created_at",
    ])

    for contact, company_name in rows:
        writer.writerow([_csv_safe(v) for v in (
            str(contact.id), company_name or "", contact.first_name or "",
            contact.last_name or "", contact.title or "",
            contact.email or "", contact.phone or "",
            contact.linkedin_url or "", contact.is_primary,
            contact.source or "", contact.created_at.isoformat() if contact.created_at else "",
        )])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=landline_contacts_export.csv"},
    )
