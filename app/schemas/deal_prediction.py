"""Pydantic schemas for deal predictions and revenue forecasting."""

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

RISK_LEVELS = ("on_track", "watch", "at_risk", "stalling")


class DealPredictionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    deal_id: uuid.UUID
    close_probability: float
    model_probability: float
    ai_confidence: Optional[float] = None
    predicted_close_date: Optional[datetime] = None
    risk_level: str
    risk_flags: list[str] = []
    contributing_factors: dict[str, Any] = {}
    ai_summary: Optional[str] = None
    model_version: str
    predicted_at: datetime
    # Injected for convenience (not a column).
    deal_title: Optional[str] = None
    company_name: Optional[str] = None


class ForecastPeriod(BaseModel):
    """Weighted revenue expected to close within a calendar month."""

    period: str  # "2026-07"
    label: str  # "July 2026"
    deal_count: int
    pipeline_value: float  # raw sum of deal values closing this period
    weighted_value: float  # sum(value * probability)


class ForecastRiskDeal(BaseModel):
    deal_id: uuid.UUID
    deal_title: str
    company_name: Optional[str] = None
    value: float
    close_probability: float
    risk_level: str
    risk_flags: list[str] = []


class DealForecast(BaseModel):
    """Aggregate pipeline forecast weighted by AI-adjusted probabilities."""

    generated_at: datetime
    model_version: str
    active_deal_count: int
    total_pipeline_value: float
    weighted_forecast: float  # sum(value * probability/100) across active deals
    expected_close_count: float  # sum(probability/100)
    by_period: list[ForecastPeriod]
    at_risk_deals: list[ForecastRiskDeal]
    at_risk_value: float
