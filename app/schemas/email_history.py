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
    # Intent classification + hot-lead marker (incoming replies only).
    reply_classification: Optional[str] = None
    priority_flag: bool = False


class ThreadLabelRead(BaseModel):
    """A label chip attached to a thread."""

    id: uuid.UUID
    name: str
    color: str


class EmailThreadRead(BaseModel):
    """A conversation thread grouping sent and received messages."""

    gmail_thread_id: str
    subject: Optional[str] = None
    messages: list[EmailMessageRead] = []
    last_activity: datetime
    has_unread: bool = False
    lead_name: Optional[str] = None
    lead_id: Optional[uuid.UUID] = None
    # Thread-level triage surface (see EmailHistoryService._group_into_threads).
    priority: bool = False
    classification: Optional[str] = None
    is_archived: bool = False
    # Per-user view state: an active snooze (null = not snoozed) and the user's
    # labels attached to this thread.
    snoozed_until: Optional[datetime] = None
    labels: list[ThreadLabelRead] = []


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


# --- Thread read-state / archive ---

from pydantic import Field  # noqa: E402


class ThreadIdsRequest(BaseModel):
    """A batch of thread ids for a bulk read/archive action."""

    thread_ids: list[str] = Field(..., min_length=1, max_length=200)


class ThreadReadResponse(BaseModel):
    """Result of a mark-read / mark-unread action."""

    updated: int = 0  # number of reply rows whose is_read was set
    is_read: bool = True


class ThreadArchiveResponse(BaseModel):
    """Result of an archive / unarchive action."""

    updated: int = 0  # number of threads whose archive flag was set
    is_archived: bool = True


# --- Snooze ---


class ThreadSnoozeRequest(BaseModel):
    """Request body for snoozing a thread until a given time.

    ``snoozed_until=None`` un-snoozes (the thread returns immediately). A
    timestamp in the past is treated as un-snooze by the query layer.
    """

    snoozed_until: Optional[datetime] = None


class BulkThreadSnoozeRequest(ThreadSnoozeRequest):
    """Snooze/un-snooze a batch of threads."""

    thread_ids: list[str] = Field(..., min_length=1, max_length=200)


class ThreadSnoozeResponse(BaseModel):
    """Result of a snooze / un-snooze action."""

    updated: int = 0
    snoozed_until: Optional[datetime] = None


# --- Labels ---


class LabelCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    color: str = Field("gray", max_length=24)


class LabelUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    color: Optional[str] = Field(None, max_length=24)


class LabelRead(BaseModel):
    id: uuid.UUID
    name: str
    color: str
    created_at: Optional[datetime] = None
    thread_count: int = 0


class ThreadLabelToggleResponse(BaseModel):
    """Result of attaching / detaching a label to a thread."""

    gmail_thread_id: str
    label_id: uuid.UUID
    attached: bool


# --- Analytics ---


class DailyVolumePoint(BaseModel):
    date: str
    sent: int = 0
    received: int = 0


class InboxAnalyticsResponse(BaseModel):
    """Mailbox analytics over a trailing window (see get_inbox_analytics)."""

    window_days: int = 30
    sent_count: int = 0
    received_count: int = 0
    unread_count: int = 0
    needs_reply_count: int = 0
    responded_count: int = 0
    avg_response_seconds: Optional[float] = None
    median_response_seconds: Optional[float] = None
    classification_breakdown: dict[str, int] = {}
    daily_volume: list[DailyVolumePoint] = []
