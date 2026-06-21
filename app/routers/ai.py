"""AI-powered endpoints for intelligent email and lead decisions."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.services.ai_service import get_ai_service

router = APIRouter(prefix="/ai", tags=["ai"])


def _uuid_or_400(raw: str, field: str = "id") -> UUID:
    """Parse a UUID from request-body text, returning HTTP 400 on bad input."""
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid {field}: not a valid UUID")


class GenerateEmailRequest(BaseModel):
    company_id: Optional[str] = None
    contact_id: Optional[str] = None
    company_name: str = ""
    contact_name: str = ""
    contact_title: str = ""
    industry: str = ""
    purpose: str = Field("initial outreach", description="e.g., initial outreach, follow-up, introduction")
    tone: str = Field("professional but warm", description="e.g., formal, casual, professional but warm")
    additional_context: str = ""


class ImproveEmailRequest(BaseModel):
    subject: str
    body: str
    instruction: str = Field("", description="Specific improvement instructions")


class ScoreLeadRequest(BaseModel):
    company_id: str
    engagement_history: str = ""
    notes: str = ""


class GenerateFollowupRequest(BaseModel):
    original_subject: str
    original_body: str
    step_number: int = Field(ge=1, le=3)
    contact_name: str
    days_since_last: int = Field(ge=1)


class AnalyzeReplyRequest(BaseModel):
    reply_body: str
    original_subject: str = ""
    original_body: str = ""
    contact_name: str = ""
    company_name: str = ""


class SendDraftReplyRequest(BaseModel):
    to_email: str
    subject: str
    body: str
    send_id: Optional[str] = None


@router.post("/generate-email")
async def generate_email(
    payload: GenerateEmailRequest,
    db: AsyncSession = Depends(get_db),
):
    """Generate a personalized email using AI."""
    ai = get_ai_service()

    company_name = payload.company_name
    contact_name = payload.contact_name
    contact_title = payload.contact_title
    industry = payload.industry

    if payload.company_id:
        result = await db.execute(
            select(Company).where(Company.id == _uuid_or_400(payload.company_id, "company_id"))
        )
        company = result.scalar_one_or_none()
        if company:
            company_name = company_name or company.name
            industry = industry or company.industry_segment or ""

    if payload.contact_id:
        result = await db.execute(
            select(Contact).where(Contact.id == _uuid_or_400(payload.contact_id, "contact_id"))
        )
        contact = result.scalar_one_or_none()
        if contact:
            first = contact.first_name or ""
            last = contact.last_name or ""
            contact_name = contact_name or f"{first} {last}".strip()
            contact_title = contact_title or contact.title or ""

    if not company_name:
        raise HTTPException(status_code=400, detail="Company name is required (provide company_id or company_name)")

    result = await ai.generate_email(
        company_name=company_name,
        contact_name=contact_name,
        contact_title=contact_title,
        industry=industry,
        purpose=payload.purpose,
        tone=payload.tone,
        additional_context=payload.additional_context,
    )
    return result


@router.post("/improve-email")
async def improve_email(payload: ImproveEmailRequest):
    """Improve an existing email draft using AI."""
    ai = get_ai_service()
    return await ai.improve_email(
        subject=payload.subject,
        body=payload.body,
        instruction=payload.instruction,
    )


@router.post("/score-lead")
async def score_lead(
    payload: ScoreLeadRequest,
    db: AsyncSession = Depends(get_db),
):
    """Intelligently score a lead using AI."""
    result = await db.execute(
        select(Company).where(Company.id == _uuid_or_400(payload.company_id, "company_id"))
    )
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    city = company.city or ""
    state = company.state_province or ""
    country = company.country or ""
    location_parts = [p for p in (city, state, country) if p]
    location = ", ".join(location_parts)

    ai = get_ai_service()
    return await ai.score_lead(
        company_name=company.name,
        industry=company.industry_segment or "",
        employee_count=company.employee_count,
        location=location,
        engagement_history=payload.engagement_history,
        notes=payload.notes or company.notes or "",
    )


@router.post("/analyze-company/{company_id}")
async def analyze_company(
    company_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Analyze a company and get AI-powered outreach strategy."""
    result = await db.execute(
        select(Company).where(Company.id == company_id)
    )
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    contacts_result = await db.execute(
        select(Contact).where(Contact.company_id == company.id)
    )
    contacts = contacts_result.scalars().all()

    contact_list = []
    for c in contacts:
        first = c.first_name or ""
        last = c.last_name or ""
        name = f"{first} {last}".strip()
        contact_list.append({"name": name, "title": c.title, "email": c.email})

    company_data = {
        "name": company.name,
        "domain": company.domain,
        "industry": company.industry_segment,
        "employee_count": company.employee_count,
        "city": company.city,
        "state": company.state_province,
        "country": company.country,
        "website": company.website,
        "stage": company.stage,
        "contacts": contact_list,
        "notes": company.notes,
        "tags": company.tags,
    }

    ai = get_ai_service()
    return await ai.analyze_company(company_data)


