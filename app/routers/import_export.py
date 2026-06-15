"""CSV import and export endpoints."""

import csv
import io
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.schemas.dashboard import CSVImportResult

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/import-export", tags=["import/export"])


@router.post("/import/csv", response_model=CSVImportResult)
async def import_csv(
    file: UploadFile = File(...),
    source: str = "csv_import",
    db: AsyncSession = Depends(get_db),
):
    """Import companies and contacts from a CSV file.

    Expected columns: company_name, domain, city, state, country,
    employee_count, contact_name, contact_email, contact_title

    All columns are optional except company_name.
    """
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a .csv")

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # Handle BOM
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))

    imported_companies = 0
    imported_contacts = 0
    skipped = 0
    errors = []

    for row_num, row in enumerate(reader, start=2):  # Row 1 is header
        try:
            # Normalize keys to lowercase
            row = {k.strip().lower().replace(" ", "_"): v.strip() for k, v in row.items() if k}

            company_name = row.get("company_name", "").strip()
            if not company_name:
                skipped += 1
                continue

            # Check for existing company by domain first, then name
            domain = row.get("domain", "").strip() or None
            existing = None

            if domain:
                result = await db.execute(
                    select(Company).where(Company.domain == domain).limit(1)
                )
                existing = result.scalar_one_or_none()

            if not existing:
                result = await db.execute(
                    select(Company).where(Company.name == company_name).limit(1)
                )
                existing = result.scalar_one_or_none()

            if existing:
                company = existing
                skipped += 1
            else:
                # Parse employee count
                emp_str = row.get("employee_count", "").strip()
                employee_count = None
                if emp_str:
                    try:
                        employee_count = int(emp_str.replace(",", ""))
                    except ValueError:
                        pass

                country = row.get("country", "US").strip().upper()
                if country not in ("US", "CA"):
                    country = "US"

                company = Company(
                    name=company_name,
                    domain=domain,
                    city=row.get("city", "").strip() or None,
                    state_province=row.get("state", "").strip() or None,
                    country=country,
                    employee_count=employee_count,
                    source=source,
                )
                db.add(company)
                await db.flush()
                imported_companies += 1

            # Handle contact if present
            contact_name = row.get("contact_name", "").strip()
            contact_email = row.get("contact_email", "").strip()
            contact_title = row.get("contact_title", "").strip()

            if contact_name or contact_email:
                # Parse contact name
                first_name = None
                last_name = None
                if contact_name:
                    parts = contact_name.split(None, 1)
                    first_name = parts[0] if parts else None
                    last_name = parts[1] if len(parts) > 1 else None

                # Check for duplicate contact by email
                if contact_email:
                    result = await db.execute(
                        select(Contact).where(
                            Contact.company_id == company.id,
                            Contact.email == contact_email,
                        ).limit(1)
                    )
                    if result.scalar_one_or_none():
                        continue  # Skip duplicate contact

                contact = Contact(
                    company_id=company.id,
                    first_name=first_name,
                    last_name=last_name,
                    email=contact_email or None,
                    title=contact_title or None,
                    source=source,
                )
                db.add(contact)
                imported_contacts += 1

        except Exception as e:
            errors.append(f"Row {row_num}: {str(e)}")
            if len(errors) > 100:
                errors.append("... (truncated, too many errors)")
                break

    await db.flush()
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
    db: AsyncSession = Depends(get_db),
):
    """Export all companies as a CSV file."""
    result = await db.execute(
        select(Company).order_by(Company.created_at.desc())
    )
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
        writer.writerow([
            str(c.id), c.name, c.domain or "", c.city or "",
            c.state_province or "", c.country, c.employee_count or "",
            c.naics_code or "", c.naics_description or "",
            c.industry_segment or "", c.website or "", c.phone or "",
            c.stage, c.source, ",".join(c.tags or []),
            c.created_at.isoformat() if c.created_at else "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=forge_companies_export.csv"},
    )


@router.get("/export/contacts/csv")
async def export_contacts_csv(
    db: AsyncSession = Depends(get_db),
):
    """Export all contacts as a CSV file."""
    result = await db.execute(
        select(Contact, Company.name)
        .outerjoin(Company, Contact.company_id == Company.id)
        .order_by(Contact.created_at.desc())
    )
    rows = result.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "company_name", "first_name", "last_name", "title",
        "email", "phone", "linkedin_url", "is_primary", "source", "created_at",
    ])

    for contact, company_name in rows:
        writer.writerow([
            str(contact.id), company_name or "", contact.first_name or "",
            contact.last_name or "", contact.title or "",
            contact.email or "", contact.phone or "",
            contact.linkedin_url or "", contact.is_primary,
            contact.source or "", contact.created_at.isoformat() if contact.created_at else "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=forge_contacts_export.csv"},
    )
