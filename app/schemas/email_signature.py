"""Pydantic schemas for email signatures."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class EmailSignatureBase(BaseModel):
    name: str = Field(..., max_length=100)
    content_html: str = Field("", description="HTML content for the signature")


class EmailSignatureCreate(EmailSignatureBase):
    is_default: bool = False


class EmailSignatureUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    content_html: Optional[str] = None
    is_default: Optional[bool] = None


class EmailSignatureRead(EmailSignatureBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_default: bool
    created_at: datetime
    updated_at: datetime
