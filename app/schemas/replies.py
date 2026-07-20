"""Pydantic schemas for the Dialtone Priority Inbox (Feature 1 — Reply
Classification).

These power the operator-facing ``/dialtone/inbox*`` surface: the classified
reply list, per-reply detail (with a lazily-drafted suggested response for hot
leads), the header stat badges, and the manual reclassify override.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class InboxReply(BaseModel):
    """One classified reply row in the priority inbox."""

    id: str
    reply_classification: Optional[str] = None
    classification_confidence: Optional[float] = None
    priority_flag: bool = False
    handled_action: Optional[str] = None
    classified_at: Optional[datetime] = None
    is_read: bool = False

    from_email: str
    subject: Optional[str] = None
    snippet: Optional[str] = None
    received_at: datetime

    contact_id: Optional[str] = None
    contact_name: Optional[str] = None
    company_id: Optional[str] = None
    company_name: Optional[str] = None

    # return_date, referral, keyword-hit, model, source, manual overrides.
    classification_meta: dict[str, Any] = {}


class InboxList(BaseModel):
    items: list[InboxReply]
    total: int
    page: int
    page_size: int


class BounceBreakdown(BaseModel):
    """Delivery-failure detail for a ``bounce`` reply.

    Surfaces what the bounce pipeline recorded about the dead address so the
    operator can tell a hard bounce (dead mailbox — suppress and move on) from a
    soft one (transient — worth a retry) without leaving the reply detail. The
    ``category``/``status_code``/``diagnostic`` come from the matching
    ``BounceRecord`` when one exists; otherwise from the reply's own
    classification metadata and the contact's bounce counters.
    """

    # 'hard' | 'soft' — drives the severity colour (hard=red, soft=yellow).
    category: Optional[str] = None
    # DSN Status (e.g. "5.1.1") and the raw Diagnostic-Code, when available.
    status_code: Optional[str] = None
    diagnostic: Optional[str] = None
    # The address that actually bounced (never mailer-daemon@…).
    recipient: Optional[str] = None
    # Contact-level bounce history for context.
    bounce_count: Optional[int] = None
    last_bounced_at: Optional[datetime] = None
    # Whether the bounce pipeline suppressed the contact.
    suppressed: Optional[bool] = None


class InboxReplyDetail(InboxReply):
    """Full reply detail — adds the body and, for ``interested`` replies, a
    lazily-computed suggested draft (via ``analyze_reply``)."""

    body_text: Optional[str] = None
    body_html: Optional[str] = None
    email_send_id: Optional[str] = None
    # Only populated for interested replies (drafting is expensive, so it's lazy).
    suggested_draft: Optional[dict[str, Any]] = None
    # Only populated for `bounce` replies — the delivery-failure breakdown.
    bounce: Optional[BounceBreakdown] = None


class InboxStats(BaseModel):
    """Counts for the inbox header badges."""

    # Per-category counts (interested, not_interested, out_of_office,
    # wrong_person, unsubscribe, unknown).
    by_category: dict[str, int] = {}
    total: int = 0
    unclassified: int = 0
    priority: int = 0
    unread: int = 0


class ReclassifyRequest(BaseModel):
    """Operator override of a reply's classification."""

    category: str
    priority_flag: Optional[bool] = None


class ApproveSendRequest(BaseModel):
    """Operator-approved reply to dispatch from the Dialtone inbox.

    ``body`` is the (possibly operator-edited) suggested draft. ``subject``
    defaults to ``Re: <original subject>`` when omitted. ``body_html`` may carry a
    pre-rendered HTML body; otherwise ``body`` is treated as plain text and
    wrapped by the dispatcher.
    """

    subject: Optional[str] = None
    body: str
    body_html: Optional[str] = None


class ApproveSendResult(BaseModel):
    status: str  # "sent" | "skipped" | "halted" | "suppressed" | "error"
    detail: Optional[str] = None
    to_email: Optional[str] = None
    transport: Optional[str] = None
    sender: Optional[str] = None
    reply_id: Optional[str] = None
