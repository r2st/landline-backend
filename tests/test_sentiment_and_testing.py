"""Tests for sentiment analysis, A/B testing, and email testing features.

Covers:
- Sentiment analysis service logic
- A/B testing statistical significance calculations
- Email testing/simulation endpoints
- Sentiment API endpoints
- Integration between sentiment and email replies
"""

import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_sentiment import EmailSentimentScore, ABTestExperiment, EmailTestExchange
from app.services.ab_testing_service import (
    evaluate_ab_test,
    calculate_required_sample_size,
    _z_score_two_proportions,
    _z_to_confidence,
)


# ---------------------------------------------------------------------------
# A/B Testing Service — Pure math, no mocks needed
# ---------------------------------------------------------------------------


class TestABTestingStatistics:
    """Test the A/B testing statistical significance calculations."""

    def test_equal_rates_not_significant(self):
        """Equal performance should never be significant."""
        result = evaluate_ab_test(
            a_sent=500, a_metric=50,
            b_sent=500, b_metric=50,
        )
        assert result["is_significant"] is False
        assert result["winner"] is None
        assert result["rate_a"] == result["rate_b"]
        assert result["lift"] == 0.0

    def test_clear_winner_is_significant(self):
        """Large difference with sufficient sample should be significant."""
        result = evaluate_ab_test(
            a_sent=1000, a_metric=100,  # 10% rate
            b_sent=1000, b_metric=200,  # 20% rate
        )
        assert result["is_significant"] is True
        assert result["winner"] == "B"
        assert result["confidence"] > 0.95
        assert result["lift"] > 0

    def test_variant_a_wins(self):
        """When A clearly outperforms B."""
        result = evaluate_ab_test(
            a_sent=1000, a_metric=200,  # 20%
            b_sent=1000, b_metric=100,  # 10%
        )
        assert result["winner"] == "A"
        assert result["is_significant"] is True

    def test_insufficient_sample_not_significant(self):
        """Too few sends should never declare significance."""
        result = evaluate_ab_test(
            a_sent=10, a_metric=5,
            b_sent=10, b_metric=1,
        )
        assert result["is_significant"] is False
        assert result["sample_sufficient"] is False
        assert result["remaining_samples_a"] > 0

    def test_zero_sends_returns_safe_defaults(self):
        """Zero sends should return safe defaults without errors."""
        result = evaluate_ab_test(
            a_sent=0, a_metric=0,
            b_sent=0, b_metric=0,
        )
        assert result["winner"] is None
        assert result["is_significant"] is False
        assert result["status"] == "insufficient_data"

    def test_marginal_difference_not_significant(self):
        """Small differences with moderate sample shouldn't trigger false positives."""
        result = evaluate_ab_test(
            a_sent=300, a_metric=30,  # 10%
            b_sent=300, b_metric=33,  # 11%
        )
        assert result["is_significant"] is False

    def test_confidence_increases_with_sample_size(self):
        """Same rate difference should be more confident with larger samples."""
        small = evaluate_ab_test(a_sent=200, a_metric=20, b_sent=200, b_metric=40)
        large = evaluate_ab_test(a_sent=2000, a_metric=200, b_sent=2000, b_metric=400)
        assert large["confidence"] >= small["confidence"]

    def test_z_score_symmetric(self):
        """Swapping A and B should give the same absolute z-score."""
        z1 = _z_score_two_proportions(0.1, 500, 0.2, 500)
        z2 = _z_score_two_proportions(0.2, 500, 0.1, 500)
        assert abs(abs(z1) - abs(z2)) < 0.001

    def test_z_to_confidence_known_values(self):
        """Known Z-scores should give approximately known confidence levels."""
        assert abs(_z_to_confidence(1.96) - 0.95) < 0.02
        assert abs(_z_to_confidence(2.576) - 0.99) < 0.02
        assert _z_to_confidence(0.0) < 0.1

    def test_required_sample_size_reasonable(self):
        """Sample size calculation should give reasonable numbers."""
        n = calculate_required_sample_size(baseline_rate=0.15)
        assert n >= 200
        assert n <= 10000  # Should be reasonable for 15% baseline

    def test_required_sample_size_edge_cases(self):
        """Edge cases shouldn't crash."""
        assert calculate_required_sample_size(0.0) == 200
        assert calculate_required_sample_size(1.0) == 200
        assert calculate_required_sample_size(0.5) >= 200

    def test_higher_baseline_needs_fewer_samples(self):
        """Higher baseline rates need fewer samples to detect same relative effect."""
        n_low = calculate_required_sample_size(baseline_rate=0.05)
        n_high = calculate_required_sample_size(baseline_rate=0.30)
        assert n_high < n_low

    def test_custom_confidence_threshold(self):
        """Custom confidence threshold should affect significance."""
        result_95 = evaluate_ab_test(
            a_sent=500, a_metric=50, b_sent=500, b_metric=75,
            confidence_threshold=0.95,
        )
        result_99 = evaluate_ab_test(
            a_sent=500, a_metric=50, b_sent=500, b_metric=75,
            confidence_threshold=0.99,
        )
        # Higher threshold is harder to reach
        if result_95["is_significant"]:
            assert result_95["confidence"] >= 0.95


