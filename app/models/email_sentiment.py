"""Email sentiment analysis models — tracks sentiment scores and analysis for email replies."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class EmailSentimentScore(Base):
    """Sentiment analysis result for an email reply.

    Stores both the overall sentiment score and granular emotion/intent
    classifications. Used for tracking reply quality trends and automating
    follow-up decisions.
    """
    __tablename__ = "email_sentiment_scores"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email_reply_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_replies.id", ondelete="CASCADE"), nullable=True, index=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Overall sentiment: 1-10 scale (1=very negative, 5=neutral, 10=very positive)
    sentiment_score: Mapped[float] = mapped_column(Float, nullable=False)
    sentiment_label: Mapped[str] = mapped_column(String(20), nullable=False)  # positive/negative/neutral/mixed

    # Intent classification
    intent: Mapped[str] = mapped_column(String(30), nullable=False)  # interested/not_now/not_interested/question/referral/auto_reply/objection/meeting_request
    intent_confidence: Mapped[float] = mapped_column(Float, default=0.0)  # 0.0-1.0

    # Granular emotions (stored as JSON dict with emotion: intensity pairs)
    emotions: Mapped[dict] = mapped_column(JSONB, default=dict)
    # e.g. {"interest": 0.8, "urgency": 0.3, "frustration": 0.1, "satisfaction": 0.0}

    # Buying signals detected
    buying_signals: Mapped[list] = mapped_column(JSONB, default=list)
    # e.g. ["asked about pricing", "mentioned timeline", "requested demo"]

    # Objections detected
    objections: Mapped[list] = mapped_column(JSONB, default=list)
    # e.g. ["budget concerns", "timing not right", "using competitor"]

    # Recommended next action
    recommended_action: Mapped[str | None] = mapped_column(String(50))
    # e.g. "schedule_demo", "send_case_study", "follow_up_later", "mark_not_interested"
    action_reasoning: Mapped[str | None] = mapped_column(Text)

    # The analyzed text (snippet for reference)
    analyzed_text: Mapped[str | None] = mapped_column(Text)

    # AI model used
    model_used: Mapped[str | None] = mapped_column(String(50))

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    email_reply: Mapped["EmailReply | None"] = relationship("EmailReply")
    contact: Mapped["Contact | None"] = relationship("Contact")
    company: Mapped["Company | None"] = relationship("Company")

    def __repr__(self) -> str:
        return f"<EmailSentimentScore score={self.sentiment_score} intent={self.intent}>"


class ABTestExperiment(Base):
    """A/B test experiment with statistical tracking.

    Goes beyond the basic ab_variant column on EmailSend to provide
    proper experiment management with statistical significance.
    """
    __tablename__ = "ab_test_experiments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outreach_sequences.id", ondelete="SET NULL"), nullable=True
    )
    step_number: Mapped[int | None] = mapped_column(Integer)

    # Variants
    variant_a_subject: Mapped[str] = mapped_column(Text, nullable=False)
    variant_b_subject: Mapped[str] = mapped_column(Text, nullable=False)

    # Results
    variant_a_sent: Mapped[int] = mapped_column(Integer, default=0)
    variant_a_opened: Mapped[int] = mapped_column(Integer, default=0)
    variant_a_clicked: Mapped[int] = mapped_column(Integer, default=0)
    variant_a_replied: Mapped[int] = mapped_column(Integer, default=0)
    variant_b_sent: Mapped[int] = mapped_column(Integer, default=0)
    variant_b_opened: Mapped[int] = mapped_column(Integer, default=0)
    variant_b_clicked: Mapped[int] = mapped_column(Integer, default=0)
    variant_b_replied: Mapped[int] = mapped_column(Integer, default=0)

    # Statistical results
    winner: Mapped[str | None] = mapped_column(String(1))  # "A", "B", or None
    confidence_level: Mapped[float | None] = mapped_column(Float)  # 0.0-1.0
    is_significant: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(String(20), default="running")  # running/completed/insufficient_data

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    def __repr__(self) -> str:
        return f"<ABTestExperiment name={self.name} winner={self.winner}>"


class EmailTestExchange(Base):
    """Simulated email exchange for testing email content and sentiment analysis.

    Allows testing email sequences by simulating conversations with
    various reply scenarios (positive, negative, questions, objections).
    """
    __tablename__ = "email_test_exchanges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    scenario: Mapped[str] = mapped_column(String(50), nullable=False)
    # scenario: "positive_lead", "price_objection", "competitor_using", "not_now", "referral", "aggressive_no"

    # The simulated conversation (list of message dicts)
    messages: Mapped[list] = mapped_column(JSONB, default=list)
    # e.g. [{"role": "sender", "subject": "...", "body": "...", "timestamp": "..."},
    #        {"role": "prospect", "subject": "...", "body": "...", "sentiment_score": 7.5}]

    # Configuration
    industry: Mapped[str | None] = mapped_column(String(100))
    persona_name: Mapped[str | None] = mapped_column(String(100))
    persona_title: Mapped[str | None] = mapped_column(String(100))
    persona_company: Mapped[str | None] = mapped_column(String(200))

    # Results
    total_messages: Mapped[int] = mapped_column(Integer, default=0)
    avg_sentiment: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str | None] = mapped_column(String(50))  # meeting_booked, lost, nurture, no_response

    status: Mapped[str] = mapped_column(String(20), default="active")  # active/completed

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    def __repr__(self) -> str:
        return f"<EmailTestExchange name={self.name} scenario={self.scenario}>"
