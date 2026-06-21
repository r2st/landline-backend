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


# --- Quick Add (compose shortcut) ---

class QuickAddRequest(BaseModel):
    """Add a contact from the compose view — creates the company if needed."""
    company_name: str = Field(..., min_length=1, max_length=255)
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    email: EmailStr
    title: Optional[str] = Field(None, max_length=150)


class QuickAddResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contact: ContactRead
    company_name: str
    company_created: bool
