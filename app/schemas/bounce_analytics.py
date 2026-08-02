"""Read models for bounce forensics and per-recipient-domain deliverability.

The shape here is deliberately split three ways, because three different
questions get asked when a bounce rate moves and one response cannot serve them:
*how bad, since when* (``BounceSummary``), *who is refusing us*
(``RecipientDomainReport``), and *which addresses exactly* (``BounceListResponse``).
"""

import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel


class BounceDayPoint(BaseModel):
    """One day of the bounce trend, split by classification."""

    day: date
    hard: int
    soft: int
    total: int


class BounceBySender(BaseModel):
    """Bounces attributed to the mailbox the DSN came back to.

    ``account_id``/``email`` are nullable: a bounce ingested before the account
    was linked, or from a mailbox since deleted, still counts and is reported
    against an unnamed sender rather than dropped.
    """

    account_id: Optional[uuid.UUID] = None
    email: Optional[str] = None
    bounces: int


class DiagnosticTally(BaseModel):
    """A DSN refusal reason and how often it was seen (first line only)."""

    diagnostic: str
    count: int


class BounceSummary(BaseModel):
    """How much bounced, of what kind, from where, and whether it is growing."""

    timeframe_days: int
    total: int
    hard: int
    soft: int
    # Distinct addresses, not notifications — a mailbox that soft-bounces every
    # day for a week is one bad address, not seven.
    distinct_addresses: int
    # Sequence-engine sends in the same window: the rate's denominator.
    sent: int
    bounce_rate: float
    hard_bounce_rate: float
    # Addresses this window's bounces put on the do-not-contact list. Should
    # track the hard-bounce address count; a large shortfall means addresses are
    # bouncing without leaving rotation.
    auto_suppressed: int
    daily: list[BounceDayPoint] = []
    by_sender: list[BounceBySender] = []
    top_diagnostics: list[DiagnosticTally] = []


class RecipientDomainStats(BaseModel):
    """Deliverability into one receiving domain."""

    domain: str
    contacts: int
    sent: int
    opened: int
    clicked: int
    replied: int
    bounced: int
    unsubscribed: int
    # From the bounce ledger, which is the only place the DSN classification is
    # kept. Covers every send path, so it can exceed ``bounced`` (sequence-engine
    # events only) for a domain we also mail manually.
    hard_bounces: int
    soft_bounces: int
    open_rate: float
    reply_rate: float
    bounce_rate: float
    # False when the domain has too few sends for its rates to mean anything.
    # ``risk`` is then ``insufficient_data`` — never a verdict.
    sufficient_sample: bool
    # 'healthy' | 'at_risk' | 'blocked' | 'insufficient_data'
    risk: str


class RecipientDomainReport(BaseModel):
    """Every receiving domain we mailed, riskiest first."""

    timeframe_days: int
    min_sends: int
    total_sent: int
    domains_seen: int
    # Volume aimed at a domain that is rejecting it — the number worth alarming
    # on, since a blocked domain matters in proportion to the budget spent on it.
    sent_to_flagged_domains: int
    flagged: list[str] = []
    domains: list[RecipientDomainStats] = []


class BounceRecordRead(BaseModel):
    """One ingested bounce, with the contact and sender resolved."""

    id: uuid.UUID
    recipient_email: str
    domain: Optional[str] = None
    bounce_kind: str
    status_code: Optional[str] = None
    diagnostic: Optional[str] = None
    processed_at: datetime
    sender_email: Optional[str] = None
    contact_id: Optional[uuid.UUID] = None
    contact_name: Optional[str] = None
    # Out of rotation by either gate — the per-contact flag or the address-level
    # suppression list. A hard bounce reading ``False`` is the actionable case.
    suppressed: bool = False
    result: dict = {}


class BounceListResponse(BaseModel):
    """A page of the bounce ledger, newest first."""

    total: int
    limit: int
    offset: int
    bounces: list[BounceRecordRead] = []
