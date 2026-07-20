"""Company group management endpoints."""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.company_group import CompanyGroup, company_group_members
from app.models.company import Company

router = APIRouter(prefix="/groups", tags=["groups"])


def _parse_uuids(raw_ids: list[str]) -> list[uuid.UUID]:
    """Parse a list of UUID strings, skipping any that are malformed."""
    parsed = []
    for raw in raw_ids:
        try:
            parsed.append(uuid.UUID(str(raw)))
        except (ValueError, AttributeError, TypeError):
            continue
    return parsed


async def _insert_new_members(db: AsyncSession, group_id, company_ids) -> int:
    """Insert membership rows, skipping ones that already exist.

    Pre-checks existing members rather than catching duplicate-key errors:
    on async Postgres a failed INSERT aborts the whole transaction, so the
    old try/except-pass approach would break every subsequent insert.
    """
    existing = await db.execute(
        select(company_group_members.c.company_id).where(
            company_group_members.c.group_id == group_id
        )
    )
    have = {row[0] for row in existing.all()}
    inserted = 0
    for cid in company_ids:
        if cid not in have:
            await db.execute(
                company_group_members.insert().values(group_id=group_id, company_id=cid)
            )
            have.add(cid)
            inserted += 1
    return inserted


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    color: str = "#3b82f6"


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None


class GroupMembersUpdate(BaseModel):
    company_ids: list[str]


