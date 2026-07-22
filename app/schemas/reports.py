"""Pydantic schemas for client-management reports."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ReportPeriod(BaseModel):
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None


class ClientRetentionReport(BaseModel):
    period: ReportPeriod
    total_clients: int           # current companies in 'client' stage
    total_churned: int           # current companies in 'churned' stage
    retention_rate: float        # clients / (clients + churned) * 100
    won_deals_in_period: int
    won_revenue_in_period: float
    lost_deals_in_period: int
    lost_value_in_period: float


class StatusValue(BaseModel):
    status: str
    count: int
    total_value: float


class RevenuePipelineReport(BaseModel):
    period: ReportPeriod
    total_deals: int
    won_revenue: float
    pipeline_value: float        # sum of pending deal values
    lost_value: float
    average_won_deal_size: float
    by_status: list[StatusValue]


class ActivitySummaryReport(BaseModel):
    period: ReportPeriod
    emails_sent: int
    replies: int
    response_rate: float         # replies / emails_sent
    interactions_total: int
    interactions_by_type: dict[str, int]
    meetings: int
    tasks_created: int
    tasks_completed: int


# ── Outreach funnel analytics (OutreachEvent-based) ──────────────────────────
# Computed from the sequence send/engagement log (``outreach_events``), NOT from
# ``email_sends`` — the cold-outreach path records events, not EmailSend rows.


class FunnelCounts(BaseModel):
    """One funnel row: the four cold-outreach outcomes plus derived rates.

    ``sent`` is the denominator; rates are share-of-sent. ``opened`` counts only
    tracked sends implicitly (an untracked send can never register an open), so
    a step whose first touch drops tracking shows a low open rate by design.
    """

    sent: int = 0
    opened: int = 0
    replied: int = 0
    bounced: int = 0
    open_rate: float = 0.0
    reply_rate: float = 0.0
    bounce_rate: float = 0.0


class StepFunnel(BaseModel):
    step_number: int
    counts: FunnelCounts


class SequenceFunnel(BaseModel):
    sequence_id: Optional[str] = None
    sequence_name: Optional[str] = None
    counts: FunnelCounts


class SubjectFunnel(BaseModel):
    subject: str
    # The A/B variant label (A-D) when the send came through an A/B test, else
    # None. Lets the same subject be attributed to its variant in the breakdown.
    variant_label: Optional[str] = None
    counts: FunnelCounts


class OutreachFunnelReport(BaseModel):
    period: ReportPeriod
    overall: FunnelCounts
    by_step: list[StepFunnel]
    by_sequence: list[SequenceFunnel]
    by_subject: list[SubjectFunnel]
