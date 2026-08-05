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
    delay_hours: int = 0
    delay_business_days: bool = False
    # The step's full cadence in words ("2 business days and 4 hours"), from
    # ``services.step_delay.describe``. ``delay_days`` alone stopped describing
    # the wait once steps could be configured in hours and business days, and a
    # step-performance table that mislabels the gap between touches is reporting
    # against the wrong variable.
    cadence: str = "immediately"
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

    # --- Manual (non-email) touches ---
    #
    # A call/LinkedIn step sends nothing, so it scores 0 on every rate above and
    # would read as a dead step. These are its real numbers: how many tasks were
    # filed, how many a human actually did, and how many expired unworked while
    # the cadence moved on without them.
    #
    # ``task_completion_rate`` is the one that matters. A multi-channel cadence
    # is only multi-channel to the extent the humans work it, and a sequence
    # whose call steps complete at 10% is an email sequence with expensive
    # pauses in it — which is not visible anywhere else.
    tasks_created: int = 0
    tasks_completed: int = 0
    tasks_expired: int = 0
    task_completion_rate: float = 0.0


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
    # Steps put on the wire, which is not the same number as ``sent`` and is the
    # honest denominator for ``bounce_rate``. A **synchronous** failure writes a
    # ``bounced`` event and no ``sent`` one, so it is an attempt that a count of
    # ``sent`` rows cannot see — and a campaign where every send failed that way
    # has ``sent == 0``, a bounce rate that divides by zero, and a reported
    # ``0.0`` that reads as the healthiest campaign on the board. See
    # ``outreach_events.AttemptTally``. ``sent`` keeps its meaning — messages
    # actually accepted — and stays the denominator of the engagement rates.
    #
    # Required rather than defaulted: a construction site that forgets it would
    # report ``attempts=0``, and ``rate()`` returns 0.0 on a zero denominator —
    # so the omission would come back as a perfectly healthy bounce rate, which
    # is the exact failure this field exists to remove.
    attempts: int
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

    # --- Ranking honesty ----------------------------------------------------
    # The lower bound of the 95% Wilson score interval on the reply rate, as a
    # fraction like every other rate here. Sorting a campaign table by the
    # *observed* rate reliably puts
    # the smallest campaign on top: 1 reply in 12 sends is 8.3%, which beats
    # every mature campaign in the table and is indistinguishable from luck. The
    # lower bound is the rate we can defend, so ranking by it puts a campaign at
    # the top only once it has earned the position.
    reply_rate_ci_low: float = 0.0
    # Whether this campaign has enough sends for its rates to be worth reading
    # at all. False is not a criticism of the campaign — only of the number.
    sample_sufficient: bool = True

    # Populated only when the caller asked for a period-over-period comparison.
    trend: "CampaignTrend | None" = None


class CampaignTrend(BaseModel):
    """One campaign's current period against the period immediately before it.

    Rate deltas are **absolute differences between the two fractions** (0.020 →
    0.030 is ``+0.010``), matching how every other rate in this response is
    expressed, and volume deltas are plain counts. Deliberately not a relative
    change: "reply rate up 50%" is the most reliably misread number on a
    dashboard, and it is unbounded besides — any improvement off a zero base is
    infinite.
    """

    # The comparison window, same length as the current one, ending where it
    # begins. Both are echoed so a reader can see what "previous" meant.
    previous_start: datetime
    previous_end: datetime

    previous_sent: int
    previous_replied: int
    previous_reply_rate: float
    previous_bounce_rate: float

    sent_delta: int
    reply_rate_delta: float
    bounce_rate_delta: float

    # 'improving' | 'declining' | 'flat' | 'insufficient_data'
    #
    # ``insufficient_data`` when either period is too small to compare, which is
    # the common case and the one a naive dashboard gets loudly wrong: a campaign
    # that sent 4 last week and 6 this week has not "improved 50%".
    direction: str
    # True when the campaign sent in this period but not the previous one — a
    # new campaign, not a recovering one. Without this a first period of activity
    # reads as infinite growth.
    newly_active: bool = False


# ``CampaignAnalytics.trend`` forward-references this, which is defined below it
# so the reading order matches the nesting. Resolve it explicitly rather than
# relying on Pydantic's lazy first-use rebuild — a model that only fails to
# build when something first validates against it fails at request time.
CampaignAnalytics.model_rebuild()


class CampaignAnalyticsResponse(BaseModel):
    """Every campaign, plus the totals across them."""

    timeframe_days: int | None = None
    campaigns: list[CampaignAnalytics]
    totals: CampaignAnalytics | None = None
    # Set when ``compare`` was requested: campaigns whose reply rate moved
    # materially, already filtered for sample size. The point of the field is
    # that it is usually short — a dashboard's job is to name the two campaigns
    # that changed, not to hand back thirty rows and a scroll bar.
    improving: list[str] = []
    declining: list[str] = []


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
