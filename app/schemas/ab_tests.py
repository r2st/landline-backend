"""Pydantic schemas for the A/B Testing surface (Feature 3).

Mirrors the ``app/schemas/dialtone.py`` style — plain typed BaseModels. Shapes
the persistence (:mod:`app.models.ab_test`) and the engine's evaluation output
(:mod:`app.services.ab_test_resolver`) into the ``/dialtone/ab-tests`` contract.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class VariantInput(BaseModel):
    """A variant to create or edit. ``None`` templates inherit the step."""

    label: Optional[str] = Field(None, description="A | B | C | D (auto-assigned if omitted)")
    subject_template: Optional[str] = None
    body_template: Optional[str] = None
    is_control: bool = False
    is_active: bool = True


class AbTestCreate(BaseModel):
    """Create an A/B test for a sequence step with its variants."""

    sequence_step_id: str
    strategy: str = "even"                 # 'even' | 'bandit'
    metric: str = "replied"                # 'replied' | 'opened' | 'clicked'
    min_sample: Optional[int] = None       # defaults from calculate_required_sample_size
    confidence_target: float = 0.95
    variants: list[VariantInput] = Field(default_factory=list)


class VariantsUpdate(BaseModel):
    """Add or edit variants on an existing test (upsert by label)."""

    variants: list[VariantInput] = Field(default_factory=list)


class DeclareWinnerRequest(BaseModel):
    """Manual winner override — by variant id or label."""

    variant_id: Optional[str] = None
    label: Optional[str] = None


class VariantStats(BaseModel):
    """Per-variant sends/engagement + rates (computed on demand)."""

    variant_id: str
    label: str
    is_control: bool
    is_active: bool
    subject_template: Optional[str] = None
    body_template: Optional[str] = None
    assigned: int = 0
    sent: int = 0
    opened: int = 0
    clicked: int = 0
    replied: int = 0
    open_rate: float = 0.0
    click_rate: float = 0.0
    reply_rate: float = 0.0


class AbTestSummary(BaseModel):
    """List-row view of a test."""

    id: str
    sequence_step_id: str
    sequence_id: Optional[str] = None
    step_number: Optional[int] = None
    status: str
    strategy: str
    metric: str
    min_sample: int
    confidence_target: float
    winner_variant_id: Optional[str] = None
    winner_label: Optional[str] = None
    declared_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    variant_count: int = 0
    total_sent: int = 0
    leader_label: Optional[str] = None
    confidence: Optional[float] = None


class AbTestDetail(AbTestSummary):
    """Full test detail with per-variant stats + engine evaluation."""

    variants: list[VariantStats] = Field(default_factory=list)
    is_significant: bool = False
    default_winner: bool = False
    sample_progress: float = 0.0  # min(variant sent) / min_sample, capped at 1.0


class AbTestList(BaseModel):
    items: list[AbTestSummary]
    total: int