@router.get("")
async def list_groups(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List company groups with member counts, paginated (newest names first)."""
    result = await db.execute(
        select(CompanyGroup)
        .options(selectinload(CompanyGroup.companies))
        .order_by(CompanyGroup.name)
        .offset(offset)
        .limit(limit)
    )
    groups = result.scalars().all()
    return [
        {
            "id": str(g.id),
            "name": g.name,
            "description": g.description,
            "color": g.color,
            "created_at": g.created_at.isoformat() if g.created_at else None,
            "member_count": len(g.companies),
            "companies": [
                {"id": str(c.id), "name": c.name, "domain": c.domain, "industry": c.industry_segment, "stage": c.stage}
                for c in g.companies
            ],
        }
        for g in groups
    ]


@router.post("")
async def create_group(payload: GroupCreate, db: AsyncSession = Depends(get_db)):
    """Create a new company group."""
    group = CompanyGroup(name=payload.name, description=payload.description, color=payload.color)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return {"id": str(group.id), "name": group.name, "description": group.description, "color": group.color, "member_count": 0, "companies": []}


@router.patch("/{group_id}")
async def update_group(group_id: uuid.UUID, payload: GroupUpdate, db: AsyncSession = Depends(get_db)):
    """Update a group's name, description, or color."""
    result = await db.execute(select(CompanyGroup).where(CompanyGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if payload.name is not None:
        group.name = payload.name
    if payload.description is not None:
        group.description = payload.description
    if payload.color is not None:
        group.color = payload.color
    await db.commit()
    return {"id": str(group.id), "name": group.name, "description": group.description, "color": group.color}


@router.delete("/{group_id}")
async def delete_group(group_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Delete a group (does not delete the companies)."""
    result = await db.execute(select(CompanyGroup).where(CompanyGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    await db.delete(group)
    await db.commit()
    return {"status": "deleted"}


@router.post("/{group_id}/members")
async def add_members(group_id: uuid.UUID, payload: GroupMembersUpdate, db: AsyncSession = Depends(get_db)):
    """Add companies to a group."""
    result = await db.execute(
        select(CompanyGroup).options(selectinload(CompanyGroup.companies)).where(CompanyGroup.id == group_id)
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    existing_ids = {c.id for c in group.companies}
    added = 0
    for cid in _parse_uuids(payload.company_ids):
        if cid not in existing_ids:
            result = await db.execute(select(Company).where(Company.id == cid))
            company = result.scalar_one_or_none()
            if company:
                group.companies.append(company)
                existing_ids.add(cid)
                added += 1

    await db.commit()
    # group.companies already reflects the appended members.
    return {"added": added, "total_members": len(group.companies)}


@router.delete("/{group_id}/members")
async def remove_members(group_id: uuid.UUID, payload: GroupMembersUpdate, db: AsyncSession = Depends(get_db)):
    """Remove companies from a group."""
    company_ids = _parse_uuids(payload.company_ids)
    removed = 0
    for cid in company_ids:
        res = await db.execute(
            delete(company_group_members).where(
                company_group_members.c.group_id == group_id,
                company_group_members.c.company_id == cid,
            )
        )
        removed += res.rowcount or 0
    await db.commit()
    return {"removed": removed}


@router.post("/auto-group-by-industry")
async def auto_group_by_industry(db: AsyncSession = Depends(get_db)):
    """Automatically create groups based on industry segments."""
    # Get all unique industries
    result = await db.execute(
        select(Company.industry_segment, Company.id)
        .where(Company.industry_segment.isnot(None))
        .where(Company.industry_segment != "")
    )
    rows = result.all()
    
    # Group by industry
    industry_map = {}
    for industry, company_id in rows:
        industry_map.setdefault(industry, []).append(company_id)
    
    created = 0
    for industry, company_ids in industry_map.items():
        # Check if group already exists
        existing = await db.execute(
            select(CompanyGroup).where(CompanyGroup.name == industry)
        )
        group = existing.scalar_one_or_none()
        
        if not group:
            group = CompanyGroup(name=industry, description=f"Auto-grouped: {industry}", color="#3b82f6")
            db.add(group)
            await db.flush()
            created += 1
        
        # Add companies (skipping any already in the group)
        await _insert_new_members(db, group.id, company_ids)

    await db.commit()
    return {"groups_created": created, "industries_found": len(industry_map)}


@router.post("/auto-group-by-country")
async def auto_group_by_country(db: AsyncSession = Depends(get_db)):
    """Automatically create groups based on company country."""
    country_names = {"US": "United States", "CA": "Canada"}

    result = await db.execute(
        select(Company.country, Company.id)
        .where(Company.country.isnot(None))
        .where(Company.country != "")
    )
    rows = result.all()

    country_map = {}
    for country_code, company_id in rows:
        country_map.setdefault(country_code, []).append(company_id)

    created = 0
    for country_code, company_ids in country_map.items():
        group_name = country_names.get(country_code, country_code)
        existing = await db.execute(
            select(CompanyGroup).where(CompanyGroup.name == group_name)
        )
        group = existing.scalar_one_or_none()

        if not group:
            group = CompanyGroup(
                name=group_name,
                description=f"Companies in {group_name}",
                color="#059669" if country_code == "CA" else "#2563eb",
            )
            db.add(group)
            await db.flush()
            created += 1

        await _insert_new_members(db, group.id, company_ids)

    await db.commit()
    return {"groups_created": created, "countries_found": len(country_map)}


@router.post("/auto-group-by-stage")
async def auto_group_by_stage(db: AsyncSession = Depends(get_db)):
    """Automatically create groups based on pipeline stage (prospect category)."""
    stage_labels = {
        "new": "New Prospects",
        "researched": "Researched",
        "scored": "Scored Leads",
        "contacted": "Contacted",
        "engaged": "Engaged / Replies",
        "qualified": "Qualified",
        "proposal": "Proposal Sent",
        "won": "Won / Customers",
        "lost": "Lost / Closed",
    }
    stage_colors = {
        "new": "#6b7280",
        "researched": "#3b82f6",
        "scored": "#8b5cf6",
        "contacted": "#f59e0b",
        "engaged": "#10b981",
        "qualified": "#06b6d4",
        "proposal": "#ec4899",
        "won": "#22c55e",
        "lost": "#ef4444",
    }

    result = await db.execute(
        select(Company.stage, Company.id)
        .where(Company.stage.isnot(None))
        .where(Company.stage != "")
    )
    rows = result.all()

    stage_map = {}
    for stage, company_id in rows:
        stage_map.setdefault(stage, []).append(company_id)

    created = 0
    for stage, company_ids in stage_map.items():
        group_name = stage_labels.get(stage, stage.title())
        existing = await db.execute(
            select(CompanyGroup).where(CompanyGroup.name == group_name)
        )
        group = existing.scalar_one_or_none()

        if not group:
            group = CompanyGroup(
                name=group_name,
                description=f"Companies in '{stage}' pipeline stage",
                color=stage_colors.get(stage, "#3b82f6"),
            )
            db.add(group)
            await db.flush()
            created += 1

        await _insert_new_members(db, group.id, company_ids)

    await db.commit()
    return {"groups_created": created, "stages_found": len(stage_map)}
