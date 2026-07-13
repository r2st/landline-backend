"""Enhanced smart suggestions engine (Feature 1.6).

Replaces the four static rules in ``routers/suggestions.py`` with an AI-powered
recommendation engine that cross-references dozens of signals per lead —
engagement, reply sentiment, buying signals, actionable news, deal stage,
client health, overdue follow-ups, and the new propensity score (Feature
1.3) — and asks Claude to produce ranked, *explained*, time-aware
next-best-actions.

Two paths:

* **AI path** (default when an API key is configured): aggregates signals for
  the most promising active leads, batches them to Claude, and returns ranked
  recommendations with reasoning and suggested timing.
* **Deterministic fallback**: a transparent scorer over the same aggregated
  signals, used in tests / keyless deploys or when the AI call fails.

A feedback loop reads historical ``SuggestionOutcome`` rows to calibrate
priority — suggestion types that led to replies/meetings get boosted; types
that get dismissed get suppressed.
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.company_news import CompanyNews
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.client_health import ClientHealthSnapshot
from app.models.email_sentiment import EmailSentimentScore
from app.models.email_template import EmailSend
from app.models.follow_up import FollowUp
from app.models.lead_score import LeadScore
from app.models.suggestion_outcome import SuggestionOutcome

logger = logging.getLogger(__name__)

_ACTIVE_STAGES = ("researching", "outreach", "engaged", "negotiating")
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
# Outcomes that count as a "win" for calibration.
_POSITIVE_OUTCOMES = {"reply", "meeting_booked", "converted", "positive_reply"}


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def gather_lead_signals(
    db: AsyncSession, *, now: Optional[datetime] = None, max_leads: int = 40
) -> list[dict]:
    """Aggregate cross-model signals for active leads into per-company bundles.

    Returns a list of dicts (one per candidate company) sorted by a rough
    interest heuristic so the most promising leads are batched to the AI first.
    """
    now = now or datetime.now(timezone.utc)

    companies = list(
        (
            await db.execute(
                select(Company).where(Company.pipeline_stage.in_(_ACTIVE_STAGES))
            )
        ).scalars().all()
    )
    if not companies:
        return []
    company_ids = {c.id for c in companies}

    # Latest lead score per company (fit + propensity + velocity).
    latest_score_at = (
        select(LeadScore.company_id, func.max(LeadScore.scored_at).label("m"))
        .where(LeadScore.company_id.in_(company_ids))
        .group_by(LeadScore.company_id)
        .subquery()
    )
    score_rows = await db.execute(
        select(LeadScore).join(
            latest_score_at,
            (LeadScore.company_id == latest_score_at.c.company_id)
            & (LeadScore.scored_at == latest_score_at.c.m),
        )
    )
    scores = {s.company_id: s for s in score_rows.scalars().all()}

    # Engagement counts + last-contact per company.
    eng_rows = await db.execute(
        select(
            Contact.company_id,
            func.count(EmailSend.opened_at),
            func.count(EmailSend.clicked_at),
            func.count(EmailSend.replied_at),
            func.max(EmailSend.sent_at),
        )
        .join(Contact, Contact.id == EmailSend.contact_id)
        .where(Contact.company_id.in_(company_ids))
        .group_by(Contact.company_id)
    )
    eng = {
        cid: {"opens": int(o or 0), "clicks": int(c or 0), "replies": int(r or 0), "last_sent_at": _aware(last)}
        for cid, o, c, r, last in eng_rows.all()
    }

    # Latest sentiment per company.
    latest_sent_at = (
        select(EmailSentimentScore.company_id, func.max(EmailSentimentScore.created_at).label("m"))
        .where(EmailSentimentScore.company_id.in_(company_ids))
        .group_by(EmailSentimentScore.company_id)
        .subquery()
    )
    sent_rows = await db.execute(
        select(EmailSentimentScore).join(
            latest_sent_at,
            (EmailSentimentScore.company_id == latest_sent_at.c.company_id)
            & (EmailSentimentScore.created_at == latest_sent_at.c.m),
        )
    )
    sentiments = {s.company_id: s for s in sent_rows.scalars().all()}

    # Actionable, un-actioned news per company (latest title).
    news_rows = await db.execute(
        select(CompanyNews).where(
            CompanyNews.company_id.in_(company_ids),
            CompanyNews.is_actionable.is_(True),
            CompanyNews.actioned.is_(False),
        )
    )
    news: dict = defaultdict(list)
    for n in news_rows.scalars().all():
        news[n.company_id].append(n)

    # Overdue follow-up counts per company.
    fu_rows = await db.execute(
        select(FollowUp.company_id, func.count(FollowUp.id))
        .where(FollowUp.company_id.in_(company_ids), FollowUp.status == "pending")
        .group_by(FollowUp.company_id)
    )
    followups = {cid: int(cnt or 0) for cid, cnt in fu_rows.all()}

    # Latest open deal per company.
    deal_rows = await db.execute(
        select(Deal).where(Deal.company_id.in_(company_ids), Deal.status == "pending")
    )
    deals: dict = {}
    for d in deal_rows.scalars().all():
        cur = deals.get(d.company_id)
        if cur is None or _aware(d.updated_at) > _aware(cur.updated_at):
            deals[d.company_id] = d

    # Latest client-health per company.
    latest_health_at = (
        select(ClientHealthSnapshot.company_id, func.max(ClientHealthSnapshot.recorded_at).label("m"))
        .where(ClientHealthSnapshot.company_id.in_(company_ids))
        .group_by(ClientHealthSnapshot.company_id)
        .subquery()
    )
    health_rows = await db.execute(
        select(ClientHealthSnapshot).join(
            latest_health_at,
            (ClientHealthSnapshot.company_id == latest_health_at.c.company_id)
            & (ClientHealthSnapshot.recorded_at == latest_health_at.c.m),
        )
    )
    health = {h.company_id: h for h in health_rows.scalars().all()}

    bundles: list[dict] = []
    for company in companies:
        cid = company.id
        s = scores.get(cid)
        e = eng.get(cid, {})
        sent = sentiments.get(cid)
        cnews = news.get(cid, [])
        deal = deals.get(cid)
        h = health.get(cid)
        last_sent = e.get("last_sent_at")
        days_since = (now - last_sent).days if last_sent else None

        bundle = {
            "company_id": str(cid),
            "company_name": company.name,
            "pipeline_stage": company.pipeline_stage,
            "industry": company.industry_segment or company.naics_description,
            "fit_score": s.fit_score if s else None,
            "propensity_score": s.propensity_score if s else None,
            "signal_velocity": s.signal_velocity if s else None,
            "recommended_rep_id": str(s.recommended_rep_id) if s and s.recommended_rep_id else None,
            "opens": e.get("opens", 0),
            "clicks": e.get("clicks", 0),
            "replies": e.get("replies", 0),
            "days_since_last_contact": days_since,
            "latest_intent": sent.intent if sent else None,
            "sentiment_score": sent.sentiment_score if sent else None,
            "buying_signals": (sent.buying_signals or []) if sent else [],
            "objections": (sent.objections or []) if sent else [],
            "actionable_news": [n.title for n in cnews[:2]],
            "overdue_follow_ups": followups.get(cid, 0),
            "deal_stage": deal.deal_stage if deal else None,
            "deal_value": float(deal.value) if deal and deal.value else None,
            "health_status": h.health_status if h else None,
        }
        bundles.append(bundle)

    bundles.sort(key=_interest_heuristic, reverse=True)
    return bundles[:max_leads]


def _interest_heuristic(b: dict) -> float:
    """Rough score to decide which leads are worth spending an AI slot on."""
    score = 0.0
    score += (b.get("propensity_score") or 0) * 1.0
    score += (b.get("fit_score") or 0) * 0.3
    score += (b.get("replies") or 0) * 15
    score += (b.get("clicks") or 0) * 6
    score += (b.get("overdue_follow_ups") or 0) * 8
    score += len(b.get("actionable_news") or []) * 10
    if b.get("latest_intent") in ("interested", "meeting_request", "question"):
        score += 25
    if (b.get("signal_velocity") or 0) > 0:
        score += b["signal_velocity"] * 10
    return score


async def calibration_weights(db: AsyncSession) -> dict:
    """Compute per-suggestion-type priority multipliers from historical outcomes.

    Types whose suggestions were accepted / led to positive outcomes are boosted
    (>1.0); types that were mostly dismissed are suppressed (<1.0). Neutral
    (no history) stays 1.0.
    """
    rows = await db.execute(
        select(SuggestionOutcome.suggestion_type, SuggestionOutcome.status, SuggestionOutcome.outcome)
    )
    agg: dict = defaultdict(lambda: {"total": 0, "positive": 0, "dismissed": 0})
    for stype, status, outcome in rows.all():
        a = agg[stype]
        a["total"] += 1
        if status == "dismissed":
            a["dismissed"] += 1
        if (outcome in _POSITIVE_OUTCOMES) or status in ("accepted", "completed"):
            a["positive"] += 1

    weights: dict = {}
    for stype, a in agg.items():
        if a["total"] < 3:
            weights[stype] = 1.0
            continue
        pos_rate = a["positive"] / a["total"]
        dismiss_rate = a["dismissed"] / a["total"]
        # Map to a 0.6-1.4 multiplier.
        weights[stype] = round(max(0.6, min(1.4, 1.0 + 0.4 * pos_rate - 0.4 * dismiss_rate)), 3)
    return weights


def _deterministic_suggestions(bundles: list[dict], weights: dict, limit: int) -> list[dict]:
    """Transparent fallback ranker over aggregated signals."""
    out: list[dict] = []
    for b in bundles:
        recs = _bundle_to_actions(b)
        for r in recs:
            w = weights.get(r["type"], 1.0)
            r["_rank"] = _interest_heuristic(b) * w * {"high": 1.3, "medium": 1.0, "low": 0.7}[r["priority"]]
            out.append(r)
    out.sort(key=lambda r: r["_rank"], reverse=True)
    for r in out:
        r.pop("_rank", None)
    return out[:limit]


def _bundle_to_actions(b: dict) -> list[dict]:
    """Derive one or more concrete suggestions from a signal bundle (fallback)."""
    recs: list[dict] = []
    cid = b["company_id"]
    name = b["company_name"]

    if b.get("latest_intent") in ("interested", "meeting_request"):
        recs.append(_mk(b, "hot_reply", "high", f"Book a meeting with {name}",
                        f"Their last reply read as '{b['latest_intent']}'. Strike while it's warm.",
                        "schedule_meeting", "today"))
    if (b.get("clicks") or 0) > 0 and (b.get("replies") or 0) == 0:
        recs.append(_mk(b, "engaged_no_reply", "high", f"Call {name} — they're clicking, not replying",
                        f"{b['clicks']} click(s) but no reply. A call converts warm-but-quiet leads.",
                        "call", "today"))
    if b.get("actionable_news"):
        recs.append(_mk(b, "news_trigger", "high", f"Reach out to {name} about recent news",
                        f"Actionable news: {b['actionable_news'][0]}", "reach_out", "today"))
    if (b.get("overdue_follow_ups") or 0) > 0:
        recs.append(_mk(b, "overdue_follow_up", "high", f"Clear {b['overdue_follow_ups']} overdue follow-up(s) for {name}",
                        "Overdue follow-ups are the highest-yield, lowest-effort action.", "complete_follow_up", "today"))
    if b.get("objections"):
        recs.append(_mk(b, "unresolved_objection", "medium", f"Address {name}'s objection",
                        f"Open objection: {b['objections'][0]}", "handle_objection", "this_week"))
    if (b.get("propensity_score") or 0) >= 60 and not recs:
        recs.append(_mk(b, "high_propensity", "medium", f"Prioritize {name} (propensity {b['propensity_score']})",
                        "High buying-propensity right now but no fresher trigger — advance the deal.",
                        "prioritize_outreach", "this_week"))
    dsc = b.get("days_since_last_contact")
    if dsc is not None and dsc >= 10 and not recs:
        recs.append(_mk(b, "going_cold", "medium", f"Re-engage {name}",
                        f"No contact in {dsc} days — the lead is going cold.", "resume_outreach", "this_week"))
    return recs


def _mk(b: dict, stype: str, priority: str, title: str, reason: str, action: str, timing: str) -> dict:
    return {
        "id": f"ai:{stype}:{b['company_id']}",
        "type": stype,
        "priority": priority,
        "title": title,
        "reason": reason,
        "action": action,
        "timing": timing,
        "company_id": b["company_id"],
        "company_name": b["company_name"],
        "recommended_rep_id": b.get("recommended_rep_id"),
        "confidence": 0.6,
        "metadata": {
            "propensity_score": b.get("propensity_score"),
            "fit_score": b.get("fit_score"),
            "pipeline_stage": b.get("pipeline_stage"),
        },
    }


async def _ai_rank(bundles: list[dict], weights: dict, limit: int) -> Optional[list[dict]]:
    """Ask Claude to rank & explain next-best-actions. Returns None on failure."""
    settings = get_settings()
    if not settings.anthropic_api_key or not bundles:
        return None
    try:
        import json

        from app.services.ai_service import get_ai_service

        # Trim bundles to the fields Claude needs to keep the prompt tight.
        compact = [
            {k: b[k] for k in (
                "company_id", "company_name", "pipeline_stage", "industry",
                "fit_score", "propensity_score", "signal_velocity", "opens", "clicks",
                "replies", "days_since_last_contact", "latest_intent", "sentiment_score",
                "buying_signals", "objections", "actionable_news", "overdue_follow_ups",
                "deal_stage", "deal_value", "health_status",
            ) if k in b}
            for b in bundles[:25]
        ]
        prompt = f"""You are a B2B sales strategist for Apprend Technologies (manufacturing software).
