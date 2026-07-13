"""Interaction CRUD — log and manage client touchpoints (meetings, calls,
emails, notes)."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import UserScope, get_scope
from app.models.company import Company
from app.models.contact import Contact
from app.models.interaction import Interaction
from app.schemas.interaction import (
    InteractionCreate,
    InteractionRead,
    InteractionUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/interactions", tags=["interactions"])


async def _get_interaction_in_scope(
    db: AsyncSession, interaction_id: uuid.UUID, scope: UserScope
) -> Interaction:
    """Load an interaction, 404-ing if it's outside the caller's scope."""
    interaction = (
        await db.execute(select(Interaction).where(Interaction.id == interaction_id))
    ).scalar_one_or_none()
    if not interaction or not scope.can_access(interaction.user_id):
        raise HTTPException(status_code=404, detail="Interaction not found")
    return interaction


@router.post("", response_model=InteractionRead, status_code=201)
async def create_interaction(
    payload: InteractionCreate,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Log an interaction with a company (optionally a specific contact)."""
    company = (
        await db.execute(select(Company.id).where(Company.id == payload.company_id))
    ).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if payload.contact_id is not None:
        contact = (
            await db.execute(select(Contact.id).where(Contact.id == payload.contact_id))
        ).scalar_one_or_none()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")

    data = payload.model_dump()
    # Default occurred_at to now when not supplied.
    if data.get("occurred_at") is None:
        data["occurred_at"] = datetime.now(timezone.utc)
    interaction = Interaction(**data)
    interaction.user_id = scope.owner_id
    db.add(interaction)
    await db.flush()
    await db.refresh(interaction)
    logger.info("Logged %s interaction %s for company %s",
                payload.interaction_type, interaction.id, payload.company_id)

    # Feature 2.5: when a meeting is logged with notes, run post-meeting analysis
    # and attach it to the most recent open brief for the company. Best-effort —
    # never let it break interaction logging.
    if payload.interaction_type == "meeting":
        notes = interaction.content or interaction.notes
        if notes:
            try:
                await _run_post_meeting_analysis(db, interaction.company_id, notes)
            except Exception as e:  # noqa: BLE001
                logger.warning("Post-meeting analysis hook failed: %s", e)

    return interaction


async def _run_post_meeting_analysis(db: AsyncSession, company_id, notes: str) -> None:
    """Attach AI post-meeting analysis to the latest open brief for a company."""
    if company_id is None:
        return
    from app.models.meeting import MeetingBrief
    from app.services.meeting_intelligence import get_meeting_intelligence

    brief = (
        await db.execute(
            select(MeetingBrief)
            .where(MeetingBrief.company_id == company_id)
            .where(MeetingBrief.status != "completed")
            .order_by(MeetingBrief.generated_at.desc())
        )
    ).scalars().first()
    mi = get_meeting_intelligence()
    await mi.analyze_post_meeting(db, company_id=company_id, notes=notes, brief=brief)


@router.get("", response_model=list[InteractionRead])
async def list_interactions(
    company_id: Optional[uuid.UUID] = Query(None),
    contact_id: Optional[uuid.UUID] = Query(None),
    interaction_type: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """List interactions, most recent first, with optional filters.

    Scoped to the caller; admins/managers may pass ``view_as_user_id``.
    """
    query = scope.apply(select(Interaction), Interaction.user_id)
    conditions = []
    if company_id:
        conditions.append(Interaction.company_id == company_id)
    if contact_id:
        conditions.append(Interaction.contact_id == contact_id)
    if interaction_type:
        conditions.append(Interaction.interaction_type == interaction_type)
    if conditions:
        query = query.where(and_(*conditions))
    query = query.order_by(Interaction.occurred_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    return list((await db.execute(query)).scalars().all())


@router.get("/{interaction_id}", response_model=InteractionRead)
async def get_interaction(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Get a single interaction by ID (must be in the caller's scope)."""
    return await _get_interaction_in_scope(db, interaction_id, scope)


@router.patch("/{interaction_id}", response_model=InteractionRead)
async def update_interaction(
    interaction_id: uuid.UUID,
    payload: InteractionUpdate,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Update an interaction."""
    interaction = await _get_interaction_in_scope(db, interaction_id, scope)
    update_data = payload.model_dump(exclude_unset=True)
    if "contact_id" in update_data and update_data["contact_id"] is not None:
        contact = (
            await db.execute(select(Contact.id).where(Contact.id == update_data["contact_id"]))
        ).scalar_one_or_none()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")
    for field, value in update_data.items():
        setattr(interaction, field, value)
    await db.flush()
    await db.refresh(interaction)
    logger.info("Updated interaction %s", interaction_id)
    return interaction


@router.delete("/{interaction_id}", status_code=204)
async def delete_interaction(
    interaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Delete an interaction (must be in the caller's scope)."""
    interaction = await _get_interaction_in_scope(db, interaction_id, scope)
    await db.delete(interaction)
    await db.flush()
    logger.info("Deleted interaction %s", interaction_id)
