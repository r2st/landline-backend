"""Celery tasks for the Referral & Warm-Intro agent.

All three tasks fan out per organization via :func:`for_each_org`, so each org's
agent runs against only its own data with its own config + LLM key, and one
org's failure never aborts the sweep.

* :func:`scan_referral_candidates` — weekly. Finds happy, long-tenured clients
  and queues an ``initial_ask`` action (drafted by the agent's LLM) plus an
  ``identified`` referral. When ``auto_ask_enabled`` the ask is sent immediately;
  otherwise it waits in the action queue for human approval.
* :func:`followup_pending_referrals` — daily. For asks sent but unanswered past
  ``followup_after_days``, queues a ``followup`` action (auto-sent when
  ``auto_ask_enabled``).
* :func:`check_referral_conversions` — daily. Detects referred contacts who have
  since closed a won deal, marks the referral ``converted`` with the deal value,
  and queues a ``thank_you`` action.

The tasks are thin ``_run_async`` wrappers over plain async helpers so the logic
is unit-testable without Celery.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.referral import Referral, ReferralAction, ReferralAgent
from app.services import referral_agent as svc
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _active_agent(db: AsyncSession) -> Optional[ReferralAgent]:
    """The org's agent config if it exists AND is switched on, else None."""
    agent = (await db.execute(select(ReferralAgent))).scalars().first()
    if agent is None or not agent.is_active:
        return None
    return agent


# ---------------------------------------------------------------------------
# 1. Scan for candidates (weekly)
# ---------------------------------------------------------------------------
async def scan_org_candidates(db: AsyncSession, *, max_new: int = 25) -> dict:
    """Find happy clients and queue initial-ask actions for this org.

    Idempotent per contact: an ``identified`` referral or a pending ``initial_ask``
    action already on file for a contact means we skip re-queuing. Returns a small
    summary for the sweep log.
    """
    agent = await _active_agent(db)
    if agent is None:
        return {"skipped": "inactive"}

    candidates = await svc.find_referral_candidates(db, agent, limit=max_new)
    queued = 0
    sent = 0
    for cand in candidates:
        contact = cand.contact
        # Skip if we already have an open ask/referral for this contact.
        open_ref = (
            await db.execute(
                select(func.count(Referral.id)).where(
                    Referral.referrer_contact_id == contact.id,
                    Referral.status.in_(("identified", "asked", "pending")),
                )
            )
        ).scalar_one() or 0
        if open_ref:
            continue

        draft = await svc.generate_referral_ask(
            db, agent, contact, cand.company, svc.relationship_summary(cand)
        )
        referral = Referral(
            referrer_contact_id=contact.id,
            referred_company=None,
            status="identified",
            notes=f"Auto-identified: {cand.company.name} (health {cand.health_score}).",
        )
        db.add(referral)
        await db.flush()

        action = ReferralAction(
            referrer_contact_id=contact.id,
            referral_id=referral.id,
            action_type="initial_ask",
            status="pending",
            ai_subject=draft.get("subject"),
            ai_message=draft.get("body"),
        )
        db.add(action)
        await db.flush()
        queued += 1

        if agent.auto_ask_enabled:
            from app.routers.referral_agent import svc_execute_action

            result = await svc_execute_action(db, action)
            if result.get("status") == "sent":
                sent += 1

    await db.commit()
    logger.info(
        "Referral scan: %d candidates, %d queued, %d auto-sent", len(candidates), queued, sent
    )
    return {"candidates": len(candidates), "queued": queued, "auto_sent": sent}


@celery_app.task(name="app.tasks.referral_agent.scan_referral_candidates")
def scan_referral_candidates():
    """Weekly per-org scan for happy clients to ask for referrals."""
    logger.info("Starting weekly referral-candidate scan")

    async def _run():
        return await for_each_org(scan_org_candidates, label="scan_referral_candidates")

    return _run_async(_run())


