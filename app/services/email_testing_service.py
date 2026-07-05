"""Email testing and simulation service.

Enables testing cold email sequences by simulating multi-message conversations
with AI-generated prospect replies. Each test exchange creates a realistic
back-and-forth conversation that can be analyzed for sentiment trends.

Research-backed scenarios reflect real B2B cold email response patterns:
- 58% of replies come from the first email
- Top campaigns achieve 10.7%+ reply rates
- Most common objections: timing, budget, competitor
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.services.sentiment_service import SentimentAnalyzer
from app.services.outreach_templates import (
    STEP_1_SUBJECT, STEP_1_BODY,
    STEP_2_SUBJECT, STEP_2_BODY,
    STEP_3_SUBJECT, STEP_3_BODY,
    STEP_4_SUBJECT, STEP_4_BODY,
)
from app.services.email_compose import render

logger = logging.getLogger(__name__)

# Pre-defined test scenarios with expected outcome profiles
TEST_SCENARIOS = {
    "positive_lead": {
        "description": "Interested prospect who wants to learn more",
        "expected_outcome": "meeting_booked",
        "reply_probability": [0.7, 0.8, 0.5, 0.3],  # per step
    },
    "price_objection": {
        "description": "Likes the product but worried about cost",
        "expected_outcome": "nurture",
        "reply_probability": [0.4, 0.6, 0.3, 0.2],
    },
    "competitor_using": {
        "description": "Already using a competitor product",
        "expected_outcome": "nurture",
        "reply_probability": [0.3, 0.4, 0.2, 0.1],
    },
    "not_now": {
        "description": "Polite but timing isn't right",
        "expected_outcome": "nurture",
        "reply_probability": [0.5, 0.3, 0.2, 0.1],
    },
    "referral": {
        "description": "Not the right person, offers referral",
        "expected_outcome": "nurture",
        "reply_probability": [0.6, 0.2, 0.1, 0.0],
    },
    "aggressive_no": {
        "description": "Annoyed by cold email, asks to be removed",
        "expected_outcome": "lost",
        "reply_probability": [0.5, 0.1, 0.0, 0.0],
    },
    "curious_questions": {
        "description": "Asks detailed questions about capabilities",
        "expected_outcome": "meeting_booked",
        "reply_probability": [0.6, 0.7, 0.5, 0.3],
    },
    "meeting_request": {
        "description": "Very interested, proactively requests meeting",
        "expected_outcome": "meeting_booked",
        "reply_probability": [0.8, 0.3, 0.1, 0.0],
    },
}

# Default test personas representing typical B2B manufacturing contacts
DEFAULT_PERSONAS = [
    {
        "name": "Alex Chen",
        "title": "VP Operations",
        "company": "Precision Metal Works Inc",
        "industry": "CNC Machining",
    },
    {
        "name": "Sarah Thompson",
        "title": "Plant Manager",
        "company": "Great Lakes Injection Molding",
        "industry": "Plastic Injection Molding",
    },
    {
        "name": "Mike Rodriguez",
        "title": "Quality Director",
        "company": "Valley Stamping Solutions",
        "industry": "Metal Stamping",
    },
    {
        "name": "Jennifer Park",
        "title": "VP Manufacturing",
        "company": "Pacific Plastics Group",
        "industry": "Plastic Injection Molding",
    },
]


class EmailTestingService:
    """Orchestrates email test exchanges with AI-generated responses."""

    def __init__(self):
        self.sentiment_analyzer = SentimentAnalyzer()

    def get_available_scenarios(self) -> list[dict]:
        """Return all available test scenarios."""
        return [
            {"id": key, **value}
            for key, value in TEST_SCENARIOS.items()
        ]

    def get_available_personas(self) -> list[dict]:
        """Return all available test personas."""
        return DEFAULT_PERSONAS

    async def create_test_exchange(
        self,
        scenario: str,
        persona_name: Optional[str] = None,
        persona_title: Optional[str] = None,
        persona_company: Optional[str] = None,
        industry: Optional[str] = None,
        custom_first_email_subject: Optional[str] = None,
        custom_first_email_body: Optional[str] = None,
    ) -> dict:
        """Create a new test exchange with an initial outreach email and AI-generated reply.

        Returns the exchange data including the first message pair and sentiment analysis.
        """
        if scenario not in TEST_SCENARIOS:
            scenario = "positive_lead"

        # Use default persona if not specified
        persona = DEFAULT_PERSONAS[0]
        if persona_name:
            persona["name"] = persona_name
        if persona_title:
            persona["title"] = persona_title
        if persona_company:
            persona["company"] = persona_company
        if industry:
            persona["industry"] = industry

        # Build merge context
        first_name = persona["name"].split()[0]
        context = {
            "first_name": first_name,
            "last_name": persona["name"].split()[-1] if len(persona["name"].split()) > 1 else "",
            "company_name": persona["company"],
            "title": persona["title"],
            "demo_link": "https://best-molding-demo.pages.dev",
        }

        # Use custom email or default Step 1
        if custom_first_email_subject and custom_first_email_body:
            subject = render(custom_first_email_subject, context)
            body = render(custom_first_email_body, context)
        else:
            subject = render(STEP_1_SUBJECT, context)
            body = render(STEP_1_BODY, context)

        # Strip HTML for the body text
        import re
        body_text = re.sub(r'<[^>]+>', '', body).strip()
        body_text = re.sub(r'\s+', ' ', body_text)

        # Generate AI reply
        reply = await self.sentiment_analyzer.generate_test_reply(
            scenario=scenario,
            original_subject=subject,
            original_body=body_text,
            persona_name=persona["name"],
            persona_title=persona["title"],
            persona_company=persona["company"],
            industry=persona.get("industry", "Manufacturing"),
        )

        # Analyze the generated reply's sentiment
        sentiment = await self.sentiment_analyzer.analyze_reply(
            reply_text=reply.get("body", ""),
            original_email_subject=subject,
            original_email_body=body_text,
            contact_name=persona["name"],
            company_name=persona["company"],
            industry=persona.get("industry"),
        )

        now = datetime.now(timezone.utc).isoformat()

        messages = [
            {
                "role": "sender",
                "subject": subject,
                "body": body_text,
                "timestamp": now,
                "step_number": 1,
            },
            {
                "role": "prospect",
                "subject": reply.get("subject", f"Re: {subject}"),
                "body": reply.get("body", ""),
                "timestamp": now,
                "sentiment_score": sentiment["sentiment_score"],
                "sentiment_label": sentiment["sentiment_label"],
                "intent": sentiment["intent"],
                "emotions": sentiment["emotions"],
                "buying_signals": sentiment["buying_signals"],
                "objections": sentiment["objections"],
                "recommended_action": sentiment["recommended_action"],
            },
        ]

        return {
            "id": str(uuid.uuid4()),
            "name": f"Test: {scenario} - {persona['name']}",
            "scenario": scenario,
            "persona_name": persona["name"],
            "persona_title": persona["title"],
            "persona_company": persona["company"],
            "industry": persona.get("industry", "Manufacturing"),
            "messages": messages,
            "total_messages": 2,
            "avg_sentiment": sentiment["sentiment_score"],
            "outcome": None,
            "status": "active",
            "created_at": now,
        }

    async def continue_exchange(
        self,
        exchange: dict,
        follow_up_subject: Optional[str] = None,
        follow_up_body: Optional[str] = None,
        step_number: int = 2,
    ) -> dict:
        """Continue an existing test exchange with a follow-up email and AI reply.

        If no follow-up content provided, uses the appropriate sequence step template.
        """
        messages = exchange.get("messages", [])
        scenario = exchange.get("scenario", "positive_lead")

        persona = {
            "name": exchange.get("persona_name", "Alex Chen"),
            "title": exchange.get("persona_title", "VP Operations"),
            "company": exchange.get("persona_company", "Precision Metal Works Inc"),
        }
        industry = exchange.get("industry", "Manufacturing")

        # Build merge context
        first_name = persona["name"].split()[0]
        context = {
            "first_name": first_name,
            "last_name": persona["name"].split()[-1] if len(persona["name"].split()) > 1 else "",
            "company_name": persona["company"],
            "title": persona["title"],
            "demo_link": "https://best-molding-demo.pages.dev",
        }

        # Use custom follow-up or default template
        step_templates = {
            2: (STEP_2_SUBJECT, STEP_2_BODY),
            3: (STEP_3_SUBJECT, STEP_3_BODY),
            4: (STEP_4_SUBJECT, STEP_4_BODY),
        }

        if follow_up_subject and follow_up_body:
            subject = render(follow_up_subject, context)
            body = render(follow_up_body, context)
        elif step_number in step_templates:
            subj_tmpl, body_tmpl = step_templates[step_number]
            subject = render(subj_tmpl, context)
            body = render(body_tmpl, context)
        else:
            subject = f"Following up, {first_name}"
            body = f"Hi {first_name}, just checking in on my previous email."

        import re
        body_text = re.sub(r'<[^>]+>', '', body).strip()
        body_text = re.sub(r'\s+', ' ', body_text)

        now = datetime.now(timezone.utc).isoformat()

        # Add the follow-up message
        messages.append({
            "role": "sender",
            "subject": subject,
            "body": body_text,
            "timestamp": now,
            "step_number": step_number,
        })

        # Generate AI reply
        reply = await self.sentiment_analyzer.generate_test_reply(
            scenario=scenario,
            original_subject=subject,
            original_body=body_text,
            persona_name=persona["name"],
            persona_title=persona["title"],
            persona_company=persona["company"],
            industry=industry,
        )

        # Analyze sentiment
        sentiment = await self.sentiment_analyzer.analyze_reply(
            reply_text=reply.get("body", ""),
            original_email_subject=subject,
            original_email_body=body_text,
            contact_name=persona["name"],
            company_name=persona["company"],
            industry=industry,
        )

        messages.append({
            "role": "prospect",
            "subject": reply.get("subject", f"Re: {subject}"),
            "body": reply.get("body", ""),
            "timestamp": now,
            "sentiment_score": sentiment["sentiment_score"],
            "sentiment_label": sentiment["sentiment_label"],
            "intent": sentiment["intent"],
            "emotions": sentiment["emotions"],
            "buying_signals": sentiment["buying_signals"],
            "objections": sentiment["objections"],
            "recommended_action": sentiment["recommended_action"],
        })

        # Calculate average sentiment across all prospect replies
        prospect_scores = [
            m["sentiment_score"]
            for m in messages
            if m["role"] == "prospect" and "sentiment_score" in m
        ]
        avg_sentiment = sum(prospect_scores) / len(prospect_scores) if prospect_scores else 5.0

        # Determine outcome based on last reply
        outcome = None
        if sentiment["intent"] in ("meeting_request", "interested") and sentiment["sentiment_score"] >= 7:
            outcome = "meeting_booked"
        elif sentiment["intent"] == "not_interested" or sentiment["sentiment_score"] <= 3:
            outcome = "lost"
        elif step_number >= 4:
            outcome = "no_response" if sentiment["intent"] == "auto_reply" else "nurture"

        exchange.update({
            "messages": messages,
            "total_messages": len(messages),
            "avg_sentiment": round(avg_sentiment, 2),
            "outcome": outcome,
            "status": "completed" if outcome else "active",
        })

        return exchange
