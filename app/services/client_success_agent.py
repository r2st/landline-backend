"""Client Success Agent service.

The brains behind the autonomous Client Success Agent. It watches per-client
health (see :mod:`app.services.health`) and, when a client's score crosses the
org's configured thresholds, drafts a personalized play:

* **check-in** — a warm, activity-aware email when health slips below the
  warning threshold;
* **escalation** — a manager-facing summary when health is critical;
* **review_call** — a suggestion to book a review call alongside an escalation;
* **celebration** — a congratulatory note when a client bounces back to thriving.

Every AI generation routes through **OpenRouter** using the *agent's own*
``llm_model`` and ``llm_api_key``. Key resolution is strict and per-tenant:

1. the agent's encrypted ``llm_api_key`` (decrypted here) if set, else
2. the org-level OpenRouter key in ``system_settings`` (key
   ``openrouter_api_key``), else
3. the process-wide ``OPENROUTER_API_KEY`` from settings.

No hardcoded model or key is ever used — the resolved model/key are handed to a
per-call :class:`~app.services.ai_service.AIService` instance.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.client_health import ClientHealthSnapshot
from app.models.client_success_agent import (
    ClientSuccessAction,
    ClientSuccessAgent,
)
from app.models.company import Company
from app.models.contact import Contact
from app.models.system_setting import SystemSetting
from app.services.ai_service import AIService
from app.services.crypto import TokenCryptoError, decrypt, encrypt
from app.services.health import compute_company_health

logger = logging.getLogger(__name__)

# Companies in this pipeline stage are the agent's "clients".
CLIENT_STAGE = "client"
# A client is "thriving" at/above this score; crossing up into it from below is
# the celebration milestone.
CELEBRATION_SCORE = 80
# The org-level OpenRouter key lives under this system_settings key, shape
# ``{"api_key": "sk-or-..."}``.
ORG_OPENROUTER_SETTING_KEY = "openrouter_api_key"


class ClientSuccessAgentService:
    """Stateless orchestrator; every method takes the session to act within."""

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    async def get_or_create_config(self, db: AsyncSession) -> ClientSuccessAgent:
        """Return the current org's agent config, creating a default row if none.

        Reads are org-filtered globally (see :mod:`app.models.mixins`), so this
        finds *this* org's row; the insert is stamped with the current org by the
        before_flush hook. Callers run inside a bound org context (request or
        ``for_each_org``).
        """
        config = (
            await db.execute(select(ClientSuccessAgent).limit(1))
        ).scalar_one_or_none()
        if config is None:
            config = ClientSuccessAgent()
            db.add(config)
            await db.flush()
            await db.refresh(config)
        return config

    # ------------------------------------------------------------------
    # AI backend resolution
    # ------------------------------------------------------------------
    async def resolve_api_key(
        self, db: AsyncSession, agent: ClientSuccessAgent
    ) -> Optional[str]:
        """Resolve the OpenRouter key for *agent*: per-agent → org → global.

        Returns ``None`` only when no key is configured at any layer (the caller
        then treats AI as unavailable and degrades gracefully).
        """
        # 1. Per-agent key (encrypted at rest).
        if agent.llm_api_key:
            try:
                return decrypt(agent.llm_api_key)
            except TokenCryptoError:
                logger.error(
                    "Client Success Agent %s: stored llm_api_key could not be "
                    "decrypted; falling back to org/global key.",
                    agent.id,
                )

        # 2. Org-level OpenRouter key from system_settings.
        setting = (
            await db.execute(
                select(SystemSetting).where(
                    SystemSetting.key == ORG_OPENROUTER_SETTING_KEY
                )
            )
        ).scalar_one_or_none()
        if setting and isinstance(setting.value, dict):
            org_key = setting.value.get("api_key")
            if org_key:
                return org_key

        # 3. Process-wide fallback.
        return get_settings().openrouter_api_key or None

    async def _ai_for(
        self, db: AsyncSession, agent: ClientSuccessAgent
    ) -> AIService:
        """Build an :class:`AIService` bound to the agent's model + resolved key.

        Always uses the agent's ``llm_model`` and the resolved OpenRouter key —
        never a hardcoded model/key.
        """
        api_key = await self.resolve_api_key(db, agent)
        return AIService(openrouter_model=agent.llm_model, openrouter_api_key=api_key)

    # ------------------------------------------------------------------
    # AI generations
    # ------------------------------------------------------------------
    async def generate_checkin(
        self,
        db: AsyncSession,
        agent: ClientSuccessAgent,
        contact: Optional[Contact],
        health_data: dict,
        recent_activity: str,
    ) -> str:
        """AI-draft a personalized check-in email body for a slipping client."""
        ai = await self._ai_for(db, agent)
        name = _display_name(contact)
        company = health_data.get("company_name") or "your team"
        guidance = agent.checkin_template or _DEFAULT_CHECKIN_GUIDANCE
        prompt = f"""You are a customer-success manager writing a short, warm check-in email to a client whose engagement has cooled off.