# ---------------------------------------------------------------------------
# Sentiment Score Model — Database tests
# ---------------------------------------------------------------------------


class TestSentimentScoreModel:
    """Test EmailSentimentScore model persistence."""

    @pytest.mark.asyncio
    async def test_create_sentiment_score(self, db_session: AsyncSession, sample_contact: Contact):
        """Can create and retrieve a sentiment score."""
        score = EmailSentimentScore(
            id=uuid.uuid4(),
            contact_id=sample_contact.id,
            company_id=sample_contact.company_id,
            sentiment_score=7.5,
            sentiment_label="positive",
            intent="interested",
            intent_confidence=0.85,
            emotions={"interest": 0.8, "urgency": 0.3},
            buying_signals=["asked about pricing"],
            objections=[],
            recommended_action="schedule_demo",
            action_reasoning="Prospect shows clear interest and asked about pricing",
            analyzed_text="I'd love to see a demo. What does pricing look like?",
            model_used="claude-sonnet-4-6",
        )
        db_session.add(score)
        await db_session.flush()

        result = await db_session.execute(
            select(EmailSentimentScore).where(EmailSentimentScore.id == score.id)
        )
        loaded = result.scalar_one()
        assert loaded.sentiment_score == 7.5
        assert loaded.sentiment_label == "positive"
        assert loaded.intent == "interested"
        assert loaded.emotions["interest"] == 0.8
        assert "asked about pricing" in loaded.buying_signals

    @pytest.mark.asyncio
    async def test_sentiment_linked_to_reply(self, db_session: AsyncSession, sample_contact: Contact):
        """Sentiment score can be linked to an email reply."""
        reply = EmailReply(
            id=uuid.uuid4(),
            from_email="john@example.com",
            subject="Re: Test",
            body_text="Sounds interesting, tell me more.",
            received_at=datetime.now(timezone.utc),
            contact_id=sample_contact.id,
            company_id=sample_contact.company_id,
        )
        db_session.add(reply)
        await db_session.flush()

        score = EmailSentimentScore(
            id=uuid.uuid4(),
            email_reply_id=reply.id,
            contact_id=sample_contact.id,
            sentiment_score=7.0,
            sentiment_label="positive",
            intent="interested",
            intent_confidence=0.75,
        )
        db_session.add(score)
        await db_session.flush()

        result = await db_session.execute(
            select(EmailSentimentScore).where(EmailSentimentScore.email_reply_id == reply.id)
        )
        loaded = result.scalar_one()
        assert loaded.email_reply_id == reply.id
        assert loaded.sentiment_score == 7.0


# ---------------------------------------------------------------------------
# A/B Test Experiment Model
# ---------------------------------------------------------------------------


