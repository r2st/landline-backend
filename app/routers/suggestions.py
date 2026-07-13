"""Smart suggestions — next-best-action recommendations.

Surfaces the most valuable actions a salesperson should take right now, derived
from four signals: overdue follow-ups, leads that have gone stale, fresh
actionable news, and newly high-scoring leads that haven't been worked yet.

The ranking is deterministic (rule-based) so the panel is fast and predictable;
each suggestion carries the signal that produced it plus a concrete ``action``
the frontend can wire to a button.
"""

import logging
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import UserScope, get_scope
from app.models.company import Company
from app.models.company_news import CompanyNews
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.follow_up import FollowUp
from app.models.lead_score import LeadScore
from app.models.suggestion_outcome import SuggestionOutcome
from app.schemas.suggestions import (
    AISuggestion,
    AISuggestionsResponse,
    Suggestion,
    SuggestionOutcomeCreate,
    SuggestionOutcomeRead,
    SuggestionsResponse,
)
from app.services import smart_suggestions

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/suggestions", tags=["suggestions"])

# Pipeline stages that represent an active, in-flight deal — the ones worth
# nudging when they go quiet. (new_lead = not started, client/churned = done.)
_ACTIVE_STAGES = ("researching", "outreach", "engaged", "negotiating")
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _company_names(db: AsyncSession, ids: set) -> dict:
    if not ids:
        return {}
    rows = await db.execute(
        select(Company.id, Company.name).where(Company.id.in_(ids))
    )
    return {cid: name for cid, name in rows.all()}


async def _overdue_follow_ups(db: AsyncSession, now: datetime) -> list[Suggestion]:
    rows = await db.execute(
        select(FollowUp).where(FollowUp.status == "pending")
    )
    suggestions: list[Suggestion] = []
    overdue = []
    for fu in rows.scalars().all():
        due = _aware(fu.due_date)
        if due is not None and due < now:
            overdue.append((fu, due))

    company_ids = {fu.company_id for fu, _ in overdue if fu.company_id}
    contact_ids = {fu.contact_id for fu, _ in overdue if fu.contact_id}
    names = await _company_names(db, company_ids)
    contact_names = {}
    if contact_ids:
        crows = await db.execute(select(Contact).where(Contact.id.in_(contact_ids)))
        contact_names = {c.id: c.full_name for c in crows.scalars().all()}

    for fu, due in overdue:
        days_over = (now - due).days
        who = contact_names.get(fu.contact_id) or names.get(fu.company_id) or "a lead"
        suggestions.append(Suggestion(
            id=f"overdue_follow_up:{fu.id}",
            type="overdue_follow_up",
            priority="high",
            title=f"Follow up with {who}",
            reason=(
                f"Follow-up is {days_over} day(s) overdue"
                + (f": {fu.reason}" if fu.reason else "")
            ),
            action="complete_follow_up",
            company_id=fu.company_id,
            company_name=names.get(fu.company_id),
            contact_id=fu.contact_id,
            contact_name=contact_names.get(fu.contact_id),
            signal_at=fu.due_date,
            metadata={"follow_up_id": str(fu.id), "days_overdue": days_over},
        ))
    return suggestions


