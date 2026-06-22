"""Contact CRUD endpoints linked to companies."""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import undefer
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.schemas.contact import (
    ContactCreate,
    ContactDetailRead,
    ContactListResponse,
    ContactRead,
    ContactUpdate,
    DuplicateCheckResponse,
    DuplicateMatch,
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


_CONTACT_SORTS = {"created_at", "first_name", "last_name", "email", "title"}


@router.get("/", response_model=ContactListResponse)
async def list_contacts(
    company_id: Optional[uuid.UUID] = Query(None),
    q: Optional[str] = Query(None, description="Search name, email, or title"),
    unsubscribed: Optional[bool] = Query(None, description="Filter by unsubscribe status"),
    has_email: Optional[bool] = Query(None, description="Only contacts with/without an email"),
    sort_by: str = Query("created_at", description="created_at, first_name, last_name, email, title"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List contacts with filtering, search, sorting, and pagination."""
    query = select(Contact)
    count_query = select(func.count(Contact.id))

    conditions = []
    if company_id:
        conditions.append(Contact.company_id == company_id)
    if unsubscribed is not None:
        conditions.append(Contact.unsubscribed == unsubscribed)
    if has_email is not None:
        conditions.append(Contact.email.isnot(None) if has_email else Contact.email.is_(None))
    if q:
        search = f"%{q}%"
        from sqlalchemy import or_
        conditions.append(or_(
            Contact.first_name.ilike(search),
            Contact.last_name.ilike(search),
            Contact.email.ilike(search),
            Contact.title.ilike(search),
        ))

    if conditions:
        from sqlalchemy import and_
        combined = and_(*conditions)
        query = query.where(combined)
        count_query = count_query.where(combined)

    total_result = await db.execute(count_query)
    total = total_result.scalar()

    sort_col = getattr(Contact, sort_by) if sort_by in _CONTACT_SORTS else Contact.created_at
    query = query.order_by(sort_col.asc() if sort_dir == "asc" else sort_col.desc())

    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)

    result = await db.execute(query)
    contacts = list(result.scalars().all())

    return ContactListResponse(items=contacts, total=total, page=page, page_size=page_size)


@router.get("/check-duplicate", response_model=DuplicateCheckResponse)
async def check_duplicate_contact(
    email: Optional[str] = Query(None),
    phone: Optional[str] = Query(None, description="Any phone number from the card"),
    first_name: Optional[str] = Query(None),
    last_name: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Find existing contacts that look like the one about to be saved.

    Used by the card scanner before a quick-add to warn about (and offer to open)
    a likely duplicate. Matches on exact email, on a shared phone number (compared
    by significant digits across every stored number), or on a full first+last
    name match. Phone/name comparison is done in Python so it works the same on
    Postgres and the SQLite test DB and tolerates formatting differences.
    """
    email_n = (email or "").strip().lower()
    first_n = (first_name or "").strip().lower()
    last_n = (last_name or "").strip().lower()
    # Significant digits of the probe phone (drop a leading US country code so
    # "+1 555…" matches "555…"); require 7+ to avoid matching on extensions.
    probe_digits = _digits(phone or "")
    if len(probe_digits) == 11 and probe_digits.startswith("1"):
        probe_digits = probe_digits[1:]

    if not email_n and not probe_digits and not (first_n and last_n):
        return DuplicateCheckResponse(matches=[])

    # Pull a bounded, recent candidate set with its company name. additional_phones
    # is a normal (non-deferred) column, so it loads here for the phone comparison.
    rows = await db.execute(
        select(Contact, Company.name)
        .join(Company, Contact.company_id == Company.id)
        .order_by(Contact.created_at.desc())
        .limit(2000)
    )

    def phone_digits_of(c: Contact) -> set[str]:
        out: set[str] = set()
        nums = [c.phone, c.mobile, c.fax]
        for p in (c.additional_phones or []):
            if isinstance(p, dict):
                nums.append(p.get("number"))
        for n in nums:
            d = _digits(n or "")
            if len(d) == 11 and d.startswith("1"):
                d = d[1:]
            if len(d) >= 7:
                out.add(d)
        return out

    matches: list[DuplicateMatch] = []
    for contact, company_name in rows.all():
        reasons: list[str] = []
        if email_n and (contact.email or "").strip().lower() == email_n:
            reasons.append("email")
        if probe_digits and len(probe_digits) >= 7:
            if any(d == probe_digits or d.endswith(probe_digits) or probe_digits.endswith(d)
                   for d in phone_digits_of(contact)):
                reasons.append("phone")
        if first_n and last_n and (contact.first_name or "").strip().lower() == first_n \
                and (contact.last_name or "").strip().lower() == last_n:
            reasons.append("name")
        if reasons:
            matches.append(DuplicateMatch(
                id=contact.id,
                first_name=contact.first_name,
                last_name=contact.last_name,
                title=contact.title,
                email=contact.email,
                company_id=contact.company_id,
                company_name=company_name,
                source=contact.source,
                has_card_image=contact.has_card_image,
                created_at=contact.created_at,
                match_reasons=reasons,
            ))

    return DuplicateCheckResponse(matches=matches)


@router.get("/{contact_id}", response_model=ContactDetailRead)
async def get_contact(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get a single contact by ID, including its scanned card image (if any)."""
    result = await db.execute(
        select(Contact).where(Contact.id == contact_id).options(undefer(Contact.card_image))
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


@router.get("/{contact_id}/card-image")
async def get_contact_card_image(
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return the raw scanned business-card image for a contact.

    Serves the decoded bytes with the correct content type so the image can be
    fetched as a blob (the API-key auth header rules out a bare <img src>)."""
    import base64
    from fastapi import Response

    result = await db.execute(
        select(Contact).where(Contact.id == contact_id).options(undefer(Contact.card_image))
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if not contact.card_image:
        raise HTTPException(status_code=404, detail="No card image for this contact")

    data_url = contact.card_image
    media_type = "image/jpeg"
    b64 = data_url
    if data_url.startswith("data:"):
        header, _, b64 = data_url.partition(",")
        # header looks like "data:image/png;base64"
        if ":" in header and ";" in header:
            media_type = header.split(":", 1)[1].split(";", 1)[0] or media_type
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(status_code=500, detail="Stored card image is corrupt")
    return Response(content=raw, media_type=media_type)


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


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def _collect_phones(payload: "QuickAddRequest") -> list[dict]:
    """Merge phone / mobile / fax / additional_phones into one de-duplicated,
    labelled list so a card with several numbers keeps all of them."""
    entries: list[dict] = []
    seen: set[str] = set()

    def add(label: Optional[str], number: Optional[str]):
        num = (number or "").strip()
        if not num:
            return
        key = _digits(num) or num.lower()
        if key in seen:
            return
        seen.add(key)
        entries.append({"label": label, "number": num})

    add("Office", payload.phone)
    add("Mobile", payload.mobile)
    add("Fax", payload.fax)
    for p in (payload.additional_phones or []):
        # p is a PhoneEntry pydantic model
        add(p.label, p.number)
    return entries


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

    # Preserve every phone number the scan found, labelled, with no duplicates.
    extra_phones = _collect_phones(payload)

    contact = Contact(
        company_id=company.id,
        first_name=(payload.first_name or None),
        last_name=(payload.last_name or None),
        title=(payload.title or None),
        department=(payload.department or None),
        email=_valid_email(payload.email),
        phone=(payload.phone or None),
        mobile=(payload.mobile or None),
        fax=(payload.fax or None),
        additional_phones=(extra_phones or None),
        linkedin_url=(payload.linkedin_url or None),
        website=(payload.website or None),
        # Location mirrored onto the contact so the detail view shows the card
        # exactly as scanned (the company keeps its own normalized copy).
        address=(payload.address or None),
        city=(payload.city or None),
        state=(payload.state or None),
        postal_code=(payload.postal_code or None),
        country=(payload.country or None),
        notes=(payload.notes or None),
        card_image=(payload.card_image or None),
        source=source,
    )
    db.add(contact)
    await db.flush()
    await db.refresh(contact)
    if payload.card_image:
        # refresh() re-expires the deferred card_image column; mark the value we
        # just persisted as already-loaded (no extra UPDATE) so has_card_image
        # is accurate in the response without shipping the blob itself.
        set_committed_value(contact, "card_image", payload.card_image)
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
