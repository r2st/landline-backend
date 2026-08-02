"""Deal Progression Agent — service layer.

Everything the agent *does* lives here, framed as small, session-in functions so
both the API routes and the Celery beat tasks call the same logic (and the tests
exercise it directly, without HTTP or a broker):

* :func:`get_or_create_config` — the per-org config singleton.
* :func:`scan_stale_deals` — the workhorse: find deals that have gone quiet past
  their stage threshold and record the right :class:`DealAction` for each.
* :func:`generate_followup` / :func:`suggest_next_action` — the two AI touches.
* :func:`auto_advance_stage` — move a deal right off a real signal (reply, booked
  meeting), never backwards.
* :func:`flag_at_risk` / :func:`escalate_to_manager` — the human-in-the-loop rails.
* :func:`approve_action` / :func:`dismiss_action` / :func:`execute_action` — the
  action lifecycle the router and the hourly executor share.

All reads/writes rely on the caller having bound the org context (the API
dependency does it per request; the tasks do it per org via ``for_each_org``), so
the global org filter isolates every query automatically.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.deal_activity import DealActivity
from app.models.deal_progression_agent import (
    DEFAULT_LLM_MODEL,
    DEFAULT_STALE_THRESHOLDS,
    STAGE_ORDER,
    DealAction,
    DealProgressionAgent,
)
from app.models.email_reply import EmailReply

logger = logging.getLogger(__name__)

# The org-level system_settings key the agent falls back to for its OpenRouter
# credential when the agent config itself has no key.
ORG_OPENROUTER_KEY_SETTING = "OPENROUTER_API_KEY"

# Signal → the stage transitions it justifies. The agent only ever moves a deal
# to the *right* (never regresses), and only along an explicitly-listed hop, so a
# spurious signal can't teleport a deal to closing.
AUTO_ADVANCE_MAP: dict[str, dict[str, str]] = {
    # A prospect replied: they're engaged — move an early deal forward.
    "reply": {"qualification": "discovery", "discovery": "proposal"},
    # A meeting got booked: that's a proposal→negotiation signal.
    "meeting_booked": {"proposal": "negotiation"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a possibly-naive timestamp (SQLite) to UTC-aware."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# --- Config -------------------------------------------------------------------

async def get_or_create_config(db: AsyncSession) -> DealProgressionAgent:
    """Return the current org's agent config, creating a default if absent.

    Relies on the bound org context: the global filter scopes the SELECT and the
    before-flush hook stamps ``org_id`` on the INSERT.
    """
    existing = (await db.execute(select(DealProgressionAgent))).scalars().first()
    if existing is not None:
        return existing

    config = DealProgressionAgent(
        is_active=True,
        stale_thresholds=dict(DEFAULT_STALE_THRESHOLDS),
        auto_advance_enabled=False,
        auto_followup_enabled=True,
        max_auto_followups_per_deal=3,
        escalation_threshold_days=14,
    )
    db.add(config)
    await db.flush()
    return config


def _threshold_for_stage(config: DealProgressionAgent, stage: str) -> int:
    """Days-with-no-activity threshold for a stage, falling back to the default."""
    thresholds = config.stale_thresholds or {}
    if stage in thresholds:
        return int(thresholds[stage])
    return DEFAULT_STALE_THRESHOLDS.get(stage, 7)


# --- LLM resolution -----------------------------------------------------------

async def resolve_llm(
    db: AsyncSession, config: DealProgressionAgent
) -> tuple[str, str | None]:
    """Resolve the (model, api_key) the agent's AI calls should use.

    Model: the agent's ``llm_model`` (falls back to the default). Key, in order:

    1. the agent's own ``llm_api_key`` (decrypted from Fernet ciphertext);
    2. the org-level ``OPENROUTER_API_KEY`` in ``system_settings``;
    3. the global env ``OPENROUTER_API_KEY``.

    Returns ``(model, key_or_None)``. A ``None`` key means no credential is
    available — the AI touch will fail its request and degrade to a template.
    """
    model = config.llm_model or DEFAULT_LLM_MODEL

    key: str | None = None
    if config.llm_api_key:
        try:
            from app.services.crypto import decrypt

            key = decrypt(config.llm_api_key)
        except Exception as exc:  # bad/rotated key — fall through to fallbacks
            logger.warning("Could not decrypt agent llm_api_key: %s", exc)
            key = None

    if not key:
        try:
            from app.services import system_settings

            val = await system_settings.get_value(db, ORG_OPENROUTER_KEY_SETTING)
            if isinstance(val, str) and val.strip():
                key = val.strip()
            elif isinstance(val, dict) and isinstance(val.get("value"), str):
                key = val["value"].strip() or None
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Org OPENROUTER_API_KEY lookup failed: %s", exc)

    if not key:
        from app.config import get_settings

        key = get_settings().openrouter_api_key or None

    return model, key


def _ai_service(model: str, key: str | None):
    """Build an AIService pinned to OpenRouter with the agent's model + key."""
    from app.services.ai_service import AIService

    return AIService(
        openrouter_model=model, openrouter_api_key=key, force_openrouter=True
    )


