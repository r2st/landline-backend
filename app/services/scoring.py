"""Lead scoring service — rule-based scoring and AI scoring via Claude API.

Enhanced with sales-book recommendations:
- **Signal Recency Decay** (ai-cold-calling-guidelines.md): Recent engagement
  signals are weighted higher — a click 30 minutes ago is worth 20× more than
  one from two days ago.
- **Engagement Velocity Bonus** (ai-cold-calling-guidelines.md): Clustered
  signals (2+ events in 24 hours) indicate active buying behaviour.
- **Account Tier Classification** (digital-sales-strategy.md ABM model):
  Tier 1 (Strategic) / Tier 2 (Target) / Tier 3 (Prospect) classification for
  prioritised resource allocation.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.lead_score import LeadScore

logger = logging.getLogger(__name__)

# Engagement bonus weights (points added on top of data-completeness). These
# reward real buying signals — a reply is worth far more than an open.
_ENGAGEMENT_WEIGHTS = {"replied": 12, "clicked": 7, "opened": 3}
_NEWS_ACTIONABLE_BONUS = 5
_MAX_ENGAGEMENT_BONUS = 25

# Buyer-intent signals (job postings, funding, expansion, etc.) are strong
# timing signals — a relevant + urgent one can add up to this many fit points.
_MAX_INTENT_BONUS = 15


# ---------------------------------------------------------------------------
# Signal Recency Decay (from ai-cold-calling-guidelines.md)
# ---------------------------------------------------------------------------
_RECENCY_BRACKETS: list[tuple[float, float]] = [
    (0.5, 1.0),    # within 30 min
    (1.0, 0.85),   # within 1 hour
    (2.0, 0.70),   # within 2 hours
    (4.0, 0.50),   # within 4 hours
    (8.0, 0.30),   # within 8 hours
    (24.0, 0.15),  # within 24 hours
    (48.0, 0.05),  # within 48 hours
]
_RECENCY_FLOOR = 0.02


def signal_recency_weight(
    last_event_at: Optional[datetime], now: Optional[datetime] = None
) -> float:
    """Return a 0.0-1.0 weight reflecting how recently the lead engaged.

    Uses the decay curve from *ai-cold-calling-guidelines.md*: a signal within
    30 minutes scores 1.0 and decays through seven brackets down to 0.02 for
    anything older than 48 hours.
    """
    if last_event_at is None:
        return 0.0
    if now is None:
        now = datetime.now(timezone.utc)
    if last_event_at.tzinfo is None:
        last_event_at = last_event_at.replace(tzinfo=timezone.utc)
    hours_ago = max(0, (now - last_event_at).total_seconds() / 3600.0)
    for threshold_hours, weight in _RECENCY_BRACKETS:
        if hours_ago <= threshold_hours:
            return weight
    return _RECENCY_FLOOR


# ---------------------------------------------------------------------------
# Engagement Velocity Bonus (from ai-cold-calling-guidelines.md)
# ---------------------------------------------------------------------------
_VELOCITY_BONUSES: list[tuple[int, float]] = [
    (4, 0.30),   # 4+ distinct events in 24h
    (3, 0.20),   # 3 events
    (2, 0.10),   # 2 events
]


async def _engagement_velocity(
    db: AsyncSession, company: Company, now: Optional[datetime] = None
) -> tuple[float, int]:
    """Count distinct engagement events in the last 24 hours and return
    ``(bonus, signal_count)``.

    Events counted: email opens, clicks, and replies from any contact at
    this company.  Each non-null timestamp within the window counts as one
    signal.
    """
    from app.models.email_template import EmailSend

    if now is None:
        now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=24)

    rows = await db.execute(
        select(
            EmailSend.opened_at,
            EmailSend.clicked_at,
            EmailSend.replied_at,
        )
        .join(Contact, Contact.id == EmailSend.contact_id)
        .where(
            Contact.company_id == company.id,
            or_(
                EmailSend.opened_at >= window_start,
                EmailSend.clicked_at >= window_start,
                EmailSend.replied_at >= window_start,
            ),
        )
    )
    signal_count = 0
    most_recent: Optional[datetime] = None
    for opened, clicked, replied in rows.all():
        for ts in (opened, clicked, replied):
            if ts is None:
                continue
            # SQLite (tests) returns naive datetimes; normalize before comparing.
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= window_start:
                signal_count += 1
                if most_recent is None or ts > most_recent:
                    most_recent = ts

    bonus = 0.0
    for threshold, b in _VELOCITY_BONUSES:
        if signal_count >= threshold:
            bonus = b
            break
    return bonus, signal_count, most_recent


# ---------------------------------------------------------------------------
# Account Tier Classification (from digital-sales-strategy.md ABM model)
# ---------------------------------------------------------------------------

def classify_account_tier(
    industry_pts: int, size_pts: int, location_pts: int, engagement_pts: int
) -> tuple[str, float]:
    """Classify an account into ABM tiers based on its sub-scores.

    Returns ``(tier_name, tier_value)`` where tier_value is the account value
    multiplier from *ai-cold-calling-guidelines.md*.
    """
    total = industry_pts + size_pts + location_pts + engagement_pts
    if industry_pts >= 35 and size_pts >= 20 and location_pts >= 15 and total >= 70:
        return "tier_1_strategic", 1.0
    if industry_pts >= 20 and total >= 45:
        return "tier_2_target", 0.75
    if total >= 25:
        return "tier_3_prospect", 0.50
    return "non_target", 0.25


def _industry_score(naics_code: Optional[str]) -> tuple[int, str]:
    """Score based on NAICS code. Manufacturing = 31-33."""
    if not naics_code:
        return 0, "No NAICS code available"
    prefix = naics_code[:2]
    if prefix in ("31", "32", "33"):
        return 40, f"Manufacturing NAICS {naics_code} — perfect fit"
    # Related industries
    related = {
        "42": "Wholesale trade — often serves manufacturing",
        "48": "Transportation — logistics for manufacturing",
        "49": "Warehousing — supports manufacturing supply chain",
        "54": "Professional/technical services — may serve manufacturing",
        "56": "Admin/support services — may serve manufacturing",
    }
    if prefix in related:
        return 20, related[prefix]
    return 0, f"NAICS {naics_code} not related to manufacturing"


def _size_score(employee_count: Optional[int]) -> tuple[int, str]:
    """Score based on company size. Sweet spot is 50-500."""
    if employee_count is None:
        return 5, "Employee count unknown"
    if 50 <= employee_count <= 500:
        return 25, f"{employee_count} employees — ideal mid-market manufacturer"
    if 500 < employee_count <= 2000:
        return 20, f"{employee_count} employees — large manufacturer"
    if 10 <= employee_count < 50:
        return 15, f"{employee_count} employees — small manufacturer"
    if employee_count > 2000:
        return 10, f"{employee_count} employees — enterprise (longer sales cycle)"
    return 5, f"{employee_count} employees — very small, may not need software"


def _location_score(country: str) -> tuple[int, str]:
    """Score based on country. US = 20, CA = 15."""
    if country == "US":
        return 20, "United States — primary market"
    if country == "CA":
        return 15, "Canada — secondary market (CASL applies)"
    return 0, f"Country {country} outside target markets"


def _completeness_score(
    company: Company,
    contacts: list[Contact],
) -> tuple[int, dict[str, int]]:
    """Score based on data completeness. Max 15 points."""
    score = 0
    breakdown: dict[str, int] = {}

    # Check contacts for email
    has_email = any(c.email for c in contacts)
    if has_email:
        score += 5
        breakdown["email"] = 5

    # Phone
    if company.phone:
        score += 3
        breakdown["phone"] = 3

    # Website
    if company.website or company.domain:
        score += 3
        breakdown["website"] = 3

    # Contact name
    has_named_contact = any(c.first_name or c.last_name for c in contacts)
    if has_named_contact:
        score += 4
        breakdown["contact_name"] = 4

    return score, breakdown


async def _engagement_signal(
    db: AsyncSession, company: Company
) -> tuple[int, dict]:
    """Score real engagement signals for a company: email opens/clicks/replies
    and recent actionable news. Returns (points, breakdown). Capped so a hot
    lead can't run away with the score.

    Imported lazily to avoid import cycles and to keep the firmographic-only
    unit tests (which never create sends/news) returning zero here.
    """
    from app.models.email_template import EmailSend
    from app.models.company_news import CompanyNews

    breakdown: dict = {}
    points = 0

    # Email engagement across all of this company's contacts.
    eng = await db.execute(
        select(
            func.count(EmailSend.opened_at),
            func.count(EmailSend.clicked_at),
            func.count(EmailSend.replied_at),
        )
        .join(Contact, Contact.id == EmailSend.contact_id)
        .where(Contact.company_id == company.id)
    )
    opened, clicked, replied = eng.one()
    if replied:
        points += _ENGAGEMENT_WEIGHTS["replied"]
        breakdown["replied"] = int(replied)
    if clicked:
        points += _ENGAGEMENT_WEIGHTS["clicked"]
        breakdown["clicked"] = int(clicked)
    if opened:
        points += _ENGAGEMENT_WEIGHTS["opened"]
        breakdown["opened"] = int(opened)

    # A recent, actionable news event is a timely outreach trigger.
    news_count = await db.execute(
        select(func.count(CompanyNews.id)).where(
            CompanyNews.company_id == company.id,
            CompanyNews.is_actionable.is_(True),
        )
    )
    actionable_news = news_count.scalar() or 0
    if actionable_news:
        points += _NEWS_ACTIONABLE_BONUS
        breakdown["actionable_news"] = int(actionable_news)

    points = min(points, _MAX_ENGAGEMENT_BONUS)
    return points, breakdown


async def _intent_signal_bonus(
    db: AsyncSession, company: Company, now: Optional[datetime] = None
) -> tuple[int, dict]:
    """Score active buyer-intent signals for a company. Returns (points, breakdown).

    Only signals still inside their buying window (``expires_at`` in the future,
    or null) count. The strongest signal dominates via its relevance-weighted
    urgency, with a smaller bonus for additional corroborating signals — several
    independent intent signals at once is a very strong buy indicator. Capped at
    ``_MAX_INTENT_BONUS`` so intent can't swamp firmographic fit.

    Imported lazily so firmographic-only unit tests (which never create signals)
    return zero here.
    """
    from app.models.intent_signal import IntentSignal

    if now is None:
        now = datetime.now(timezone.utc)

    rows = await db.execute(
        select(IntentSignal)
        .where(IntentSignal.company_id == company.id)
        .where(or_(IntentSignal.expires_at.is_(None), IntentSignal.expires_at >= now))
    )
    signals = list(rows.scalars().all())
    if not signals:
        return 0, {}

    scores = sorted((s.combined_score for s in signals), reverse=True)
    # Top signal contributes most; each extra active signal adds a diminishing tail.
    top = scores[0]
    corroboration = sum(scores[1:4]) * 0.15
    raw = (top / 100.0) * _MAX_INTENT_BONUS + corroboration / 10.0
    points = int(min(raw, _MAX_INTENT_BONUS))

    breakdown = {
        "active_signals": len(signals),
        "top_combined_score": round(top, 1),
        "types": sorted({s.signal_type for s in signals}),
    }
    return points, breakdown


async def score_company_rule_based(
    db: AsyncSession,
    company: Company,
) -> LeadScore:
    """Apply rule-based scoring to a company. Returns the new LeadScore.

    Combines firmographic fit (industry, size, location), data completeness,
    real engagement signals, **signal recency decay**, **engagement velocity**,
    and **account tier classification** per sales-book recommendations.
    """
    now = datetime.now(timezone.utc)

    # Load contacts
    result = await db.execute(
        select(Contact).where(Contact.company_id == company.id)
    )
    contacts = list(result.scalars().all())

    industry_pts, industry_reason = _industry_score(company.naics_code)
    size_pts, size_reason = _size_score(company.employee_count)
    loc_pts, loc_reason = _location_score(company.country)
    completeness_pts, completeness_breakdown = _completeness_score(company, contacts)
    engagement_pts, engagement_breakdown = await _engagement_signal(db, company)

    # --- NEW: Signal recency decay ---
    # Find the most recent engagement event across all contacts at this company.
    velocity_bonus, velocity_signals, most_recent_event = await _engagement_velocity(
        db, company, now
    )
    recency = signal_recency_weight(most_recent_event, now)
    recency_bonus = int(recency * 8)  # up to 8 bonus points for fresh signals

    # --- NEW: Engagement velocity bonus ---
    velocity_bonus_pts = int(velocity_bonus * 100 * 0.07)  # up to 7 bonus points

    # --- NEW: Buyer-intent signal bonus (Phase 2.1) ---
    intent_pts, intent_breakdown = await _intent_signal_bonus(db, company, now)

    # --- NEW: Account tier classification ---
    tier_name, tier_value = classify_account_tier(
        industry_pts, size_pts, loc_pts, engagement_pts
    )

    # completeness + live engagement + recency + velocity + intent bonuses
    other_pts = completeness_pts + engagement_pts
    base_score = industry_pts + size_pts + loc_pts + other_pts
    fit_score = min(100, base_score + recency_bonus + velocity_bonus_pts + intent_pts)

    factors = {
        "industry": {"score": industry_pts, "reason": industry_reason},
        "size": {"score": size_pts, "reason": size_reason},
        "location": {"score": loc_pts, "reason": loc_reason},
        "completeness": {"score": completeness_pts, "breakdown": completeness_breakdown},
        "engagement": {"score": engagement_pts, "breakdown": engagement_breakdown},
        "signal_recency": {
            "weight": recency,
            "bonus_points": recency_bonus,
            "most_recent_event": most_recent_event.isoformat() if most_recent_event else None,
        },
        "engagement_velocity": {
            "bonus": velocity_bonus,
            "bonus_points": velocity_bonus_pts,
            "signals_in_24h": velocity_signals,
        },
        "intent_signals": {
            "bonus_points": intent_pts,
            **intent_breakdown,
        },
        "account_tier": {
            "tier": tier_name,
            "value": tier_value,
        },
    }

    lead_score = LeadScore(
        company_id=company.id,
        score_type="rule_based",
        fit_score=fit_score,
        industry_score=industry_pts,
        size_score=size_pts,
        location_score=loc_pts,
        engagement_score=other_pts,
        factors=factors,
    )
    db.add(lead_score)
    await db.flush()

    logger.info(
        "Scored company %s (%s): fit=%d [industry=%d, size=%d, location=%d, "
        "completeness=%d, engagement=%d, recency_bonus=%d, velocity_bonus=%d, "
        "intent_bonus=%d, tier=%s]",
        company.name, company.id, fit_score,
        industry_pts, size_pts, loc_pts, completeness_pts, engagement_pts,
        recency_bonus, velocity_bonus_pts, intent_pts, tier_name,
    )
    return lead_score


async def score_company_ai(
    db: AsyncSession,
    company: Company,
    anthropic_client,
) -> LeadScore:
    """Use Claude API to produce an AI-driven lead score.

    Sends company data to Claude and asks it to evaluate fit for
    Apprend Technologies' manufacturing software products.
    """
    result = await db.execute(
        select(Contact).where(Contact.company_id == company.id)
    )
    contacts = list(result.scalars().all())

    contact_info = ""
    for c in contacts[:5]:
        contact_info += f"  - {c.full_name}, {c.title or 'N/A'}, {c.email or 'N/A'}\n"

    prompt = f"""You are a B2B lead scoring expert for Apprend Technologies, which makes software
