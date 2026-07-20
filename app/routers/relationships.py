"""Relationship intelligence & org-chart endpoints (feature 2.3).

Exposed under ``/companies/{id}/...`` per the feature spec:

- ``GET  /companies/{id}/stakeholder-map``   — the buying-committee map + org chart.
- ``POST /companies/{id}/multi-thread-plan``  — coverage-gap analysis + recommendations.

Both build on ``relationship_intelligence`` and degrade to heuristics with no AI key.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.relationship import OrgRelationship, StakeholderMap
from app.services.relationship_intelligence import get_relationship_intelligence

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/companies", tags=["relationships"])


async def _require_company(db: AsyncSession, company_id: uuid.UUID) -> Company:
    company = (
        await db.execute(select(Company).where(Company.id == company_id))
    ).scalar_one_or_none()
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.get("/{company_id}/stakeholder-map")
async def get_stakeholder_map(
    company_id: uuid.UUID,
    rebuild: bool = Query(False, description="Recompute the map and org edges from scratch."),
    use_ai: bool = Query(True, description="Use the LLM to refine role classification."),
    limit: int = Query(200, ge=1, le=1000, description="Max stakeholders/edges returned"),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Return the stakeholder map + inferred org chart for a company.

    When ``rebuild`` is true (or no map exists yet) the map and reporting edges
    are (re)computed and persisted; otherwise the stored rows are returned.
    ``limit``/``offset`` bound how many stakeholders and org edges are returned.
    """
    await _require_company(db, company_id)
    ri = get_relationship_intelligence()

    existing = (
        await db.execute(
            select(StakeholderMap)
            .where(StakeholderMap.company_id == company_id)
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()

    if rebuild or not existing:
        stakeholders = await ri.build_stakeholder_map(db, company_id, persist=True, use_ai=use_ai)
        org_edges = await ri.infer_org_relationships(db, company_id, persist=True)
        # Bound the freshly-computed lists the same way as the stored path.
        stakeholders = stakeholders[offset:offset + limit]
        org_edges = org_edges[offset:offset + limit]
    else:
        stakeholders = [
            {
                "contact_id": str(s.contact_id),
                "inferred_role": s.inferred_role,
                "relationship_strength": s.relationship_strength,
                "engagement_score": s.engagement_score,
                "engagement_trend": s.engagement_trend,
                "last_interaction": s.last_interaction.isoformat() if s.last_interaction else None,
                "rationale": s.rationale,
            }
            for s in existing
        ]
        edges = (
            await db.execute(
                select(OrgRelationship)
                .where(OrgRelationship.company_id == company_id)
                .offset(offset)
                .limit(limit)
            )
        ).scalars().all()
        org_edges = [
            {
                "contact_id": str(e.contact_id),
                "related_contact_id": str(e.related_contact_id),
                "relationship_type": e.relationship_type,
                "confidence": e.confidence,
                "source": e.source,
            }
            for e in edges
        ]

    return {
        "company_id": str(company_id),
        "stakeholders": stakeholders,
        "org_relationships": org_edges,
    }


@router.post("/{company_id}/multi-thread-plan")
async def create_multi_thread_plan(
    company_id: uuid.UUID,
    use_ai: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    """Analyze committee coverage and return prioritized multi-threading recommendations."""
    await _require_company(db, company_id)
    ri = get_relationship_intelligence()
    try:
        return await ri.multi_thread_plan(db, company_id, use_ai=use_ai)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