async def _resolve_config(
    db: AsyncSession, config: DealProgressionAgent | None
) -> DealProgressionAgent:
    return config if config is not None else await get_or_create_config(db)


# --- Activity / staleness -----------------------------------------------------

async def last_activity_at(db: AsyncSession, deal: Deal) -> datetime:
    """The most recent moment anything happened on a deal.

    The latest of: the deal's own ``updated_at``, its most recent
    :class:`DealActivity`, and the most recent inbound reply from any contact at
    the deal's company. This is what "no activity in X days" is measured against.
    """
    candidates: list[datetime] = [_aware(deal.updated_at) or _aware(deal.created_at) or _now()]

    latest_activity = (
        await db.execute(
            select(func.max(DealActivity.created_at)).where(
                DealActivity.deal_id == deal.id
            )
        )
    ).scalar_one_or_none()
    if latest_activity is not None:
        candidates.append(_aware(latest_activity))

    latest_reply = (
        await db.execute(
            select(func.max(EmailReply.received_at)).where(
                EmailReply.company_id == deal.company_id
            )
        )
    ).scalar_one_or_none()
    if latest_reply is not None:
        candidates.append(_aware(latest_reply))

    return max(c for c in candidates if c is not None)


def _days_since(dt: datetime, now: Optional[datetime] = None) -> int:
    now = now or _now()
    return max(0, (now - dt).days)


async def _open_action_types(db: AsyncSession, deal_id: uuid.UUID) -> set[str]:
    """Action types that already have an open (pending/approved) row for a deal.

    Used to avoid nagging: we don't stack a second suggestion/follow-up on a deal
    that already has one waiting for a human.
    """
    rows = (
        await db.execute(
            select(DealAction.action_type).where(
                DealAction.deal_id == deal_id,
                DealAction.status.in_(("pending", "approved")),
            )
        )
    ).scalars().all()
    return set(rows)


async def _auto_followup_count(db: AsyncSession, deal_id: uuid.UUID) -> int:
    """How many follow-ups the agent has already produced for a deal."""
    return (
        await db.execute(
            select(func.count(DealAction.id)).where(
                DealAction.deal_id == deal_id,
                DealAction.action_type == "auto_followup",
            )
        )
    ).scalar_one()


async def _primary_contact(db: AsyncSession, company_id: uuid.UUID) -> Optional[Contact]:
    """Best contact to address on a deal: the primary, else any with an email."""
    primary = (
        await db.execute(
            select(Contact)
            .where(Contact.company_id == company_id, Contact.is_primary.is_(True))
            .limit(1)
        )
    ).scalars().first()
    if primary is not None:
        return primary
    return (
        await db.execute(
            select(Contact)
            .where(Contact.company_id == company_id, Contact.email.isnot(None))
            .order_by(Contact.created_at.asc())
            .limit(1)
        )
    ).scalars().first()


# --- AI touches ---------------------------------------------------------------

