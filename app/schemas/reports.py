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
