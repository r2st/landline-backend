"""Sentiment analysis API endpoints.

Provides endpoints for analyzing email reply sentiment, viewing sentiment
trends, and managing sentiment data for outreach optimization.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.email_reply import EmailReply
from app.models.email_sentiment import EmailSentimentScore
from app.models.contact import Contact
from app.models.company import Company
from app.services.sentiment_service import SentimentAnalyzer

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sentiment"])

analyzer = SentimentAnalyzer()


# --- Schemas ---

class SentimentAnalyzeRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Email reply text to analyze")
    original_subject: Optional[str] = None
    original_body: Optional[str] = None
    contact_name: Optional[str] = None
    company_name: Optional[str] = None
    industry: Optional[str] = None
    email_reply_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    company_id: Optional[uuid.UUID] = None


class SentimentAnalyzeResponse(BaseModel):
    sentiment_score: float
    sentiment_label: str
    intent: str
    intent_confidence: float
    emotions: dict
    buying_signals: list
    objections: list
    recommended_action: str
    action_reasoning: str
    model_used: Optional[str] = None
    saved: bool = False
    id: Optional[uuid.UUID] = None


class SentimentScoreRead(BaseModel):
    id: uuid.UUID
    email_reply_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    company_id: Optional[uuid.UUID] = None
    sentiment_score: float
    sentiment_label: str
    intent: str
    intent_confidence: float
    emotions: dict
    buying_signals: list
    objections: list
    recommended_action: Optional[str] = None
    action_reasoning: Optional[str] = None
    analyzed_text: Optional[str] = None
    model_used: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class SentimentTrendPoint(BaseModel):
    date: str
    avg_score: float
    count: int
    positive: int
    negative: int
    neutral: int


class SentimentSummary(BaseModel):
    total_analyzed: int
    avg_sentiment: float
    positive_pct: float
    negative_pct: float
    neutral_pct: float
    top_intents: list[dict]
    top_buying_signals: list[str]
    top_objections: list[str]
    trend: list[SentimentTrendPoint]


class SentimentBatchRequest(BaseModel):
    replies: list[SentimentAnalyzeRequest]


# --- Endpoints ---


@router.post("/sentiment/analyze", response_model=SentimentAnalyzeResponse)
async def analyze_sentiment(
    payload: SentimentAnalyzeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Analyze the sentiment of an email reply text.

    Optionally saves the result to the database if email_reply_id,
    contact_id, or company_id is provided.
    """
    result = await analyzer.analyze_reply(
        reply_text=payload.text,
        original_email_subject=payload.original_subject,
        original_email_body=payload.original_body,
        contact_name=payload.contact_name,
        company_name=payload.company_name,
        industry=payload.industry,
    )

    saved = False
    record_id = None

    # Save to database if we have context to link it
    if payload.email_reply_id or payload.contact_id or payload.company_id:
        score = EmailSentimentScore(
            email_reply_id=payload.email_reply_id,
            contact_id=payload.contact_id,
            company_id=payload.company_id,
            sentiment_score=result["sentiment_score"],
            sentiment_label=result["sentiment_label"],
            intent=result["intent"],
            intent_confidence=result["intent_confidence"],
            emotions=result.get("emotions", {}),
            buying_signals=result.get("buying_signals", []),
            objections=result.get("objections", []),
            recommended_action=result.get("recommended_action"),
            action_reasoning=result.get("action_reasoning"),
            analyzed_text=payload.text[:1000],  # Store first 1000 chars
            model_used=result.get("model_used"),
        )
        db.add(score)
        await db.flush()
        await db.refresh(score)
        saved = True
        record_id = score.id

    return SentimentAnalyzeResponse(
        sentiment_score=result["sentiment_score"],
        sentiment_label=result["sentiment_label"],
        intent=result["intent"],
        intent_confidence=result["intent_confidence"],
        emotions=result.get("emotions", {}),
        buying_signals=result.get("buying_signals", []),
        objections=result.get("objections", []),
        recommended_action=result.get("recommended_action", "no_action"),
        action_reasoning=result.get("action_reasoning", ""),
        model_used=result.get("model_used"),
        saved=saved,
        id=record_id,
    )


