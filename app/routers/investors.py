"""Investor-outreach API — fundraising CRM for Apprend Technologies.

Mirrors the lead-gen side of the platform, pointed at capital providers:

* ``/investors``                       — CRUD + search/filter over investors
* ``/investors/pipeline``              — fundraising funnel stats by status
* ``/investors/{id}/timeline``         — an investor's full interaction history
* ``/investors/{id}/interactions``     — CRUD over logged touches
* ``/investors/{id}/outreach``         — AI-generated personalised outreach email
* ``/investors/documents``             — CRUD over fundraising documents (deck,
                                         model, term sheet, …); ``investor_id`` is
                                         optional so a doc can be a general
                                         data-room copy.

Investors are soft-deleted (``deleted_at``) so history and analytics survive.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.investor import (
    INVESTOR_STATUSES,
    Investor,
    InvestorDocument,
    InvestorInteraction,
)
from app.schemas.investor import (
    InvestorCreate,
    InvestorDocumentCreate,
    InvestorDocumentRead,
    InvestorDocumentUpdate,
    InvestorInteractionCreate,
    InvestorInteractionRead,
    InvestorInteractionUpdate,
    InvestorOutreachRequest,
    InvestorOutreachResponse,
    InvestorPipeline,
    InvestorPipelineStage,
    InvestorRead,
    InvestorTimeline,
    InvestorTimelineEvent,
    InvestorUpdate,
)
from app.services import ai_investor_outreach

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/investors", tags=["investors"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _get_active_investor(db: AsyncSession, investor_id: uuid.UUID) -> Investor:
    investor = (
        await db.execute(
            select(Investor).where(
                Investor.id == investor_id, Investor.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if not investor:
        raise HTTPException(status_code=404, detail="Investor not found")
    return investor


# --------------------------------------------------------------------------- #
# Investors — list / pipeline (static routes before /{investor_id})
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[InvestorRead])
async def list_investors(
    status: Optional[str] = Query(None),
    stage: Optional[str] = Query(None),
    focus: Optional[str] = Query(None, description="Filter by a focus sector (exact, case-insensitive)"),
    search: Optional[str] = Query(None, description="Search name/firm/email/location/notes"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List investors (excludes soft-deleted), most recently updated first.

    ``focus`` matches a single sector inside the investor's ``investment_focus``
    list; it's applied in Python so it stays portable across Postgres/SQLite.
    """
    conditions = [Investor.deleted_at.is_(None)]
    if status:
        conditions.append(Investor.status == status)
    if stage:
        conditions.append(Investor.investment_stage == stage)
    if search:
        term = f"%{search}%"
        conditions.append(
            Investor.name.ilike(term)
            | Investor.firm.ilike(term)
            | Investor.email.ilike(term)
            | Investor.location.ilike(term)
            | Investor.notes.ilike(term)
        )

    query = select(Investor).where(and_(*conditions)).order_by(
        Investor.updated_at.desc(), Investor.created_at.desc()
    )

    if focus:
        # JSONB containment isn't portable to the SQLite test DB — filter in
        # Python, then paginate the filtered set.
        rows = list((await db.execute(query)).scalars().all())
        needle = focus.strip().lower()
        rows = [r for r in rows if needle in [f.lower() for f in (r.investment_focus or [])]]
        start = (page - 1) * page_size
        return rows[start : start + page_size]

    query = query.offset((page - 1) * page_size).limit(page_size)
    return list((await db.execute(query)).scalars().all())


@router.get("/pipeline", response_model=InvestorPipeline)
async def investor_pipeline(db: AsyncSession = Depends(get_db)):
    """Fundraising funnel: count + potential cheque total (paise) per status.

    ``potential_paise`` sums ``typical_check_size_max`` across live investors in
    each stage — a rough ceiling on capital in that part of the funnel. Every
    canonical status is returned (0 when empty) so the funnel renders in order.
    """
    query = (
        select(
            Investor.status,
            func.count(Investor.id),
            func.coalesce(func.sum(Investor.typical_check_size_max), 0),
        )
        .where(Investor.deleted_at.is_(None))
        .group_by(Investor.status)
    )
    rows = {s: (c, p) for (s, c, p) in (await db.execute(query)).all()}

    stages = [
        InvestorPipelineStage(
            status=s,
            count=rows.get(s, (0, 0))[0],
            potential_paise=int(rows.get(s, (0, 0))[1] or 0),
        )
        for s in INVESTOR_STATUSES
    ]
    return InvestorPipeline(total=sum(st.count for st in stages), stages=stages)


