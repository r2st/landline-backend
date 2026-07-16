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


class InboxReplyDetail(InboxReply):
    """Full reply detail — adds the body and, for ``interested`` replies, a
    lazily-computed suggested draft (via ``analyze_reply``)."""

    body_text: Optional[str] = None
    body_html: Optional[str] = None
    email_send_id: Optional[str] = None
    # Only populated for interested replies (drafting is expensive, so it's lazy).
    suggested_draft: Optional[dict[str, Any]] = None


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