@router.post("/sentiment/analyze-reply/{reply_id}", response_model=SentimentAnalyzeResponse)
async def analyze_reply_sentiment(
    reply_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Analyze sentiment for a stored email reply by its ID.

    Fetches the reply from the database, analyzes it, and saves the result.
    """
    result = await db.execute(select(EmailReply).where(EmailReply.id == reply_id))
    reply = result.scalar_one_or_none()
    if not reply:
        raise HTTPException(status_code=404, detail="Email reply not found")

    # Get context from related entities
    contact_name = None
    company_name = None
    industry = None

    if reply.contact_id:
        contact_result = await db.execute(select(Contact).where(Contact.id == reply.contact_id))
        contact = contact_result.scalar_one_or_none()
        if contact:
            contact_name = f"{contact.first_name} {contact.last_name}".strip()

    if reply.company_id:
        company_result = await db.execute(select(Company).where(Company.id == reply.company_id))
        company = company_result.scalar_one_or_none()
        if company:
            company_name = company.name
            industry = company.industry_segment

    # Get original email context
    original_subject = None
    original_body = None
    if reply.email_send:
        original_subject = reply.email_send.subject
        original_body = reply.email_send.body_html

    text = reply.body_text or reply.snippet or ""
    if not text:
        raise HTTPException(status_code=400, detail="Reply has no text content to analyze")

    analysis = await analyzer.analyze_reply(
        reply_text=text,
        original_email_subject=original_subject,
        original_email_body=original_body,
        contact_name=contact_name,
        company_name=company_name,
        industry=industry,
    )

    # Save the result
    score = EmailSentimentScore(
        email_reply_id=reply.id,
        contact_id=reply.contact_id,
        company_id=reply.company_id,
        sentiment_score=analysis["sentiment_score"],
        sentiment_label=analysis["sentiment_label"],
        intent=analysis["intent"],
        intent_confidence=analysis["intent_confidence"],
        emotions=analysis.get("emotions", {}),
        buying_signals=analysis.get("buying_signals", []),
        objections=analysis.get("objections", []),
        recommended_action=analysis.get("recommended_action"),
        action_reasoning=analysis.get("action_reasoning"),
        analyzed_text=text[:1000],
        model_used=analysis.get("model_used"),
    )
    db.add(score)
    await db.flush()
    await db.refresh(score)

    return SentimentAnalyzeResponse(
        sentiment_score=analysis["sentiment_score"],
        sentiment_label=analysis["sentiment_label"],
        intent=analysis["intent"],
        intent_confidence=analysis["intent_confidence"],
        emotions=analysis.get("emotions", {}),
        buying_signals=analysis.get("buying_signals", []),
        objections=analysis.get("objections", []),
        recommended_action=analysis.get("recommended_action", "no_action"),
        action_reasoning=analysis.get("action_reasoning", ""),
        model_used=analysis.get("model_used"),
        saved=True,
        id=score.id,
    )


@router.get("/sentiment/scores", response_model=list[SentimentScoreRead])
async def list_sentiment_scores(
    contact_id: Optional[uuid.UUID] = Query(None),
    company_id: Optional[uuid.UUID] = Query(None),
    intent: Optional[str] = Query(None),
    min_score: Optional[float] = Query(None, ge=1, le=10),
    max_score: Optional[float] = Query(None, ge=1, le=10),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """List sentiment scores with optional filters."""
    query = select(EmailSentimentScore).order_by(desc(EmailSentimentScore.created_at))

    if contact_id:
        query = query.where(EmailSentimentScore.contact_id == contact_id)
    if company_id:
        query = query.where(EmailSentimentScore.company_id == company_id)
    if intent:
        query = query.where(EmailSentimentScore.intent == intent)
    if min_score is not None:
        query = query.where(EmailSentimentScore.sentiment_score >= min_score)
    if max_score is not None:
        query = query.where(EmailSentimentScore.sentiment_score <= max_score)

    query = query.limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/sentiment/summary", response_model=SentimentSummary)
async def get_sentiment_summary(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Get a summary of sentiment analysis data over a time period."""
    from datetime import timedelta
    from sqlalchemy import cast, Date

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Total count
    total_q = await db.execute(
        select(func.count(EmailSentimentScore.id)).where(
            EmailSentimentScore.created_at >= cutoff
        )
    )
    total = total_q.scalar() or 0

    if total == 0:
        return SentimentSummary(
            total_analyzed=0,
            avg_sentiment=0.0,
            positive_pct=0.0,
            negative_pct=0.0,
            neutral_pct=0.0,
            top_intents=[],
            top_buying_signals=[],
            top_objections=[],
            trend=[],
        )

    # Average sentiment
    avg_q = await db.execute(
        select(func.avg(EmailSentimentScore.sentiment_score)).where(
            EmailSentimentScore.created_at >= cutoff
        )
    )
    avg_sentiment = round(float(avg_q.scalar() or 5.0), 2)

    # Label distribution
    label_q = await db.execute(
        select(
            EmailSentimentScore.sentiment_label,
            func.count(EmailSentimentScore.id).label("cnt"),
        )
        .where(EmailSentimentScore.created_at >= cutoff)
        .group_by(EmailSentimentScore.sentiment_label)
    )
    label_counts = {row.sentiment_label: row.cnt for row in label_q.all()}

    positive = label_counts.get("positive", 0) + label_counts.get("very_positive", 0)
    negative = label_counts.get("negative", 0)
    neutral = label_counts.get("neutral", 0) + label_counts.get("mixed", 0)

    # Intent distribution
    intent_q = await db.execute(
        select(
            EmailSentimentScore.intent,
            func.count(EmailSentimentScore.id).label("cnt"),
        )
        .where(EmailSentimentScore.created_at >= cutoff)
        .group_by(EmailSentimentScore.intent)
        .order_by(desc("cnt"))
        .limit(10)
    )
    top_intents = [{"intent": row.intent, "count": row.cnt} for row in intent_q.all()]

    # Daily trend
    trend_q = await db.execute(
        select(
            cast(EmailSentimentScore.created_at, Date).label("date"),
            func.avg(EmailSentimentScore.sentiment_score).label("avg_score"),
            func.count(EmailSentimentScore.id).label("count"),
        )
        .where(EmailSentimentScore.created_at >= cutoff)
        .group_by(cast(EmailSentimentScore.created_at, Date))
        .order_by(cast(EmailSentimentScore.created_at, Date))
    )
    trend = []
    for row in trend_q.all():
        trend.append(SentimentTrendPoint(
            date=str(row.date),
            avg_score=round(float(row.avg_score), 2),
            count=row.count,
            positive=0,  # Simplified — would need subquery for per-day breakdown
            negative=0,
            neutral=0,
        ))

    return SentimentSummary(
        total_analyzed=total,
        avg_sentiment=avg_sentiment,
        positive_pct=round(positive / total * 100, 1) if total else 0.0,
        negative_pct=round(negative / total * 100, 1) if total else 0.0,
        neutral_pct=round(neutral / total * 100, 1) if total else 0.0,
        top_intents=top_intents,
        top_buying_signals=[],  # Would need to aggregate JSONB array elements
        top_objections=[],
        trend=trend,
    )