async def _stale_leads(
    db: AsyncSession, now: datetime, stale_days: int
) -> list[Suggestion]:
    # Most recent email to any contact at each company.
    last_email = (
        select(
            Contact.company_id.label("company_id"),
            func.max(EmailSend.sent_at).label("last_sent_at"),
        )
        .join(Contact, EmailSend.contact_id == Contact.id)
        .group_by(Contact.company_id)
        .subquery()
    )

    rows = await db.execute(
        select(Company, last_email.c.last_sent_at)
        .outerjoin(last_email, last_email.c.company_id == Company.id)
        .where(Company.pipeline_stage.in_(_ACTIVE_STAGES))
    )

    threshold = now - timedelta(days=stale_days)
    suggestions: list[Suggestion] = []
    for company, last_sent_at in rows.all():
        last_sent = _aware(last_sent_at)
        if last_sent is not None and last_sent > threshold:
            continue  # touched recently — not stale
        days_since = (now - last_sent).days if last_sent is not None else None
        reason = (
            f"No outreach in {days_since} days while in '{company.pipeline_stage}'"
            if days_since is not None
            else f"In '{company.pipeline_stage}' but never emailed"
        )
        suggestions.append(Suggestion(
            id=f"stale_lead:{company.id}",
            type="stale_lead",
            priority="medium",
            title=f"Re-engage {company.name}",
            reason=reason,
            action="resume_outreach",
            company_id=company.id,
            company_name=company.name,
            signal_at=last_sent_at,
            metadata={
                "pipeline_stage": company.pipeline_stage,
                "days_since_contact": days_since,
            },
        ))
    return suggestions


async def _recent_news(
    db: AsyncSession, now: datetime, news_days: int
) -> list[Suggestion]:
    cutoff = now - timedelta(days=news_days)
    rows = await db.execute(
        select(CompanyNews).where(
            CompanyNews.is_actionable == True,  # noqa: E712
            CompanyNews.actioned == False,  # noqa: E712
        )
    )
    items = []
    for news in rows.scalars().all():
        when = _aware(news.published_at or news.discovered_at)
        if when is not None and when < cutoff:
            continue
        items.append((news, when))

    names = await _company_names(db, {n.company_id for n, _ in items})
    suggestions: list[Suggestion] = []
    for news, when in items:
        cname = names.get(news.company_id, "a lead")
        suggestions.append(Suggestion(
            id=f"recent_news:{news.id}",
            type="recent_news",
            priority="high",
            title=f"Reach out to {cname} about recent news",
            reason=f"{(news.category or 'News').title()}: {news.title}",
            action="reach_out",
            company_id=news.company_id,
            company_name=names.get(news.company_id),
            signal_at=news.published_at or news.discovered_at,
            metadata={
                "news_id": str(news.id),
                "url": news.url,
                "category": news.category,
                "relevance_score": news.relevance_score,
            },
        ))
    return suggestions


async def _high_scores(
    db: AsyncSession, now: datetime, threshold: int, score_days: int
) -> list[Suggestion]:
    # Latest score per company.
    latest = (
        select(
            LeadScore.company_id,
            func.max(LeadScore.scored_at).label("max_at"),
        )
        .group_by(LeadScore.company_id)
        .subquery()
    )
    rows = await db.execute(
        select(LeadScore, Company)
        .join(
            latest,
            (LeadScore.company_id == latest.c.company_id)
            & (LeadScore.scored_at == latest.c.max_at),
        )
        .join(Company, Company.id == LeadScore.company_id)
        .where(LeadScore.fit_score >= threshold)
    )

    cutoff = now - timedelta(days=score_days)
    suggestions: list[Suggestion] = []
    for score, company in rows.all():
        # Only nudge leads that haven't been worked far yet.
        if company.pipeline_stage not in ("new_lead", "researching"):
            continue
        scored = _aware(score.scored_at)
        if scored is not None and scored < cutoff:
            continue
        suggestions.append(Suggestion(
            id=f"high_score:{company.id}",
            type="high_score",
            priority="high",
            title=f"Prioritize {company.name} (fit {score.fit_score})",
            reason=(
                f"High {score.score_type.replace('_', ' ')} fit score of "
                f"{score.fit_score} but still in '{company.pipeline_stage}'"
            ),
            action="prioritize_outreach",
            company_id=company.id,
            company_name=company.name,
            signal_at=score.scored_at,
            metadata={
                "fit_score": score.fit_score,
                "score_type": score.score_type,
                "pipeline_stage": company.pipeline_stage,
            },
        ))
    return suggestions


