"""Test-send + deliverability verification API.

Backs the "Send Test Email" and "Test Deliverability" UI. A test send goes out
through the exact same compliant pipeline as real outreach (unsubscribe footer,
List-Unsubscribe headers, plain-text alternative) so it verifies the real thing,
then reports the sending domain's SPF/DKIM/DMARC posture so an operator can
confirm authentication passes before scaling.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.contact import Contact
from app.services import domain_auth
from app.services.email_dispatch import resolve_sender_info, send_outreach_email
from app.services.email_validation import validate_email

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/email", tags=["email-test"])


class TestSendRequest(BaseModel):
    to_email: EmailStr = Field(..., description="Where to send the test email")
    subject: Optional[str] = Field(None, max_length=200)
    body: Optional[str] = Field(None, description="HTML/plain body; a default is used if omitted")


class DomainAuthSummary(BaseModel):
    domain: Optional[str] = None
    overall: Optional[str] = None
    spf: Optional[dict] = None
    dkim: Optional[dict] = None
    dmarc: Optional[dict] = None
    recommendations: Optional[list] = None


class TestSendResponse(BaseModel):
    sent: bool
    status: str
    reason: Optional[str] = None
    transport: Optional[str] = None
    sender: Optional[str] = None
    to_email: str
    domain_auth: Optional[DomainAuthSummary] = None


_DEFAULT_BODY = (
    "<p>This is a Landline deliverability test.</p>"
    "<p>If you received it, sending is configured correctly. This message "
    "includes the same compliance footer and unsubscribe header as real outreach.</p>"
)


@router.post("/test-send", response_model=TestSendResponse)
async def test_send(payload: TestSendRequest, db: AsyncSession = Depends(get_db)):
    """Send a fully-compliant test email through the configured sender.

    Honours the global sending kill switch (a test send is still a send). Always
    returns the sending domain's SPF/DKIM/DMARC posture, even when the send is
    blocked, so the operator can diagnose configuration either way.
    """
    to_email = str(payload.to_email)
    sender = await resolve_sender_info(db)
    from_domain = sender.from_email.split("@", 1)[-1] if sender.from_email else None

    # Deliverability posture of the sending domain (DNS only — safe to run
    # regardless of whether the send itself proceeds).
    auth = None
    if from_domain:
        auth = await asyncio.to_thread(domain_auth.check_domain_auth, from_domain)

    subject = payload.subject or "Landline deliverability test"
    body = payload.body or _DEFAULT_BODY

    send_result = await send_outreach_email(
        db,
        to_email=to_email,
        subject=subject,
        body_html=body,
        include_unsubscribe=True,
    )
    await db.commit()

    status = send_result.get("status", "error")
    return TestSendResponse(
        sent=status == "sent",
        status=status,
        reason=send_result.get("reason") or send_result.get("detail"),
        transport=send_result.get("transport") or sender.transport,
        sender=send_result.get("from_email") or sender.from_email,
        to_email=to_email,
        domain_auth=DomainAuthSummary(**auth) if auth else None,
    )


@router.get("/deliverability", response_model=DomainAuthSummary)
async def deliverability(db: AsyncSession = Depends(get_db)):
    """SPF/DKIM/DMARC posture for the domain outreach currently sends from."""
    sender = await resolve_sender_info(db)
    from_domain = sender.from_email.split("@", 1)[-1] if sender.from_email else None
    if not from_domain:
        return DomainAuthSummary(overall="unknown", recommendations=["No sender configured"])
    auth = await asyncio.to_thread(domain_auth.check_domain_auth, from_domain)
    return DomainAuthSummary(**auth)


class ValidateEmailRequest(BaseModel):
    email: str


class ValidateEmailResponse(BaseModel):
    email: str
    status: str  # 'valid' | 'invalid' | 'unknown'


@router.post("/validate", response_model=ValidateEmailResponse)
async def validate(payload: ValidateEmailRequest):
    """Validate a single address (syntax + MX). Does not send anything."""
    status = await asyncio.to_thread(validate_email, payload.email)
    return ValidateEmailResponse(email=payload.email, status=status)


class ValidateContactsResponse(BaseModel):
    checked: int
    valid: int
    invalid: int
    unknown: int


@router.post("/validate-contacts", response_model=ValidateContactsResponse)
async def validate_contacts(
    limit: int = 200, db: AsyncSession = Depends(get_db)
):
    """Validate up to ``limit`` not-yet-checked contacts and mark undeliverable ones.

    Marks each contact's ``email_status`` so the outreach queue can skip dead
    domains before spending sender reputation.
    """
    from app.services.email_validation import validate_contact

    rows = (
        await db.execute(
            select(Contact)
            .where(Contact.email.is_not(None))
            .where(Contact.email_status.is_(None))
            .limit(max(1, min(limit, 1000)))
        )
    ).scalars().all()

    counts = {"valid": 0, "invalid": 0, "unknown": 0}
    for contact in rows:
        status = await validate_contact(db, contact)
        counts[status] = counts.get(status, 0) + 1
    await db.commit()
    return ValidateContactsResponse(
        checked=len(rows),
        valid=counts["valid"],
        invalid=counts["invalid"],
        unknown=counts["unknown"],
    )
