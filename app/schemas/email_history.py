"""Pydantic schemas for the unified email communication feature."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class EmailMessageRead(BaseModel):
    """A single email message (sent or received) in a thread."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    direction: str  # "sent" or "received"
    from_email: str
    to_email: Optional[str] = None
    subject: Optional[str] = None
    body_html: Optional[str] = None
    body_text: Optional[str] = None
    snippet: Optional[str] = None
    status: Optional[str] = None  # sent/delivered/opened/clicked/replied for sent; "received" for received
    gmail_thread_id: Optional[str] = None
    gmail_message_id: Optional[str] = None
    timestamp: datetime  # sent_at or received_at
    opened_at: Optional[datetime] = None
    clicked_at: Optional[datetime] = None
    is_read: bool = True
    contact_id: Optional[uuid.UUID] = None
    contact_name: Optional[str] = None
    company_id: Optional[uuid.UUID] = None
    company_name: Optional[str] = None
    # The connected Gmail account this message was sent from / received on.
    gmail_account_id: Optional[uuid.UUID] = None
    gmail_account_email: Optional[str] = None


class EmailThreadRead(BaseModel):
    """A conversation thread grouping sent and received messages."""

    gmail_thread_id: str
    subject: Optional[str] = None
    messages: list[EmailMessageRead] = []
    last_activity: datetime
    has_unread: bool = False
    lead_name: Optional[str] = None
    lead_id: Optional[uuid.UUID] = None


class EmailInboxResponse(BaseModel):
    """Paginated inbox response."""

    threads: list[EmailThreadRead] = []
    total: int = 0
    page: int = 1
    per_page: int = 20


class EmailLeadResponse(BaseModel):
    """All email threads for a given lead (company)."""

    threads: list[EmailThreadRead] = []
    total_messages: int = 0


class EmailReplyRequest(BaseModel):
    """Request body for sending a reply."""

    body_html: str
    body_text: Optional[str] = None


class EmailReplyResponse(BaseModel):
    """Response after sending a reply."""

    status: str
    gmail_message_id: Optional[str] = None
    detail: Optional[str] = None


class SyncStatusResponse(BaseModel):
    """Response from a manual sync trigger."""

    synced: int = 0
    errors: int = 0
    last_sync: Optional[datetime] = None
