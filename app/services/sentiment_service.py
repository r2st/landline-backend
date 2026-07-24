"""Sentiment analysis service for email replies.

Uses Claude AI to analyze the emotional tone, intent, and buying signals
in email replies from prospects. Provides actionable insights for sales
follow-up decisions.

Research-backed approach (2025-2026):
- Aspect-based sentiment analysis (not just polarity)
- Emotion detection at sentence level
- Buying signal identification
- Objection mapping
- Recommended next action generation
"""

import logging
import json
from typing import Optional

from app.config import get_settings
from app.services.llm_providers import chain_client

logger = logging.getLogger(__name__)


class SentimentAnalyzer:
    """Analyzes email reply sentiment using Claude AI.

    Goes beyond simple positive/negative classification to provide:
    - Sentiment score (1-10 scale)
    - Emotion breakdown (interest, urgency, frustration, etc.)
    - Intent classification with confidence
    - Buying signal detection
    - Objection identification
    - Recommended follow-up action
    """

    SENTIMENT_LABELS = {
        (1, 3): "negative",
        (3, 4.5): "mixed",
        (4.5, 6.5): "neutral",
        (6.5, 8): "positive",
        (8, 10.1): "very_positive",
    }

    def __init__(self):
        settings = get_settings()
        # Chain-backed: reply sentiment is a Dialtone hot path, and it was
        # Anthropic-only, so it went dark the moment those credits ran out.
        self.client = chain_client(settings.anthropic_api_key)
        self.model = "claude-sonnet-4-6"

    def _score_to_label(self, score: float) -> str:
        for (lo, hi), label in self.SENTIMENT_LABELS.items():
            if lo <= score < hi:
                return label
        return "neutral"

    def _parse_json_response(self, response_text: str) -> dict:
        """Parse JSON from Claude response, handling markdown fences."""
        text = response_text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
            end = max(text.rfind("}"), text.rfind("]"))
            if start != -1 and end > start:
                return json.loads(text[start : end + 1])
            raise

    async def analyze_reply(
        self,
        reply_text: str,
        original_email_subject: Optional[str] = None,
        original_email_body: Optional[str] = None,
        contact_name: Optional[str] = None,
        company_name: Optional[str] = None,
        industry: Optional[str] = None,
    ) -> dict:
        """Analyze an email reply for sentiment, intent, and buying signals.

        Returns a dict with:
        - sentiment_score: float (1-10)
        - sentiment_label: str
        - intent: str
        - intent_confidence: float (0-1)
        - emotions: dict of emotion:intensity pairs
        - buying_signals: list of detected signals
        - objections: list of detected objections
        - recommended_action: str
        - action_reasoning: str
        """
        context_parts = []
        if original_email_subject:
            context_parts.append(f"Original email subject: {original_email_subject}")
        if original_email_body:
            # Truncate to avoid token bloat
            body_preview = original_email_body[:500]
            context_parts.append(f"Original email body (preview): {body_preview}")
        if contact_name:
            context_parts.append(f"Contact: {contact_name}")
        if company_name:
            context_parts.append(f"Company: {company_name}")
        if industry:
            context_parts.append(f"Industry: {industry}")

        context_str = "\n".join(context_parts) if context_parts else "No additional context."

        prompt = f"""You are an expert B2B sales reply analyst. Analyze this email reply from a prospect and provide a detailed sentiment assessment.

CONTEXT:
{context_str}

REPLY TO ANALYZE:
{reply_text}

Analyze the reply and return a JSON object with these fields:

{{
  "sentiment_score": <float 1-10, where 1=very negative/hostile, 5=neutral, 10=very positive/enthusiastic>,
  "intent": "<one of: interested, meeting_request, question, not_now, not_interested, objection, referral, auto_reply, out_of_office>",
  "intent_confidence": <float 0-1>,
  "emotions": {{
    "interest": <0-1>,
    "urgency": <0-1>,
    "frustration": <0-1>,
    "satisfaction": <0-1>,
    "curiosity": <0-1>,
    "skepticism": <0-1>,
    "politeness": <0-1>
  }},
  "buying_signals": [<list of specific buying signals detected, e.g. "asked about pricing", "mentioned timeline", "requested demo", "mentioned budget approval">],
  "objections": [<list of objections raised, e.g. "budget constraints", "bad timing", "using competitor", "need internal approval", "too small/early stage">],
  "recommended_action": "<one of: schedule_demo, send_case_study, send_pricing, follow_up_later, address_objection, nurture_sequence, connect_referral, mark_not_interested, no_action>",
  "action_reasoning": "<1-2 sentence explanation of why this action is recommended>",
  "competitor_mentions": [<names of any competing vendors / software products the prospect names — e.g. an incumbent ERP, MES, or quality-software they use or are evaluating. Empty list if none.>]
}}

Be precise and calibrated. A polite decline is not_interested (score ~3), not neutral.
A question about features is curious/interested (score ~7), not just neutral.
An auto-reply or out-of-office is truly neutral (score 5).
"Let's set up a call" is meeting_request with high interest (score ~9).
Return ONLY the JSON object."""

        try:
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = message.content[0].text
            result = self._parse_json_response(response_text)

            # Ensure all required fields exist with defaults
            score = float(result.get("sentiment_score", 5.0))
            score = max(1.0, min(10.0, score))  # Clamp to valid range

            return {
                "sentiment_score": score,
                "sentiment_label": self._score_to_label(score),
                "intent": result.get("intent", "auto_reply"),
                "intent_confidence": float(result.get("intent_confidence", 0.5)),
                "emotions": result.get("emotions", {}),
                "buying_signals": result.get("buying_signals", []),
                "objections": result.get("objections", []),
                "recommended_action": result.get("recommended_action", "no_action"),
                "action_reasoning": result.get("action_reasoning", ""),
                "competitor_mentions": result.get("competitor_mentions", []) or [],
                "model_used": self.model,
            }
        except Exception as e:
            logger.error("Sentiment analysis failed: %s", str(e))
            return {
                "sentiment_score": 5.0,
                "sentiment_label": "neutral",
                "intent": "auto_reply",
                "intent_confidence": 0.0,
                "emotions": {},
                "buying_signals": [],
                "objections": [],
                "recommended_action": "no_action",
                "action_reasoning": f"Analysis failed: {str(e)}",
                "competitor_mentions": [],
                "model_used": self.model,
                "error": str(e),
            }

    async def analyze_batch(self, replies: list[dict]) -> list[dict]:
        """Analyze multiple replies. Each dict should have 'text' and optionally
        'original_subject', 'original_body', 'contact_name', 'company_name', 'industry'."""
        results = []
        for reply in replies:
            result = await self.analyze_reply(
                reply_text=reply["text"],
                original_email_subject=reply.get("original_subject"),
                original_email_body=reply.get("original_body"),
                contact_name=reply.get("contact_name"),
                company_name=reply.get("company_name"),
                industry=reply.get("industry"),
            )
            results.append(result)
        return results

    async def generate_test_reply(
        self,
        scenario: str,
        original_subject: str,
        original_body: str,
        persona_name: str = "Alex Chen",
        persona_title: str = "VP Operations",
        persona_company: str = "Precision Manufacturing Co",
        industry: str = "CNC Machining",
    ) -> dict:
        """Generate a realistic test reply for a given scenario.

        Scenarios: positive_lead, price_objection, competitor_using,
        not_now, referral, aggressive_no, curious_questions, meeting_request
        """
        scenario_descriptions = {
            "positive_lead": "The prospect is genuinely interested. They want to learn more and are open to a demo. They mention a specific pain point the product could solve.",
            "price_objection": "The prospect likes the concept but has budget concerns. They ask about pricing and whether there's a smaller pilot or free trial.",
            "competitor_using": "The prospect already uses a competitor product. They're somewhat satisfied but open to hearing how this is different.",
            "not_now": "The prospect is polite but busy. They say timing isn't right — maybe in Q3/Q4. They don't completely close the door.",
            "referral": "The prospect isn't the right person but offers to connect you with someone who might be interested.",
            "aggressive_no": "The prospect is annoyed by the cold email. They firmly decline and ask to be removed from the list.",
            "curious_questions": "The prospect is intrigued and asks 2-3 specific questions about the product, use cases, or ROI.",
            "meeting_request": "The prospect is very interested and proactively suggests scheduling a call or demo.",
        }

        scenario_desc = scenario_descriptions.get(scenario, scenario_descriptions["positive_lead"])

        prompt = f"""Generate a realistic email reply from a prospect. This is for testing a cold email system.

ORIGINAL EMAIL:
Subject: {original_subject}
Body: {original_body}

PROSPECT PERSONA:
Name: {persona_name}
Title: {persona_title}
Company: {persona_company}
Industry: {industry}

SCENARIO: {scenario}
{scenario_desc}

Generate a realistic reply that a real {persona_title} would write. Keep it natural — varying length, tone, and formality based on the scenario. Include typos occasionally for realism.

Return a JSON object:
{{
  "subject": "Re: {original_subject}",
  "body": "<the reply body text, no HTML>",
  "expected_sentiment_score": <float 1-10>,
  "expected_intent": "<expected intent classification>"
}}

Return ONLY the JSON object."""

        try:
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = message.content[0].text
            return self._parse_json_response(response_text)
        except Exception as e:
            logger.error("Test reply generation failed: %s", str(e))
            return {
                "subject": f"Re: {original_subject}",
                "body": f"Thank you for reaching out. [Test reply generation failed: {str(e)}]",
                "expected_sentiment_score": 5.0,
                "expected_intent": "auto_reply",
                "error": str(e),
            }
