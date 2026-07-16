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
