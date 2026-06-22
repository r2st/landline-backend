"""Pydantic schemas for interactions (logged client touchpoints)."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Types accepted by the manual /interactions create endpoint. Internal writers
# (reply tracker, email logger) may use other values directly on the model.
VALID_INTERACTION_TYPES = ("meeting", "call", "email", "note")


class InteractionCreate(BaseModel):
    company_id: uuid.UUID
    contact_id: Optional[uuid.UUID] = None
    interaction_type: str
    subject: Optional[str] = Field(None, max_length=255)
    content: Optional[str] = None
    occurred_at: Optional[datetime] = None
    follow_up_date: Optional[datetime] = None

    @field_validator("interaction_type")
    @classmethod
    def _validate_type(cls, v):
        if v not in VALID_INTERACTION_TYPES:
            raise ValueError(f"interaction_type must be one of {VALID_INTERACTION_TYPES}")
        return v


class InteractionUpdate(BaseModel):
    contact_id: Optional[uuid.UUID] = None
    interaction_type: Optional[str] = None
    subject: Optional[str] = Field(None, max_length=255)
    content: Optional[str] = None
    occurred_at: Optional[datetime] = None
    follow_up_date: Optional[datetime] = None

    @field_validator("interaction_type")
    @classmethod
    def _validate_type(cls, v):
        if v is not None and v not in VALID_INTERACTION_TYPES:
            raise ValueError(f"interaction_type must be one of {VALID_INTERACTION_TYPES}")
        return v


class InteractionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    interaction_type: str
    subject: Optional[str] = None
    content: Optional[str] = None
    notes: Optional[str] = None
    occurred_at: datetime
    follow_up_date: Optional[datetime] = None
    created_at: Optional[datetime] = None
