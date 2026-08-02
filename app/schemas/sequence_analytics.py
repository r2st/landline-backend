"""Schemas for per-sequence and cross-campaign analytics (Feature 4)."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class SequenceStepAnalytics(BaseModel):
    """Per-step funnel counts and rates for one sequence step.

    Rates are fractions (0..1) of that step's ``sent`` count. A step with no
    sends reports 0 rates rather than dividing by zero.
    """

    step_number: int
    channel: str
    delay_days: int
    label: str | None = None  # a short subject/preview for the step

    sent: int
    opened: int
    clicked: int
    replied: int
    bounced: int

    open_rate: float
    click_rate: float
    reply_rate: float
    bounce_rate: float


class SequenceAnalytics(BaseModel):
    """Funnel + outcome analytics for a whole sequence."""

    sequence_id: uuid.UUID
    name: str

    total_enrollments: int
    active_enrollments: int
    completed_enrollments: int
    replied_enrollments: int
    bounced_enrollments: int

    # Enrolled → deal outcome.
    deals_attributed: int
    conversion_rate: float  # deals_attributed / total_enrollments

    steps: list[SequenceStepAnalytics]


class CampaignAnalytics(BaseModel):
    """One campaign's headline numbers, for comparing campaigns side by side.

    Deliberately flat and free of per-step detail: this row exists to answer
    "which campaign is working?", and the drill-down into *why* is what
    ``SequenceAnalytics`` is for.
    """

    sequence_id: uuid.UUID
    name: str
    industry: str | None = None
    tone: str | None = None
    status: str
    is_active: bool

    total_enrollments: int
    active_enrollments: int

    sent: int
    opened: int
    clicked: int
    replied: int
    bounced: int
    unsubscribed: int

    open_rate: float
    click_rate: float
    reply_rate: float
    bounce_rate: float
    unsubscribe_rate: float

    # Enrolled → deal, the only rate that pays for anything.
    deals_attributed: int
    conversion_rate: float

    # What share of this campaign's sends were AI-composed rather than the static
    # template, and the mean number of prospect data points behind them. A
    # campaign whose reply rate moved is only interpretable next to these:
    # "personalization works" and "the provider chain was down all week" produce
    # very different numbers here and identical send counts.
    ai_composed: int
    ai_composed_rate: float
    avg_personalization_depth: float

    last_send_at: datetime | None = None


class CampaignAnalyticsResponse(BaseModel):
    """Every campaign, plus the totals across them."""

    timeframe_days: int | None = None
    campaigns: list[CampaignAnalytics]
    totals: CampaignAnalytics | None = None


class RankedLead(BaseModel):
    """A prospect ranked by recency-weighted engagement."""

    contact_id: uuid.UUID
    contact_name: str
    email: str
    title: str | None = None
    company_id: uuid.UUID | None = None
    company_name: str = ""

    sent: int
    opened: int
    clicked: int
    replied: int
    bounced: int

    score: float          # 0-100
    tier: str             # hot | warm | cool | cold
    last_engaged_at: datetime | None = None
    active_enrollments: int
    suppressed: bool
    reasons: list[str]


class RankedLeadsResponse(BaseModel):
    """The ranked list plus its tier histogram."""

    leads: list[RankedLead]
    tier_counts: dict[str, int]
    total_ranked: int
