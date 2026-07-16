"""Email template management, preview, and compose/send endpoints.

Powers the Outreach > Email Campaigns experience: managing reusable email
templates (one marked default), previewing merged output for a contact, and
composing/sending outreach emails to selected contacts.
"""

import csv
import io
import logging
import uuid
from datetime import datetime, date
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user_optional
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend, EmailTemplate
from app.models.interaction import Interaction
from app.models.user import User
from app.schemas.email_template import (
    ABStats,
    ABStatsResponse,
    CampaignAnalytics,
    EmailComposeRequest,
    EmailComposeResponse,
    EmailComposeResult,
    EmailSendPaginatedResponse,
    EmailSendRead,
    EmailTemplateCreate,
    EmailTemplateRead,
    EmailTemplateUpdate,
    TemplatePreviewRequest,
    TemplatePreviewResponse,
    SendTestRequest,
    SendTestResponse,
    AIEmailGenerateRequest,
    AIEmailGenerateResponse,
)
from app.services.email_compose import SAMPLE_CONTEXT, build_merge_context, render
from app.services.ai_email_generator import generate_email as ai_generate_email
from app.services.email_dispatch import get_account, resolve_sender_info, send_outreach_email
from app.routers.tracking import generate_tracking_pixel_url

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


# --- Send Test ---

@router.post("/email-templates/send-test", response_model=SendTestResponse)
async def send_test_email(
    payload: SendTestRequest,
    db: AsyncSession = Depends(get_db),
):
    """Send a single test email using a template with placeholder merge data.

    Renders the template with sample values, prefixes the subject with
    ``[TEST]``, and sends to the provided ``test_email`` address. Useful for
    verifying template content and deliverability before sending to real
    contacts.
    """
    template = await _get_template_or_404(db, payload.template_id)

    # Build sample merge context for test rendering.
    context = {
        "first_name": "Test",
        "last_name": "User",
        "full_name": "Test User",
        "company": "Sample Company",
        "company_name": "Sample Company",
        "industry_segment": "Technology",
        "industry": "Technology",
        "city": "New York",
        "contact_title": "VP of Marketing",
        "title": "VP of Marketing",
        "email": payload.test_email,
        "demo_link": "https://best-molding-demo.pages.dev",
    }

    subject = "[TEST] " + render(template.subject, context)
    body_html = render(template.body, context)

    try:
        send_result = await send_outreach_email(
            db,
            to_email=payload.test_email,
            subject=subject,
            body_html=body_html,
            sender_account_id=payload.sender_account_id,
        )
        status = send_result.get("status", "error")
        detail = send_result.get("reason") or send_result.get("detail")

        if status in ("sent", "skipped"):
            logger.info(
                "Test email sent to %s using template %s",
                payload.test_email, payload.template_id,
            )
            return SendTestResponse(status="sent", subject=subject, detail=detail)

        logger.warning(
            "Test email failed for %s using template %s: %s",
            payload.test_email, payload.template_id, detail,
        )
        return SendTestResponse(status="failed", subject=subject, detail=detail)
    except Exception as exc:
        logger.exception("Test email error for %s", payload.test_email)
        return SendTestResponse(
            status="failed", subject=subject, detail=str(exc),
        )

