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
from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.schemas.dashboard import CSVImportResult
from app.services.ingestion import (
    CandidateRecord,
    ContactRecord,
    IngestionError,
    get_ingestion_service,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/import-export", tags=["import/export"])

# Characters that spreadsheet apps (Excel, Sheets, LibreOffice) interpret as the
# start of a formula. A cell beginning with one of these can execute when the
# exported CSV is opened, so we defuse it by prefixing a single quote.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    """Neutralize CSV formula injection in an exported cell value."""
    text = "" if value is None else str(value)
    if text and text[0] in _CSV_FORMULA_PREFIXES:
        return "'" + text
    return text


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

    MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum 10MB allowed.")
    try:
        text = content.decode("utf-8-sig")  # Handle BOM
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))

    ingestion = get_ingestion_service()
    imported_companies = 0
    imported_contacts = 0
    skipped = 0
    errors = []
    new_company_ids = []  # Track newly created companies for auto-scoring

    for row_num, row in enumerate(reader, start=2):  # Row 1 is header
        try:
            # Normalize keys to lowercase
            row = {k.strip().lower().replace(" ", "_"): v.strip() for k, v in row.items() if k}

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
            if len(errors) > 100:
                errors.append("... (truncated, too many errors)")
                break
        except Exception as e:
            errors.append(f"Row {row_num}: {str(e)}")
            if len(errors) > 100:
                errors.append("... (truncated, too many errors)")
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