Below is structured signal data for {len(compact)} active leads. Produce the ranked
next-best-actions a rep should take, cross-referencing the signals. For each, explain
WHY (cite the specific signals) and WHEN to act.

Only recommend actions that the signals justify. Prefer time-sensitive, high-yield
actions (fresh buying intent, clicks without replies, actionable news, overdue
follow-ups). Return at most {limit} suggestions, best first.

LEADS (JSON):
{json.dumps(compact, default=str)}

Return ONLY JSON:
{{"suggestions": [
  {{"company_id": "...", "type": "hot_reply|engaged_no_reply|news_trigger|overdue_follow_up|unresolved_objection|high_propensity|going_cold|multi_thread",
    "priority": "high|medium|low",
    "title": "short imperative action",
    "reason": "one sentence citing the signals",
    "action": "schedule_meeting|call|reach_out|complete_follow_up|handle_objection|prioritize_outreach|resume_outreach",
    "timing": "today|this_week|next_week",
    "confidence": 0.0-1.0}}
]}}"""
        data = await get_ai_service().complete_json(prompt, max_tokens=2000)
        if not isinstance(data, dict) or "error" in data or "suggestions" not in data:
            return None

        by_id = {b["company_id"]: b for b in bundles}
        out: list[dict] = []
        for item in data.get("suggestions", []):
            cid = str(item.get("company_id", ""))
            b = by_id.get(cid)
            if b is None:
                continue
            stype = item.get("type", "ai_suggestion")
            priority = item.get("priority", "medium")
            if priority not in _PRIORITY_RANK:
                priority = "medium"
            out.append({
                "id": f"ai:{stype}:{cid}",
                "type": stype,
                "priority": priority,
                "title": item.get("title", f"Follow up with {b['company_name']}"),
                "reason": item.get("reason", ""),
                "action": item.get("action", "reach_out"),
                "timing": item.get("timing", "this_week"),
                "company_id": cid,
                "company_name": b["company_name"],
                "recommended_rep_id": b.get("recommended_rep_id"),
                "confidence": float(item.get("confidence", 0.7) or 0.7),
                "metadata": {
                    "propensity_score": b.get("propensity_score"),
                    "fit_score": b.get("fit_score"),
                    "pipeline_stage": b.get("pipeline_stage"),
                    "calibration_weight": weights.get(stype, 1.0),
                },
            })
        # Apply feedback calibration to reorder within priority bands.
        out.sort(key=lambda r: (
            _PRIORITY_RANK.get(r["priority"], 9),
            -(r["confidence"] * weights.get(r["type"], 1.0)),
        ))
        return out[:limit]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("AI suggestion ranking failed: %s", exc)
        return None


async def generate_ai_suggestions(
    db: AsyncSession, *, limit: int = 15, use_ai: bool = True, now: Optional[datetime] = None
) -> dict:
    """Produce ranked AI next-best-actions across active leads.

    Falls back to the deterministic ranker when AI is unavailable/disabled.
    Returns ``{"suggestions": [...], "total": int, "source": "ai"|"rules"}``.
    """
    bundles = await gather_lead_signals(db, now=now)
    weights = await calibration_weights(db)

    suggestions = None
    source = "rules"
    if use_ai:
        suggestions = await _ai_rank(bundles, weights, limit)
        if suggestions is not None:
            source = "ai"
    if suggestions is None:
        suggestions = _deterministic_suggestions(bundles, weights, limit)

    return {"suggestions": suggestions, "total": len(suggestions), "source": source}