@router.post("/email-compose", response_model=EmailComposeResponse)
async def compose_and_send(
    payload: EmailComposeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Compose and send an outreach email to the selected contacts.

    Uses the specified template, or the default template if none is given.
    Records each send in ``email_sends`` (and logs an interaction) for tracking.
    When authenticated, each send is attributed to the current user so the
    per-client communication history shows who sent what.
    """
    # Attribute sends to the acting user, falling back to the sender Gmail
    # account's owner (for automated/API-key batches sent from a user's inbox).
    sender_user_id = current_user.id if current_user is not None else None
    if payload.template_id is not None:
        template = await _get_template_or_404(db, payload.template_id)
    else:
        template = await _get_default_template(db)
        if template is None:
            raise HTTPException(status_code=400, detail="No default template configured")

    # Validate template subject and body are not empty before sending.
    if not template.subject or not template.subject.strip():
        raise HTTPException(status_code=400, detail="Template subject cannot be empty")
    if not template.body or not template.body.strip():
        raise HTTPException(status_code=400, detail="Template body cannot be empty")

    # Resolve who this batch is sent as (selected/active Gmail account, else SES).
    sender_info = await resolve_sender_info(db, payload.sender_account_id)

    # If we don't have an authenticated user, attribute the send to the owner
    # of the sending account (so history still shows who the mailbox belongs to).
    if sender_user_id is None and sender_info.account_id is not None:
        acct = await get_account(db, sender_info.account_id)
        sender_user_id = getattr(acct, "user_id", None) if acct else None

    results: list[EmailComposeResult] = []
    sent = skipped = errors = 0

    for i, contact_id in enumerate(payload.contact_ids):
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

        # Never email a contact who has unsubscribed or been suppressed
        # (CAN-SPAM / CASL; suppression is the Feature-1 hard opt-out flag).
        if contact.unsubscribed or getattr(contact, "suppressed", False):
            skipped += 1
            reason = "suppressed" if getattr(contact, "suppressed", False) else "unsubscribed"
            results.append(EmailComposeResult(
                contact_id=contact_id, to_email=contact.email,
                status="skipped", detail=reason,
            ))
            db.add(EmailSend(
                template_id=template.id, contact_id=contact.id,
                to_email=contact.email, subject=template.subject,
                status="skipped", error=reason,
            ))
            continue

        try:
            # A/B variant selection
            variant = "B" if payload.subject_b and i % 2 else "A" if payload.subject_b else None

            company_result = await db.execute(
                select(Company).where(Company.id == contact.company_id)
            )
            company = company_result.scalar_one_or_none()
            context = build_merge_context(contact, company)

            # Use variant B subject if applicable
            if variant == "B":
                subject = render(payload.subject_b, context)
            else:
                subject = render(template.subject, context)
            body_html = render(template.body, context)

            # Create an EmailSend record first so we have a send_id for tracking
            email_send = EmailSend(
                template_id=template.id,
                contact_id=contact.id,
                to_email=contact.email,
                subject=subject,
                status="pending",
                ab_variant=variant,
                user_id=sender_user_id,
            )
            db.add(email_send)
            await db.flush()

            # Handle scheduled sends
            if payload.scheduled_at:
                email_send.status = "scheduled"
                email_send.scheduled_at = payload.scheduled_at
                sent += 1
                results.append(EmailComposeResult(
                    contact_id=contact_id, to_email=contact.email,
                    status="scheduled", detail="scheduled for later",
                ))
                continue

            # Inject open tracking pixel into email body
            tracking_pixel_url = generate_tracking_pixel_url(str(email_send.id))
            body_html = body_html + f'<img src="{tracking_pixel_url}" width="1" height="1" style="display:none" />'

            send_result = await send_outreach_email(
                db,
                to_email=contact.email,
                subject=subject,
                body_html=body_html,
                sender_account_id=payload.sender_account_id,
            )
            status = send_result.get("status", "error")
            message_id = send_result.get("message_id")
            detail = send_result.get("reason") or send_result.get("detail")

            # Update the existing EmailSend record with send result
            email_send.status = status
            email_send.provider_message_id = message_id
            email_send.error = None if status == "sent" else detail
            # Capture Gmail thread tracking fields
            if send_result.get("gmail_message_id"):
                email_send.gmail_message_id = send_result["gmail_message_id"]
            if send_result.get("gmail_thread_id"):
                email_send.gmail_thread_id = send_result["gmail_thread_id"]
            if send_result.get("from_email"):
                email_send.from_email = send_result["from_email"]
            if send_result.get("sender_account_id"):
                from uuid import UUID as _UUID
                email_send.sender_account_id = _UUID(send_result["sender_account_id"])
            # Store the rendered body for thread view
            email_send.body_html = body_html

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
        except Exception as exc:
            logger.exception("Error processing contact %s in compose batch", contact_id)
            errors += 1
            results.append(EmailComposeResult(
                contact_id=contact_id,
                to_email=getattr(contact, "email", None),
                status="error",
                detail=str(exc),
            ))

    await db.flush()
    logger.info(
        "Compose to %d contacts via '%s' as %s (%s): sent=%d skipped=%d errors=%d",
        len(payload.contact_ids), template.name, sender_info.from_email,
        sender_info.transport, sent, skipped, errors,
    )
    return EmailComposeResponse(
        template_id=template.id,
        sent=sent,
        skipped=skipped,
        errors=errors,
        transport=sender_info.transport,
        sender=sender_info.from_email,
        results=results,
    )


def _apply_email_send_filters(
    query,
    contact_id: Optional[uuid.UUID] = None,
    status: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
):
    """Apply common filters to an EmailSend query."""
    if contact_id is not None:
        query = query.where(EmailSend.contact_id == contact_id)
    if status is not None:
        query = query.where(EmailSend.status == status)
    if date_from is not None:
        query = query.where(EmailSend.sent_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to is not None:
        query = query.where(EmailSend.sent_at <= datetime.combine(date_to, datetime.max.time()))
    return query


@router.get(
    "/email-sends",
    response_model=Union[EmailSendPaginatedResponse, list[EmailSendRead]],
)
async def list_email_sends(
    contact_id: Optional[uuid.UUID] = Query(None),
    status: Optional[str] = Query(None, description="Filter by status (sent, skipped, error, scheduled, bounced)"),
    date_from: Optional[date] = Query(None, description="Filter sends on or after this date (YYYY-MM-DD)"),
    date_to: Optional[date] = Query(None, description="Filter sends on or before this date (YYYY-MM-DD)"),
    page: Optional[int] = Query(None, ge=1, description="Page number (enables pagination when provided)"),
    per_page: int = Query(50, ge=1, le=200, description="Items per page (used with page param)"),
    limit: int = Query(500, ge=1, le=2000, description="Max results when not using pagination"),
    db: AsyncSession = Depends(get_db),
):
    """List email send history (most recent first) for the tracking view.

    Supports two modes:
    - **List mode** (default): returns a flat list, controlled by ``limit``.
      Backward-compatible with existing frontend hooks.
    - **Paginated mode**: when ``page`` is provided, returns
      ``{items, total, page, per_page, pages}``.

    Both modes accept optional ``contact_id``, ``status``, ``date_from``,
    and ``date_to`` filters.
    """
    base = select(EmailSend).order_by(EmailSend.sent_at.desc())
    base = _apply_email_send_filters(base, contact_id, status, date_from, date_to)

    if page is not None:
        # Paginated mode
        count_q = select(func.count()).select_from(
            _apply_email_send_filters(
                select(EmailSend.id), contact_id, status, date_from, date_to
            ).subquery()
        )
        total = (await db.execute(count_q)).scalar() or 0
        pages = max(1, -(-total // per_page))  # ceil division

        query = base.offset((page - 1) * per_page).limit(per_page)
        result = await db.execute(query)
        items = list(result.scalars().all())

        return EmailSendPaginatedResponse(
            items=items,
            total=total,
            page=page,
            per_page=per_page,
            pages=pages,
        )

    # Legacy list mode
    query = base.limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/email-sends/export-csv")
async def export_email_sends_csv(
    contact_id: Optional[uuid.UUID] = Query(None),
    status: Optional[str] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Export email send history as a CSV file.

    Accepts the same filters as the list endpoint. Returns all matching
    rows (no pagination limit).
    """
    query = select(EmailSend).order_by(EmailSend.sent_at.desc())
    query = _apply_email_send_filters(query, contact_id, status, date_from, date_to)

    result = await db.execute(query)
    sends = list(result.scalars().all())

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "template_id", "contact_id", "to_email", "subject",
        "status", "sent_at", "opened_at", "clicked_at", "ab_variant",
        "scheduled_at", "error",
    ])
    for s in sends:
        writer.writerow([
            str(s.id),
            str(s.template_id) if s.template_id else "",
            str(s.contact_id) if s.contact_id else "",
            s.to_email or "",
            s.subject or "",
            s.status,
            s.sent_at.isoformat() if s.sent_at else "",
            s.opened_at.isoformat() if s.opened_at else "",
            s.clicked_at.isoformat() if s.clicked_at else "",
            s.ab_variant or "",
            s.scheduled_at.isoformat() if s.scheduled_at else "",
            s.error or "",
        ])

    output.seek(0)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="email_sends_{timestamp}.csv"'
        },
    )