# --------------------------------------------------------------------------- #
# Documents (static routes — declared before /{investor_id})
# --------------------------------------------------------------------------- #


@router.get("/documents", response_model=list[InvestorDocumentRead])
async def list_documents(
    investor_id: Optional[uuid.UUID] = Query(None),
    type: Optional[str] = Query(None),
    general_only: bool = Query(False, description="Only investor-agnostic (unassigned) docs"),
    db: AsyncSession = Depends(get_db),
):
    """List fundraising documents, newest first. Filter by investor and/or type."""
    query = select(InvestorDocument)
    if investor_id:
        query = query.where(InvestorDocument.investor_id == investor_id)
    if general_only:
        query = query.where(InvestorDocument.investor_id.is_(None))
    if type:
        query = query.where(InvestorDocument.type == type)
    query = query.order_by(InvestorDocument.created_at.desc())
    return list((await db.execute(query)).scalars().all())


@router.post("/documents", response_model=InvestorDocumentRead, status_code=201)
async def create_document(
    payload: InvestorDocumentCreate, db: AsyncSession = Depends(get_db)
):
    """Create a fundraising document (attached to an investor, or general)."""
    if payload.investor_id is not None:
        await _get_active_investor(db, payload.investor_id)
    doc = InvestorDocument(**payload.model_dump())
    db.add(doc)
    await db.flush()
    await db.refresh(doc)
    logger.info("Created investor document %s (%s)", doc.id, doc.type)
    return doc


