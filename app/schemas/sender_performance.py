"""Per-mailbox outreach performance — which sender is producing, not which is trusted.

Deliverability already reports per sender: ``/deliverability/senders`` gives
bounce rate, complaint rate, open rate, a reputation score and a throttle factor
for every assessed account. That answers "is this mailbox in trouble".

It does not answer "is this mailbox working". A mailbox can deliver perfectly
and never get a reply, and one that does is costing warm-up capacity the rest of
the fleet could be using. Reply rate is not a deliverability metric and does not
belong on ``SenderHealth`` — conflating "trusted" with "effective" is how a
green dashboard ends up describing a sender nobody answers.
"""

import uuid
from typing import Optional

from pydantic import BaseModel


class SenderPerformance(BaseModel):
    """One sending mailbox's funnel over the window.

    Rates are fractions (0..1) of that sender's ``sent`` count, matching the
    other Dialtone analytics surfaces.
    """

    account_id: Optional[uuid.UUID] = None
    account_email: Optional[str] = None
    # True for the row collecting sends whose enrollment has no pinned mailbox.
    # It exists rather than being dropped because those sends are real volume,
    # and a per-sender table whose columns don't add up to the total is the kind
    # of report people stop trusting.
    unattributed: bool = False

    sent: int = 0
    opened: int = 0
    clicked: int = 0
    replied: int = 0
    bounced: int = 0

    open_rate: float = 0.0
    click_rate: float = 0.0
    reply_rate: float = 0.0
    bounce_rate: float = 0.0


class SenderPerformanceReport(BaseModel):
    days: int
    total_sent: int
    senders: list[SenderPerformance] = []
