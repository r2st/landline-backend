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
    # Sales-book enhancements
    sales_framework: Optional[str] = None
    discovery_scorecard: Optional[dict] = None
    objections_encountered: Optional[list[str]] = None
    optimal_call_window: Optional[str] = None
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


# --------------------------------------------------------------------------- #
# AI-generated custom scripts
# --------------------------------------------------------------------------- #

# Call types accepted by the generate endpoint. Aliases map onto the canonical
# call_scripts ``script_type`` values.
CUSTOM_SCRIPT_CALL_TYPES = ("initial", "follow_up", "voicemail")

# Sales methodology frameworks from the research docs (15+ books distilled).
# Each framework structures the AI-generated script differently.
SALES_FRAMEWORKS = (
    "challenger",       # Challenger Sale 6-Step Commercial Teaching Pitch
    "gap_selling",      # Gap Selling Discovery Model (Keenan)
    "spin",             # SPIN Selling Question Flow (Rackham)
    "voss_tactical",    # Never Split the Difference Tactical Empathy (Voss)
    "blount_fanatical", # Fanatical Prospecting 5-Step (Blount)
    "weinberg_story",   # New Sales Simplified Power Statement (Weinberg)
    "sobczak_smart",    # Smart Calling Pre-Call Intelligence (Sobczak)
)


class CustomScriptRequest(BaseModel):
    contact_id: Optional[uuid.UUID] = None
    company_id: uuid.UUID
    call_type: str = "initial"
    # Optional: cache the generated script onto a specific open call task.
    call_task_id: Optional[uuid.UUID] = None
    # Force a fresh generation even if a cached script already exists.
    force: bool = False
    # Sales methodology framework — when None the AI picks the best fit.
    sales_framework: Optional[str] = None

    @field_validator("call_type")
    @classmethod
    def _vct(cls, v):
        return _check(v, CUSTOM_SCRIPT_CALL_TYPES, "call_type")

    @field_validator("sales_framework")
    @classmethod
    def _vsf(cls, v):
        return _check(v, SALES_FRAMEWORKS, "sales_framework") if v else v


class CustomScript(BaseModel):
    """The structured, personalised script the AI produces for one call."""

    opening_hook: str
    personalization_notes: Optional[str] = None
    talking_points: list[str] = Field(default_factory=list)
    qualifying_questions: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    objection_handling: list[dict] = Field(default_factory=list)
    call_to_action: Optional[str] = None
    voicemail: Optional[str] = None


class CustomScriptResponse(BaseModel):
    call_task_id: Optional[uuid.UUID] = None
    company_id: uuid.UUID
    company_name: Optional[str] = None
    contact_id: Optional[uuid.UUID] = None
    contact_name: Optional[str] = None
    contact_title: Optional[str] = None
    industry: str
    call_type: str
    # "ai" when Claude generated it, "template" when we fell back to the
    # canonical industry script (no API key, or generation failed/timed out).
    source: str
    ai_available: bool = True
    note: Optional[str] = None
    generated_at: Optional[datetime] = None
    # The sales framework used for script generation (None = auto-selected).
    sales_framework: Optional[str] = None
    script: CustomScript


# --------------------------------------------------------------------------- #
# Discovery call scorecard (Gap Selling + Challenger Sale best practices)
# --------------------------------------------------------------------------- #


class DiscoveryScorecard(BaseModel):
    """Seven-element scorecard from Gap Selling's discovery architecture.

    Reps self-assess each element 1-5 after a discovery call. The aggregate
    score predicts deal progression.
    """
    current_state_identified: int = Field(0, ge=0, le=5)
    root_cause_uncovered: int = Field(0, ge=0, le=5)
    impact_quantified: int = Field(0, ge=0, le=5)
    emotion_surfaced: int = Field(0, ge=0, le=5)
    future_state_defined: int = Field(0, ge=0, le=5)
    next_steps_agreed: int = Field(0, ge=0, le=5)
    champion_identified: int = Field(0, ge=0, le=5)

    @property
    def total_score(self) -> int:
        return (
            self.current_state_identified + self.root_cause_uncovered
            + self.impact_quantified + self.emotion_surfaced
            + self.future_state_defined + self.next_steps_agreed
            + self.champion_identified
        )


class CallTaskUpdateEnhanced(CallTaskUpdate):
    """Extended update payload that includes discovery scorecard and objection
    tracking from sales-book best practices."""
    discovery_scorecard: Optional[DiscoveryScorecard] = None
    objections_encountered: Optional[list[str]] = None
    sales_framework_used: Optional[str] = None

    @field_validator("sales_framework_used")
    @classmethod
    def _vsfu(cls, v):
        return _check(v, SALES_FRAMEWORKS, "sales_framework_used") if v else v
