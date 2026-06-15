"""Pydantic schemas for the dashboard / metrics endpoints."""

from typing import Optional

from pydantic import BaseModel


class PipelineStageStat(BaseModel):
    stage: str
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
    pipeline: list[PipelineStageStat]
    outreach: OutreachMetrics
    score_distribution: list[ScoringDistribution]


class CSVImportResult(BaseModel):
    imported_companies: int
    imported_contacts: int
    skipped: int
    errors: list[str]
