"""Pydantic schemas for tasks."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

VALID_PRIORITIES = ("low", "medium", "high", "urgent")
VALID_TASK_STATUSES = ("pending", "in_progress", "completed", "cancelled")
VALID_TASK_TYPES = ("follow_up", "meeting", "call", "email", "review")


def _check(value, allowed, label):
    if value is not None and value not in allowed:
        raise ValueError(f"{label} must be one of {allowed}")
    return value


class TaskCreate(BaseModel):
    company_id: uuid.UUID
    contact_id: Optional[uuid.UUID] = None
    title: str = Field(..., max_length=255)
    description: Optional[str] = None
    due_date: Optional[datetime] = None
    priority: str = "medium"
    status: str = "pending"
    assigned_to: Optional[str] = Field(None, max_length=255)
    task_type: str = "follow_up"

    @field_validator("priority")
    @classmethod
    def _vp(cls, v):
        return _check(v, VALID_PRIORITIES, "priority")

    @field_validator("status")
    @classmethod
    def _vs(cls, v):
        return _check(v, VALID_TASK_STATUSES, "status")

    @field_validator("task_type")
    @classmethod
    def _vt(cls, v):
        return _check(v, VALID_TASK_TYPES, "task_type")


class TaskUpdate(BaseModel):
    contact_id: Optional[uuid.UUID] = None
    title: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    due_date: Optional[datetime] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    assigned_to: Optional[str] = Field(None, max_length=255)
    task_type: Optional[str] = None

    @field_validator("priority")
    @classmethod
    def _vp(cls, v):
        return _check(v, VALID_PRIORITIES, "priority")

    @field_validator("status")
    @classmethod
    def _vs(cls, v):
        return _check(v, VALID_TASK_STATUSES, "status")

    @field_validator("task_type")
    @classmethod
    def _vt(cls, v):
        return _check(v, VALID_TASK_TYPES, "task_type")


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    contact_id: Optional[uuid.UUID] = None
    title: str
    description: Optional[str] = None
    due_date: Optional[datetime] = None
    priority: str
    status: str
    assigned_to: Optional[str] = None
    task_type: str
    created_at: datetime
    updated_at: datetime
