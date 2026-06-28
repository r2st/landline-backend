"""Pre-call background briefing for sales reps.

Before a rep dials, this assembles everything they need on one screen: who they
are calling, how the lead has engaged with email so far, the industry's real
pain points, recent interaction history, suggested talking points, and the right
industry call script. Built from a ``CallTask`` (or a bare company/contact) so
it works both from the queue and ad-hoc.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import CallScript, CallTask
from app.models.company import Company
from app.models.contact import Contact
from app.models.interaction import Interaction
from app.schemas.call import (
    CallBriefing,
    CallScriptRead,
    EngagementSummary,
)
from app.services import call_scripts_data, industry_verticals as iv
from app.services.call_queue import engagement_by_contact


async def _script_for(
    db: AsyncSession, industry_key: str, script_type: str = "initial_cold_call"
) -> CallScriptRead | None:
    """Prefer a DB-stored script for the industry; fall back to the canonical
    in-code template so a brief always has a script even on a fresh DB."""
    row = (
        await db.execute(
            select(CallScript).where(
                CallScript.industry == industry_key,
                CallScript.script_type == script_type,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return CallScriptRead.model_validate(row)

    tpl = call_scripts_data.get_script(industry_key, script_type)
    if tpl is None:
        return None
    return CallScriptRead(**tpl.as_row())


async def build_briefing(
    db: AsyncSession,
    *,
    call_task: CallTask | None = None,
    company: Company | None = None,
    contact: Contact | None = None,
    script_type: str = "initial_cold_call",
) -> CallBriefing:
    """Assemble a :class:`CallBriefing`.

    Provide either a ``call_task`` (company/contact are loaded from it) or an
    explicit ``company`` (and optional ``contact``).
    """
    now = datetime.now(timezone.utc)

    if call_task is not None:
        company = (
            await db.execute(select(Company).where(Company.id == call_task.company_id))
        ).scalar_one_or_none()
        if call_task.contact_id is not None:
            contact = (
                await db.execute(
                    select(Contact).where(Contact.id == call_task.contact_id)
                )
            ).scalar_one_or_none()

    if company is None:
        raise ValueError("build_briefing requires a company (directly or via call_task)")

    vertical = iv.resolve_vertical_or_default(
        company.industry_segment or "", company.name or ""
    )

    # Engagement summary for the contact (if any).
    engagement = EngagementSummary()
    if contact is not None:
        eng = (await engagement_by_contact(db, [contact.id])).get(contact.id)
        if eng is not None:
            engagement.emails_sent = eng.emails_sent
            engagement.opens = eng.opens
            engagement.clicks = eng.clicks
            engagement.replies = eng.replies
            engagement.last_contact_at = eng.last_contact_at
            if eng.last_contact_at is not None:
                engagement.days_since_last_contact = (
                    now - eng.last_contact_at
                ).days

    # Recent interactions (most recent first).
    interactions: list[dict] = []
    iquery = select(Interaction).where(Interaction.company_id == company.id)
    if contact is not None:
        # Include contact-specific and company-wide interactions.
        iquery = select(Interaction).where(
            (Interaction.company_id == company.id)
            | (Interaction.contact_id == contact.id)
        )
    irows = (
        await db.execute(iquery.order_by(Interaction.occurred_at.desc()).limit(5))
    ).scalars().all()
    for it in irows:
        interactions.append(
            {
                "type": it.interaction_type,
                "subject": it.subject,
                "notes": it.notes,
                "occurred_at": it.occurred_at.isoformat() if it.occurred_at else None,
            }
        )

    script = await _script_for(db, vertical.key, script_type)

    # Suggested talking points: lead with engagement context, then the script's
    # opening hook and a couple of qualifying questions / pain points.
    talking_points: list[str] = []
    if engagement.replies:
        talking_points.append(
            "They've replied before — reference the conversation and pick it back up."
        )
    elif engagement.clicks:
        talking_points.append(
            "They clicked a link recently — ask what caught their eye on the dashboard."
        )
    elif engagement.opens >= 3:
        talking_points.append(
            f"They've opened your emails {engagement.opens} times — there's interest; "
            "name it directly."
        )
    elif engagement.opens:
        talking_points.append(
            "They've opened an email but not engaged — open with curiosity, not a pitch."
        )
    else:
        talking_points.append(
            "Cold — no email engagement yet; lead with the industry pain, not the product."
        )
    if engagement.days_since_last_contact is not None:
        talking_points.append(
            f"Last touch was {engagement.days_since_last_contact} days ago."
        )

    if script is not None:
        talking_points.append(f"Open with: {script.opening_hook}")
        for q in (script.qualifying_questions or [])[:3]:
            talking_points.append(f"Ask: {q}")
        if script.call_to_action:
            talking_points.append(f"Close with: {script.call_to_action}")

    phone = None
    if contact is not None:
        phone = contact.phone or contact.mobile
    phone = phone or company.phone

    return CallBriefing(
        call_task_id=call_task.id if call_task is not None else None,
        company_id=company.id,
        company_name=company.name,
        contact_id=contact.id if contact is not None else None,
        contact_name=(contact.full_name if contact is not None else None),
        contact_title=(contact.title if contact is not None else None),
        phone=phone,
        industry=vertical.key,
        priority=(call_task.priority if call_task is not None else None),
        engagement=engagement,
        pain_points=list(vertical.pain_points),
        talking_points=talking_points,
        recent_interactions=interactions,
        script=script,
    )
