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


# --- Quick Add (compose shortcut) ---

from app.schemas.contact import QuickAddRequest, QuickAddResponse


@router.post("/quick-add", response_model=QuickAddResponse, status_code=201)
async def quick_add_contact(
    payload: QuickAddRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create a contact from the compose view.

    Looks up the company by name (case-insensitive). If none exists, a new
    company is created with source='manual'. The contact is then linked to
    that company. This lets users add a recipient on the fly without leaving
    the compose tab.
    """
    company_name = payload.company_name.strip()
    company_created = False

    # Try to find an existing company by name (case-insensitive).
    result = await db.execute(
        select(Company).where(func.lower(Company.name) == company_name.lower())
    )
    company = result.scalar_one_or_none()

    if company is None:
        company = Company(name=company_name, source="manual", country="US")
        db.add(company)
        await db.flush()
        company_created = True
        logger.info("Quick-add created company '%s' (%s)", company_name, company.id)

    contact = Contact(
        company_id=company.id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=payload.email,
        title=payload.title,
        source="quick-add",
    )
    db.add(contact)
    await db.flush()
    await db.refresh(contact)
    logger.info(
        "Quick-add created contact %s <%s> for company '%s'",
        contact.id, payload.email, company_name,
    )
    return QuickAddResponse(
        contact=contact,
        company_name=company_name,
        company_created=company_created,
    )
