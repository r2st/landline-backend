"""Pydantic schemas for the Gmail OAuth connection endpoints."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class AuthorizeResponse(BaseModel):
    """The Google consent URL the frontend should open."""

    authorization_url: str
    state: str


class GmailAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: Optional[str] = None
    is_active: bool
    status: str
    connected_at: datetime
    last_used_at: Optional[datetime] = None


class GmailStatusResponse(BaseModel):
    """Overall connection status for the settings UI and compose 'From' line."""

    configured: bool  # whether Google OAuth env vars are set on the server
    connected: bool  # whether at least one account is connected
    active_email: Optional[str] = None
    active_account_id: Optional[uuid.UUID] = None
    fallback_sender: str  # the SES sender used when nothing is connected
    accounts: list[GmailAccountRead] = []
