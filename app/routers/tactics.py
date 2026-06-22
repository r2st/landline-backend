"""Sales-tactics (playbook) endpoints — AI-generated multi-channel outreach
assets for a single lead.

Backs the frontend "Tactics" tab: discover executives, draft LinkedIn / phone /
referral / event outreach, and build a multi-touch campaign plan. The drafting
endpoints don't touch the database; ``discover-executives`` creates
de-duplicated contacts via Apollo.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.schemas.tactics import (
    DiscoverExecutivesResponse,
    EventOutreachRequest,
    LinkedinDraftRequest,
    PhoneScriptRequest,
    ReferralDraftRequest,
)
from app.services.ai_service import get_ai_service
from app.services.enrichment import get_enrichment_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tactics", tags=["tactics"])


def _ai_or_502(result: dict) -> dict:
    """Surface a clean 502 when the AI provider errored, instead of leaking a
    half-empty payload to the frontend."""
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(
            status_code=502,
            detail=f"AI generation failed: {result['error']}",
        )
    return result


async def _get_company(db: AsyncSession, company_id: uuid.UUID) -> Company:
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.post("/discover-executives/{company_id}", response_model=DiscoverExecutivesResponse)
async def discover_executives(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Find decision-maker executives for a company via Apollo and create
    de-duplicated contacts for any new ones."""
    company = await _get_company(db, company_id)

    service = get_enrichment_service()
    executives = await service.discover_executives(db, company)

    message = None
    if not executives:
        if not company.domain:
            message = "Add a company domain to discover executives."
        else:
            message = "No new executives found from public sources."

    return DiscoverExecutivesResponse(
        company_id=str(company.id),
        company_name=company.name,
        executives=executives,
        executives_found=len(executives),
        contacts_created=len(executives),
        message=message,
    )


@router.post("/linkedin-draft")
async def linkedin_draft(payload: LinkedinDraftRequest):
    """Draft a LinkedIn connection note, follow-up, and post comment."""
    ai = get_ai_service()
    result = await ai.linkedin_messages(
        company_name=payload.company_name,
        contact_name=payload.contact_name,
        contact_title=payload.contact_title,
        company_industry=payload.company_industry,
        company_domain=payload.company_domain,
        topic=payload.topic,
    )
    return _ai_or_502(result)


@router.post("/phone-script")
async def phone_script(payload: PhoneScriptRequest):
    """Generate a cold-call script (opener, value prop, discovery, objections, close)."""
    ai = get_ai_service()
    result = await ai.phone_script(
        company_name=payload.company_name,
        contact_name=payload.contact_name,
        contact_title=payload.contact_title,
        company_industry=payload.company_industry,
        product=payload.product,
    )
    return _ai_or_502(result)


@router.post("/referral-draft")
async def referral_draft(payload: ReferralDraftRequest):
    """Draft a warm-intro request plus a forwardable blurb."""
    ai = get_ai_service()
    result = await ai.referral_request(
        company_name=payload.company_name,
        referrer_name=payload.referrer_name,
        target_name=payload.target_name,
        target_title=payload.target_title,
        company_industry=payload.company_industry,
    )
    return _ai_or_502(result)


@router.post("/event-outreach")
async def event_outreach(payload: EventOutreachRequest):
    """Draft a 3-touch event sequence (pre / at / post event)."""
    ai = get_ai_service()
    result = await ai.event_outreach(
        company_name=payload.company_name,
        contact_name=payload.contact_name,
        event_name=payload.event_name,
        event_type=payload.event_type,
        company_industry=payload.company_industry,
    )
    return _ai_or_502(result)


@router.post("/multi-touch-plan/{company_id}")
async def multi_touch_plan(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Build a sequenced multi-channel campaign plan for a company."""
    company = await _get_company(db, company_id)

    # Summarize known contacts so the plan can name real roles.
    contacts_result = await db.execute(
        select(Contact).where(Contact.company_id == company.id).limit(10)
    )
    contacts = list(contacts_result.scalars().all())
    contacts_summary = ", ".join(
        f"{c.full_name} ({c.title})" if c.title else c.full_name for c in contacts
    )

    # Latest fit score, if any, to calibrate the strategy.
    from app.models.lead_score import LeadScore
    score_result = await db.execute(
        select(LeadScore.fit_score)
        .where(LeadScore.company_id == company.id)
        .order_by(LeadScore.scored_at.desc())
        .limit(1)
    )
    fit_score = score_result.scalar_one_or_none()

    location = ", ".join(
        p for p in (company.city, company.state_province, company.country) if p
    )

    ai = get_ai_service()
    result = await ai.multi_touch_plan(
        company_name=company.name,
        company_industry=company.industry_segment or "",
        employee_count=company.employee_count,
        location=location,
        contacts_summary=contacts_summary,
        fit_score=fit_score,
    )
    return _ai_or_502(result)