Client contact: {name}
Company: {company}
Current health score: {health_data.get('score')} / 100 ({health_data.get('health_status')})
Recent activity: {recent_activity}

GUIDANCE: {guidance}

Write a genuine, low-pressure check-in. 4-6 sentences. Reference that it's been a while, offer specific help, and end with one easy question that invites a reply. No corporate filler, no "I hope this email finds you well". Sound like a real person who cares that they succeed.

Return ONLY the email body text (no subject line, no JSON)."""
        message = await ai.complete_text(prompt, max_tokens=400)
        return message.strip() or _fallback_checkin(name, recent_activity)

    async def generate_celebration(
        self,
        db: AsyncSession,
        agent: ClientSuccessAgent,
        contact: Optional[Contact],
        milestone: str,
    ) -> str:
        """AI-draft a congratulatory message for a client milestone/win."""
        ai = await self._ai_for(db, agent)
        name = _display_name(contact)
        guidance = agent.celebration_template or _DEFAULT_CELEBRATION_GUIDANCE
        prompt = f"""You are a customer-success manager sending a short, genuine congratulations to a client who just hit a milestone.

Client contact: {name}
Milestone: {milestone}

GUIDANCE: {guidance}

Write a brief, upbeat note (2-4 sentences) celebrating this with them. Specific, warm, no salesy CTA. Just make them feel appreciated.

Return ONLY the message body text (no subject line, no JSON)."""
        message = await ai.complete_text(prompt, max_tokens=250)
        return message.strip() or _fallback_celebration(name, milestone)

    async def generate_escalation_summary(
        self,
        db: AsyncSession,
        agent: ClientSuccessAgent,
        contact: Optional[Contact],
        health_data: dict,
        reason: str,
    ) -> str:
        """AI-draft an internal, manager-facing escalation brief.

        This is NOT sent to the client — it's a concise summary for whoever owns
        the account so they can intervene.
        """
        ai = await self._ai_for(db, agent)
        name = _display_name(contact)
        company = health_data.get("company_name") or "the client"
        guidance = agent.escalation_template or _DEFAULT_ESCALATION_GUIDANCE
        prompt = f"""You are a customer-success agent escalating an at-risk client to a manager for intervention. Write an INTERNAL brief (not sent to the client).

Client: {name} at {company}
Health score: {health_data.get('score')} / 100 ({health_data.get('health_status')})
Why escalating: {reason}

GUIDANCE: {guidance}

Write a tight internal summary (3-5 sentences): the risk, the likely cause from the signals, and a recommended next step for the manager. Be direct and actionable.

