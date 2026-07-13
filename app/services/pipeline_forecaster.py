"""Predictive pipeline & revenue forecasting (Feature 1.1).

Predicts close probability per deal and forecasts pipeline revenue by combining:

* **A statistical model** trained on historical deal outcomes (won/lost)
  correlated with engagement features (email opens/clicks/replies, reply
  sentiment, meeting cadence, time-in-stage, client health). Uses a
  gradient-boosted tree (scikit-learn) when there is enough labelled history and
  the library is available; otherwise falls back to a transparent heuristic
  built on stage base-rates and engagement signals.
* **Claude qualitative analysis** of signals the tabular model can't capture
  (reply-sentiment trend, objection patterns), producing an AI-adjusted
  composite confidence. Skipped when no API key is configured.
* **Anomaly / stall detection** that flags deals whose engagement diverges from
  historical winners (e.g. no meeting in 14 days during negotiation).

The service never raises into request handlers — a training or AI failure
degrades gracefully to the heuristic path.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.deal_prediction import DealPrediction
from app.models.email_template import EmailSend
from app.models.email_sentiment import EmailSentimentScore
from app.models.interaction import Interaction
from app.models.client_health import ClientHealthSnapshot

logger = logging.getLogger(__name__)

# Static per-stage base close-rates (used by the heuristic and as a prior).
_STAGE_BASE_PROB = {
    "qualification": 10.0,
    "discovery": 25.0,
    "proposal": 50.0,
    "negotiation": 75.0,
    "closing": 90.0,
}
_STAGE_ORDER = {s: i for i, s in enumerate(_STAGE_BASE_PROB)}

# Meeting-cadence expectations per stage (days) — exceeding these flags a stall.
_STALL_THRESHOLD_DAYS = {
    "qualification": 21,
    "discovery": 21,
    "proposal": 14,
    "negotiation": 14,
    "closing": 10,
}

# Need at least this many decided (won+lost) deals to train a statistical model.
_MIN_TRAINING_DEALS = 25
_MODEL_VERSION_TRAINED = "gbt_v1"
_MODEL_VERSION_HEURISTIC = "heuristic_v1"

# Feature order used for both training and inference — keep in sync.
_FEATURE_KEYS = [
    "stage_order",
    "stage_base_prob",
    "value_log",
    "age_days",
    "days_in_stage",
    "emails_sent",
    "opens",
    "clicks",
    "replies",
    "avg_sentiment",
    "meetings",
    "days_since_activity",
    "health_score",
]


@dataclass
class DealFeatures:
    """Engineered feature bundle for one deal."""

    deal_id: str
    stage: str
    value: float
    won: Optional[bool] = None  # label (None for open deals)
    vector: dict = field(default_factory=dict)
    last_activity_at: Optional[datetime] = None
    avg_sentiment: Optional[float] = None
    meetings: int = 0


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _collect_deal_features(
    db: AsyncSession, deals: list[Deal], now: datetime
) -> dict[str, DealFeatures]:
    """Build feature vectors for a set of deals in a handful of batched queries.

    Engagement is aggregated at the *company* level (email sends, sentiment,
    interactions) since a deal is tied to a company. This keeps the query count
    O(1) regardless of how many deals are scored.
    """
    if not deals:
        return {}

    company_ids = {d.company_id for d in deals}

    # --- Email engagement per company (join sends -> contacts -> company). ---
    eng_rows = await db.execute(
        select(
            Contact.company_id,
            func.count(EmailSend.id),
            func.count(EmailSend.opened_at),
            func.count(EmailSend.clicked_at),
            func.count(EmailSend.replied_at),
            func.max(
                func.coalesce(EmailSend.replied_at, EmailSend.clicked_at, EmailSend.opened_at, EmailSend.sent_at)
            ),
        )
        .join(Contact, Contact.id == EmailSend.contact_id)
        .where(Contact.company_id.in_(company_ids))
        .group_by(Contact.company_id)
    )
    eng: dict = {}
    for cid, sent, opened, clicked, replied, last_at in eng_rows.all():
        eng[cid] = {
            "emails_sent": int(sent or 0),
            "opens": int(opened or 0),
            "clicks": int(clicked or 0),
            "replies": int(replied or 0),
            "last_email_at": _aware(last_at),
        }

    # --- Average reply sentiment per company. ---
    sent_rows = await db.execute(
        select(
            EmailSentimentScore.company_id,
            func.avg(EmailSentimentScore.sentiment_score),
        )
        .where(EmailSentimentScore.company_id.in_(company_ids))
        .group_by(EmailSentimentScore.company_id)
    )
    sentiment = {cid: float(avg) for cid, avg in sent_rows.all() if avg is not None}

    # --- Meeting / interaction cadence per company. ---
    inter_rows = await db.execute(
        select(
            Interaction.company_id,
            func.count(Interaction.id),
            func.max(Interaction.occurred_at),
        )
        .where(Interaction.company_id.in_(company_ids))
        .group_by(Interaction.company_id)
    )
    inter: dict = {}
    for cid, cnt, last_at in inter_rows.all():
        inter[cid] = {"meetings": int(cnt or 0), "last_interaction_at": _aware(last_at)}

    # --- Latest client-health score per company. ---
    health_rows = await db.execute(
        select(
            ClientHealthSnapshot.company_id,
            func.max(ClientHealthSnapshot.recorded_at),
        )
        .where(ClientHealthSnapshot.company_id.in_(company_ids))
        .group_by(ClientHealthSnapshot.company_id)
    )
    latest_health_at = {cid: _aware(at) for cid, at in health_rows.all()}
    health_score: dict = {}
    if latest_health_at:
        hs_rows = await db.execute(
            select(ClientHealthSnapshot.company_id, ClientHealthSnapshot.score, ClientHealthSnapshot.recorded_at)
            .where(ClientHealthSnapshot.company_id.in_(latest_health_at.keys()))
        )
        for cid, score, at in hs_rows.all():
            if _aware(at) == latest_health_at.get(cid):
                health_score[cid] = int(score or 0)

    import math

    out: dict[str, DealFeatures] = {}
    for deal in deals:
        cid = deal.company_id
        e = eng.get(cid, {})
        it = inter.get(cid, {})
        created = _aware(deal.created_at) or now
        updated = _aware(deal.updated_at) or created
        age_days = max(0.0, (now - created).total_seconds() / 86400.0)
        days_in_stage = max(0.0, (now - updated).total_seconds() / 86400.0)

        last_email = e.get("last_email_at")
        last_inter = it.get("last_interaction_at")
        last_activity = max([d for d in (last_email, last_inter, updated) if d is not None], default=updated)
        days_since_activity = max(0.0, (now - last_activity).total_seconds() / 86400.0) if last_activity else age_days

        avg_sent = sentiment.get(cid)
        vector = {
            "stage_order": float(_STAGE_ORDER.get(deal.deal_stage, 0)),
            "stage_base_prob": _STAGE_BASE_PROB.get(deal.deal_stage, 10.0),
            "value_log": math.log1p(float(deal.value or 0.0)),
            "age_days": age_days,
            "days_in_stage": days_in_stage,
            "emails_sent": float(e.get("emails_sent", 0)),
            "opens": float(e.get("opens", 0)),
            "clicks": float(e.get("clicks", 0)),
            "replies": float(e.get("replies", 0)),
            "avg_sentiment": float(avg_sent) if avg_sent is not None else 5.0,
            "meetings": float(it.get("meetings", 0)),
            "days_since_activity": days_since_activity,
            "health_score": float(health_score.get(cid, 50)),
        }

        won: Optional[bool] = None
        if deal.status == "won":
            won = True
        elif deal.status == "lost":
            won = False

        out[str(deal.id)] = DealFeatures(
            deal_id=str(deal.id),
            stage=deal.deal_stage,
            value=float(deal.value or 0.0),
            won=won,
            vector=vector,
            last_activity_at=last_activity,
            avg_sentiment=avg_sent,
            meetings=int(it.get("meetings", 0)),
        )
    return out


def _train_model(training: list[DealFeatures]):
    """Train a gradient-boosted classifier on decided deals.

    Returns a callable ``predict_proba(vector_dict) -> float(0-1)`` or ``None`` if
    scikit-learn is unavailable or the data is insufficient / single-class.
    """
    labelled = [f for f in training if f.won is not None]
    if len(labelled) < _MIN_TRAINING_DEALS:
        return None
    labels = [1 if f.won else 0 for f in labelled]
    if len(set(labels)) < 2:
        return None  # need both won and lost examples

    try:
        import numpy as np
        from sklearn.ensemble import GradientBoostingClassifier
    except Exception as exc:  # pragma: no cover - env without sklearn
        logger.info("scikit-learn unavailable (%s); using heuristic forecaster", exc)
        return None

    try:
        X = np.array([[f.vector[k] for k in _FEATURE_KEYS] for f in labelled], dtype=float)
        y = np.array(labels, dtype=int)
        clf = GradientBoostingClassifier(
            n_estimators=120, max_depth=3, learning_rate=0.08, subsample=0.9, random_state=42
        )
        clf.fit(X, y)

        importances = {k: float(v) for k, v in zip(_FEATURE_KEYS, clf.feature_importances_)}

        def _predict(vector: dict) -> float:
            row = np.array([[vector[k] for k in _FEATURE_KEYS]], dtype=float)
            proba = clf.predict_proba(row)[0]
            # Class order follows clf.classes_ ([0, 1]) — take P(class == 1).
            idx = list(clf.classes_).index(1)
            return float(proba[idx])

        _predict.importances = importances  # type: ignore[attr-defined]
        logger.info("Trained deal-forecast GBT on %d decided deals", len(labelled))
        return _predict
    except Exception as exc:  # pragma: no cover
        logger.warning("Deal-forecast training failed: %s", exc)
        return None


def _heuristic_probability(f: DealFeatures) -> tuple[float, dict]:
    """Transparent stage-base-rate probability nudged by engagement signals.

    Returns ``(probability_0_1, factor_contributions)``.
    """
    base = _STAGE_BASE_PROB.get(f.stage, 10.0) / 100.0
    v = f.vector
    factors: dict = {"stage_base_rate": round(base, 3)}

    adj = 0.0
    # Replies are the strongest positive signal.
    if v["replies"] > 0:
        bump = min(0.15, 0.05 * v["replies"])
        adj += bump
        factors["replies"] = round(bump, 3)
    if v["clicks"] > 0:
        bump = min(0.08, 0.02 * v["clicks"])
        adj += bump
        factors["clicks"] = round(bump, 3)
    # Positive reply sentiment (1-10 scale, 5 = neutral).
    if f.avg_sentiment is not None:
        s_adj = (f.avg_sentiment - 5.0) / 5.0 * 0.10
        adj += s_adj
        factors["sentiment"] = round(s_adj, 3)
    # Meeting cadence — winners meet regularly.
    if v["meetings"] >= 2:
        adj += 0.06
        factors["meeting_cadence"] = 0.06
    # Staleness penalty — no activity for a long time bleeds probability.
    if v["days_since_activity"] > 14:
        pen = -min(0.25, 0.01 * (v["days_since_activity"] - 14))
        adj += pen
        factors["staleness"] = round(pen, 3)
    # Long time-in-stage penalty.
    stall_days = _STALL_THRESHOLD_DAYS.get(f.stage, 21)
    if v["days_in_stage"] > stall_days:
        pen = -min(0.15, 0.005 * (v["days_in_stage"] - stall_days))
        adj += pen
        factors["time_in_stage"] = round(pen, 3)

    prob = max(0.01, min(0.99, base + adj))
    return prob, factors


def _risk_assessment(f: DealFeatures, probability: float, now: datetime) -> tuple[str, list[str]]:
    """Flag stalling / at-risk deals from engagement anomalies."""
    flags: list[str] = []
    v = f.vector
    stall_days = _STALL_THRESHOLD_DAYS.get(f.stage, 21)

    if v["days_since_activity"] >= 14:
        flags.append(f"no_activity_{int(v['days_since_activity'])}d")
    if f.stage in ("negotiation", "closing") and v["meetings"] == 0:
        flags.append("no_meeting_in_late_stage")
    if v["days_in_stage"] > stall_days:
        flags.append(f"stage_stalled_{int(v['days_in_stage'])}d")
    if f.avg_sentiment is not None and f.avg_sentiment < 4.0:
        flags.append("negative_sentiment")
    if v["emails_sent"] >= 3 and v["replies"] == 0 and v["opens"] == 0:
        flags.append("no_engagement")

    if probability >= 0.6 and not flags:
        level = "on_track"
    elif probability >= 0.4 and len(flags) <= 1:
        level = "watch"
    elif probability >= 0.2 or len(flags) <= 2:
        level = "at_risk"
    else:
        level = "stalling"
    # A late-stage deal going quiet is always at least at_risk.
    if "no_meeting_in_late_stage" in flags and level in ("on_track", "watch"):
        level = "at_risk"
    return level, flags


def _predicted_close_date(f: DealFeatures, deal: Deal, now: datetime) -> Optional[datetime]:
    """Estimate a close date from remaining stages and observed velocity."""
    existing = _aware(deal.close_date)
    if existing is not None and existing > now:
        return existing
    remaining_stages = max(1, len(_STAGE_BASE_PROB) - _STAGE_ORDER.get(f.stage, 0))
    # Assume ~14 days per remaining stage, scaled up if the deal is already slow.
    per_stage = 14.0
    if f.vector["days_in_stage"] > 30:
        per_stage = 21.0
    return now + timedelta(days=per_stage * remaining_stages)


async def _ai_adjustment(f: DealFeatures, deal: Deal, company_name: str) -> tuple[Optional[float], Optional[str]]:
    """Ask Claude for a qualitative confidence read. Returns (confidence_0_100, summary).

    Gated on a configured API key so tests and keyless deploys stay hermetic.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        return None, None
    try:
        from app.services.ai_service import get_ai_service

        v = f.vector
        prompt = f"""You are a B2B sales forecasting analyst. Assess how likely this deal is to
close, focusing on qualitative momentum signals a statistical model misses.

Deal: {deal.title} at {company_name}
Stage: {f.stage} (value ${f.value:,.0f})
Days in current stage: {int(v['days_in_stage'])}
Emails sent / opens / clicks / replies: {int(v['emails_sent'])}/{int(v['opens'])}/{int(v['clicks'])}/{int(v['replies'])}
Meetings logged: {int(v['meetings'])}
Avg reply sentiment (1-10): {f.avg_sentiment if f.avg_sentiment is not None else 'n/a'}
Days since last activity: {int(v['days_since_activity'])}

Return ONLY JSON:
{{"confidence": 0-100, "momentum": "accelerating|steady|slowing|stalled", "summary": "one sentence"}}"""
        data = await get_ai_service().complete_json(prompt, max_tokens=300)
        if not isinstance(data, dict) or "error" in data:
            return None, None
        conf = data.get("confidence")
        conf = max(0.0, min(100.0, float(conf))) if conf is not None else None
        return conf, data.get("summary")
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Deal AI adjustment failed: %s", exc)
        return None, None


