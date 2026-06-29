"""Pydantic schemas for Contact endpoints."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class PhoneEntry(BaseModel):
    """A single labelled phone number (e.g. {"label": "Office", "number": "..."})."""
    label: Optional[str] = None
    number: str


class ContactBase(BaseModel):
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    title: Optional[str] = Field(None, max_length=150)
    department: Optional[str] = Field(None, max_length=150)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=30)
    mobile: Optional[str] = Field(None, max_length=30)
    fax: Optional[str] = Field(None, max_length=30)
    additional_phones: Optional[list[PhoneEntry]] = None
    linkedin_url: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = None
    is_primary: bool = False
    # 'genuine' or 'test' — see app.services.contact_grouping. Omit to let the
    # server auto-classify from the email/source/name.
    contact_group: Optional[str] = Field(None, max_length=20)


class ContactCreate(ContactBase):
    company_id: uuid.UUID
    source: Optional[str] = Field(None, max_length=50)


class ContactUpdate(BaseModel):
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    title: Optional[str] = Field(None, max_length=150)
    department: Optional[str] = Field(None, max_length=150)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=30)
    mobile: Optional[str] = Field(None, max_length=30)
    fax: Optional[str] = Field(None, max_length=30)
    additional_phones: Optional[list[PhoneEntry]] = None
    linkedin_url: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = None
    is_primary: Optional[bool] = None
    source: Optional[str] = Field(None, max_length=50)
    contact_group: Optional[str] = Field(None, max_length=20)


class ContactRead(ContactBase):
    """Contact as returned in lists. Excludes the heavy ``card_image`` payload;
    use ``has_card_image`` to know whether to fetch the detail endpoint."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    source: Optional[str] = None
    has_card_image: bool = False
    created_at: datetime
    updated_at: datetime


class ContactDetailRead(ContactRead):
    """Single-contact view — adds the full base64 card image data URL."""
    card_image: Optional[str] = None


class ContactListResponse(BaseModel):
    items: list[ContactRead]
    total: int
    # Pagination metadata (mirrors CompanyListResponse). Optional so callers that
    # return a full, unpaginated set (all contacts for one company) can omit them.
    page: Optional[int] = None
    page_size: Optional[int] = None


# --- Quick Add (compose shortcut + business-card scanner) ---

class QuickAddRequest(BaseModel):
    """Add a contact on the fly — creates the company if needed.

    Used both by the compose view (name + email + company) and by the business
    card scanner, which also supplies phone, links and the company's location /
    website. Everything is optional so a partial OCR read still saves, and
    `email` is a plain string (validated, then dropped if malformed) so an
    imperfect scan never 422s the whole save.
    """
    # Person — stored on the Contact.
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    title: Optional[str] = Field(None, max_length=150)
    department: Optional[str] = Field(None, max_length=150)
    email: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=30)
    mobile: Optional[str] = Field(None, max_length=30)
    fax: Optional[str] = Field(None, max_length=30)
    # Every number found on the card, labelled — nothing is dropped.
    additional_phones: Optional[list[PhoneEntry]] = None
    linkedin_url: Optional[str] = None
    # Company / location — stored on the find-or-created Company AND, for the
    # detail view, mirrored onto the Contact.
    company_name: Optional[str] = Field(None, max_length=255)
    website: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = None
    notes: Optional[str] = None
    # The scanned card image as a base64 data URL ("data:image/...;base64,...").
    card_image: Optional[str] = None
    # Where the contact came from ('manual', 'card_scan', …). Drives the
    # frontend "Scanned" filter, so it must round-trip unchanged.
    source: Optional[str] = Field(None, max_length=50)
    # 'genuine' or 'test'. Omit to let the server auto-classify.
    contact_group: Optional[str] = Field(None, max_length=20)


class QuickAddResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contact: ContactRead
    company_id: uuid.UUID
    company_name: str
    company_created: bool


# --- Duplicate detection (run before saving a scanned card) ---

class DuplicateMatch(BaseModel):
    """An existing contact that looks like the one about to be saved."""
    id: uuid.UUID
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None
    company_id: uuid.UUID
    company_name: Optional[str] = None
    source: Optional[str] = None
    has_card_image: bool = False
    created_at: datetime
    # Why this was flagged: any of "email", "phone", "name".
    match_reasons: list[str] = []


class DuplicateCheckResponse(BaseModel):
    matches: list[DuplicateMatch] = []