for manufacturing companies (press operations, quality control, factory floor management).

Evaluate this company as a potential customer. Return a JSON object with:
- fit_score (0-100): overall likelihood they'd benefit from our software
- industry_score (0-40): how closely aligned to manufacturing
- size_score (0-25): whether size is right for our products
- location_score (0-20): US/Canada market fit
- engagement_score (0-15): data quality and contact accessibility
- reasoning: one paragraph explaining your score

Company Data:
- Name: {company.name}
- Domain: {company.domain or 'N/A'}
- Industry: {company.naics_description or 'N/A'} (NAICS: {company.naics_code or 'N/A'})
- Segment: {company.industry_segment or 'N/A'}
- Location: {company.city or ''}, {company.state_province or ''}, {company.country}
- Employees: {company.employee_count or 'Unknown'}
- Website: {company.website or 'N/A'}
Contacts:
{contact_info or '  None found'}

Respond with ONLY the JSON object, no markdown fences."""

    try:
        # Keep the AI scorer on the same current model as the rest of the app.
        from app.services.ai_service import get_ai_service
        message = anthropic_client.messages.create(
            model=get_ai_service().model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )

        import json
        response_text = message.content[0].text.strip()
        # Tolerate markdown code fences around the JSON.
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
            response_text = response_text.strip()
        data = json.loads(response_text)

        lead_score = LeadScore(
            company_id=company.id,
            score_type="ai",
            fit_score=max(0, min(100, data.get("fit_score", 0))),
            industry_score=max(0, min(40, data.get("industry_score", 0))),
            size_score=max(0, min(25, data.get("size_score", 0))),
            location_score=max(0, min(20, data.get("location_score", 0))),
            engagement_score=max(0, min(15, data.get("engagement_score", 0))),
            factors={"ai_reasoning": data.get("reasoning", ""), "raw_response": data},
        )
        db.add(lead_score)
        await db.flush()

        logger.info("AI scored company %s: fit=%d", company.name, lead_score.fit_score)
        return lead_score

    except Exception as e:
        logger.error("AI scoring failed for %s: %s", company.name, str(e))
        # Fall back to rule-based
        return await score_company_rule_based(db, company)
