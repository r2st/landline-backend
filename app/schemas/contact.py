"""Pydantic schemas for Contact endpoints."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ContactBase(BaseModel):
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    title: Optional[str] = Field(None, max_length=150)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=30)
    linkedin_url: Optional[str] = None
    is_primary: bool = False


class ContactCreate(ContactBase):
    company_id: uuid.UUID
    source: Optional[str] = Field(None, max_length=50)


class ContactUpdate(BaseModel):
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    title: Optional[str] = Field(None, max_length=150)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=30)
    linkedin_url: Optional[str] = None
    is_primary: Optional[bool] = None
    source: Optional[str] = Field(None, max_length=50)


class ContactRead(ContactBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    source: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ContactListResponse(BaseModel):
    items: list[ContactRead]
    total: int


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
    email: Optional[str] = None
    phone: Optional[str] = Field(None, max_length=30)
    linkedin_url: Optional[str] = None
    # Company / location — stored on the find-or-created Company.
    company_name: Optional[str] = Field(None, max_length=255)
    website: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = Field(None, max_length=100)
    state: Optional[str] = Field(None, max_length=100)
    country: Optional[str] = None
    notes: Optional[str] = None
    # Where the contact came from ('manual', 'card_scan', …). Drives the
    # frontend "Scanned" filter, so it must round-trip unchanged.
    source: Optional[str] = Field(None, max_length=50)


class QuickAddResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contact: ContactRead
    company_id: uuid.UUID
    company_name: str
    company_created: bool