Return ONLY the brief text (no JSON)."""
        message = await ai.complete_text(prompt, max_tokens=350)
        return message.strip() or f"Escalation: {name} at {company} is at risk. {reason}"

    # ------------------------------------------------------------------
    # Action builders (persist a ClientSuccessAction row)
    # ------------------------------------------------------------------
    def _new_action(
        self,
        *,
        action_type: str,
        company: Optional[Company],
        contact: Optional[Contact],
        trigger_reason: str,
        score: Optional[int],
        ai_message: Optional[str],
    ) -> ClientSuccessAction:
        return ClientSuccessAction(
            action_type=action_type,
            company_id=company.id if company else None,
            contact_id=contact.id if contact else None,
            trigger_reason=trigger_reason,
            health_score_at_trigger=score,
            ai_message=ai_message,
            status="pending",
        )

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------
    async def scan_client_health(
        self, db: AsyncSession, *, limit: int = 500
    ) -> dict:
        """Scan the current org's clients and enqueue actions for those that need one.

        For each client (company in the ``client`` stage) it computes current
        health, compares it to the previous snapshot, and — respecting the
        config's thresholds, auto-toggles, and per-client debounce window —
        creates at most one new pending action per client per scan.

        Runs inside a bound org context. Returns a small summary dict. The
        caller commits.
        """
        agent = await self.get_or_create_config(db)
        if not agent.is_active:
            return {"active": False, "clients_scanned": 0, "actions_created": 0}

        clients = list(
            (
                await db.execute(
                    select(Company)
                    .where(Company.pipeline_stage == CLIENT_STAGE)
                    .limit(limit)
                )
            ).scalars().all()
        )

        created = 0
        counts = {"checkin": 0, "escalation": 0, "celebration": 0, "review_call": 0}
        for company in clients:
            try:
                actions = await self._evaluate_client(db, agent, company)
                for action in actions:
                    db.add(action)
                    created += 1
                    counts[action.action_type] = counts.get(action.action_type, 0) + 1
            except Exception:
                logger.exception(
                    "Client Success scan failed for company %s", company.id
                )
        await db.flush()
        return {
            "active": True,
            "clients_scanned": len(clients),
            "actions_created": created,
            "by_type": counts,
        }

    async def execute_pending_checkins(
        self, db: AsyncSession, *, limit: int = 200
    ) -> dict:
        """Mark this org's approved auto check-ins as sent.

        A pending ``checkin`` in an active org that has ``auto_checkin_enabled``
        is already approved-by-config (the scan only enqueues opted-in auto
        plays), so this flips it to ``sent`` and stamps ``executed_at``. Other
        action types stay pending for a human. Returns ``{"sent": n}``. The
        caller commits.
        """
        agent = (
            await db.execute(select(ClientSuccessAgent).limit(1))
        ).scalar_one_or_none()
        if agent is None or not agent.is_active or not agent.auto_checkin_enabled:
            return {"sent": 0}

        pending = list(
            (
                await db.execute(
                    select(ClientSuccessAction)
                    .where(
                        ClientSuccessAction.action_type == "checkin",
                        ClientSuccessAction.status == "pending",
                    )
                    .order_by(ClientSuccessAction.created_at.asc())
                    .limit(limit)
                )
            ).scalars().all()
        )
        now = datetime.now(timezone.utc)
        for action in pending:
            action.status = "sent"
            action.executed_at = now
        await db.flush()
        return {"sent": len(pending)}

    async def _evaluate_client(
        self, db: AsyncSession, agent: ClientSuccessAgent, company: Company
    ) -> list[ClientSuccessAction]:
        """Decide what (if any) action a single client warrants right now."""
        health = await compute_company_health(db, company)
        score = health.score
        health_data = {
            "score": score,
            "health_status": health.health_status,
            "company_name": company.name,
        }
        contact = await self._primary_contact(db, company.id)
        recent_activity = _describe_activity(health)
        previous_score = await self._previous_score(db, company.id)

        actions: list[ClientSuccessAction] = []

        # --- Celebration: bounced back into "thriving" territory. -------------
        if (
            agent.celebration_enabled
            and score >= CELEBRATION_SCORE
            and previous_score is not None
            and previous_score < CELEBRATION_SCORE
        ):
            if not await self._recently_actioned(db, company.id, "celebration", agent):
                milestone = (
                    f"Health score climbed to {score}/100 — back to thriving "
                    f"(up from {previous_score})."
                )
                msg = await self.generate_celebration(db, agent, contact, milestone)
                actions.append(
                    self._new_action(
                        action_type="celebration",
                        company=company,
                        contact=contact,
                        trigger_reason=milestone,
                        score=score,
                        ai_message=msg,
                    )
                )
            # A win and a risk are mutually exclusive this scan.
            return actions

        # --- Critical: escalate + suggest a review call. ----------------------
        if score <= agent.health_critical_threshold:
            reason = (
                f"Health is critical at {score}/100 "
                f"(≤ {agent.health_critical_threshold}). {recent_activity}"
            )
            if agent.auto_escalation_enabled and not await self._recently_actioned(
                db, company.id, "escalation", agent
            ):
                summary = await self.generate_escalation_summary(
                    db, agent, contact, health_data, reason
                )
                actions.append(
                    self._new_action(
                        action_type="escalation",
                        company=company,
                        contact=contact,
                        trigger_reason=reason,
                        score=score,
                        ai_message=summary,
                    )
                )
                # Pair the escalation with a review-call suggestion (debounced
                # on its own type so it isn't re-suggested every scan).
                if not await self._recently_actioned(
                    db, company.id, "review_call", agent
                ):
                    actions.append(
                        self._new_action(
                            action_type="review_call",
                            company=company,
                            contact=contact,
                            trigger_reason=(
                                "Critical health — book a review call to intervene."
                            ),
                            score=score,
                            ai_message=None,
                        )
                    )
            return actions

        # --- Warning: draft a check-in. ---------------------------------------
        if score <= agent.health_warning_threshold:
            if agent.auto_checkin_enabled and not await self._recently_actioned(
                db, company.id, "checkin", agent
            ):
                reason = (
                    f"Health slipped to {score}/100 "
                    f"(≤ {agent.health_warning_threshold}). {recent_activity}"
                )
                msg = await self.generate_checkin(
                    db, agent, contact, health_data, recent_activity
                )
                actions.append(
                    self._new_action(
                        action_type="checkin",
                        company=company,
                        contact=contact,
                        trigger_reason=reason,
                        score=score,
                        ai_message=msg,
                    )
                )
        return actions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _primary_contact(
        self, db: AsyncSession, company_id: uuid.UUID
    ) -> Optional[Contact]:
        """The primary contact for a company, else any contact, else None."""
        stmt = (
            select(Contact)
            .where(Contact.company_id == company_id)
            .order_by(Contact.is_primary.desc(), Contact.created_at.asc())
            .limit(1)
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def _previous_score(
        self, db: AsyncSession, company_id: uuid.UUID
    ) -> Optional[int]:
        """The score from this company's most recent health snapshot, if any."""
        stmt = (
            select(ClientHealthSnapshot.score)
            .where(ClientHealthSnapshot.company_id == company_id)
            .order_by(ClientHealthSnapshot.recorded_at.desc())
            .limit(1)
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def _recently_actioned(
        self,
        db: AsyncSession,
        company_id: uuid.UUID,
        action_type: str,
        agent: ClientSuccessAgent,
    ) -> bool:
        """True if an action of this type was created for the company within the
        agent's ``check_frequency_days`` window — the debounce that stops the
        agent nagging a persistently-low client every scan."""
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=max(1, agent.check_frequency_days)
        )
        count = (
            await db.execute(
                select(func.count(ClientSuccessAction.id)).where(
                    ClientSuccessAction.company_id == company_id,
                    ClientSuccessAction.action_type == action_type,
                    ClientSuccessAction.created_at >= cutoff,
                )
            )
        ).scalar_one()
        return bool(count)


