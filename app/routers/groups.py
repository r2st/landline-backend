"""Company group management endpoints."""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.company_group import CompanyGroup, company_group_members
from app.models.company import Company

router = APIRouter(prefix="/groups", tags=["groups"])


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
async def list_groups(db: AsyncSession = Depends(get_db)):
    """List all company groups with member counts."""
    result = await db.execute(
        select(CompanyGroup).options(selectinload(CompanyGroup.companies)).order_by(CompanyGroup.name)
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
async def update_group(group_id: str, payload: GroupUpdate, db: AsyncSession = Depends(get_db)):
    """Update a group's name, description, or color."""
    result = await db.execute(select(CompanyGroup).where(CompanyGroup.id == uuid.UUID(group_id)))
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
async def delete_group(group_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a group (does not delete the companies)."""
    result = await db.execute(select(CompanyGroup).where(CompanyGroup.id == uuid.UUID(group_id)))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    await db.delete(group)
    await db.commit()
    return {"status": "deleted"}


@router.post("/{group_id}/members")
async def add_members(group_id: str, payload: GroupMembersUpdate, db: AsyncSession = Depends(get_db)):
    """Add companies to a group."""
    result = await db.execute(
        select(CompanyGroup).options(selectinload(CompanyGroup.companies)).where(CompanyGroup.id == uuid.UUID(group_id))
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    
    existing_ids = {str(c.id) for c in group.companies}
    added = 0
    for cid in payload.company_ids:
        if cid not in existing_ids:
            result = await db.execute(select(Company).where(Company.id == uuid.UUID(cid)))
            company = result.scalar_one_or_none()
            if company:
                group.companies.append(company)
                added += 1
    
    await db.commit()
    return {"added": added, "total_members": len(group.companies) + added}


@router.delete("/{group_id}/members")
async def remove_members(group_id: str, payload: GroupMembersUpdate, db: AsyncSession = Depends(get_db)):
    """Remove companies from a group."""
    for cid in payload.company_ids:
        await db.execute(
            delete(company_group_members).where(
                company_group_members.c.group_id == uuid.UUID(group_id),
                company_group_members.c.company_id == uuid.UUID(cid),
            )
        )
    await db.commit()
    return {"removed": len(payload.company_ids)}


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
        
        # Add companies
        for cid in company_ids:
            try:
                await db.execute(
                    company_group_members.insert().values(group_id=group.id, company_id=cid)
                )
            except Exception:
                pass  # Already a member
    
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

        for cid in company_ids:
            try:
                await db.execute(
                    company_group_members.insert().values(group_id=group.id, company_id=cid)
                )
            except Exception:
                pass

    await db.commit()
    return {"groups_created": created, "countries_found": len(country_map)}