@router.post("/generate-followup")
async def generate_followup(payload: GenerateFollowupRequest):
    """Generate an intelligent follow-up email."""
    ai = get_ai_service()
    result = await ai.generate_followup(
        original_subject=payload.original_subject,
        original_body=payload.original_body,
        step_number=payload.step_number,
        contact_name=payload.contact_name,
        days_since_last=payload.days_since_last,
    )
    return result


@router.post("/analyze-reply")
async def analyze_reply(payload: AnalyzeReplyRequest):
    """Analyze a reply and get AI-drafted response."""
    ai = get_ai_service()
    return await ai.analyze_reply(
        reply_body=payload.reply_body,
        original_subject=payload.original_subject,
        original_body=payload.original_body,
        contact_name=payload.contact_name,
        company_name=payload.company_name,
    )


@router.get("/pending-replies")
async def get_pending_replies(db: AsyncSession = Depends(get_db)):
    """Get replies that need human review (not auto-sent)."""
    result = await db.execute(
        select(EmailSend)
        .where(EmailSend.status == "replied")
        .where(EmailSend.replied_at.isnot(None))
        .order_by(EmailSend.replied_at.desc())
        .limit(20)
    )
    replied = result.scalars().all()

    items = []
    for r in replied:
        contact_name = ""
        company_name = ""
        if r.contact_id:
            cr = await db.execute(select(Contact).where(Contact.id == r.contact_id))
            contact = cr.scalar_one_or_none()
            if contact:
                first = contact.first_name or ""
                last = contact.last_name or ""
                contact_name = f"{first} {last}".strip()
                if contact.company_id:
                    cmr = await db.execute(select(Company).where(Company.id == contact.company_id))
                    company = cmr.scalar_one_or_none()
                    if company:
                        company_name = company.name

        items.append({
            "send_id": str(r.id),
            "to_email": r.to_email,
            "subject": r.subject,
            "sent_at": r.sent_at.isoformat() if r.sent_at else None,
            "replied_at": r.replied_at.isoformat() if r.replied_at else None,
            "contact_name": contact_name,
            "company_name": company_name,
        })

    return {"pending_replies": items}

@router.post("/scan-card")
async def scan_business_card(file: UploadFile = File(...)):
    """Extract contact info from a business card image."""
    import base64

    # Validate file type
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    # Read and encode
    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:  # 10MB limit
        raise HTTPException(status_code=413, detail="Image too large (max 10MB)")

    image_base64 = base64.b64encode(contents).decode("utf-8")

    # Pass the real media type through; Claude vision only accepts these, so
    # fall back to JPEG for anything else (e.g. HEIC) as a best effort.
    supported = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    media_type = file.content_type if file.content_type in supported else "image/jpeg"

    ai = get_ai_service()
    result = await ai.extract_business_card(image_base64, media_type=media_type)

    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    return result

