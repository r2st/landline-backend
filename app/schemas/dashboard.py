"""Pydantic schemas for the dashboard / metrics endpoints."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class PipelineStageStat(BaseModel):
    stage: str
    count: int
    value: float = 0


class IndustryStat(BaseModel):
    industry: str
    count: int


class OutreachMetrics(BaseModel):
    total_sent: int
    total_delivered: int
    total_opened: int
    total_clicked: int
    total_replied: int
    total_bounced: int
    open_rate: float
    click_rate: float
    reply_rate: float
    bounce_rate: float


class ScoringDistribution(BaseModel):
    bucket: str
    count: int


class DashboardOverview(BaseModel):
    total_companies: int
    total_contacts: int
    total_scored: int
    avg_fit_score: Optional[float] = None
    emails_sent: int = 0
    leads_this_week: int = 0
    leads_prev_week: int = 0
    tasks_due_today: int = 0
    active_deals_count: int = 0
    active_deals_value: float = 0
    contacts_this_week: int = 0
    contacts_prev_week: int = 0
    emails_sent_prev_week: int = 0
    deals_won_this_month: int = 0
    deals_won_value_this_month: float = 0
    emails_sent_today: int = 0
    emails_opened: int = 0
    pipeline: list[PipelineStageStat]
    outreach: OutreachMetrics
    score_distribution: list[ScoringDistribution]
    industry_breakdown: list[IndustryStat] = []


class ActivityItem(BaseModel):
    id: str
    type: str
    description: str
    company_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    company_name: Optional[str] = None
    created_at: datetime


class TopLead(BaseModel):
    id: uuid.UUID
    name: str
    stage: str
    score: Optional[int] = None
    industry: Optional[str] = None


class EmailTrendPoint(BaseModel):
    date: str
    sent: int


class DashboardKPIs(BaseModel):
    emails_sent_today: int = 0
    open_rate: float = 0
    replies_this_week: int = 0
    deals_won_this_month: int = 0
    deals_won_value: float = 0
    active_pipeline_value: float = 0
    avg_response_time_hours: Optional[float] = None


class CSVImportResult(BaseModel):
    imported_companies: int
    imported_contacts: int
    skipped: int
    errors: list[str]
