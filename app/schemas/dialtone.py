"""Pydantic schemas for the Dialtone Console — the unified activity log for
Landline's autonomous cold-email agent, Dialtone.

The console has two surfaces:

* ``/dialtone/activity`` — a chronological (newest-first) event stream of
  everything Dialtone does: emails sent, engagement (opens / clicks / replies /
  bounces), errors, and lead discovery. Filterable by date range, event type,
  status, and contact.
* ``/dialtone/console`` — the header/at-a-glance view: kill-switch state,
  warm-up ramp progress (today vs the daily cap), today's engagement counts, and
  per-sender warm-up/health.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel

# The event types the Dialtone activity feed can emit / be filtered by.
DIALTONE_EVENT_TYPES = (
    "email_sent",
    "email_opened",
    "email_clicked",
    "reply",
    "delivered",
    "bounced",
    "unsubscribed",
    "error",
    "lead_discovered",
    # Feature 2: the danger monitor auto-paused all sending (severity error), and
    # the operator's acknowledged resume that cleared it.
    "auto_paused",
    "resumed",
    # Feature 5: a sending domain's health changed (score/band transition or a
    # new blacklist listing).
    "domain_health_changed",
    # Feature 1: an incoming reply was auto-classified + routed.
    "reply_classified",
    # Feature 3: an A/B test concluded (a winner was declared and promoted).
    "ab_test_concluded",
    # Warm-lead phone enrichment: a phone number was auto-found for a prospect
    # showing repeated engagement, so the operator can follow up by phone.
    "phone_enriched",
)


class DialtoneEvent(BaseModel):
    """One entry in Dialtone's chronological activity log."""

    id: str
    type: str
    timestamp: datetime
    title: str
    description: Optional[str] = None
    # Where the event came from: "email_send" | "outreach" | "prospecting".
    source: str
    # "ok" | "warn" | "error" — drives the console's status dot colour.
    severity: str = "ok"
    # Type-specific extras (to_email, subject, contact name, status, error, …).
    metadata: dict[str, Any] = {}


class DialtoneActivityFeed(BaseModel):
    items: list[DialtoneEvent]
    total: int
    page: int
    page_size: int


class DialtoneSenderStatus(BaseModel):
    """Per-sender warm-up + deliverability snapshot for the console."""

    account_id: str
    account_type: str  # gmail | smtp
    email: Optional[str] = None
    health_status: str = "unknown"  # healthy | watch | throttled | critical | unknown
    reputation_score: Optional[int] = None
    throttle_factor: float = 1.0
    effective_daily_limit: int = 0
    warmup_daily_limit: Optional[int] = None
    used_today: int = 0
    remaining_today: int = 0


class DialtoneTodayCounts(BaseModel):
    """Today's activity tallies (in the reporting timezone)."""

    emails_sent: int = 0
    opens: int = 0
    clicks: int = 0
    replies: int = 0
    bounces: int = 0
    errors: int = 0
    leads_discovered: int = 0


class DialtoneWarmup(BaseModel):
    """Warm-up ramp progress for the whole system."""

    today_sent: int = 0
    daily_limit: int = 0
    daily_remaining: int = 0
    monthly_sent: int = 0
    warmup_active: bool = False
    warmup_week: Optional[int] = None


class DialtoneSending(BaseModel):
    """Global kill-switch state."""

    enabled: bool = True
    reason: Optional[str] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None


class DialtoneConsole(BaseModel):
    """The Dialtone Console header — a single call powers the whole top-of-page."""

    generated_at: datetime
    date: str  # the reporting-day these "today" numbers cover (YYYY-MM-DD)
    sending: DialtoneSending
    warmup: DialtoneWarmup
    today: DialtoneTodayCounts
    senders: list[DialtoneSenderStatus] = []


# ─── Phone enrichment summary ────────────────────────────────────────────────


class PhoneEnrichmentRecent(BaseModel):
    """One recently phone-enriched contact for the activity strip."""

    contact_id: str
    contact_name: Optional[str] = None
    company_name: Optional[str] = None
    phone: Optional[str] = None
    source: Optional[str] = None       # 'apollo' | 'company'
    source_label: str = ""             # 'direct dial' | 'company line'
    enriched_at: Optional[datetime] = None


class PhoneEnrichmentSummary(BaseModel):
    """At-a-glance stats for the warm-lead phone-enrichment sweep.

    ``attempts`` counts every contact the sweep has looked up (``phone_enriched_at``
    stamped, hit or miss); ``enriched`` counts the ones it found an auto-number
    for (``phone_source`` in apollo/company). ``hit_rate`` is their ratio.
    """

    enriched: int = 0
    attempts: int = 0
    hit_rate: float = 0.0
    # Source breakdown among enriched contacts.
    apollo: int = 0        # Apollo people-match direct dials
    company: int = 0       # company main-line fallbacks
    recent: list[PhoneEnrichmentRecent] = []