class TestABTestExperimentModel:
    """Test ABTestExperiment model persistence."""

    @pytest.mark.asyncio
    async def test_create_experiment(self, db_session: AsyncSession):
        """Can create an A/B test experiment."""
        exp = ABTestExperiment(
            id=uuid.uuid4(),
            name="Subject Line Test: Fear vs Curiosity",
            variant_a_subject="who takes the blame when cavity 4 goes bad?",
            variant_b_subject="the part that keeps you up at night",
            variant_a_sent=100,
            variant_a_opened=25,
            variant_b_sent=100,
            variant_b_opened=35,
            status="running",
        )
        db_session.add(exp)
        await db_session.flush()

        result = await db_session.execute(
            select(ABTestExperiment).where(ABTestExperiment.id == exp.id)
        )
        loaded = result.scalar_one()
        assert loaded.name == "Subject Line Test: Fear vs Curiosity"
        assert loaded.variant_a_opened == 25
        assert loaded.variant_b_opened == 35
        assert loaded.status == "running"

    @pytest.mark.asyncio
    async def test_experiment_winner_update(self, db_session: AsyncSession):
        """Can update an experiment with winner results."""
        exp = ABTestExperiment(
            id=uuid.uuid4(),
            name="Test Experiment",
            variant_a_subject="Subject A",
            variant_b_subject="Subject B",
            variant_a_sent=500,
            variant_a_opened=50,
            variant_b_sent=500,
            variant_b_opened=100,
        )
        db_session.add(exp)
        await db_session.flush()

        exp.winner = "B"
        exp.confidence_level = 0.97
        exp.is_significant = True
        exp.status = "completed"
        exp.completed_at = datetime.now(timezone.utc)
        await db_session.flush()

        result = await db_session.execute(
            select(ABTestExperiment).where(ABTestExperiment.id == exp.id)
        )
        loaded = result.scalar_one()
        assert loaded.winner == "B"
        assert loaded.is_significant is True


# ---------------------------------------------------------------------------
# Email Test Exchange Model
# ---------------------------------------------------------------------------


class TestEmailTestExchangeModel:
    """Test EmailTestExchange model persistence."""

    @pytest.mark.asyncio
    async def test_create_exchange(self, db_session: AsyncSession):
        """Can create a test exchange with messages."""
        exchange = EmailTestExchange(
            id=uuid.uuid4(),
            name="Test: positive_lead - Alex Chen",
            scenario="positive_lead",
            persona_name="Alex Chen",
            persona_title="VP Operations",
            persona_company="Precision Metal Works",
            industry="CNC Machining",
            messages=[
                {"role": "sender", "subject": "Test", "body": "Hello"},
                {"role": "prospect", "subject": "Re: Test", "body": "Interested!",
                 "sentiment_score": 8.0, "intent": "interested"},
            ],
            total_messages=2,
            avg_sentiment=8.0,
            status="active",
        )
        db_session.add(exchange)
        await db_session.flush()

        result = await db_session.execute(
            select(EmailTestExchange).where(EmailTestExchange.id == exchange.id)
        )
        loaded = result.scalar_one()
        assert loaded.scenario == "positive_lead"
        assert len(loaded.messages) == 2
        assert loaded.messages[1]["sentiment_score"] == 8.0

    @pytest.mark.asyncio
    async def test_exchange_completion(self, db_session: AsyncSession):
        """Can mark an exchange as completed with outcome."""
        exchange = EmailTestExchange(
            id=uuid.uuid4(),
            name="Test Exchange",
            scenario="meeting_request",
            messages=[],
            total_messages=0,
            status="active",
        )
        db_session.add(exchange)
        await db_session.flush()

        exchange.status = "completed"
        exchange.outcome = "meeting_booked"
        exchange.completed_at = datetime.now(timezone.utc)
        await db_session.flush()

        result = await db_session.execute(
            select(EmailTestExchange).where(EmailTestExchange.id == exchange.id)
        )
        loaded = result.scalar_one()
        assert loaded.status == "completed"
        assert loaded.outcome == "meeting_booked"


