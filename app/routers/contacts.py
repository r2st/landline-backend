"""Contact CRUD endpoints linked to companies."""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.schemas.contact import (
    ContactCreate,
    ContactListResponse,
    ContactRead,
    ContactUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.post("/", response_model=ContactRead, status_code=201)
async def create_contact(
    payload: ContactCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new contact linked to a company."""
    # Verify company exists
    result = await db.execute(
        select(Company).where(Company.id == payload.company_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Company not found")

    contact = Contact(**payload.model_dump())
    db.add(contact)
    await db.flush()
    await db.refresh(contact)
    logger.info("Created contact %s for company %s", contact.id, payload.company_id)
    return contact


@router.get("/", response_model=ContactListResponse)
async def list_contacts(
    company_id: Optional[uuid.UUID] = Query(None),
    q: Optional[str] = Query(None, description="Search name, email, or title"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List contacts, optionally filtered by company."""
    query = select(Contact)
    count_query = select(func.count(Contact.id))

    if company_id:
        query = query.where(Contact.company_id == company_id)
        count_query = count_query.where(Contact.company_id == company_id)

    if q:
        search = f"%{q}%"
        from sqlalchemy import or_
        search_filter = or_(
            Contact.first_name.ilike(search),
            Contact.last_name.ilike(search),
            Contact.email.ilike(search),
            Contact.title.ilike(search),
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    total_result = await db.execute(count_query)
    total = total_result.scalar()

    offset = (page - 1) * page_size
    query = query.order_by(Contact.created_at.desc()).offset(offset).limit(page_size)

    result = await db.execute(query)
    contacts = list(result.scalars().all())

    return ContactListResponse(items=contacts, total=total)


@router.get("/{contact_id}", response_model=ContactRead)
async def get_contact(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get a single contact by ID."""
    result = await db.execute(select(Contact).where(Contact.id == contact_id))
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


@router.patch("/{contact_id}", response_model=ContactRead)
async def update_contact(
    contact_id: uuid.UUID,
    payload: ContactUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update contact fields."""
    result = await db.execute(select(Contact).where(Contact.id == contact_id))
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(contact, field, value)

    await db.flush()
    await db.refresh(contact)
    logger.info("Updated contact %s", contact_id)
    return contact


@router.delete("/{contact_id}", status_code=204)
async def delete_contact(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Delete a contact."""
    result = await db.execute(select(Contact).where(Contact.id == contact_id))
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    await db.delete(contact)
    await db.flush()
    logger.info("Deleted contact %s", contact_id)


@router.get("/company/{company_id}", response_model=ContactListResponse)
async def get_company_contacts(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get all contacts for a specific company."""
    result = await db.execute(
        select(Company).where(Company.id == company_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Company not found")

    contacts_result = await db.execute(
        select(Contact)
        .where(Contact.company_id == company_id)
        .order_by(Contact.is_primary.desc(), Contact.created_at.desc())
    )
    contacts = list(contacts_result.scalars().all())

    return ContactListResponse(items=contacts, total=len(contacts))


# --- Quick Add (compose shortcut + business-card scanner) ---

from app.schemas.contact import QuickAddRequest, QuickAddResponse

# Companies are constrained to these two countries (see Company check constraint),
# so free-text country values from OCR must be mapped onto US/CA.
_COUNTRY_MAP = {
    "us": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US",
    "united states": "US", "united states of america": "US", "america": "US",
    "ca": "CA", "can": "CA", "canada": "CA",
}


def _normalize_country(raw: Optional[str]) -> str:
    return _COUNTRY_MAP.get((raw or "").strip().lower(), "US")


def _valid_email(raw: Optional[str]) -> Optional[str]:
    """Return a syntactically valid email or None, so bad OCR never blocks a save."""
    from pydantic import TypeAdapter, EmailStr

    value = (raw or "").strip()
    if not value:
        return None
    try:
        return TypeAdapter(EmailStr).validate_python(value)
    except Exception:
        return None


@router.post("/quick-add", response_model=QuickAddResponse, status_code=201)
async def quick_add_contact(
    payload: QuickAddRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create a contact on the fly (compose view or business-card scanner).

    Finds or creates the Company by name, then attaches the contact. Person
    fields (name, title, email, phone, LinkedIn) live on the Contact; company
    and location fields are stored on the Company. The caller's ``source`` is
    preserved unchanged so the frontend "Scanned" filter keeps working.
    """
    source = (payload.source or "manual").strip() or "manual"
    company_name = (payload.company_name or "").strip()
    company_created = False

    company = None
    if company_name:
        result = await db.execute(
            select(Company).where(func.lower(Company.name) == company_name.lower())
        )
        company = result.scalar_one_or_none()

    if company is None:
        # Company.name is required; fall back to the person's name or email.
        fallback_name = (
            company_name
            or " ".join(p for p in (payload.first_name, payload.last_name) if p).strip()
            or (payload.email or "").strip()
            or "Unknown Company"
        )
        company = Company(
            name=fallback_name,
            city=(payload.city or None),
            state_province=(payload.state or None),
            country=_normalize_country(payload.country),
            website=(payload.website or None),
            phone=(payload.phone or None),
            notes=(payload.address or None),
            source=source,
        )
        db.add(company)
        await db.flush()
        company_created = True
        logger.info("Quick-add created company '%s' (%s)", company.name, company.id)

    contact = Contact(
        company_id=company.id,
        first_name=(payload.first_name or None),
        last_name=(payload.last_name or None),
        title=(payload.title or None),
        email=_valid_email(payload.email),
        phone=(payload.phone or None),
        linkedin_url=(payload.linkedin_url or None),
        source=source,
    )
    db.add(contact)
    await db.flush()
    await db.refresh(contact)
    logger.info(
        "Quick-add created contact %s <%s> for company '%s' (source=%s)",
        contact.id, contact.email, company.name, source,
    )
    return QuickAddResponse(
        contact=contact,
        company_id=company.id,
        company_name=company.name,
        company_created=company_created,
    )
