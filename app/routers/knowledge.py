"""Knowledge base API.

Surfaces — for the website — everything that used to live in CSV/markdown files
and in-code config:

- ``/knowledge/contacts``    — CRUD over the industry contacts database
- ``/knowledge/documents``   — CRUD over editable reference docs (markdown)
- ``/knowledge/guidelines``  — shortcut to the sales-psychology document
- ``/knowledge/strategy``    — shortcut to the contact-approach document
- ``/knowledge/industries``  — industry-vertical configs (pain language, sequences)
- ``/knowledge/ai-guidelines`` — the AI sales-engine guidelines (read-only)
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.knowledge import IndustryContact, KnowledgeDocument
from app.schemas.knowledge import (
    IndustryContactCreate,
    IndustryContactRead,
    IndustryContactUpdate,
    IndustryCount,
    IndustryVerticalRead,
    KnowledgeDocumentCreate,
    KnowledgeDocumentRead,
    KnowledgeDocumentSummary,
    KnowledgeDocumentUpdate,
)
from app.services import industry_verticals
from app.services.outreach import ai_sales_guidelines as ai_guidelines

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/knowledge", tags=["knowledge"])


# --- Industry contacts ---------------------------------------------------------

@router.get("/contacts", response_model=list[IndustryContactRead])
async def list_contacts(
    industry: Optional[str] = Query(None, description="Filter by exact industry"),
    company: Optional[str] = Query(None, description="Case-insensitive company name match"),
    search: Optional[str] = Query(None, description="Search name/title/email/company"),
    group: Optional[str] = Query(None, description="Filter by contact group: 'genuine' or 'test'"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """List industry contacts with optional filters, ordered by company."""
    query = select(IndustryContact)
    if industry:
        query = query.where(IndustryContact.industry == industry)
    if company:
        query = query.where(IndustryContact.company_name.ilike(f"%{company}%"))
    if group:
        query = query.where(IndustryContact.contact_group == group)
    if search:
        term = f"%{search}%"
        query = query.where(
            IndustryContact.contact_name.ilike(term)
            | IndustryContact.contact_title.ilike(term)
            | IndustryContact.contact_email.ilike(term)
            | IndustryContact.company_name.ilike(term)
        )
    query = query.order_by(IndustryContact.company_name, IndustryContact.contact_name)
    query = query.offset((page - 1) * page_size).limit(page_size)
    return list((await db.execute(query)).scalars().all())


@router.get("/contacts/industries", response_model=list[IndustryCount])
async def list_contact_industries(db: AsyncSession = Depends(get_db)):
    """Distinct industries with contact and company counts (for UI filters)."""
    query = (
        select(
            IndustryContact.industry,
            func.count(IndustryContact.id),
            func.count(func.distinct(IndustryContact.company_name)),
        )
        .group_by(IndustryContact.industry)
        .order_by(IndustryContact.industry)
    )
    rows = (await db.execute(query)).all()
    return [
        IndustryCount(industry=i, contact_count=c, company_count=co)
        for (i, c, co) in rows
    ]


@router.get("/contacts/{contact_id}", response_model=IndustryContactRead)
async def get_contact(contact_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get a single industry contact by ID."""
    contact = await db.get(IndustryContact, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


@router.post("/contacts", response_model=IndustryContactRead, status_code=201)
async def create_contact(
    payload: IndustryContactCreate, db: AsyncSession = Depends(get_db)
):
    """Add a new industry contact."""
    data = payload.model_dump()
    # Seeded leads are all real; default manually-added ones to 'genuine' too.
    data["contact_group"] = data.get("contact_group") or "genuine"
    contact = IndustryContact(**data)
    db.add(contact)
    await db.flush()
    await db.refresh(contact)
    logger.info("Created industry contact %s (%s)", contact.id, contact.company_name)
    return contact


@router.put("/contacts/{contact_id}", response_model=IndustryContactRead)
async def update_contact(
    contact_id: uuid.UUID,
    payload: IndustryContactUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update an industry contact (partial update — only provided fields change)."""
    contact = await db.get(IndustryContact, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(contact, field, value)
    await db.flush()
    await db.refresh(contact)
    logger.info("Updated industry contact %s", contact_id)
    return contact


@router.delete("/contacts/{contact_id}", status_code=204)
async def delete_contact(contact_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Delete an industry contact."""
    contact = await db.get(IndustryContact, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    await db.delete(contact)
    await db.flush()
    logger.info("Deleted industry contact %s", contact_id)


# --- Knowledge documents -------------------------------------------------------

@router.get("/documents", response_model=list[KnowledgeDocumentSummary])
async def list_documents(
    category: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List reference documents (metadata only, no bodies)."""
    query = select(KnowledgeDocument)
    if category:
        query = query.where(KnowledgeDocument.category == category)
    query = query.order_by(KnowledgeDocument.category, KnowledgeDocument.title)
    return list((await db.execute(query)).scalars().all())


@router.get("/documents/{key}", response_model=KnowledgeDocumentRead)
async def get_document(key: str, db: AsyncSession = Depends(get_db)):
    """Get a full document by its slug key."""
    doc = await _get_doc_by_key(db, key)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.post("/documents", response_model=KnowledgeDocumentRead, status_code=201)
async def create_document(
    payload: KnowledgeDocumentCreate, db: AsyncSession = Depends(get_db)
):
    """Create a new reference document."""
    if await _get_doc_by_key(db, payload.key):
        raise HTTPException(status_code=409, detail="Document key already exists")
    doc = KnowledgeDocument(**payload.model_dump())
    db.add(doc)
    await db.flush()
    await db.refresh(doc)
    logger.info("Created knowledge document %s", doc.key)
    return doc


@router.put("/documents/{key}", response_model=KnowledgeDocumentRead)
async def update_document(
    key: str, payload: KnowledgeDocumentUpdate, db: AsyncSession = Depends(get_db)
):
    """Edit a reference document (title/category/format/content)."""
    doc = await _get_doc_by_key(db, key)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(doc, field, value)
    await db.flush()
    await db.refresh(doc)
    logger.info("Updated knowledge document %s", key)
    return doc


@router.delete("/documents/{key}", status_code=204)
async def delete_document(key: str, db: AsyncSession = Depends(get_db)):
    """Delete a reference document."""
    doc = await _get_doc_by_key(db, key)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    await db.delete(doc)
    await db.flush()
    logger.info("Deleted knowledge document %s", key)


@router.get("/guidelines", response_model=KnowledgeDocumentRead)
async def get_guidelines(db: AsyncSession = Depends(get_db)):
    """Shortcut to the sales-psychology guidelines document."""
    doc = await _get_doc_by_key(db, "sales-psychology-guidelines")
    if not doc:
        raise HTTPException(status_code=404, detail="Guidelines not found")
    return doc


@router.get("/strategy", response_model=KnowledgeDocumentRead)
async def get_strategy(db: AsyncSession = Depends(get_db)):
    """Shortcut to the contact-approach strategy document."""
    doc = await _get_doc_by_key(db, "contact-approach-strategy")
    if not doc:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return doc


# --- Industry verticals + AI guidelines (read-only config) ---------------------

@router.get("/industries", response_model=list[IndustryVerticalRead])
async def list_industries():
    """Industry-vertical configs: pain language, demo links, and full sequences."""
    return [_serialize_vertical(v) for v in industry_verticals.all_verticals()]


@router.get("/industries/{key}", response_model=IndustryVerticalRead)
async def get_industry(key: str):
    """A single industry vertical config by its machine key."""
    vertical = industry_verticals.get_vertical(key)
    if not vertical:
        raise HTTPException(status_code=404, detail="Industry vertical not found")
    return _serialize_vertical(vertical)


@router.get("/ai-guidelines")
async def get_ai_guidelines():
    """The AI sales-engine guidelines: principles, formulas, cadence, profiles."""
    return {
        "outreach_principles": ai_guidelines.OUTREACH_PRINCIPLES,
        "email_structure": ai_guidelines.EMAIL_STRUCTURE,
        "subject_line_formulas": ai_guidelines.SUBJECT_LINE_FORMULAS,
        "follow_up_cadence": ai_guidelines.FOLLOW_UP_CADENCE,
        "anti_spam_rules": ai_guidelines.ANTI_SPAM_RULES,
        "personalization_tokens": ai_guidelines.PERSONALIZATION_TOKENS,
        "industry_profiles": ai_guidelines.INDUSTRY_PROFILES,
    }


# --- helpers -------------------------------------------------------------------

async def _get_doc_by_key(db: AsyncSession, key: str) -> Optional[KnowledgeDocument]:
    return (
        await db.execute(select(KnowledgeDocument).where(KnowledgeDocument.key == key))
    ).scalar_one_or_none()


def _serialize_vertical(v) -> IndustryVerticalRead:
    return IndustryVerticalRead(
        key=v.key,
        name=v.name,
        sequence_name=v.sequence_name,
        description=v.description,
        demo_link=v.demo_link,
        keywords=list(v.keywords),
        pain_points=list(v.pain_points),
        steps=[
            {
                "step_number": n,
                "delay_days": d,
                "subject_template": s,
                "body_template": b,
            }
            for (n, d, s, b) in v.steps
        ],
        subject_ab=v.subject_ab,
    )
