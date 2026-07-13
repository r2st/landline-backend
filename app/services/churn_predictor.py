"""Predictive churn scoring for existing clients (feature 3.2).

Turns the reactive health score into a forward-looking churn probability by
reading the *trajectory* of a client's health snapshots plus current engagement
signals. The core is a transparent, deterministic heuristic (fully testable
offline); an optional Claude pass narrates *why* a client is at risk.
"""

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.services.health import compute_company_health, get_health_history

logger = logging.getLogger(__name__)


class ChurnPredictor:
    async def predict(
        self, db: AsyncSession, company: Company, use_ai: bool = False
    ) -> dict:
        """Return {probability (0..1), risk_level, risk_factors: [...], summary}.

        Signals & weights (each contributes to the probability):
          * low current health score
          * downward health trend across snapshots
          * growing time since last contact
          * poor email responsiveness
          * loss of meeting cadence
        """
        health = await compute_company_health(db, company)
        history = await get_health_history(db, company.id, limit=6)

        factors: list[dict] = []
        prob = 0.0

        # 1) Absolute current health (inverted). A 100 score adds ~0, a 0 adds ~0.35.
        score_component = round((100 - health.score) / 100 * 0.35, 3)
        if score_component > 0.05:
            factors.append({
                "factor": "low_health_score",
                "weight": score_component,
                "detail": f"current health score is {health.score}/100 ({health.health_status})",
            })
        prob += score_component

        # 2) Trend: compare current score to the oldest snapshot in the window.
        if len(history) >= 2:
            # history is most-recent-first.
            newest = history[0].score
            oldest = history[-1].score
            drop = oldest - newest
            if drop >= 10:
                trend_component = round(min(drop / 100, 0.5) * 0.6, 3)
                prob += trend_component
                factors.append({
                    "factor": "declining_health_trend",
                    "weight": trend_component,
                    "detail": f"health fell {drop} points across the last {len(history)} snapshots",
                })

        # 3) Days since last contact.
        dslc = health.signals.days_since_last_contact
        if dslc is None or dslc > 45:
            recency_component = 0.2 if dslc is None else round(min((dslc - 45) / 90, 1.0) * 0.2, 3)
            recency_component = max(recency_component, 0.1)
            prob += recency_component
            detail = "never contacted" if dslc is None else f"{dslc} days since last contact"
            factors.append({
                "factor": "contact_gap",
                "weight": recency_component,
                "detail": detail,
            })

        # 4) Responsiveness.
        if health.signals.emails_sent >= 3 and health.signals.response_rate < 0.15:
            factors.append({
                "factor": "low_responsiveness",
                "weight": 0.15,
                "detail": f"response rate {round(health.signals.response_rate * 100)}% over {health.signals.emails_sent} emails",
            })
            prob += 0.15

        # 5) Meeting cadence loss.
        if health.signals.meetings_last_90d == 0:
            factors.append({
                "factor": "no_recent_meetings",
                "weight": 0.1,
                "detail": "no meetings logged in the last 90 days",
            })
            prob += 0.1

        prob = round(max(0.0, min(1.0, prob)), 3)
        risk_level = "high" if prob >= 0.6 else "medium" if prob >= 0.3 else "low"
        # Rank factors by contribution.
        factors.sort(key=lambda f: f["weight"], reverse=True)

        summary = self._heuristic_summary(company, prob, risk_level, factors)
        if use_ai and factors:
            ai_summary = await self._ai_summary(company, factors)
            if ai_summary:
                summary = ai_summary

        return {
            "probability": prob,
            "risk_level": risk_level,
            "risk_factors": factors,
            "summary": summary,
            "health_score": health.score,
            "health_status": health.health_status,
        }

    def _heuristic_summary(self, company: Company, prob: float, level: str, factors: list[dict]) -> str:
        if not factors:
            return f"{company.name} shows no churn-risk signals."
        top = ", ".join(f["factor"].replace("_", " ") for f in factors[:3])
        return f"{company.name} is {level} churn risk ({round(prob * 100)}%). Key drivers: {top}."

    async def _ai_summary(self, company, factors: list[dict]) -> Optional[str]:
        try:
            from app.services.ai_service import get_ai_service

            drivers = "; ".join(f"{f['factor']}: {f['detail']}" for f in factors)
            prompt = (
                f"A B2B manufacturing-software client, {company.name}, has these churn-risk "
                f"signals: {drivers}. In 2 sentences, explain what's likely happening and the single "
                f"highest-leverage save action a rep should take this week. Be concrete, no fluff."
            )
            text = await get_ai_service().complete_text(prompt, max_tokens=200)
            return text.strip() or None
        except Exception as e:  # pragma: no cover
            logger.warning("Churn AI summary failed: %s", e)
            return None


_predictor: Optional[ChurnPredictor] = None


def get_churn_predictor() -> ChurnPredictor:
    global _predictor
    if _predictor is None:
        _predictor = ChurnPredictor()
    return _predictor