@router.get("", response_model=SuggestionsResponse)
async def get_suggestions(
    stale_days: int = Query(30, ge=1, le=365),
    high_score_threshold: int = Query(75, ge=0, le=100),
    score_days: int = Query(30, ge=1, le=365),
    news_days: int = Query(14, ge=1, le=365),
    limit: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Ranked next-best-action suggestions across all leads."""
    now = datetime.now(timezone.utc)

    suggestions: list[Suggestion] = []
    suggestions += await _overdue_follow_ups(db, now)
    suggestions += await _stale_leads(db, now, stale_days)
    suggestions += await _recent_news(db, now, news_days)
    suggestions += await _high_scores(db, now, high_score_threshold, score_days)

    # Rank: priority first, then most-recent signal.
    def _sort_key(s: Suggestion):
        ts = _aware(s.signal_at)
        return (
            _PRIORITY_RANK.get(s.priority, 9),
            -(ts.timestamp() if ts else 0),
        )

    suggestions.sort(key=_sort_key)
    counts = Counter(s.type for s in suggestions)
    limited = suggestions[:limit]

    return SuggestionsResponse(
        suggestions=limited,
        total=len(suggestions),
        counts_by_type=dict(counts),
    )


@router.get("/ai", response_model=AISuggestionsResponse)
async def get_ai_suggestions(
    limit: int = Query(15, ge=1, le=50),
    use_ai: bool = Query(True, description="Use Claude to rank; false forces the deterministic ranker"),
    db: AsyncSession = Depends(get_db),
):
    """AI-powered, cross-signal next-best-actions with reasoning and timing.

    Aggregates engagement, sentiment, buying signals, news, deal stage, health,
    propensity, and overdue follow-ups per active lead, then ranks them (via
    Claude when an API key is configured, else a transparent fallback). Falls
    back gracefully so the panel always returns something.
    """
    now = datetime.now(timezone.utc)
    result = await smart_suggestions.generate_ai_suggestions(db, limit=limit, use_ai=use_ai, now=now)
    return AISuggestionsResponse(
        suggestions=[AISuggestion(**s) for s in result["suggestions"]],
        total=result["total"],
        source=result["source"],
        generated_at=now,
    )


@router.post("/outcome", response_model=SuggestionOutcomeRead, status_code=201)
async def record_suggestion_outcome(
    payload: SuggestionOutcomeCreate,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Record that a suggestion was accepted / dismissed / completed (feedback loop).

    Feeds the priority-calibration model that boosts suggestion types which lead
    to positive outcomes and suppresses ones that get dismissed.
    """
    acted = None
    if payload.status in ("accepted", "dismissed", "completed"):
        acted = datetime.now(timezone.utc)
    outcome = SuggestionOutcome(
        user_id=scope.owner_id,
        suggestion_key=payload.suggestion_key,
        suggestion_type=payload.suggestion_type,
        status=payload.status,
        action=payload.action,
        outcome=payload.outcome,
        company_id=payload.company_id,
        contact_id=payload.contact_id,
        payload=payload.payload,
        notes=payload.notes,
        acted_at=acted,
    )
    db.add(outcome)
    await db.flush()
    await db.refresh(outcome)
    logger.info("Recorded suggestion outcome %s (%s/%s)", outcome.id, outcome.suggestion_type, outcome.status)
    return outcome


@router.get("/outcomes", response_model=list[SuggestionOutcomeRead])
async def list_suggestion_outcomes(
    suggestion_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None, pattern="^(suggested|accepted|dismissed|completed)$"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """List recorded suggestion outcomes (scoped to the caller), newest first."""
    query = scope.apply(select(SuggestionOutcome), SuggestionOutcome.user_id)
    if suggestion_type:
        query = query.where(SuggestionOutcome.suggestion_type == suggestion_type)
    if status:
        query = query.where(SuggestionOutcome.status == status)
    query = query.order_by(SuggestionOutcome.created_at.desc()).limit(limit)
    return list((await db.execute(query)).scalars().all())