# ---------------------------------------------------------------------------
# Sentiment Analysis API Endpoints
# ---------------------------------------------------------------------------


class TestSentimentAPI:
    """Test sentiment API endpoints."""

    @pytest.mark.asyncio
    async def test_analyze_sentiment_endpoint(self, client: AsyncClient):
        """POST /sentiment/analyze should return sentiment analysis."""
        with patch("app.routers.sentiment.analyzer") as mock_analyzer:
            mock_analyzer.analyze_reply = AsyncMock(return_value={
                "sentiment_score": 7.5,
                "sentiment_label": "positive",
                "intent": "interested",
                "intent_confidence": 0.85,
                "emotions": {"interest": 0.8},
                "buying_signals": ["asked about demo"],
                "objections": [],
                "recommended_action": "schedule_demo",
                "action_reasoning": "Clear interest shown",
                "model_used": "claude-sonnet-4-6",
            })

            resp = await client.post("/api/v1/sentiment/analyze", json={
                "text": "Sounds interesting! Can we schedule a demo next week?",
                "contact_name": "John Smith",
                "company_name": "Acme Corp",
            })

            assert resp.status_code == 200
            data = resp.json()
            assert data["sentiment_score"] == 7.5
            assert data["intent"] == "interested"
            assert data["recommended_action"] == "schedule_demo"
            assert data["saved"] is False  # No IDs provided

    @pytest.mark.asyncio
    async def test_analyze_and_save_sentiment(
        self, client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
    ):
        """POST /sentiment/analyze with contact_id should save to DB."""
        with patch("app.routers.sentiment.analyzer") as mock_analyzer:
            mock_analyzer.analyze_reply = AsyncMock(return_value={
                "sentiment_score": 3.0,
                "sentiment_label": "negative",
                "intent": "not_interested",
                "intent_confidence": 0.9,
                "emotions": {"frustration": 0.6},
                "buying_signals": [],
                "objections": ["budget constraints"],
                "recommended_action": "mark_not_interested",
                "action_reasoning": "Clear decline",
                "model_used": "claude-sonnet-4-6",
            })

            resp = await client.post("/api/v1/sentiment/analyze", json={
                "text": "Not interested, please remove me from your list.",
                "contact_id": str(sample_contact.id),
                "company_id": str(sample_contact.company_id),
            })

            assert resp.status_code == 200
            data = resp.json()
            assert data["saved"] is True
            assert data["id"] is not None

    @pytest.mark.asyncio
    async def test_list_sentiment_scores(self, client: AsyncClient):
        """GET /sentiment/scores should return a list."""
        resp = await client.get("/api/v1/sentiment/scores")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_sentiment_summary(self, client: AsyncClient):
        """GET /sentiment/summary should return a summary."""
        resp = await client.get("/api/v1/sentiment/summary?days=30")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_analyzed" in data
        assert "avg_sentiment" in data
        assert "trend" in data


# ---------------------------------------------------------------------------
# Email Testing API Endpoints
# ---------------------------------------------------------------------------


