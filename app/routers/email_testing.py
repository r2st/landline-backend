"""Email testing and simulation API endpoints.

Provides endpoints for creating simulated email exchanges,
running test conversations with AI-generated prospect replies,
and analyzing the results.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.email_sentiment import EmailTestExchange
from app.services.email_testing_service import EmailTestingService, TEST_SCENARIOS

logger = logging.getLogger(__name__)
router = APIRouter(tags=["email-testing"])

testing_service = EmailTestingService()


# --- Schemas ---


class CreateTestExchangeRequest(BaseModel):
    scenario: str = Field("positive_lead", description="Test scenario type")
    persona_name: Optional[str] = None
    persona_title: Optional[str] = None
    persona_company: Optional[str] = None
    industry: Optional[str] = None
    custom_first_email_subject: Optional[str] = None
    custom_first_email_body: Optional[str] = None


class ContinueTestExchangeRequest(BaseModel):
    follow_up_subject: Optional[str] = None
    follow_up_body: Optional[str] = None
    step_number: int = Field(2, ge=2, le=10)


class TestExchangeRead(BaseModel):
    id: uuid.UUID
    name: str
    scenario: str
    persona_name: Optional[str] = None
    persona_title: Optional[str] = None
    persona_company: Optional[str] = None
    industry: Optional[str] = None
    messages: list
    total_messages: int
    avg_sentiment: Optional[float] = None
    outcome: Optional[str] = None
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TestScenarioInfo(BaseModel):
    id: str
    description: str
    expected_outcome: str
    reply_probability: list[float]


class TestPersonaInfo(BaseModel):
    name: str
    title: str
    company: str
    industry: str


# --- Endpoints ---


@router.get("/email-testing/scenarios", response_model=list[TestScenarioInfo])
async def list_test_scenarios():
    """List all available test scenarios for email simulation."""
    return testing_service.get_available_scenarios()


@router.get("/email-testing/personas", response_model=list[TestPersonaInfo])
async def list_test_personas():
    """List all available test personas."""
    return testing_service.get_available_personas()


@router.post("/email-testing/exchanges", response_model=TestExchangeRead)
async def create_test_exchange(
    payload: CreateTestExchangeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create a new simulated email exchange.

    Sends the first outreach email (using the sequence template) and
    generates an AI-powered reply based on the selected scenario.
    """
    exchange_data = await testing_service.create_test_exchange(
        scenario=payload.scenario,
        persona_name=payload.persona_name,
        persona_title=payload.persona_title,
        persona_company=payload.persona_company,
        industry=payload.industry,
        custom_first_email_subject=payload.custom_first_email_subject,
        custom_first_email_body=payload.custom_first_email_body,
    )

    # Save to database
    exchange = EmailTestExchange(
        id=uuid.UUID(exchange_data["id"]),
        name=exchange_data["name"],
        scenario=exchange_data["scenario"],
        persona_name=exchange_data.get("persona_name"),
        persona_title=exchange_data.get("persona_title"),
        persona_company=exchange_data.get("persona_company"),
        industry=exchange_data.get("industry"),
        messages=exchange_data["messages"],
        total_messages=exchange_data["total_messages"],
        avg_sentiment=exchange_data.get("avg_sentiment"),
        outcome=exchange_data.get("outcome"),
        status=exchange_data["status"],
    )
    db.add(exchange)
    await db.flush()
    await db.refresh(exchange)

    logger.info("Created test exchange %s with scenario '%s'", exchange.id, exchange.scenario)
    return exchange


@router.post("/email-testing/exchanges/{exchange_id}/continue", response_model=TestExchangeRead)
async def continue_test_exchange(
    exchange_id: uuid.UUID,
    payload: ContinueTestExchangeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Continue an existing test exchange with a follow-up email.

    Sends the next sequence step and generates an AI reply.
    """
    result = await db.execute(
        select(EmailTestExchange).where(EmailTestExchange.id == exchange_id)
    )
    exchange = result.scalar_one_or_none()
    if not exchange:
        raise HTTPException(status_code=404, detail="Test exchange not found")

    if exchange.status == "completed":
        raise HTTPException(status_code=400, detail="Exchange is already completed")

    # Build exchange dict for the service
    exchange_dict = {
        "id": str(exchange.id),
        "name": exchange.name,
        "scenario": exchange.scenario,
        "persona_name": exchange.persona_name,
        "persona_title": exchange.persona_title,
        "persona_company": exchange.persona_company,
        "industry": exchange.industry,
        "messages": exchange.messages or [],
        "total_messages": exchange.total_messages,
        "avg_sentiment": exchange.avg_sentiment,
        "outcome": exchange.outcome,
        "status": exchange.status,
    }

    updated = await testing_service.continue_exchange(
        exchange=exchange_dict,
        follow_up_subject=payload.follow_up_subject,
        follow_up_body=payload.follow_up_body,
        step_number=payload.step_number,
    )

    # Update database record
    exchange.messages = updated["messages"]
    exchange.total_messages = updated["total_messages"]
    exchange.avg_sentiment = updated.get("avg_sentiment")
    exchange.outcome = updated.get("outcome")
    exchange.status = updated["status"]
    if updated["status"] == "completed":
        exchange.completed_at = datetime.now(timezone.utc)

    await db.flush()
    await db.refresh(exchange)

    logger.info("Continued test exchange %s to step %d", exchange_id, payload.step_number)
    return exchange


@router.get("/email-testing/exchanges", response_model=list[TestExchangeRead])
async def list_test_exchanges(
    scenario: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List all test exchanges with optional filters."""
    query = select(EmailTestExchange).order_by(desc(EmailTestExchange.created_at))

    if scenario:
        query = query.where(EmailTestExchange.scenario == scenario)
    if status:
        query = query.where(EmailTestExchange.status == status)

    query = query.limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/email-testing/exchanges/{exchange_id}", response_model=TestExchangeRead)
async def get_test_exchange(
    exchange_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get a specific test exchange by ID."""
    result = await db.execute(
        select(EmailTestExchange).where(EmailTestExchange.id == exchange_id)
    )
    exchange = result.scalar_one_or_none()
    if not exchange:
        raise HTTPException(status_code=404, detail="Test exchange not found")
    return exchange


@router.delete("/email-testing/exchanges/{exchange_id}", status_code=204)
async def delete_test_exchange(
    exchange_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Delete a test exchange."""
    result = await db.execute(
        select(EmailTestExchange).where(EmailTestExchange.id == exchange_id)
    )
    exchange = result.scalar_one_or_none()
    if not exchange:
        raise HTTPException(status_code=404, detail="Test exchange not found")
    await db.delete(exchange)
    await db.flush()
    logger.info("Deleted test exchange %s", exchange_id)
