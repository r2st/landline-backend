"""Pydantic schemas for Smart Send-Time Optimization (Feature 6).

Powers three read-only Console surfaces under ``/dialtone/send-timing``:
global optimal times (wraps the existing analyzer), a per-contact inspection
view (resolved tz + learned profile + computed window), and the optimized-vs-
control experiment results.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class OptimalWindow(BaseModel):
    """A computed recipient-local send window."""

    tz: str
    dow_preference: list[int]     # 0=Sunday .. 6=Saturday
    hour_start: int               # inclusive, recipient-local
    hour_end: int                 # exclusive, recipient-local
    confidence: float
    source: str                   # 'personal' | 'cohort' | 'default'


class ContactProfile(BaseModel):
    """The persisted per-contact engagement profile (may be absent)."""

    buckets: dict[str, Any] = {}
    opens_total: int = 0
    best_dow: Optional[int] = None
    best_hour: Optional[int] = None
    confidence: float = 0.0
    last_computed: Optional[datetime] = None


class ContactSendTiming(BaseModel):
    """A contact's resolved timezone, engagement profile, and next optimal send."""

    contact_id: str
    email: Optional[str] = None
    timezone: Optional[str] = None
    tz_source: Optional[str] = None
    profile: Optional[ContactProfile] = None
    window: OptimalWindow
    next_send_at: Optional[datetime] = None


class ExperimentArm(BaseModel):
    """Aggregate engagement for one timing arm."""

    arm: str                      # 'optimized' | 'control'
    enrollments: int = 0
    sent: int = 0
    opens: int = 0
    replies: int = 0
    open_rate: float = 0.0        # opens / sent
    reply_rate: float = 0.0       # replies / sent


class SendTimingExperiment(BaseModel):
    """Optimized-vs-control results: per-arm stats, lift, and significance."""

    arms: list[ExperimentArm]
    open_rate_lift: Optional[float] = None      # optimized − control (percentage points, 0-1)
    reply_rate_lift: Optional[float] = None
    open_rate_confidence: Optional[float] = None
    reply_rate_confidence: Optional[float] = None
    significant: bool = False                   # reply-rate lift at >= confidence target
    confidence_target: float = 0.95
    note: str = ""


class HeatmapCell(BaseModel):
    """One (day-of-week, hour) cell of the global send/open heatmap."""

    dow: int                      # 0=Sunday .. 6=Saturday
    hour: int                     # 0..23 (send time, reporting tz)
    sent: int = 0
    opened: int = 0
    open_rate: float = 0.0        # opens / sent, 0-1


class SendTimingHeatmap(BaseModel):
    """The full (dow, hour) grid of open rates — powers the results heatmap."""

    has_data: bool = False
    cells: list[HeatmapCell] = []
    total_sent: int = 0
    total_opened: int = 0
    overall_open_rate: float = 0.0
    best_cell: Optional[HeatmapCell] = None
    min_sample: int = 0           # cells below this are too thin to trust


class SendTimingStatus(BaseModel):
    """Overall optimizer status — is it on, and how much has it learned."""

    enabled: bool
    optimized_fraction: float
    min_opens: int
    max_delay_hours: int
    reply_weight: int
    # Best-practice default window (recipient-local) used when data is thin.
    default_dows: list[int]
    default_hour_start: int
    default_hour_end: int
    # Business-hours guardrail the window is always intersected with.
    business_hour_start: int
    business_hour_end: int
    weekdays_only: bool
    # What the optimizer has learned so far.
    profiles_total: int = 0
    profiles_with_signal: int = 0     # opens_total >= min_opens (personal pattern trusted)
    profiles_with_opens: int = 0      # opens_total > 0
    contacts_with_timezone: int = 0
    last_computed: Optional[datetime] = None


class ContactWindowSummary(BaseModel):
    """A learned contact's best window — one row of the per-contact list."""

    contact_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    timezone: Optional[str] = None
    tz_source: Optional[str] = None
    best_dow: Optional[int] = None
    best_hour: Optional[int] = None
    opens_total: int = 0
    confidence: float = 0.0
    window: OptimalWindow
    last_computed: Optional[datetime] = None


class ContactWindowList(BaseModel):
    """Top learned contacts with their computed optimal send windows."""

    contacts: list[ContactWindowSummary] = []
    total: int = 0
