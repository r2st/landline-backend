"""Pydantic schemas for Feature 2 — Auto-Pause on Danger Signals.

Surfaces the danger monitor's state to the Console's Deliverability Health
widget and drives the acknowledged manual-resume flow:

* ``GET /dialtone/health/signals`` — current metrics (global + per account),
  thresholds, and pass/warn/breach status (``DangerSignalsHealth``).
* ``GET /dialtone/health/signals/events`` — trip history (``DangerEventList``).
* ``POST /dialtone/resume`` — acknowledged resume (``ResumeRequest`` →
  ``ResumeResponse``).
* ``POST /dialtone/health/signals/test`` — dry-run the evaluator
  (``DangerTestResult``).
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ThresholdConfig(BaseModel):
    """The active thresholds (config defaults + any runtime override)."""

    bounce_rate: float
    bounce_rate_warn: float
    complaint_rate: float
    complaint_rate_warn: float
    hard_bounce_count: int
    hard_bounce_count_warn: int
    min_sample_size: int


class MetricStatus(BaseModel):
    """One evaluated metric for one scope (global or a single account)."""

    metric: str  # bounce_rate | complaint_rate | hard_bounce_count
    scope: str  # global | account
    window: str  # 24h
    observed_value: float  # rate (0..1) or a count
    threshold: float
    warn_threshold: float
    sample_size: int
    status: str  # pass | warn | breach
    account_id: Optional[str] = None
    account_email: Optional[str] = None
    hard_bounces: int = 0


class ActiveTrip(BaseModel):
    """The most recent unresolved auto-pause trip, if any."""

    id: str
    metric: str
    scope: str
    account_email: Optional[str] = None
    observed_value: float
    threshold: float
    sample_size: int
    reason: Optional[str] = None
    triggered_at: datetime


class DangerSignalsHealth(BaseModel):
    """Powers the Deliverability Health widget."""

    generated_at: datetime
    window_hours: int
    # healthy | watch | breach | paused
    overall_status: str
    sending_enabled: bool
    breach: bool
    thresholds: ThresholdConfig
    global_metrics: list[MetricStatus] = []
    account_metrics: list[MetricStatus] = []
    breaches: list[MetricStatus] = []
    warnings: list[MetricStatus] = []
    active_trip: Optional[ActiveTrip] = None


class DangerEvent(BaseModel):
    """One row from ``danger_signal_events`` (a trip)."""

    id: str
    metric: str
    scope: str
    account_id: Optional[str] = None
    account_email: Optional[str] = None
    window: str
    observed_value: float
    threshold: float
    sample_size: int
    action_taken: str
    details: dict = {}
    triggered_at: datetime
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    resolve_note: Optional[str] = None


class DangerEventList(BaseModel):
    items: list[DangerEvent]
    total: int
    page: int
    page_size: int


class ResumeRequest(BaseModel):
    """Acknowledged manual resume. Requires referencing the active trip."""

    acknowledge_danger_signal_id: UUID
    note: str = Field(..., min_length=1, description="Why it's safe to resume (audited).")
    # Resume even if live metrics are still breaching (logged). Default False —
    # the endpoint refuses (409) a resume into a still-broken state.
    force: bool = False


class ResumeResponse(BaseModel):
    resumed: bool
    sending_enabled: bool
    reason: Optional[str] = None
    enrollments_affected: int = 0
    resolved_event_ids: list[str] = []
    forced: bool = False
    # The live danger state at resume time (so the UI can show what was overridden).
    still_breaching: bool = False


class DangerTestResult(BaseModel):
    """Dry-run of the evaluator — what WOULD trip, without pausing."""

    generated_at: datetime
    window_hours: int
    overall_status: str
    sending_enabled: bool
    would_pause: bool
    would_pause_reason: Optional[str] = None
    breaches: list[MetricStatus] = []
    warnings: list[MetricStatus] = []
    metrics: list[MetricStatus] = []
    thresholds: ThresholdConfig