@router.get("/email-sends/ab-stats", response_model=ABStatsResponse)
async def get_ab_stats(
    template_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Get A/B test results for a template."""
    from sqlalchemy import func as sa_func

    result = await db.execute(
        select(
            EmailSend.ab_variant,
            sa_func.count(EmailSend.id).label("total"),
            sa_func.count(EmailSend.opened_at).label("opened"),
            sa_func.count(EmailSend.clicked_at).label("clicked"),
        )
        .where(
            EmailSend.template_id == template_id,
            EmailSend.ab_variant.isnot(None),
        )
        .group_by(EmailSend.ab_variant)
    )
    rows = result.all()
    stats = []
    for row in rows:
        total = row.total or 0
        stats.append(ABStats(
            variant=row.ab_variant,
            total=total,
            opened=row.opened or 0,
            clicked=row.clicked or 0,
            open_rate=round((row.opened or 0) / total * 100, 1) if total else 0,
            click_rate=round((row.clicked or 0) / total * 100, 1) if total else 0,
        ))
    return ABStatsResponse(template_id=template_id, stats=stats)


@router.get("/email-sends/analytics", response_model=CampaignAnalytics)
async def get_campaign_analytics(db: AsyncSession = Depends(get_db)):
    """Get campaign analytics summary."""
    from datetime import timedelta
    from sqlalchemy import cast, Date

    # Total counts by time period
    all_sends = await db.execute(
        select(func.count(EmailSend.id)).where(EmailSend.status != "scheduled")
    )
    total_all = all_sends.scalar() or 0

    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)

    sends_7d = await db.execute(
        select(func.count(EmailSend.id)).where(
            EmailSend.sent_at >= seven_days_ago,
            EmailSend.status != "scheduled",
        )
    )
    total_7d = sends_7d.scalar() or 0

    sends_30d = await db.execute(
        select(func.count(EmailSend.id)).where(
            EmailSend.sent_at >= thirty_days_ago,
            EmailSend.status != "scheduled",
        )
    )
    total_30d = sends_30d.scalar() or 0

    # Rates
    if total_all > 0:
        opened = await db.execute(
            select(func.count(EmailSend.id)).where(EmailSend.opened_at.isnot(None))
        )
        clicked = await db.execute(
            select(func.count(EmailSend.id)).where(EmailSend.clicked_at.isnot(None))
        )
        replied = await db.execute(
            select(func.count(EmailSend.id)).where(EmailSend.replied_at.isnot(None))
        )
        bounced = await db.execute(
            select(func.count(EmailSend.id)).where(EmailSend.status == "bounced")
        )
        open_rate = round((opened.scalar() or 0) / total_all * 100, 1)
        click_rate = round((clicked.scalar() or 0) / total_all * 100, 1)
        reply_rate = round((replied.scalar() or 0) / total_all * 100, 1)
        bounce_rate = round((bounced.scalar() or 0) / total_all * 100, 1)
    else:
        open_rate = click_rate = reply_rate = bounce_rate = 0.0

    # Daily sends for last 30 days
    daily = await db.execute(
        select(
            cast(EmailSend.sent_at, Date).label("date"),
            func.count(EmailSend.id).label("count"),
        )
        .where(
            EmailSend.sent_at >= thirty_days_ago,
            EmailSend.status != "scheduled",
        )
        .group_by(cast(EmailSend.sent_at, Date))
        .order_by(cast(EmailSend.sent_at, Date))
    )
    daily_sends = [{"date": str(row.date), "count": row.count} for row in daily.all()]

    return CampaignAnalytics(
        total_sent_7d=total_7d,
        total_sent_30d=total_30d,
        total_sent_all=total_all,
        open_rate=open_rate,
        click_rate=click_rate,
        reply_rate=reply_rate,
        bounce_rate=bounce_rate,
        daily_sends=daily_sends,
    )


# --- AI Email Generation ---

@router.post("/email-templates/generate", response_model=AIEmailGenerateResponse)
async def generate_ai_email(payload: AIEmailGenerateRequest):
    """Generate a personalized outreach email using AI.

    Takes lead data (name, company, industry, title) and produces a
    tailored subject + body. Falls back to a template-based response
    if AI generation fails.

    The demo_link in the response is dynamically selected based on
    the target company's industry.
    """
    lead_data = {
        "first_name": payload.first_name,
        "company_name": payload.company_name,
        "industry": payload.industry,
        "title": payload.title,
        "city": payload.city,
    }

    result = await ai_generate_email(
        lead_data=lead_data,
        template_type=payload.template_type,
        custom_instructions=payload.custom_instructions,
    )

    return AIEmailGenerateResponse(
        subject=result["subject"],
        body=result["body"],
        demo_link=result["demo_link"],
        ai_generated=result["ai_generated"],
        model=result.get("model"),
        template_type=result["template_type"],
    )
