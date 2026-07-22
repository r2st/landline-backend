"""Schemas specific to the public REST API (``/api/v1/public``).

Most public endpoints reuse the app's existing domain schemas (ContactRead,
CompanyRead, DealRead, …). The types here cover the surface those don't:
sequence listing and enrollment management, shaped for an external integrator
rather than the in-app UI.
"""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class SequenceRead(BaseModel):
    """An outreach sequence as exposed to public-API callers."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: Optional[str] = None
    industry: Optional[str] = None
    status: str
    tone: str
    cadence_pattern: Optional[str] = None
    created_at: datetime


class EnrollmentCreate(BaseModel):
    """Enroll a contact into a sequence."""

    contact_id: uuid.UUID
    sequence_id: uuid.UUID


class EnrollmentRead(BaseModel):
    """An enrollment (a contact's progress through a sequence)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    contact_id: uuid.UUID
    sequence_id: uuid.UUID
    current_step: int
    status: str
    enrolled_at: datetime
    completed_at: Optional[datetime] = None
    next_send_at: Optional[datetime] = None


class DealNotePublicRead(BaseModel):
    """A deal note for the public API (drops the internal user attribution)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    deal_id: uuid.UUID
    content: str
    created_at: datetime
    updated_at: datetime


class PaginatedMeta(BaseModel):
    """Pagination envelope reused across list endpoints."""

    total: int
    page: int
    page_size: int


class ContactListPublic(PaginatedMeta):
    items: list = Field(default_factory=list)


class CompanyListPublic(PaginatedMeta):
    items: list = Field(default_factory=list)


class DealListPublic(PaginatedMeta):
    items: list = Field(default_factory=list)
