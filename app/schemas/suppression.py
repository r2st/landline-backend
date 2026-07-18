"""Schemas for the suppression-list API (Feature 2)."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class SuppressionEntryRead(BaseModel):
    id: uuid.UUID
    email: str
    reason: str
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}


class SuppressionListResponse(BaseModel):
    """Paginated suppression list."""

    items: list[SuppressionEntryRead]
    total: int
    page: int
    page_size: int


class SuppressionImportResult(BaseModel):
    """Outcome of a bulk CSV import."""

    added: int
    duplicates: int
    invalid: int
    total: int


class SuppressionEntryCreate(BaseModel):
    """Add a single address by hand."""

    email: str
    reason: str = "manual"
