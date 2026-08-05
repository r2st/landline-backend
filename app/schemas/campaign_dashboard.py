"""Schemas for the campaign dashboard — the operator's landing view.

The campaign *table* (``/dialtone/campaigns/analytics``) answers "what are all
the numbers". This answers the question someone actually opens a dashboard with:
**which two or three campaigns should I do something about this morning, and
what?** Those are different jobs, and a table cannot do the second one — thirty
rows sorted by reply rate is a spreadsheet, not a decision.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class DashboardWindow(BaseModel):
    """The period being reported, and the one it is compared against.

    Both are echoed rather than implied by ``days`` so a reader can tell what
    "previous" meant without reconstructing it — and so a screenshot of the
    dashboard is still interpretable a week later.
    """

    days: int
    since: datetime
    until: datetime
    previous_start: datetime
    previous_end: datetime


class PortfolioSummary(BaseModel):
    """Every campaign's sends in the window, pooled, and where they are heading.

    Pooled rather than averaged across campaigns: a mean of per-campaign reply
    rates lets a 6-send campaign count as much as a 600-send one, which reliably
    reports a portfolio doing better or worse than any mail actually did.

    Deal attribution is **lifetime, not windowed** — hence the field name. A deal
    closes weeks after the send that sourced it, so a 30-day deal count next to
    30-day send counts would describe a conversion rate that never existed. It is
    reported because "did this make money" is the only question that finally
    matters, and hidden behind an explicit name because it does not share the
    window with everything above it.
    """

    campaigns: int            # campaigns that sent in the window
    active_campaigns: int     # campaigns currently switched on

    sent: int
    # Steps put on the wire, the denominator ``bounce_rate`` divides by. See
    # ``CampaignAnalytics.attempts``: a synchronous failure writes ``bounced``
    # and no ``sent``, so the two numbers differ exactly where it matters.
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

    # The reply rate the whole portfolio can defend, on the same 95% Wilson
    # interval the campaign rows use. At portfolio volume this is usually close
    # to the observed rate; when it is not, the portfolio is smaller than it
    # looks.
    reply_rate_ci_low: float
    sample_sufficient: bool

    # What share of the window's sends were AI-composed rather than static
    # mail-merge. A portfolio reply rate that moved is uninterpretable without
    # it: the provider chain going dark changes this number and nothing else.
    ai_composed_rate: float
    avg_personalization_depth: float

    active_enrollments: int
    deals_attributed_lifetime: int

    # --- Movement against the previous equal-length window -------------------
    previous_sent: int
    previous_reply_rate: float
    reply_rate_delta: float
    # 'improving' | 'declining' | 'flat' | 'insufficient_data'
    direction: str


class CampaignRank(BaseModel):
    """One campaign's place on a dashboard list, with the reason it is there.

    ``reason`` is prose on purpose. A rank without a reason invites the reader to
    infer one from whichever column is nearest, and the nearest column is almost
    never the one that put the campaign on the list.
    """

    sequence_id: uuid.UUID
    name: str

    sent: int
    replied: int
    reply_rate: float
    # The 95% interval on that reply rate. The pair is what makes a one-line
    # ranking honest: 0 replies in 20 sends has a lower bound of 0 and an upper
    # bound near 16%, which is not a failing campaign — it is a campaign that has
    # not sent enough mail to have failed at anything yet.
    reply_rate_ci_low: float
    reply_rate_ci_high: float
    bounce_rate: float

    # From the period-over-period comparison, when there was enough volume in
    # both periods to have one.
    direction: str | None = None
    reason: str


class DashboardAlert(BaseModel):
    """Something to act on, in the order it should be acted on.

    Every alert carries the number that raised it and the threshold it crossed.
    An alert that says only "bounce rate high" makes the reader open the table to
    find out how high, which is the work the alert existed to save.
    """

    # 'critical' — reputation or revenue is being burned right now.
    # 'warning'  — heading that way, or already off the rails but cheap.
    # 'info'     — worth knowing, nothing on fire.
    severity: str
    kind: str  # stable slug: 'bounce_rate' | 'stalled' | 'no_replies' | ...

    sequence_id: uuid.UUID | None = None
    campaign: str | None = None

    message: str
    metric: str | None = None
    value: float | None = None
    threshold: float | None = None


class CampaignDashboard(BaseModel):
    """One call, one screen.

    Assembling this from the campaign table, the bounce summary, the trend
    series and the enrollment queue took five round trips and left every
    consumer to re-derive "which campaign is worst" for itself — differently
    each time.
    """

    window: DashboardWindow
    portfolio: PortfolioSummary

    # Ranked by the reply rate each campaign can *defend*, so a campaign reaches
    # the top only once it has earned the position rather than once it has been
    # lucky twice.
    leaderboard: list[CampaignRank] = []
    # Campaigns whose reply rate is defensibly *below* the portfolio — the whole
    # 95% interval sits under it, so this is "worse", not "unlucky so far".
    laggards: list[CampaignRank] = []
    # Sending, but not yet enough to rank in either direction. Listed rather than
    # dropped: a campaign invisible on every list looks switched off.
    needs_volume: list[CampaignRank] = []

    alerts: list[DashboardAlert] = []

    # Campaigns with sends in the window, whether or not they made a list.
    campaign_count: int