async def generate_followup(
    db: AsyncSession,
    deal: Deal,
    contact: Optional[Contact],
    last_interaction: Optional[datetime],
    config: DealProgressionAgent | None = None,
) -> dict:
    """AI-draft a contextual follow-up email for a stalled deal.

    Runs on the org's configured OpenRouter model + key (see
    :func:`resolve_llm`). Returns ``{"subject", "body"}``. Degrades to a plain,
    non-empty template when the AI backend is unavailable (no key, out of
    credits, rate-limited) so a stalled deal still gets a nudge the operator can
    send.
    """
    config = await _resolve_config(db, config)

    company = (
        await db.execute(select(Company).where(Company.id == deal.company_id))
    ).scalars().first()
    company_name = company.name if company else "your company"
    contact_name = ""
    if contact is not None:
        contact_name = " ".join(
            p for p in (contact.first_name, contact.last_name) if p
        ).strip()

    days_since = _days_since(_aware(last_interaction)) if last_interaction else 0

    try:
        model, key = await resolve_llm(db, config)
        ai = _ai_service(model, key)
        result = await ai.generate_followup(
            original_subject=deal.title,
            original_body="",
            step_number=await _auto_followup_count(db, deal.id) + 1,
            contact_name=contact_name or "there",
            days_since_last=days_since,
        )
        if isinstance(result, dict) and result.get("body") and not result.get("error"):
            return {
                "subject": result.get("subject") or f"Re: {deal.title}",
                "body": result["body"],
            }
    except Exception as exc:  # never let a stalled-deal draft blow up the scan
        logger.warning("generate_followup AI call failed for deal %s: %s", deal.id, exc)

    # Fallback: a short, honest nudge grounded in the deal + gap.
    first = (contact_name.split()[0] if contact_name else "there")
    gap_line = (
        f"It's been about {days_since} days since we last connected"
        if days_since
        else "Wanted to float this back to the top of your inbox"
    )
    return {
        "subject": f"Re: {deal.title}",
        "body": (
            f"Hi {first}, {gap_line} on {deal.title} for {company_name}. "
            "Is this still something you're weighing? Happy to jump on a quick "
            "call if it'd help — otherwise no worries, just let me know."
        ),
    }


async def suggest_next_action(
    db: AsyncSession, deal: Deal, config: DealProgressionAgent | None = None
) -> str:
    """AI-suggest the single best next step for a deal (human-readable text).

    Runs on the org's configured OpenRouter model + key. Falls back to a
    stage-appropriate default when the AI backend is unavailable.
    """
    config = await _resolve_config(db, config)

    company = (
        await db.execute(select(Company).where(Company.id == deal.company_id))
    ).scalars().first()
    company_name = company.name if company else "the company"
    value_str = f"${float(deal.value):,.0f}" if deal.value is not None else "unknown value"

    prompt = (
        "You are a sales manager coaching a rep on a stalled B2B deal. In ONE "
        "short sentence, tell them the single best next action. Be concrete "
        "(e.g. 'send the ROI case study', 'get the economic buyer on a call', "
        "'ask what's blocking sign-off').\n\n"
        f"Deal: {deal.title}\nCompany: {company_name}\nStage: {deal.deal_stage}\n"
        f"Value: {value_str}\n\nReturn only the sentence, no preamble."
    )
    try:
        model, key = await resolve_llm(db, config)
        ai = _ai_service(model, key)
        text = (await ai.complete_text(prompt, max_tokens=120)).strip()
        if text:
            # Keep it to a single tidy line.
            return text.splitlines()[0].strip().strip('"')
    except Exception as exc:
        logger.warning("suggest_next_action AI call failed for deal %s: %s", deal.id, exc)

    return _default_suggestion(deal.deal_stage)


async def analyze_deal(
    db: AsyncSession,
    deal: Deal,
    days_stale: int,
    threshold: int,
    config: DealProgressionAgent | None = None,
) -> str:
    """AI risk analysis for a stalled deal (used to reason about at-risk /
    escalation actions). Runs on the org's configured model + key.

    Returns one or two sentences on *why* the deal is at risk and what it needs.
    Falls back to a deterministic summary when the AI backend is unavailable, so
    the action always has a reason.
    """
    config = await _resolve_config(db, config)

    company = (
        await db.execute(select(Company).where(Company.id == deal.company_id))
    ).scalars().first()
    company_name = company.name if company else "the company"
    value_str = f"${float(deal.value):,.0f}" if deal.value is not None else "unknown value"
    fallback = (
        f"No activity for {days_stale} days on a '{deal.deal_stage}' deal "
        f"(threshold {threshold}d)."
    )

    prompt = (
        "You are a sales manager triaging a stalled B2B deal. In ONE or TWO "
        "short sentences, say why it's at risk and what it needs to get moving. "
        "No preamble.\n\n"
        f"Deal: {deal.title}\nCompany: {company_name}\nStage: {deal.deal_stage}\n"
        f"Value: {value_str}\nDays with no activity: {days_stale} "
        f"(stage threshold {threshold}d)."
    )
    try:
        model, key = await resolve_llm(db, config)
        ai = _ai_service(model, key)
        text = (await ai.complete_text(prompt, max_tokens=160)).strip()
        if text:
            return " ".join(text.split())
    except Exception as exc:
        logger.warning("analyze_deal AI call failed for deal %s: %s", deal.id, exc)

    return fallback