# --- Default AI guidance + graceful fallbacks --------------------------------

_DEFAULT_CHECKIN_GUIDANCE = (
    "Be warm and human. Acknowledge the gap in contact without guilt-tripping. "
    "Offer concrete help and make replying effortless."
)
_DEFAULT_CELEBRATION_GUIDANCE = (
    "Be genuinely happy for them. Keep it short and personal, no upsell."
)
_DEFAULT_ESCALATION_GUIDANCE = (
    "Be concise and factual for an internal reader. Lead with the risk and the "
    "recommended intervention."
)


def _display_name(contact: Optional[Contact]) -> str:
    if contact is None:
        return "there"
    parts = [contact.first_name, contact.last_name]
    name = " ".join(p for p in parts if p).strip()
    return name or (contact.email or "there")


def _describe_activity(health) -> str:
    """One-line, human summary of the activity signals behind a health score."""
    signals = health.signals
    bits = []
    if signals.days_since_last_contact is not None:
        bits.append(f"last contact {signals.days_since_last_contact} days ago")
    else:
        bits.append("no recorded contact")
    bits.append(f"reply rate {round(signals.response_rate * 100)}%")
    bits.append(f"{signals.meetings_last_90d} meeting(s) in 90 days")
    return "; ".join(bits) + "."


def _fallback_checkin(name: str, recent_activity: str) -> str:
    return (
        f"Hi {name}, it's been a while since we connected and I wanted to check "
        f"in. I noticed {recent_activity} Is there anything we can help with "
        f"right now? Happy to jump on a quick call whenever works for you."
    )


def _fallback_celebration(name: str, milestone: str) -> str:
    return (
        f"Hi {name}, just wanted to say congratulations — {milestone} "
        f"It's great to see, and we're glad to be part of it."
    )


_service: Optional[ClientSuccessAgentService] = None


def get_client_success_agent_service() -> ClientSuccessAgentService:
    global _service
    if _service is None:
        _service = ClientSuccessAgentService()
    return _service


def encrypt_api_key(raw: str) -> str:
    """Encrypt a raw OpenRouter key for storage (thin re-export for the router)."""
    return encrypt(raw)
