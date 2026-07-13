"""Expansion-opportunity detection for existing clients (feature 3.2).

Surfaces upsell / cross-sell / referral potential by combining account-health
strength with growth signals: new stakeholders appearing, active expansion /
hiring / funding intent signals, strong engagement, and won-deal footprint.
Deterministic core; optional Claude pass drafts the recommended play.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.intent_signal import IntentSignal
from app.services.health import compute_company_health

logger = logging.getLogger(__name__)

_EXPANSION_SIGNAL_TYPES = ("expansion", "hiring", "funding", "m&a")


class ExpansionDetector:
    async def detect(
        self, db: AsyncSession, company: Company, use_ai: bool = False
    ) -> dict:
        """Return {expansion_score (0-100), signals: [...], recommended_play, ready}."""
        now = datetime.now(timezone.utc)
        ninety_days_ago = now - timedelta(days=90)
        health = await compute_company_health(db, company)

        signals: list[dict] = []
        score = 0

        # 1) Healthy, engaged account is a prerequisite for expansion.
        if health.health_status == "healthy":
            score += 30
            signals.append({"type": "account_healthy", "detail": f"health {health.score}/100", "weight": 30})
        elif health.health_status == "at_risk":
            score += 5

        if health.signals.response_rate >= 0.4 and health.signals.emails_sent >= 2:
            score += 15
            signals.append({
                "type": "high_engagement",
                "detail": f"{round(health.signals.response_rate * 100)}% email response rate",
                "weight": 15,
            })

        # 2) New stakeholders appearing (org growing / more entry points).
        new_contacts = (
            await db.execute(
                select(func.count(Contact.id)).where(
                    Contact.company_id == company.id,
                    Contact.created_at >= ninety_days_ago,
                )
            )
        ).scalar() or 0
        if new_contacts >= 1:
            pts = min(20, 8 * new_contacts)
            score += pts
            signals.append({
                "type": "new_stakeholders",
                "detail": f"{new_contacts} new contact(s) added in the last 90 days",
                "weight": pts,
            })

        # 3) External growth intent signals.
        intent_rows = (
            await db.execute(
                select(IntentSignal).where(
                    IntentSignal.company_id == company.id,
                    IntentSignal.signal_type.in_(_EXPANSION_SIGNAL_TYPES),
                )
            )
        ).scalars().all()
        active_growth = [s for s in intent_rows if s.is_active(now)]
        if active_growth:
            pts = min(25, 12 * len(active_growth))
            score += pts
            top = active_growth[0]
            signals.append({
                "type": "growth_intent_signal",
                "detail": f"{len(active_growth)} active growth signal(s), e.g. {top.signal_type}: {top.title}",
                "weight": pts,
            })

        # 4) Won-deal footprint (proven buyer → cross-sell surface).
        won_row = (
            await db.execute(
                select(func.count(Deal.id), func.coalesce(func.sum(Deal.value), 0)).where(
                    Deal.company_id == company.id, Deal.status == "won"
                )
            )
        ).one()
        won_count = won_row[0] or 0
        if won_count >= 1:
            score += 10
            signals.append({
                "type": "proven_buyer",
                "detail": f"{won_count} won deal(s) totaling ${float(won_row[1] or 0):,.0f}",
                "weight": 10,
            })

        score = max(0, min(100, score))
        ready = score >= 50 and health.health_status != "churned"

        recommended_play = self._heuristic_play(company, score, signals)
        if use_ai and ready and signals:
            ai_play = await self._ai_play(company, signals)
            if ai_play:
                recommended_play = ai_play

        return {
            "expansion_score": score,
            "signals": signals,
            "recommended_play": recommended_play,
            "ready": ready,
            "health_status": health.health_status,
        }

    def _heuristic_play(self, company: Company, score: int, signals: list[dict]) -> str:
        if score < 50:
            return f"No strong expansion signal for {company.name} yet — keep nurturing."
        types = {s["type"] for s in signals}
        if "growth_intent_signal" in types:
            return f"{company.name} is growing — propose expanded seats/modules tied to their expansion."
        if "new_stakeholders" in types:
            return f"New stakeholders at {company.name} — run a value review and multithread the account."
        return f"{company.name} is a healthy, proven buyer — introduce an adjacent module or ask for a referral."

    async def _ai_play(self, company, signals: list[dict]) -> Optional[str]:
        try:
            from app.services.ai_service import get_ai_service

            sig = "; ".join(f"{s['type']}: {s['detail']}" for s in signals)
            prompt = (
                f"An existing manufacturing-software client, {company.name}, shows these expansion "
                f"signals: {sig}. In 2 sentences, recommend the single best expansion play "
                f"(upsell, cross-sell, or referral) and how to open the conversation."
            )
            text = await get_ai_service().complete_text(prompt, max_tokens=200)
            return text.strip() or None
        except Exception as e:  # pragma: no cover
            logger.warning("Expansion AI play failed: %s", e)
            return None


_detector: Optional[ExpansionDetector] = None


def get_expansion_detector() -> ExpansionDetector:
    global _detector
    if _detector is None:
        _detector = ExpansionDetector()
    return _detector