# ---------------------------------------------------------------------------
# 2. Follow up on unanswered asks (daily)
# ---------------------------------------------------------------------------
async def followup_org_referrals(db: AsyncSession) -> dict:
    """Queue follow-up actions for asks unanswered past the threshold."""
    agent = await _active_agent(db)
    if agent is None:
        return {"skipped": "inactive"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=agent.followup_after_days)
    stale = (
        await db.execute(
            select(Referral).where(
                Referral.status == "asked",
                Referral.ask_sent_at.isnot(None),
                Referral.ask_sent_at <= cutoff,
            )
        )
    ).scalars().all()

    queued = 0
    sent = 0
    for referral in stale:
        # One pending/sent follow-up per referral — don't pile on.
        already = (
            await db.execute(
                select(func.count(ReferralAction.id)).where(
                    ReferralAction.referral_id == referral.id,
                    ReferralAction.action_type == "followup",
                    ReferralAction.status.in_(("pending", "sent")),
                )
            )
        ).scalar_one() or 0
        if already:
            continue

        contact = await _load_contact(db, referral.referrer_contact_id)
        company = await _load_company_for_contact(db, contact)
        draft = await svc.generate_followup(db, agent, referral, contact, company)
        action = ReferralAction(
            referrer_contact_id=referral.referrer_contact_id,
            referral_id=referral.id,
            action_type="followup",
            status="pending",
            ai_subject=draft.get("subject"),
            ai_message=draft.get("body"),
        )
        db.add(action)
        await db.flush()
        queued += 1

        # Move to 'pending' so we don't re-select it next run even before sending.
        referral.status = "pending"

        if agent.auto_ask_enabled:
            from app.routers.referral_agent import svc_execute_action

            result = await svc_execute_action(db, action)
            if result.get("status") == "sent":
                sent += 1

    await db.commit()
    logger.info("Referral follow-up: %d stale, %d queued, %d auto-sent", len(stale), queued, sent)
    return {"stale": len(stale), "queued": queued, "auto_sent": sent}


@celery_app.task(name="app.tasks.referral_agent.followup_pending_referrals")
def followup_pending_referrals():
    """Daily follow-up on unanswered referral asks."""
    logger.info("Starting daily referral follow-up pass")

    async def _run():
        return await for_each_org(followup_org_referrals, label="followup_pending_referrals")

    return _run_async(_run())


# ---------------------------------------------------------------------------
# 3. Detect conversions (daily)
# ---------------------------------------------------------------------------
async def check_org_conversions(db: AsyncSession) -> dict:
    """Mark referrals converted when the referred contact closes a won deal."""
    agent = await _active_agent(db)
    if agent is None:
        return {"skipped": "inactive"}

    # Referrals that landed a concrete contact but haven't converted yet.
    open_refs = (
        await db.execute(
            select(Referral).where(
                Referral.referred_contact_id.isnot(None),
                Referral.status.notin_(("converted", "declined")),
            )
        )
    ).scalars().all()

    converted = 0
    for referral in open_refs:
        contact = await _load_contact(db, referral.referred_contact_id)
        if contact is None:
            continue
        won = (
            await db.execute(
                select(Deal)
                .where(Deal.company_id == contact.company_id, Deal.status == "won")
                .order_by(Deal.close_date.desc().nullslast())
            )
        ).scalars().first()
        if won is None:
            continue

        now = datetime.now(timezone.utc)
        referral.status = "converted"
        referral.converted_at = now
        referral.received_at = referral.received_at or now
        # Deal value → paise (value is stored as a major-unit Numeric).
        if won.value is not None:
            referral.referral_value = int(round(float(won.value) * 100))
        converted += 1

        # Queue a thank-you to the referrer.
        referrer = await _load_contact(db, referral.referrer_contact_id)
        draft = await svc.generate_thankyou(
            db, agent, referral, referrer, referral.referral_value
        )
        db.add(
            ReferralAction(
                referrer_contact_id=referral.referrer_contact_id,
                referral_id=referral.id,
                action_type="thank_you",
                status="pending",
                ai_subject=draft.get("subject"),
                ai_message=draft.get("body"),
            )
        )
        await db.flush()

        if agent.auto_ask_enabled and referral.referrer_contact_id is not None:
            # Auto-send the freshly-created thank-you action.
            from app.routers.referral_agent import svc_execute_action

            action = (
                await db.execute(
                    select(ReferralAction)
                    .where(
                        ReferralAction.referral_id == referral.id,
                        ReferralAction.action_type == "thank_you",
                        ReferralAction.status == "pending",
                    )
                    .order_by(ReferralAction.created_at.desc())
                )
            ).scalars().first()
            if action is not None:
                await svc_execute_action(db, action)

    await db.commit()
    logger.info("Referral conversions: %d checked, %d converted", len(open_refs), converted)
    return {"checked": len(open_refs), "converted": converted}


@celery_app.task(name="app.tasks.referral_agent.check_referral_conversions")
def check_referral_conversions():
    """Daily check for referred contacts who have closed a deal."""
    logger.info("Starting daily referral-conversion check")

    async def _run():
        return await for_each_org(check_org_conversions, label="check_referral_conversions")

    return _run_async(_run())


# ---------------------------------------------------------------------------
# small loaders
# ---------------------------------------------------------------------------
async def _load_contact(db: AsyncSession, contact_id) -> Optional[Contact]:
    if contact_id is None:
        return None
    return (
        await db.execute(select(Contact).where(Contact.id == contact_id))
    ).scalar_one_or_none()


async def _load_company_for_contact(
    db: AsyncSession, contact: Optional[Contact]
) -> Optional[Company]:
    if contact is None:
        return None
    return (
        await db.execute(select(Company).where(Company.id == contact.company_id))
    ).scalar_one_or_none()