async def predict_deal(
    db: AsyncSession,
    deal: Deal,
    *,
    model=None,
    features: Optional[DealFeatures] = None,
    company_name: Optional[str] = None,
    use_ai: bool = True,
    now: Optional[datetime] = None,
    persist: bool = True,
) -> DealPrediction:
    """Score a single deal and (optionally) persist a DealPrediction row."""
    now = now or datetime.now(timezone.utc)
    if features is None:
        feats = await _collect_deal_features(db, [deal], now)
        features = feats[str(deal.id)]

    if model is not None:
        try:
            model_prob = model(features.vector)
            factors = {"model_importances": getattr(model, "importances", {})}
            version = _MODEL_VERSION_TRAINED
        except Exception as exc:  # pragma: no cover
            logger.warning("Model inference failed, using heuristic: %s", exc)
            model_prob, factors = _heuristic_probability(features)
            version = _MODEL_VERSION_HEURISTIC
    else:
        model_prob, factors = _heuristic_probability(features)
        version = _MODEL_VERSION_HEURISTIC

    composite = model_prob * 100.0
    ai_conf: Optional[float] = None
    ai_summary: Optional[str] = None
    if use_ai:
        if company_name is None:
            company_name = (
                await db.execute(select(Company.name).where(Company.id == deal.company_id))
            ).scalar_one_or_none() or "the company"
        ai_conf, ai_summary = await _ai_adjustment(features, deal, company_name)
        if ai_conf is not None:
            # Blend the model (70%) with Claude's qualitative read (30%).
            composite = 0.7 * model_prob * 100.0 + 0.3 * ai_conf

    composite = max(0.0, min(100.0, composite))
    risk_level, risk_flags = _risk_assessment(features, composite / 100.0, now)
    close_date = _predicted_close_date(features, deal, now)

    prediction = DealPrediction(
        deal_id=deal.id,
        close_probability=round(composite, 1),
        model_probability=round(model_prob * 100.0, 1),
        ai_confidence=round(ai_conf, 1) if ai_conf is not None else None,
        predicted_close_date=close_date,
        risk_level=risk_level,
        risk_flags=risk_flags,
        contributing_factors=factors,
        ai_summary=ai_summary,
        model_version=version,
    )
    if persist:
        db.add(prediction)
        await db.flush()
    return prediction