def _default_suggestion(stage: str) -> str:
    return {
        "qualification": "Confirm budget, authority, need, and timeline before investing more time.",
        "discovery": "Book a discovery call to map their pain points to your solution.",
        "proposal": "Follow up on the proposal and offer to walk the buyer through the ROI.",
        "negotiation": "Get the economic buyer on a call to unblock sign-off.",
        "closing": "Ask directly what's needed to sign this week and set a close date.",
    }.get(stage, "Reach out to re-engage the buyer and confirm next steps.")


# --- State transitions --------------------------------------------------------

async def auto_advance_stage(
    db: AsyncSession, deal: Deal, trigger_event: str
) -> Optional[DealAction]:
    """Advance a deal's stage off a signal, recording the change.

    Only moves along an explicit hop in :data:`AUTO_ADVANCE_MAP` and never
    backwards. Returns the created ``stage_change`` :class:`DealAction`, or
    ``None`` when the signal doesn't justify a move from the current stage.
    """
    transitions = AUTO_ADVANCE_MAP.get(trigger_event, {})
    target = transitions.get(deal.deal_stage)
    if target is None:
        return None
    # Guard against a backwards move (shouldn't happen given the map, but the
    # STAGE_ORDER check makes the invariant explicit and future-proof).
    if STAGE_ORDER.index(target) <= STAGE_ORDER.index(deal.deal_stage):
        return None

    old_stage = deal.deal_stage
    deal.deal_stage = target

    db.add(
        DealActivity(
            deal_id=deal.id,
            activity_type="stage_change",
            description=f"Deal Progression Agent advanced stage on '{trigger_event}' signal",
            old_value=old_stage,
            new_value=target,
        )
    )
    reasoning = (
        f"'{trigger_event}' signal on a deal in '{old_stage}' — advanced to "
        f"'{target}'."
    )
    action = DealAction(
        deal_id=deal.id,
        action_type="stage_change",
        status="executed",
        ai_reasoning=reasoning,
        suggested_action=f"Advanced {old_stage} → {target}",
        executed_at=_now(),
    )
    db.add(action)
    await db.flush()
    logger.info("Deal %s advanced %s → %s (%s)", deal.id, old_stage, target, trigger_event)
    return action


async def flag_at_risk(db: AsyncSession, deal: Deal, reason: str) -> DealAction:
    """Flag a deal as at risk and notify its owner (best-effort)."""
    action = DealAction(
        deal_id=deal.id,
        action_type="at_risk_flag",
        status="pending",
        ai_reasoning=reason,
        suggested_action=(
            f"At-risk: {deal.title} has stalled. Re-engage the buyer or mark it lost."
        ),
    )
    db.add(action)
    await db.flush()

    try:
        from app.services.notifications import get_notification_service

        company = (
            await db.execute(select(Company).where(Company.id == deal.company_id))
        ).scalars().first()
        svc = get_notification_service()
        await svc._send_notification(
            subject=f"⚠️ Deal at risk: {deal.title}",
            body=(
                f"The Deal Progression Agent flagged '{deal.title}'"
                f"{f' ({company.name})' if company else ''} as at risk.\n\n{reason}\n\n"
                "Open Landline to re-engage or update the deal."
            ),
        )
    except Exception as exc:
        logger.warning("at-risk notification failed for deal %s: %s", deal.id, exc)

    return action


async def escalate_to_manager(db: AsyncSession, deal: Deal, reason: str) -> DealAction:
    """Escalate a rotting deal to the org manager via a notification."""
    action = DealAction(
        deal_id=deal.id,
        action_type="escalation",
        status="pending",
        ai_reasoning=reason,
        suggested_action=(
            f"Escalated to manager: {deal.title} has been stalled beyond the "
            "escalation window."
        ),
    )
    db.add(action)
    await db.flush()

    try:
        from app.services.notifications import get_notification_service

        company = (
            await db.execute(select(Company).where(Company.id == deal.company_id))
        ).scalars().first()
        svc = get_notification_service()
        await svc._send_notification(
            subject=f"🚨 Deal escalation: {deal.title}",
            body=(
                f"The Deal Progression Agent escalated '{deal.title}'"
                f"{f' ({company.name})' if company else ''} to management.\n\n{reason}\n\n"
                "This deal has been stalled beyond the escalation threshold — it "
                "needs a manager's attention."
            ),
        )
    except Exception as exc:
        logger.warning("escalation notification failed for deal %s: %s", deal.id, exc)

    return action