class TestEmailTestingAPI:
    """Test email testing/simulation API endpoints."""

    @pytest.mark.asyncio
    async def test_list_scenarios(self, client: AsyncClient):
        """GET /email-testing/scenarios should return all scenarios."""
        resp = await client.get("/api/v1/email-testing/scenarios")
        assert resp.status_code == 200
        scenarios = resp.json()
        assert len(scenarios) >= 8
        scenario_ids = [s["id"] for s in scenarios]
        assert "positive_lead" in scenario_ids
        assert "price_objection" in scenario_ids
        assert "aggressive_no" in scenario_ids

    @pytest.mark.asyncio
    async def test_list_personas(self, client: AsyncClient):
        """GET /email-testing/personas should return personas."""
        resp = await client.get("/api/v1/email-testing/personas")
        assert resp.status_code == 200
        personas = resp.json()
        assert len(personas) >= 4

    @pytest.mark.asyncio
    async def test_create_test_exchange(self, client: AsyncClient):
        """POST /email-testing/exchanges should create a test exchange."""
        with patch("app.routers.email_testing.testing_service") as mock_service:
            mock_service.create_test_exchange = AsyncMock(return_value={
                "id": str(uuid.uuid4()),
                "name": "Test: positive_lead - Alex Chen",
                "scenario": "positive_lead",
                "persona_name": "Alex Chen",
                "persona_title": "VP Operations",
                "persona_company": "Precision Metal Works",
                "industry": "CNC Machining",
                "messages": [
                    {"role": "sender", "subject": "Test", "body": "Hello", "timestamp": "2026-07-02T00:00:00Z", "step_number": 1},
                    {"role": "prospect", "subject": "Re: Test", "body": "Interested!",
                     "timestamp": "2026-07-02T00:00:00Z",
                     "sentiment_score": 8.0, "sentiment_label": "positive",
                     "intent": "interested", "emotions": {}, "buying_signals": [],
                     "objections": [], "recommended_action": "schedule_demo"},
                ],
                "total_messages": 2,
                "avg_sentiment": 8.0,
                "outcome": None,
                "status": "active",
                "created_at": "2026-07-02T00:00:00Z",
            })

            resp = await client.post("/api/v1/email-testing/exchanges", json={
                "scenario": "positive_lead",
                "persona_name": "Alex Chen",
            })

            assert resp.status_code == 200
            data = resp.json()
            assert data["scenario"] == "positive_lead"
            assert len(data["messages"]) == 2

    @pytest.mark.asyncio
    async def test_list_test_exchanges(self, client: AsyncClient):
        """GET /email-testing/exchanges should return a list."""
        resp = await client.get("/api/v1/email-testing/exchanges")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_get_nonexistent_exchange(self, client: AsyncClient):
        """GET /email-testing/exchanges/<invalid> should return 404."""
        fake_id = str(uuid.uuid4())
        resp = await client.get(f"/api/v1/email-testing/exchanges/{fake_id}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Sentiment Service — Unit tests with mocked Claude
# ---------------------------------------------------------------------------


class TestSentimentService:
    """Test the SentimentAnalyzer service with mocked AI calls."""

    @pytest.mark.asyncio
    async def test_analyze_reply_positive(self):
        """Should correctly analyze a positive reply."""
        from app.services.sentiment_service import SentimentAnalyzer

        analyzer = SentimentAnalyzer()

        with patch.object(analyzer, "client") as mock_client:
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text='{"sentiment_score": 8.5, "intent": "meeting_request", "intent_confidence": 0.92, "emotions": {"interest": 0.9, "urgency": 0.5}, "buying_signals": ["requested demo", "mentioned timeline"], "objections": [], "recommended_action": "schedule_demo", "action_reasoning": "Strong buying intent"}')]
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            result = await analyzer.analyze_reply(
                reply_text="That sounds great! Can we set up a demo next Tuesday? We're looking to implement something like this in Q3.",
                contact_name="John Smith",
                company_name="Acme Corp",
            )

            assert result["sentiment_score"] == 8.5
            assert result["intent"] == "meeting_request"
            assert result["intent_confidence"] == 0.92
            assert "requested demo" in result["buying_signals"]

    @pytest.mark.asyncio
    async def test_analyze_reply_negative(self):
        """Should correctly analyze a negative reply."""
        from app.services.sentiment_service import SentimentAnalyzer

        analyzer = SentimentAnalyzer()

        with patch.object(analyzer, "client") as mock_client:
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text='{"sentiment_score": 2.0, "intent": "not_interested", "intent_confidence": 0.95, "emotions": {"frustration": 0.7}, "buying_signals": [], "objections": ["not relevant"], "recommended_action": "mark_not_interested", "action_reasoning": "Clear rejection"}')]
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            result = await analyzer.analyze_reply(
                reply_text="Please stop emailing me. We have no interest in this.",
            )

            assert result["sentiment_score"] == 2.0
            assert result["intent"] == "not_interested"
            assert result["recommended_action"] == "mark_not_interested"

    @pytest.mark.asyncio
    async def test_analyze_reply_error_returns_defaults(self):
        """Should return safe defaults when AI call fails."""
        from app.services.sentiment_service import SentimentAnalyzer

        analyzer = SentimentAnalyzer()

        with patch.object(analyzer, "client") as mock_client:
            mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))

            result = await analyzer.analyze_reply(reply_text="Some text")

            assert result["sentiment_score"] == 5.0
            assert result["sentiment_label"] == "neutral"
            assert result["intent"] == "auto_reply"
            assert "error" in result

    @pytest.mark.asyncio
    async def test_score_to_label_mapping(self):
        """Score-to-label mapping should be consistent."""
        from app.services.sentiment_service import SentimentAnalyzer

        analyzer = SentimentAnalyzer()

        assert analyzer._score_to_label(1.5) == "negative"
        assert analyzer._score_to_label(3.5) == "mixed"
        assert analyzer._score_to_label(5.0) == "neutral"
        assert analyzer._score_to_label(7.0) == "positive"
        assert analyzer._score_to_label(9.0) == "very_positive"

    @pytest.mark.asyncio
    async def test_generate_test_reply(self):
        """Should generate a test reply for a given scenario."""
        from app.services.sentiment_service import SentimentAnalyzer

        analyzer = SentimentAnalyzer()

        with patch.object(analyzer, "client") as mock_client:
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text='{"subject": "Re: Test Subject", "body": "Thanks for reaching out. I am interested in learning more about your solution.", "expected_sentiment_score": 7.5, "expected_intent": "interested"}')]
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            result = await analyzer.generate_test_reply(
                scenario="positive_lead",
                original_subject="Test Subject",
                original_body="Test body",
            )

            assert "subject" in result
            assert "body" in result
            assert result["expected_sentiment_score"] == 7.5


