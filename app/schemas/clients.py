"""Pydantic schemas for the client-management overview."""

import uuid
from typing import Optional

from pydantic import BaseModel

from app.schemas.task import TaskRead


class AtRiskClient(BaseModel):
    company_id: uuid.UUID
    company_name: str
    score: int
    days_since_last_contact: Optional[int] = None


class ClientOverview(BaseModel):
    client_count: int
    total_won_revenue: float
    won_deal_count: int
    # status -> count, e.g. {"healthy": 4, "at_risk": 2, "churned": 1}
    health_distribution: dict[str, int]
    # Mean health score across scored clients (None when there are no clients).
    avg_health_score: Optional[float] = None
    at_risk_clients: list[AtRiskClient]
    upcoming_tasks: list[TaskRead]
    upcoming_task_count: int