@router.get("/documents/{document_id}", response_model=InvestorDocumentRead)
async def get_document(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get a single document by ID."""
    doc = await db.get(InvestorDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.put("/documents/{document_id}", response_model=InvestorDocumentRead)
async def update_document(
    document_id: uuid.UUID,
    payload: InvestorDocumentUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a document (partial — only provided fields change)."""
    doc = await db.get(InvestorDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    data = payload.model_dump(exclude_unset=True)
    if data.get("investor_id") is not None:
        await _get_active_investor(db, data["investor_id"])
    for field, value in data.items():
        setattr(doc, field, value)
    await db.flush()
    await db.refresh(doc)
    logger.info("Updated investor document %s", document_id)
    return doc


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Hard-delete a document."""
    doc = await db.get(InvestorDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    await db.delete(doc)
    await db.flush()
    logger.info("Deleted investor document %s", document_id)


# --------------------------------------------------------------------------- #
# Interactions — update/delete by id (static prefix before /{investor_id})
# --------------------------------------------------------------------------- #


@router.put("/interactions/{interaction_id}", response_model=InvestorInteractionRead)
async def update_interaction(
    interaction_id: uuid.UUID,
    payload: InvestorInteractionUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a logged interaction (partial update)."""
    interaction = await db.get(InvestorInteraction, interaction_id)
    if not interaction:
        raise HTTPException(status_code=404, detail="Interaction not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(interaction, field, value)
    await db.flush()
    await db.refresh(interaction)
    logger.info("Updated investor interaction %s", interaction_id)
    return interaction


@router.delete("/interactions/{interaction_id}", status_code=204)
async def delete_interaction(
    interaction_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    """Delete a logged interaction."""
    interaction = await db.get(InvestorInteraction, interaction_id)
    if not interaction:
        raise HTTPException(status_code=404, detail="Interaction not found")
    await db.delete(interaction)
    await db.flush()
    logger.info("Deleted investor interaction %s", interaction_id)


# --------------------------------------------------------------------------- #
# Investors — create / get / update / delete
# --------------------------------------------------------------------------- #


@router.post("", response_model=InvestorRead, status_code=201)
async def create_investor(payload: InvestorCreate, db: AsyncSession = Depends(get_db)):
    """Add a new investor to the fundraising pipeline."""
    investor = Investor(**payload.model_dump())
    db.add(investor)
    await db.flush()
    await db.refresh(investor)
    logger.info("Created investor %s (%s)", investor.id, investor.name)
    return investor


@router.get("/{investor_id}", response_model=InvestorRead)
async def get_investor(investor_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get a single investor by ID."""
    return await _get_active_investor(db, investor_id)


@router.put("/{investor_id}", response_model=InvestorRead)
async def update_investor(
    investor_id: uuid.UUID,
    payload: InvestorUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update an investor (partial — only provided fields change)."""
    investor = await _get_active_investor(db, investor_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(investor, field, value)
    await db.flush()
    await db.refresh(investor)
    logger.info("Updated investor %s", investor_id)
    return investor


@router.delete("/{investor_id}", status_code=204)
async def delete_investor(investor_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Soft-delete an investor (sets ``deleted_at``; history is preserved)."""
    investor = await _get_active_investor(db, investor_id)
    investor.deleted_at = datetime.now(timezone.utc)
    await db.flush()
    logger.info("Soft-deleted investor %s", investor_id)


# --------------------------------------------------------------------------- #
# Interactions — list / create under an investor
# --------------------------------------------------------------------------- #


@router.get("/{investor_id}/interactions", response_model=list[InvestorInteractionRead])
async def list_interactions(
    investor_id: uuid.UUID,
    type: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List an investor's logged interactions, most recent first."""
    await _get_active_investor(db, investor_id)
    query = select(InvestorInteraction).where(
        InvestorInteraction.investor_id == investor_id
    )
    if type:
        query = query.where(InvestorInteraction.type == type)
    if direction:
        query = query.where(InvestorInteraction.direction == direction)
    query = query.order_by(InvestorInteraction.interaction_date.desc())
    return list((await db.execute(query)).scalars().all())


@router.post(
    "/{investor_id}/interactions",
    response_model=InvestorInteractionRead,
    status_code=201,
)
async def create_interaction(
    investor_id: uuid.UUID,
    payload: InvestorInteractionCreate,
    db: AsyncSession = Depends(get_db),
):
    """Log an interaction with an investor.

    Also stamps the investor's ``last_contacted_at`` when the touch is outbound.
    """
    investor = await _get_active_investor(db, investor_id)
    data = payload.model_dump(exclude_unset=True)
    interaction = InvestorInteraction(investor_id=investor_id, **data)
    db.add(interaction)

    when = data.get("interaction_date") or datetime.now(timezone.utc)
    if payload.direction == "outbound":
        current = investor.last_contacted_at
        if current is not None and current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if current is None or when > current:
            investor.last_contacted_at = when

    await db.flush()
    await db.refresh(interaction)
    logger.info("Logged %s interaction for investor %s", interaction.type, investor_id)
    return interaction


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #


@router.get("/{investor_id}/timeline", response_model=InvestorTimeline)
async def investor_timeline(investor_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Full interaction history for an investor, most recent first."""
    investor = await _get_active_investor(db, investor_id)
    rows = (
        await db.execute(
            select(InvestorInteraction)
            .where(InvestorInteraction.investor_id == investor_id)
            .order_by(InvestorInteraction.interaction_date.desc())
        )
    ).scalars().all()
    events = [
        InvestorTimelineEvent(
            id=r.id,
            type=r.type,
            direction=r.direction,
            subject=r.subject,
            content=r.content,
            outcome=r.outcome,
            next_steps=r.next_steps,
            interaction_date=r.interaction_date,
        )
        for r in rows
    ]
    return InvestorTimeline(investor=InvestorRead.model_validate(investor), events=events)


# --------------------------------------------------------------------------- #
# AI outreach
# --------------------------------------------------------------------------- #


@router.post("/{investor_id}/outreach", response_model=InvestorOutreachResponse)
async def generate_investor_outreach(
    investor_id: uuid.UUID,
    payload: InvestorOutreachRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Generate a personalised outreach email for an investor.

    Tailors the pitch to the investor's thesis (focus sectors, stage, cheque
    size) and Apprend's company context. Falls back to a template draft when AI
    is unavailable. With ``log_interaction=true`` the draft is saved as an
    outbound email interaction and ``last_contacted_at`` is stamped.
    """
    payload = payload or InvestorOutreachRequest()
    investor = await _get_active_investor(db, investor_id)

    draft = await ai_investor_outreach.generate_outreach(
        investor, custom_instructions=payload.custom_instructions
    )

    interaction_id = None
    if payload.log_interaction:
        interaction = InvestorInteraction(
            investor_id=investor.id,
            type="email",
            direction="outbound",
            subject=draft["subject"],
            content=draft["body"],
            outcome=f"AI outreach draft ({draft['source']})",
            interaction_date=draft["generated_at"],
        )
        db.add(interaction)
        investor.last_contacted_at = draft["generated_at"]
        await db.flush()
        await db.refresh(interaction)
        interaction_id = interaction.id

    return InvestorOutreachResponse(
        investor_id=investor.id,
        investor_name=investor.name,
        firm=investor.firm,
        subject=draft["subject"],
        body=draft["body"],
        source=draft["source"],
        ai_available=draft["ai_available"],
        note=draft.get("note"),
        interaction_id=interaction_id,
        generated_at=draft["generated_at"],
    )
