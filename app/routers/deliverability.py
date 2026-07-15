"""Deliverability API (feature 3.3).

Exposes per-account sender health, an on-demand reassessment trigger, and a
pre-send content spam-risk scanner.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.sender_health import SenderHealth
from app.services.deliverability_optimizer import get_deliverability_optimizer
from app.services import domain_auth
from app.services.bounce_handler import record_bounce

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/deliverability", tags=["deliverability"])


class ContentScanRequest(BaseModel):
    subject: str = ""
    body: str
    has_unsubscribe: bool = True
    use_ai: bool = False


class SenderHealthRead(BaseModel):
    account_id: str
    account_type: str
    account_email: Optional[str] = None
    sent_7d: int
    bounce_rate_7d: float
    complaint_rate_7d: float
    open_rate_7d: float
    reputation_score: int
    throttle_factor: float
    status: str
    last_assessed: datetime


@router.get("/senders", response_model=list[SenderHealthRead])
async def list_sender_health(db: AsyncSession = Depends(get_db)):
    """Current deliverability health for every assessed sending account."""
    rows = (
        await db.execute(select(SenderHealth).order_by(desc(SenderHealth.reputation_score)))
    ).scalars().all()
    return [
        SenderHealthRead(
            account_id=str(r.account_id),
            account_type=r.account_type,
            account_email=r.account_email,
            sent_7d=r.sent_7d,
            bounce_rate_7d=r.bounce_rate_7d,
            complaint_rate_7d=r.complaint_rate_7d,
            open_rate_7d=r.open_rate_7d,
            reputation_score=r.reputation_score,
            throttle_factor=r.throttle_factor,
            status=r.status,
            last_assessed=r.last_assessed,
        )
        for r in rows
    ]


@router.post("/assess")
async def assess_now(db: AsyncSession = Depends(get_db)):
    """Re-assess sender health across all accounts now."""
    results = await get_deliverability_optimizer().assess_all(db)
    await db.flush()
    return {"accounts_assessed": len(results), "results": results}


@router.post("/scan-content")
async def scan_content(payload: ContentScanRequest):
    """Scan an email draft for spam-filter risk before sending."""
    return await get_deliverability_optimizer().scan_content(
        payload.subject, payload.body, has_unsubscribe=payload.has_unsubscribe, use_ai=payload.use_ai
    )


@router.get("/domain-auth")
async def domain_auth_check(domain: str, dkim_selector: Optional[str] = None):
    """Check a sending domain's SPF, DKIM and DMARC DNS records.

    Pass a bare domain (or an email address). ``dkim_selector`` overrides the
    default selector probe list. Runs the (blocking) DNS lookups off the event
    loop; degrades to status "unknown" rather than erroring when DNS is
    unavailable.
    """
    if not (domain or "").strip():
        raise HTTPException(status_code=400, detail="domain is required")
    selectors = [dkim_selector] if dkim_selector else None
    return await asyncio.to_thread(domain_auth.check_domain_auth, domain, selectors)


class BounceReport(BaseModel):
    email: str
    status_code: Optional[str] = None
    diagnostic: str = ""
    kind: Optional[str] = None  # force 'hard'|'soft'; else classified


@router.post("/bounce")
async def report_bounce(payload: BounceReport, db: AsyncSession = Depends(get_db)):
    """Record a bounce (webhook or manual) and suppress the address if warranted.

    Hard bounces suppress the contact immediately; soft bounces are tracked and
    suppress after the configured threshold.
    """
    if not (payload.email or "").strip():
        raise HTTPException(status_code=400, detail="email is required")
    result = await record_bounce(
        db,
        payload.email,
        status_code=payload.status_code,
        diagnostic=payload.diagnostic,
        kind=payload.kind,
    )
    await db.commit()
    return result
