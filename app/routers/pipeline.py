"""Pipeline (kanban deal-board) endpoints."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.schemas.company import (
    VALID_PIPELINE_STAGES,
    PipelineStageCount,
    PipelineSummary,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.get("/summary", response_model=PipelineSummary)
async def pipeline_summary(db: AsyncSession = Depends(get_db)):
    """Counts of companies per kanban pipeline stage, for the deal-board view.

    Every valid stage is always present (zero-filled) so the board can render a
    stable set of columns regardless of which stages currently have cards.
    """
    result = await db.execute(
        select(Company.pipeline_stage, func.count(Company.id)).group_by(
            Company.pipeline_stage
        )
    )
    counts = {stage: count for stage, count in result.all()}

    stages = [
        PipelineStageCount(stage=stage, count=counts.get(stage, 0))
        for stage in VALID_PIPELINE_STAGES
    ]
    total = sum(c.count for c in stages)
    return PipelineSummary(stages=stages, total=total)
