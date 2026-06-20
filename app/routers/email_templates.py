"""Email template management, preview, and compose/send endpoints.

Powers the Outreach > Email Campaigns experience: managing reusable email
templates (one marked default), previewing merged output for a contact, and
composing/sending outreach emails to selected contacts.
"""

import asyncio
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend, EmailTemplate
from app.models.interaction import Interaction
from app.schemas.email_template import (
    EmailComposeRequest,
    EmailComposeResponse,
    EmailComposeResult,
    EmailSendRead,
    EmailTemplateCreate,
    EmailTemplateRead,
    EmailTemplateUpdate,
    TemplatePreviewRequest,
    TemplatePreviewResponse,
)
from app.services.email_compose import SAMPLE_CONTEXT, build_merge_context, render
from app.services.email_sender import EmailSender

logger = logging.getLogger(__name__)
router = APIRouter(tags=["email"])


# --- Helpers ---

async def _get_template_or_404(db: AsyncSession, template_id: uuid.UUID) -> EmailTemplate:
    result = await db.execute(select(EmailTemplate).where(EmailTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Email template not found")
    return template


async def _get_default_template(db: AsyncSession) -> Optional[EmailTemplate]:
    result = await db.execute(
        select(EmailTemplate).where(EmailTemplate.is_default == True)  # noqa: E712
    )
    return result.scalar_one_or_none()


async def _clear_default(db: AsyncSession, except_id: Optional[uuid.UUID] = None) -> None:
    """Unset is_default on all templates (optionally except one), to keep a single default."""
    stmt = update(EmailTemplate).where(EmailTemplate.is_default == True).values(is_default=False)  # noqa: E712
    if except_id is not None:
        stmt = stmt.where(EmailTemplate.id != except_id)
    await db.execute(stmt)
    await db.flush()


# --- Template CRUD ---

@router.get("/email-templates", response_model=list[EmailTemplateRead])
async def list_email_templates(db: AsyncSession = Depends(get_db)):
    """List all email templates (default first, then newest)."""
    result = await db.execute(
        select(EmailTemplate).order_by(
            EmailTemplate.is_default.desc(), EmailTemplate.created_at.desc()
        )
    )
    return list(result.scalars().all())


@router.post("/email-templates", response_model=EmailTemplateRead, status_code=201)
async def create_email_template(
    payload: EmailTemplateCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new email template. If marked default, any prior default is cleared.

    If no template is currently marked default, the first one created becomes default.
    """
    make_default = payload.is_default
    if not make_default and await _get_default_template(db) is None:
        make_default = True

    if make_default:
        await _clear_default(db)

    template = EmailTemplate(
        name=payload.name,
        subject=payload.subject,
        body=payload.body,
        is_default=make_default,
    )
    db.add(template)
    try:
        await db.flush()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A template with that name already exists")
    await db.refresh(template)
    logger.info("Created email template '%s' (default=%s)", template.name, template.is_default)
    return template


@router.put("/email-templates/{template_id}", response_model=EmailTemplateRead)
async def update_email_template(
    template_id: uuid.UUID,
    payload: EmailTemplateUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update an email template's fields."""
    template = await _get_template_or_404(db, template_id)
    update_data = payload.model_dump(exclude_unset=True)

    # Handle default toggling carefully to preserve the single-default invariant.
    if update_data.get("is_default") is True:
        await _clear_default(db, except_id=template_id)
    elif update_data.get("is_default") is False and template.is_default:
        raise HTTPException(
            status_code=400,
            detail="Cannot unset the default directly — mark another template as default instead",
        )

    for field, value in update_data.items():
        setattr(template, field, value)

    try:
        await db.flush()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A template with that name already exists")
    await db.refresh(template)
    logger.info("Updated email template %s", template_id)
    return template


@router.delete("/email-templates/{template_id}", status_code=204)
async def delete_email_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Delete an email template. The default template cannot be deleted."""
    template = await _get_template_or_404(db, template_id)
    if template.is_default:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the default template — set another template as default first",
        )
    await db.delete(template)
    await db.flush()
    logger.info("Deleted email template %s", template_id)


@router.post("/email-templates/{template_id}/set-default", response_model=EmailTemplateRead)
async def set_default_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Mark a template as the default, clearing the flag on any other template."""
    template = await _get_template_or_404(db, template_id)
    await _clear_default(db, except_id=template_id)
    template.is_default = True
    await db.flush()
    await db.refresh(template)
    logger.info("Set email template %s as default", template_id)
    return template


# --- Preview ---

@router.post("/email-templates/preview", response_model=TemplatePreviewResponse)
async def preview_email_template(
    payload: TemplatePreviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """Render a template's subject and body with a contact's data merged in.

    Resolution order for the template content: inline subject/body if given,
    else the referenced template_id, else the default template. The merge
    context comes from the given contact_id, or sample data if none is provided.
    """
    subject_tmpl = payload.subject
    body_tmpl = payload.body

    if subject_tmpl is None or body_tmpl is None:
        template = None
        if payload.template_id is not None:
            template = await _get_template_or_404(db, payload.template_id)
        else:
            template = await _get_default_template(db)
        if template is None:
            raise HTTPException(status_code=404, detail="No template available to preview")
        subject_tmpl = subject_tmpl if subject_tmpl is not None else template.subject
        body_tmpl = body_tmpl if body_tmpl is not None else template.body

    if payload.contact_id is not None:
        result = await db.execute(select(Contact).where(Contact.id == payload.contact_id))
        contact = result.scalar_one_or_none()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")
        company_result = await db.execute(
            select(Company).where(Company.id == contact.company_id)
        )
        company = company_result.scalar_one_or_none()
        context = build_merge_context(contact, company)
    else:
        context = dict(SAMPLE_CONTEXT)

    return TemplatePreviewResponse(
        subject=render(subject_tmpl, context),
        body=render(body_tmpl, context),
        merge_context=context,
    )


# --- Compose / Send ---

@router.post("/email-compose", response_model=EmailComposeResponse)
async def compose_and_send(
    payload: EmailComposeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Compose and send an outreach email to the selected contacts.

    Uses the specified template, or the default template if none is given.
    Records each send in ``email_sends`` (and logs an interaction) for tracking.
    """
    if payload.template_id is not None:
        template = await _get_template_or_404(db, payload.template_id)
    else:
        template = await _get_default_template(db)
        if template is None:
            raise HTTPException(status_code=400, detail="No default template configured")

    sender = EmailSender()
    results: list[EmailComposeResult] = []
    sent = skipped = errors = 0

    for contact_id in payload.contact_ids:
        result = await db.execute(select(Contact).where(Contact.id == contact_id))
        contact = result.scalar_one_or_none()
        if not contact:
            skipped += 1
            results.append(EmailComposeResult(
                contact_id=contact_id, status="skipped", detail="contact_not_found"
            ))
            continue

        if not contact.email:
            skipped += 1
            results.append(EmailComposeResult(
                contact_id=contact_id, status="skipped", detail="no_email_address"
            ))
            db.add(EmailSend(
                template_id=template.id, contact_id=contact.id,
                to_email=None, subject=template.subject, status="skipped",
                error="no_email_address",
            ))
            continue

        company_result = await db.execute(
            select(Company).where(Company.id == contact.company_id)
        )
        company = company_result.scalar_one_or_none()
        context = build_merge_context(contact, company)
        subject = render(template.subject, context)
        body_html = render(template.body, context)

        send_result = await asyncio.to_thread(
            sender.send_email,
            to_email=contact.email,
            subject=subject,
            body_html=body_html,
        )
        status = send_result.get("status", "error")
        message_id = send_result.get("message_id")
        detail = send_result.get("reason") or send_result.get("detail")

        db.add(EmailSend(
            template_id=template.id,
            contact_id=contact.id,
            to_email=contact.email,
            subject=subject,
            status=status,
            provider_message_id=message_id,
            error=None if status == "sent" else detail,
        ))

        if status in ("sent", "skipped"):
            # "skipped" means SES creds aren't configured — still a successful compose.
            db.add(Interaction(
                company_id=contact.company_id,
                contact_id=contact.id,
                interaction_type="email",
                subject=subject,
                notes=f"Outreach email composed via template '{template.name}'",
            ))

        if status == "sent":
            sent += 1
        elif status == "skipped":
            skipped += 1
        else:
            errors += 1

        results.append(EmailComposeResult(
            contact_id=contact_id, to_email=contact.email, status=status, detail=detail
        ))

    await db.flush()
    logger.info(
        "Compose to %d contacts via '%s': sent=%d skipped=%d errors=%d",
        len(payload.contact_ids), template.name, sent, skipped, errors,
    )
    return EmailComposeResponse(
        template_id=template.id, sent=sent, skipped=skipped, errors=errors, results=results
    )


@router.get("/email-sends", response_model=list[EmailSendRead])
async def list_email_sends(
    contact_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    """List email send history (most recent first) for the tracking view."""
    query = select(EmailSend).order_by(EmailSend.sent_at.desc()).limit(limit)
    if contact_id is not None:
        query = query.where(EmailSend.contact_id == contact_id)
    result = await db.execute(query)
    return list(result.scalars().all())