# ---------------------------------------------------------------------------
# Email Testing Service — Unit tests
# ---------------------------------------------------------------------------


class TestEmailTestingService:
    """Test the EmailTestingService."""

    def test_available_scenarios(self):
        """Should return all predefined scenarios."""
        from app.services.email_testing_service import EmailTestingService

        service = EmailTestingService()
        scenarios = service.get_available_scenarios()
        assert len(scenarios) >= 8
        ids = [s["id"] for s in scenarios]
        assert "positive_lead" in ids
        assert "meeting_request" in ids

    def test_available_personas(self):
        """Should return predefined personas."""
        from app.services.email_testing_service import EmailTestingService

        service = EmailTestingService()
        personas = service.get_available_personas()
        assert len(personas) >= 4
        assert any(p["name"] == "Alex Chen" for p in personas)

    @pytest.mark.asyncio
    async def test_create_test_exchange(self):
        """Should create a test exchange with AI-generated reply."""
        from app.services.email_testing_service import EmailTestingService

        service = EmailTestingService()

        with patch.object(service.sentiment_analyzer, "generate_test_reply", new_callable=AsyncMock) as mock_gen, \
             patch.object(service.sentiment_analyzer, "analyze_reply", new_callable=AsyncMock) as mock_analyze:

            mock_gen.return_value = {
                "subject": "Re: Test",
                "body": "Thanks, I'm interested in learning more.",
                "expected_sentiment_score": 7.5,
                "expected_intent": "interested",
            }
            mock_analyze.return_value = {
                "sentiment_score": 7.5,
                "sentiment_label": "positive",
                "intent": "interested",
                "intent_confidence": 0.8,
                "emotions": {"interest": 0.8},
                "buying_signals": [],
                "objections": [],
                "recommended_action": "schedule_demo",
                "action_reasoning": "Interest shown",
                "model_used": "claude-sonnet-4-6",
            }

            result = await service.create_test_exchange(scenario="positive_lead")

            assert result["scenario"] == "positive_lead"
            assert len(result["messages"]) == 2
            assert result["messages"][0]["role"] == "sender"
            assert result["messages"][1]["role"] == "prospect"
            assert result["messages"][1]["sentiment_score"] == 7.5
            assert result["status"] == "active"
