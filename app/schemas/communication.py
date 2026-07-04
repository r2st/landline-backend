"""Schemas for the unified per-client communication history."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class CommunicationEntry(BaseModel):
    """A single communication touchpoint with a contact, with user attribution."""

    id: str
    # email_sent | email_received | call | interaction
    type: str
    # outbound | inbound | internal
    direction: str
    timestamp: Optional[datetime] = None
    subject: Optional[str] = None
    summary: Optional[str] = None
    status: Optional[str] = None

    # Who performed / owns this communication.
    user_id: Optional[uuid.UUID] = None
    user_name: Optional[str] = None
    user_email: Optional[str] = None

    # Which email account it was sent from / received on (email channel only).
    email_account_id: Optional[uuid.UUID] = None
    email_account: Optional[str] = None

    contact_id: Optional[uuid.UUID] = None
    company_id: Optional[uuid.UUID] = None


class CommunicationHistoryResponse(BaseModel):
    """Full, user-attributed communication history for a contact."""

    contact_id: uuid.UUID
    contact_name: Optional[str] = None
    company_id: Optional[uuid.UUID] = None
    company_name: Optional[str] = None
    total: int = 0
    communications: list[CommunicationEntry] = []