async def rescore_active_deals(
    db: AsyncSession, *, use_ai: bool = True, limit: int = 500, now: Optional[datetime] = None
) -> dict:
    """Train once, then (re)score all pending deals. Returns a run summary."""
    now = now or datetime.now(timezone.utc)

    # Training set: decided deals (won/lost) with their features.
    decided = list(
        (
            await db.execute(
                select(Deal).where(Deal.status.in_(["won", "lost"])).limit(2000)
            )
        ).scalars().all()
    )
    active = list(
        (
            await db.execute(
                select(Deal).where(Deal.status == "pending").order_by(Deal.updated_at.asc()).limit(limit)
            )
        ).scalars().all()
    )
    if not active:
        return {"scored": 0, "model_version": _MODEL_VERSION_HEURISTIC, "trained": False}

    all_deals = decided + active
    feature_map = await _collect_deal_features(db, all_deals, now)
    model = _train_model([feature_map[str(d.id)] for d in decided])

    # Company names in one query for AI prompts / convenience.
    company_ids = {d.company_id for d in active}
    names = {
        cid: name
        for cid, name in (
            await db.execute(select(Company.id, Company.name).where(Company.id.in_(company_ids)))
        ).all()
    }

    scored = 0
    for deal in active:
        try:
            await predict_deal(
                db,
                deal,
                model=model,
                features=feature_map[str(deal.id)],
                company_name=names.get(deal.company_id, "the company"),
                use_ai=use_ai,
                now=now,
            )
            scored += 1
        except Exception as exc:
            logger.warning("Prediction failed for deal %s: %s", deal.id, exc)
            continue

    version = _MODEL_VERSION_TRAINED if model is not None else _MODEL_VERSION_HEURISTIC
    logger.info("Rescored %d active deals (model=%s)", scored, version)
    return {"scored": scored, "model_version": version, "trained": model is not None, "candidates": len(active)}