# --- The scan -----------------------------------------------------------------

async def scan_stale_deals(db: AsyncSession) -> dict:
    """Scan the org's open pipeline and record an action per stalled deal.

    For every ``pending`` deal whose time-since-last-activity has passed the
    threshold for its stage, pick the right response by severity:

    * past the escalation window → escalate to the manager;
    * well past due (≥ 2× the stage threshold) → flag at risk;
    * otherwise → draft a follow-up (if enabled and under the per-deal cap), else
      an AI next-step suggestion.

    Deals that already have an open action are skipped so the queue never nags.
    Returns a summary dict for the caller to log.
    """
    config = await get_or_create_config(db)
    summary = {
        "scanned": 0,
        "stale": 0,
        "followups": 0,
        "suggestions": 0,
        "at_risk": 0,
        "escalations": 0,
        "skipped_existing": 0,
    }
    if not config.is_active:
        summary["inactive"] = True
        return summary

    now = _now()
    deals = (
        await db.execute(select(Deal).where(Deal.status == "pending"))
    ).scalars().all()
    summary["scanned"] = len(deals)

    for deal in deals:
        last_act = await last_activity_at(db, deal)
        days_stale = _days_since(last_act, now)
        threshold = _threshold_for_stage(config, deal.deal_stage)
        if days_stale < threshold:
            continue
        summary["stale"] += 1

        open_types = await _open_action_types(db, deal.id)
        if open_types:
            summary["skipped_existing"] += 1
            continue

        reason = (
            f"No activity for {days_stale} days on a '{deal.deal_stage}' deal "
            f"(threshold {threshold}d)."
        )

        if days_stale >= config.escalation_threshold_days:
            analysis = await analyze_deal(db, deal, days_stale, threshold, config)
            await escalate_to_manager(db, deal, analysis)
            summary["escalations"] += 1
            continue

        if days_stale >= threshold * 2:
            analysis = await analyze_deal(db, deal, days_stale, threshold, config)
            await flag_at_risk(db, deal, analysis)
            summary["at_risk"] += 1
            continue

        if config.auto_followup_enabled and (
            await _auto_followup_count(db, deal.id) < config.max_auto_followups_per_deal
        ):
            contact = await _primary_contact(db, deal.company_id)
            draft = await generate_followup(db, deal, contact, last_act, config)
            db.add(
                DealAction(
                    deal_id=deal.id,
                    action_type="auto_followup",
                    status="pending",
                    ai_reasoning=reason,
                    email_subject=draft["subject"],
                    email_body=draft["body"],
                    suggested_action=(
                        f"Send a follow-up to "
                        f"{contact.email if contact and contact.email else 'the primary contact'}."
                    ),
                )
            )
            await db.flush()
            summary["followups"] += 1
            continue

        suggestion = await suggest_next_action(db, deal, config)
        db.add(
            DealAction(
                deal_id=deal.id,
                action_type="suggestion",
                status="pending",
                ai_reasoning=reason,
                suggested_action=suggestion,
            )
        )
        await db.flush()
        summary["suggestions"] += 1

    return summary


# --- Action lifecycle ---------------------------------------------------------

