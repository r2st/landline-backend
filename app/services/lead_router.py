"""Intelligent lead prioritization & routing (Feature 1.3).

Two capabilities on top of the existing fit-based scoring:

* **Propensity scoring** — answers "how likely is this lead to convert *right
  now*?" (timing), distinct from fit ("how good is this lead?"). Driven by
  engagement recency, **signal velocity** (rate of engagement change), and
  buying signals surfaced by sentiment analysis. Designed to be recomputed
  event-driven (on an email open/click/reply) rather than only on a batch cycle.

* **Rep matching** — routes a lead to the best-matched rep using each rep's
  industry expertise and historical win-rate-by-segment (from ``RepProfile``),
  weighted by available capacity.

``score_and_route`` runs fit scoring, computes propensity, picks a rep, and
persists a single ``LeadScore`` row carrying both the fit and the new
prioritization fields.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.email_sentiment import EmailSentimentScore
from app.models.lead_score import LeadScore
from app.models.rep_profile import RepProfile
from app.services.scoring import score_company_rule_based, signal_recency_weight

logger = logging.getLogger(__name__)

# Weight of each signal type toward propensity (buying-intent ordering).
_SIGNAL_WEIGHTS = {"replied": 30, "clicked": 18, "opened": 8}
# Positive-intent labels from sentiment analysis that boost propensity.
_HOT_INTENTS = {"interested", "meeting_request", "question"}


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def compute_propensity(
    db: AsyncSession, company: Company, now: Optional[datetime] = None
) -> dict:
    """Compute a timing-focused propensity score for a company.

    Returns ``{propensity_score (0-100), signal_velocity, last_signal_at,
    factors}``. Propensity blends: recency of the freshest signal, the mix of
    signal types (replies >> clicks >> opens), signal velocity (24h vs the prior
    48h), and positive-intent sentiment.
    """
    now = now or datetime.now(timezone.utc)
    window_24 = now - timedelta(hours=24)
    window_72 = now - timedelta(hours=72)

    rows = await db.execute(
        select(EmailSend.opened_at, EmailSend.clicked_at, EmailSend.replied_at)
        .join(Contact, Contact.id == EmailSend.contact_id)
        .where(Contact.company_id == company.id)
    )

    last_signal: Optional[datetime] = None
    recent_count = 0  # signals within 24h
    prior_count = 0   # signals within 24-72h
    type_points = 0
    seen_types: set[str] = set()
    for opened, clicked, replied in rows.all():
        for kind, ts in (("replied", replied), ("clicked", clicked), ("opened", opened)):
            ts = _aware(ts)
            if ts is None:
                continue
            if last_signal is None or ts > last_signal:
                last_signal = ts
            if ts >= window_24:
                recent_count += 1
            elif ts >= window_72:
                prior_count += 1
            if kind not in seen_types:
                seen_types.add(kind)
                type_points += _SIGNAL_WEIGHTS[kind]

    # Signal velocity: signals/day now vs the prior two days (rate of change).
    recent_rate = recent_count / 1.0
    prior_rate = prior_count / 2.0
    signal_velocity = round(recent_rate - prior_rate, 3)

    recency = signal_recency_weight(last_signal, now)

    # Sentiment intent boost — any recent hot-intent reply is a strong "now" signal.
    hot_intent = False
    if last_signal is not None:
        intent_rows = await db.execute(
            select(EmailSentimentScore.intent, EmailSentimentScore.sentiment_score)
            .where(
                EmailSentimentScore.company_id == company.id,
                EmailSentimentScore.created_at >= window_72,
            )
        )
        for intent, sscore in intent_rows.all():
            if intent in _HOT_INTENTS:
                hot_intent = True
                break

    # Assemble the 0-100 propensity score.
    score = 0.0
    score += min(45, type_points) * recency          # signal quality decayed by recency
    score += min(25, max(0.0, signal_velocity) * 12)  # acceleration bonus
    score += 20 if hot_intent else 0
    score += min(10, recent_count * 3)                # raw recent activity
    propensity = int(max(0, min(100, round(score))))

    factors = {
        "recency_weight": round(recency, 3),
        "signal_velocity": signal_velocity,
        "recent_24h": recent_count,
        "prior_48h": prior_count,
        "type_points": type_points,
        "hot_intent": hot_intent,
    }
    return {
        "propensity_score": propensity,
        "signal_velocity": signal_velocity,
        "last_signal_at": last_signal,
        "factors": factors,
    }


async def recommend_rep(db: AsyncSession, company: Company) -> tuple[Optional[object], dict]:
    """Pick the best-matched rep for a company. Returns ``(user_id, reasoning)``.

    Scores each rep by: segment win-rate match (does the rep win in this
    company's industry segment?), industry-expertise match, and available
    capacity. Returns ``(None, {...})`` when no rep profiles exist.
    """
    profiles = list((await db.execute(select(RepProfile))).scalars().all())
    if not profiles:
        return None, {"reason": "no_rep_profiles"}

    segment = (company.industry_segment or "").strip().lower()
    naics_prefix = (company.naics_code or "")[:2]

    best = None
    best_score = -1.0
    best_reason: dict = {}
    for p in profiles:
        score = 0.0
        reason: dict = {}

        # Segment win-rate match (strongest signal).
        seg_stats = None
        if segment and isinstance(p.win_rate_by_segment, dict):
            for key, stats in p.win_rate_by_segment.items():
                if key and key.strip().lower() == segment:
                    seg_stats = stats
                    break
        if seg_stats and isinstance(seg_stats, dict):
            rate = float(seg_stats.get("rate", 0) or 0)
            score += rate * 50
            reason["segment_win_rate"] = rate

        # Industry-expertise tag match.
        industries = [str(i).strip().lower() for i in (p.industries or [])]
        if segment and segment in industries:
            score += 20
            reason["industry_match"] = True
        elif naics_prefix and any(naics_prefix in i for i in industries):
            score += 10
            reason["naics_match"] = True

        # Overall win-rate as a mild prior.
        score += float(p.overall_win_rate or 0) * 15
        reason["overall_win_rate"] = float(p.overall_win_rate or 0)

        # Capacity — prefer reps with headroom; penalize overloaded ones.
        avail = p.available_capacity
        if avail <= 0:
            score -= 15
        else:
            score += min(10, avail) * 0.5
        reason["available_capacity"] = avail

        if score > best_score:
            best_score = score
            best = p
            best_reason = {"rep_id": str(p.user_id), "match_score": round(score, 2), **reason}

    if best is None:
        return None, {"reason": "no_match"}
    return best.user_id, best_reason


async def score_and_route(
    db: AsyncSession, company: Company, *, now: Optional[datetime] = None
) -> LeadScore:
    """Run fit scoring + propensity + rep matching, persisting one LeadScore row.

    Reuses ``score_company_rule_based`` for the fit dimensions, then augments the
    freshly-created row with propensity, signal velocity, last-signal time, the
    recommended rep, and a merged ``factors`` payload.
    """
    now = now or datetime.now(timezone.utc)
    lead_score = await score_company_rule_based(db, company)

    prop = await compute_propensity(db, company, now)
    rep_id, rep_reason = await recommend_rep(db, company)

    lead_score.propensity_score = prop["propensity_score"]
    lead_score.signal_velocity = prop["signal_velocity"]
    lead_score.last_signal_at = prop["last_signal_at"]
    lead_score.recommended_rep_id = rep_id

    factors = dict(lead_score.factors or {})
    factors["propensity"] = prop["factors"]
    factors["routing"] = rep_reason
    lead_score.factors = factors

    await db.flush()
    logger.info(
        "Routed company %s: fit=%d propensity=%d velocity=%.2f rep=%s",
        company.name, lead_score.fit_score, lead_score.propensity_score,
        lead_score.signal_velocity, rep_id,
    )
    return lead_score


async def recompute_rep_profile(db: AsyncSession, profile: RepProfile, now: Optional[datetime] = None) -> RepProfile:
    """Recompute a rep's win-rate-by-segment, aggregate rates, velocity, and load.

    Derives stats from deals owned via the rep's deal notes / created records is
    out of scope here; instead we compute from deals whose company was worked by
    this rep. To keep it dependency-light and deterministic we attribute a deal
    to the rep via ``EmailSend.user_id`` on the deal's company (the rep who ran
    outreach). Segment = the company's ``industry_segment``.
    """
    from app.models.deal import Deal

    now = now or datetime.now(timezone.utc)

    # Companies this rep has emailed (their book of business).
    company_rows = await db.execute(
        select(Contact.company_id)
        .join(EmailSend, EmailSend.contact_id == Contact.id)
        .where(EmailSend.user_id == profile.user_id)
        .distinct()
    )
    company_ids = {cid for (cid,) in company_rows.all() if cid is not None}

    seg_stats: dict[str, dict] = {}
    total_won = total_lost = 0
    velocity_days: list[float] = []

    if company_ids:
        deal_rows = await db.execute(
            select(Deal.status, Deal.created_at, Deal.close_date, Company.industry_segment)
            .join(Company, Company.id == Deal.company_id)
            .where(Deal.company_id.in_(company_ids), Deal.status.in_(["won", "lost"]))
        )
        for status, created, close_date, segment in deal_rows.all():
            seg = (segment or "unknown").strip().lower()
            s = seg_stats.setdefault(seg, {"won": 0, "lost": 0, "rate": 0.0})
            if status == "won":
                s["won"] += 1
                total_won += 1
                cd, ca = _aware(close_date), _aware(created)
                if cd and ca:
                    velocity_days.append(max(0.0, (cd - ca).total_seconds() / 86400.0))
            else:
                s["lost"] += 1
                total_lost += 1

    for s in seg_stats.values():
        decided = s["won"] + s["lost"]
        s["rate"] = round(s["won"] / decided, 3) if decided else 0.0

    profile.win_rate_by_segment = seg_stats
    profile.total_won = total_won
    profile.total_lost = total_lost
    decided = total_won + total_lost
    profile.overall_win_rate = round(total_won / decided, 3) if decided else 0.0
    profile.avg_deal_velocity_days = round(sum(velocity_days) / len(velocity_days), 1) if velocity_days else None

    # Active load: distinct active-pipeline companies in the rep's book.
    if company_ids:
        load = (
            await db.execute(
                select(func.count(func.distinct(Company.id))).where(
                    Company.id.in_(company_ids),
                    Company.pipeline_stage.in_(("researching", "outreach", "engaged", "negotiating")),
                )
            )
        ).scalar() or 0
        profile.active_load = int(load)
    else:
        profile.active_load = 0

    await db.flush()
    return profile