async def latest_predictions(db: AsyncSession, deal_ids: list) -> dict:
    """Return the most recent DealPrediction per deal id, as a dict keyed by deal_id."""
    if not deal_ids:
        return {}
    rows = await db.execute(
        select(DealPrediction).where(DealPrediction.deal_id.in_(deal_ids))
    )
    latest: dict = {}
    for pred in rows.scalars().all():
        cur = latest.get(pred.deal_id)
        if cur is None or _aware(pred.predicted_at) > _aware(cur.predicted_at):
            latest[pred.deal_id] = pred
    return latest


_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


async def build_forecast(db: AsyncSession, *, now: Optional[datetime] = None) -> dict:
    """Aggregate the latest predictions for pending deals into a revenue forecast."""
    now = now or datetime.now(timezone.utc)
    active = list(
        (await db.execute(select(Deal).where(Deal.status == "pending"))).scalars().all()
    )
    preds = await latest_predictions(db, [d.id for d in active])

    company_ids = {d.company_id for d in active}
    names = {
        cid: name
        for cid, name in (
            await db.execute(select(Company.id, Company.name).where(Company.id.in_(company_ids)))
        ).all()
    } if company_ids else {}

    total_pipeline = 0.0
    weighted = 0.0
    expected_count = 0.0
    by_period: dict[str, dict] = defaultdict(lambda: {"deal_count": 0, "pipeline_value": 0.0, "weighted_value": 0.0})
    at_risk: list[dict] = []
    at_risk_value = 0.0
    model_version = _MODEL_VERSION_HEURISTIC

    for deal in active:
        value = float(deal.value or 0.0)
        total_pipeline += value
        pred = preds.get(deal.id)
        prob = (pred.close_probability / 100.0) if pred else _STAGE_BASE_PROB.get(deal.deal_stage, 10.0) / 100.0
        if pred:
            model_version = pred.model_version
        weighted += value * prob
        expected_count += prob

        close_dt = _aware(pred.predicted_close_date) if pred and pred.predicted_close_date else _aware(deal.close_date)
        if close_dt is None:
            close_dt = now + timedelta(days=30)
        key = f"{close_dt.year:04d}-{close_dt.month:02d}"
        bucket = by_period[key]
        bucket["deal_count"] += 1
        bucket["pipeline_value"] += value
        bucket["weighted_value"] += value * prob

        if pred and pred.risk_level in ("at_risk", "stalling"):
            at_risk.append({
                "deal_id": deal.id,
                "deal_title": deal.title,
                "company_name": names.get(deal.company_id),
                "value": value,
                "close_probability": pred.close_probability,
                "risk_level": pred.risk_level,
                "risk_flags": pred.risk_flags or [],
            })
            at_risk_value += value

    periods = []
    for key in sorted(by_period):
        year, month = key.split("-")
        b = by_period[key]
        periods.append({
            "period": key,
            "label": f"{_MONTHS[int(month) - 1]} {year}",
            "deal_count": b["deal_count"],
            "pipeline_value": round(b["pipeline_value"], 2),
            "weighted_value": round(b["weighted_value"], 2),
        })

    at_risk.sort(key=lambda d: d["value"], reverse=True)
    return {
        "generated_at": now,
        "model_version": model_version,
        "active_deal_count": len(active),
        "total_pipeline_value": round(total_pipeline, 2),
        "weighted_forecast": round(weighted, 2),
        "expected_close_count": round(expected_count, 2),
        "by_period": periods,
        "at_risk_deals": at_risk,
        "at_risk_value": round(at_risk_value, 2),
    }