async def execute_action(db: AsyncSession, action: DealAction) -> dict:
    """Perform an approved/pending action, marking it executed.

    * ``auto_followup`` → send the drafted email to the deal's primary contact.
    * ``stage_change`` → already applied at creation; just mark executed.
    * everything else → advisory; marking executed is the human acknowledging it.

    Returns a small result dict describing what happened.
    """
    result: dict = {"action_type": action.action_type}

    if action.action_type == "auto_followup":
        deal = (
            await db.execute(select(Deal).where(Deal.id == action.deal_id))
        ).scalars().first()
        contact = (
            await _primary_contact(db, deal.company_id) if deal is not None else None
        )
        if contact is None or not contact.email:
            result["status"] = "skipped"
            result["reason"] = "no contact email on file"
            action.status = "executed"
            action.executed_at = _now()
            await db.flush()
            return result

        from app.services.email_dispatch import send_outreach_email

        body_html = (action.email_body or "").replace("\n", "<br>")
        send_result = await send_outreach_email(
            db=db,
            to_email=contact.email,
            subject=action.email_subject or f"Re: {deal.title}",
            body_html=body_html,
            body_text=action.email_body,
        )
        result.update(send_result)
        status = send_result.get("status")

        if status != "sent":
            # ``executed`` was written here unconditionally, so a follow-up the
            # dispatcher *declined* was retired as though it had gone out — and
            # the hourly sweep, whose whole job is to send `approved`
            # follow-ups, never looked at it again. That was survivable while
            # a decline meant a warm-up hold; it stopped being survivable when
            # the daily cap started declining routinely, because the actions
            # dropped are exactly the ones a busy day produces.
            #
            # ``suppressed`` is the one decline that will never clear: the
            # recipient asked not to be contacted, so retrying it hourly is a
            # queue that cannot drain and a gate re-answering the same question
            # forever. That is what ``expired`` is for. Everything else — a
            # spent cap, a warm-up hold, an engaged kill switch, a transport
            # error — is a *later* yes, so the action stays `approved` and the
            # next sweep picks it up once there is allowance to send it with.
            terminal = status == "suppressed"
            if terminal:
                action.status = "expired"
                db.add(
                    DealActivity(
                        deal_id=deal.id,
                        activity_type="followup_skipped",
                        description=(
                            "Deal Progression Agent dropped a follow-up to "
                            f"{contact.email}: on the do-not-contact list"
                        ),
                        new_value=status,
                    )
                )
            logger.info(
                "Deal follow-up to %s not sent (%s): %s",
                contact.email, status, send_result.get("reason") or send_result.get("detail"),
            )
            await db.flush()
            return result

        # Only a real send goes on the deal timeline. Writing one per attempt
        # would put an hourly "sent a follow-up" entry under a deal nobody has
        # emailed, for as long as the cap holds.
        db.add(
            DealActivity(
                deal_id=deal.id,
                activity_type="followup_sent",
                description=f"Deal Progression Agent sent a follow-up to {contact.email}",
                new_value=status,
            )
        )
        action.status = "executed"
        action.executed_at = _now()
        await db.flush()
        return result

    # stage_change / suggestion / at_risk_flag / escalation: no side-effect to
    # perform here (stage_change already applied; the rest are advisory).
    action.status = "executed"
    action.executed_at = _now()
    await db.flush()
    result["status"] = "executed"
    return result


async def approve_action(db: AsyncSession, action: DealAction) -> dict:
    """Approve an action.

    Auto-follow-ups are executed immediately (the email goes out). The other
    action types are advisory, so approval marks them executed.
    """
    if action.status not in ("pending", "approved"):
        return {"status": action.status, "noop": True}
    action.status = "approved"
    await db.flush()
    return await execute_action(db, action)


async def dismiss_action(db: AsyncSession, action: DealAction) -> DealAction:
    """Dismiss a pending action — the human rejected the agent's suggestion."""
    if action.status in ("pending", "approved"):
        action.status = "dismissed"
        action.dismissed_at = _now()
        await db.flush()
    return action


# --- Stats --------------------------------------------------------------------

async def compute_stats(db: AsyncSession) -> dict:
    """Aggregate counts for the dashboard, scoped to the current org."""
    rows = (
        await db.execute(
            select(DealAction.action_type, DealAction.status, func.count(DealAction.id))
            .group_by(DealAction.action_type, DealAction.status)
        )
    ).all()

    by_type: dict[str, int] = {}
    by_type_status: dict[tuple[str, str], int] = {}
    pending = 0
    for atype, status, count in rows:
        by_type[atype] = by_type.get(atype, 0) + count
        by_type_status[(atype, status)] = count
        if status in ("pending", "approved"):
            pending += count

    followups_sent = by_type_status.get(("auto_followup", "executed"), 0)
    stage_advances = by_type.get("stage_change", 0)
    # "at risk" = open at-risk flags + open escalations (deals needing eyes now).
    at_risk_count = (
        by_type_status.get(("at_risk_flag", "pending"), 0)
        + by_type_status.get(("at_risk_flag", "approved"), 0)
        + by_type_status.get(("escalation", "pending"), 0)
        + by_type_status.get(("escalation", "approved"), 0)
    )

    return {
        "deals_nudged": sum(by_type.values()),
        "followups_sent": followups_sent,
        "stage_advances": stage_advances,
        "at_risk_count": at_risk_count,
        "pending_actions": pending,
        "suggestions": by_type.get("suggestion", 0),
        "escalations": by_type.get("escalation", 0),
    }
