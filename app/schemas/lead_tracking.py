"""Pydantic schemas for the lead-generation / Dialtone tracking API
(``GET /leads/stats``, ``GET /leads/recent``, ``GET /dialtone/status``).

These endpoints exist so an external system (or Claude) can poll Landline's
lead-gen performance without a human in the loop. They reuse the Dialtone
Console's counting conventions (``DialtoneTodayCounts`` / ``DialtoneFunnelRates``)
so a number here always means the same thing it means on the console.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.schemas.dialtone import DialtoneFunnelRates, DialtoneSending, DialtoneTodayCounts, DialtoneWarmup


class LeadOrgBreakdown(BaseModel):
    """One organization's slice of a stats window.

    For a platform_admin caller, ``by_org`` on the parent response carries one
    of these per tenant with any activity in the window. For a regular org
    user, the global org read filter (see app.models.mixins) already scopes
    every underlying query to their own org, so this is just their own totals
    restated — never another tenant's.
    """

    org_id: Optional[str] = None
    org_name: Optional[str] = None
    counts: DialtoneTodayCounts
    rates: DialtoneFunnelRates
    leads_qualified: int = 0
    deals_created: int = 0
    deals_won: int = 0
    conversion_rate: float = 0.0


class LeadStatsResponse(BaseModel):
    """``GET /leads/stats`` — overall lead-gen performance for a window."""

    generated_at: datetime
    timeframe: str
    timeframe_label: str
    range_start: Optional[str] = None
    range_end: Optional[str] = None
    counts: DialtoneTodayCounts
    rates: DialtoneFunnelRates
    leads_qualified: int = 0
    deals_created: int = 0
    deals_won: int = 0
    # deals_won / emails_sent — the coarsest possible "did this outreach pay
    # off" number; slice by org via by_org for anything finer.
    conversion_rate: float = 0.0
    by_org: list[LeadOrgBreakdown] = []


class RecentLeadEvent(BaseModel):
    """One entry in the recent-lead-activity feed."""

    id: str
    type: str
    timestamp: datetime
    org_id: Optional[str] = None
    org_name: Optional[str] = None
    contact_name: Optional[str] = None
    to_email: Optional[str] = None
    company_name: Optional[str] = None
    subject: Optional[str] = None
    description: Optional[str] = None
    severity: str = "ok"


class RecentLeadActivity(BaseModel):
    """``GET /leads/recent`` — paginated, newest-first lead activity."""

    items: list[RecentLeadEvent]
    total: int
    page: int
    page_size: int


class DialtoneStatus(BaseModel):
    """``GET /dialtone/status`` — the autonomous agent's health + performance."""

    generated_at: datetime
    timeframe: str
    timeframe_label: str
    range_start: Optional[str] = None
    range_end: Optional[str] = None
    sending: DialtoneSending
    warmup: DialtoneWarmup
    counts: DialtoneTodayCounts
    rates: DialtoneFunnelRates
    active_senders: int = 0
    healthy_senders: int = 0
    by_org: list[LeadOrgBreakdown] = []
