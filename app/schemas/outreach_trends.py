"""Read models for outreach performance over time.

The existing analytics answer "what is our reply rate". These answer "is it
getting better", which is a different question and the one a dashboard is for.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel


class TrendBucket(BaseModel):
    """One period of outreach, and how it compares with the period before it."""

    period_start: date
    period_end: date
    # The newest bucket has not finished. Its *rates* are comparable with a full
    # period; its *counts* are not, and a chart that plots them side by side
    # shows a volume collapse that did not happen.
    partial: bool

    sent: int
    opened: int
    clicked: int
    replied: int
    bounced: int

    open_rate: float
    click_rate: float
    reply_rate: float
    bounce_rate: float

    # Change against the preceding bucket, in percentage *points*. None on the
    # oldest bucket, which has nothing to be compared against. Points rather
    # than percent because a relative change is unreadable down here: 0.2% to
    # 0.3% is "up 50%" and also noise.
    reply_rate_change: Optional[float] = None
    open_rate_change: Optional[float] = None


class OutreachTrends(BaseModel):
    """A window of outreach performance, oldest bucket first."""

    interval: str
    days: int

    total_sent: int
    total_opened: int
    total_clicked: int
    total_replied: int
    total_bounced: int

    overall_open_rate: float
    overall_click_rate: float
    overall_reply_rate: float
    overall_bounce_rate: float

    # Second half of the window against the first, in percentage points, pooled
    # rather than averaged over buckets. None when there are too few complete
    # periods to say anything — which is honest, and better than a number
    # computed from two noisy endpoints.
    reply_rate_trend: Optional[float] = None
    # "improving" / "declining" / "flat" / "insufficient_data".
    direction: str

    buckets: list[TrendBucket] = []
