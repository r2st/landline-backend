"""Schemas for per-sequence analytics (Feature 4)."""

import uuid

from pydantic import BaseModel


class SequenceStepAnalytics(BaseModel):
    """Per-step funnel counts and rates for one sequence step.

    Rates are fractions (0..1) of that step's ``sent`` count. A step with no
    sends reports 0 rates rather than dividing by zero.
    """

    step_number: int
    channel: str
    delay_days: int
    label: str | None = None  # a short subject/preview for the step

    sent: int
    opened: int
    clicked: int
    replied: int
    bounced: int

    open_rate: float
    click_rate: float
    reply_rate: float
    bounce_rate: float


class SequenceAnalytics(BaseModel):
    """Funnel + outcome analytics for a whole sequence."""

    sequence_id: uuid.UUID
    name: str

    total_enrollments: int
    active_enrollments: int
    completed_enrollments: int
    replied_enrollments: int
    bounced_enrollments: int

    # Enrolled → deal outcome.
    deals_attributed: int
    conversion_rate: float  # deals_attributed / total_enrollments

    steps: list[SequenceStepAnalytics]
