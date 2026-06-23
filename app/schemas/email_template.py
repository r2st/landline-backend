"""Pydantic schemas for email templates, preview, and compose/send."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# --- Templates ---

class EmailTemplateBase(BaseModel):
    name: str = Field(..., max_length=255)
    subject: str = Field(..., max_length=500)
    body: str = Field(..., description="HTML body, supports {{merge_field}} placeholders")


class EmailTemplateCreate(EmailTemplateBase):
    is_default: bool = False


class EmailTemplateUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    subject: Optional[str] = Field(None, max_length=500)
    body: Optional[str] = None
    is_default: Optional[bool] = None


class EmailTemplateRead(EmailTemplateBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_default: bool
    created_at: datetime
    updated_at: datetime


# --- Preview ---

class TemplatePreviewRequest(BaseModel):
    """Render a template with a contact's data merged in.

    Provide ``template_id`` to preview a stored template, or pass inline
    ``subject``/``body`` to preview unsaved edits. Provide ``contact_id`` to
    merge a real contact's data; omit it to use sample placeholder data.
    """

    template_id: Optional[uuid.UUID] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    contact_id: Optional[uuid.UUID] = None


class TemplatePreviewResponse(BaseModel):
    subject: str
    body: str
    merge_context: dict


# --- Compose / Send ---

class EmailComposeRequest(BaseModel):
    contact_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=50)
    template_id: Optional[uuid.UUID] = None
    # Connected Gmail account to send as. Defaults to the active account, then
    # falls back to SES when no Gmail account is connected.
    sender_account_id: Optional[uuid.UUID] = None
    subject_b: Optional[str] = Field(None, max_length=500, description="Variant B subject for A/B testing")
    scheduled_at: Optional[datetime] = Field(None, description="Schedule send for later")


class EmailComposeResult(BaseModel):
    contact_id: uuid.UUID
    to_email: Optional[str] = None
    status: str
    detail: Optional[str] = None


class EmailComposeResponse(BaseModel):
    template_id: uuid.UUID
    sent: int
    skipped: int
    errors: int
    # How the batch was sent: "gmail" or "ses", and the visible From address.
    transport: str
    sender: str
    results: list[EmailComposeResult]


# --- Send history (tracking) ---

class EmailSendRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    to_email: Optional[str] = None
    subject: Optional[str] = None
    status: str
    sent_at: datetime
    opened_at: Optional[datetime] = None
    clicked_at: Optional[datetime] = None
    ab_variant: Optional[str] = None
    scheduled_at: Optional[datetime] = None

# Intentional empty line above for readability


# --- A/B Testing ---

class ABStats(BaseModel):
    variant: str
    total: int
    opened: int
    clicked: int
    open_rate: float
    click_rate: float


class ABStatsResponse(BaseModel):
    template_id: uuid.UUID
    stats: list[ABStats]


# --- Campaign Analytics ---

class CampaignAnalytics(BaseModel):
    total_sent_7d: int = 0
    total_sent_30d: int = 0
    total_sent_all: int = 0
    open_rate: float = 0.0
    click_rate: float = 0.0
    reply_rate: float = 0.0
    bounce_rate: float = 0.0
    daily_sends: list[dict] = Field(default_factory=list)


# --- Send Test ---

class SendTestRequest(BaseModel):
    template_id: uuid.UUID
    test_email: EmailStr
    sender_account_id: Optional[uuid.UUID] = None


class SendTestResponse(BaseModel):
    status: str  # "sent" or "failed"
    detail: Optional[str] = None
    subject: Optional[str] = None


# --- AI Email Generation ---

class AIEmailGenerateRequest(BaseModel):
    """Request body for AI-powered email generation."""
    first_name: str = Field(..., max_length=100)
    company_name: str = Field("", max_length=255)
    industry: str = Field("", max_length=255)
    title: str = Field("", max_length=255)
    city: str = Field("", max_length=255)
    template_type: str = Field("first_touch", max_length=50)
    custom_instructions: Optional[str] = Field(None, max_length=500)


class AIEmailGenerateResponse(BaseModel):
    subject: str
    body: str
    demo_link: str
    ai_generated: bool
    model: Optional[str] = None
    template_type: str
