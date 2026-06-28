"""Pydantic schemas for the cold-call system."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.call import (
    CALL_OUTCOMES,
    CALL_PRIORITIES,
    CALL_SCRIPT_INDUSTRIES,
    CALL_SCRIPT_TYPES,
    CALL_STATUSES,
)


def _check(value, allowed, label):
    if value is not None and value not in allowed:
        raise ValueError(f"{label} must be one of {allowed}")
    return value


# --------------------------------------------------------------------------- #
# Call tasks
# --------------------------------------------------------------------------- #


class CallTaskCreate(BaseModel):
    company_id: uuid.UUID
    contact_id: Optional[uuid.UUID] = None
    assigned_to: Optional[str] = Field(None, max_length=255)
    priority: str = "warm"
    status: str = "pending"
    scheduled_at: Optional[datetime] = None
    notes: Optional[str] = None

    @field_validator("priority")
    @classmethod
    def _vp(cls, v):
        return _check(v, CALL_PRIORITIES, "priority")

    @field_validator("status")
    @classmethod
    def _vs(cls, v):
        return _check(v, CALL_STATUSES, "status")


class CallTaskUpdate(BaseModel):
    contact_id: Optional[uuid.UUID] = None
    assigned_to: Optional[str] = Field(None, max_length=255)
    priority: Optional[str] = None
    status: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    outcome: Optional[str] = None
    notes: Optional[str] = None
    call_duration_seconds: Optional[int] = Field(None, ge=0)

    @field_validator("priority")
    @classmethod
    def _vp(cls, v):
        return _check(v, CALL_PRIORITIES, "priority")

    @field_validator("status")
    @classmethod
    def _vs(cls, v):
        return _check(v, CALL_STATUSES, "status")

    @field_validator("outcome")
    @classmethod
    def _vo(cls, v):
        return _check(v, CALL_OUTCOMES, "outcome")


class CallTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    contact_id: Optional[uuid.UUID] = None
    assigned_to: Optional[str] = None
    status: str
    priority: str
    scheduled_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    outcome: Optional[str] = None
    notes: Optional[str] = None
    call_duration_seconds: Optional[int] = None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- #
# Call scripts
# --------------------------------------------------------------------------- #


class ObjectionRebuttal(BaseModel):
    objection: str
    rebuttal: str


class CallScriptUpsert(BaseModel):
    industry: str
    script_type: str = "initial_cold_call"
    title: str = Field(..., max_length=255)
    opening_hook: str
    qualifying_questions: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    value_proposition: Optional[str] = None
    objection_handling: list[ObjectionRebuttal] = Field(default_factory=list)
    call_to_action: Optional[str] = None

    @field_validator("industry")
    @classmethod
    def _vi(cls, v):
        return _check(v, CALL_SCRIPT_INDUSTRIES, "industry")

    @field_validator("script_type")
    @classmethod
    def _vt(cls, v):
        return _check(v, CALL_SCRIPT_TYPES, "script_type")


class CallScriptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[uuid.UUID] = None
    industry: str
    script_type: str
    title: str
    opening_hook: str
    qualifying_questions: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    value_proposition: Optional[str] = None
    objection_handling: list[dict] = Field(default_factory=list)
    call_to_action: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #


class CallNotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    call_task_id: Optional[uuid.UUID] = None
    notification_type: str
    message: str
    sent_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
    created_at: datetime


# --------------------------------------------------------------------------- #
# Queue generation + briefing
# --------------------------------------------------------------------------- #


class QueueGenerateRequest(BaseModel):
    assigned_to: Optional[str] = Field(None, max_length=255)
    limit: int = Field(20, ge=1, le=100)
    stages: Optional[list[str]] = None  # pipeline stages to draw from


class QueueGenerateResponse(BaseModel):
    generated: int
    hot: int
    warm: int
    cold: int
    skipped_existing: int
    tasks: list[CallTaskRead]


class EngagementSummary(BaseModel):
    emails_sent: int = 0
    opens: int = 0
    clicks: int = 0
    replies: int = 0
    last_contact_at: Optional[datetime] = None
    days_since_last_contact: Optional[int] = None


class CallBriefing(BaseModel):
    call_task_id: Optional[uuid.UUID] = None
    company_id: uuid.UUID
    company_name: str
    contact_id: Optional[uuid.UUID] = None
    contact_name: Optional[str] = None
    contact_title: Optional[str] = None
    phone: Optional[str] = None
    industry: str
    priority: Optional[str] = None
    engagement: EngagementSummary
    pain_points: list[str] = Field(default_factory=list)
    talking_points: list[str] = Field(default_factory=list)
    recent_interactions: list[dict] = Field(default_factory=list)
    script: Optional[CallScriptRead] = None
