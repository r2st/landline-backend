"""Conversation intelligence model — thread-level email analysis (feature 1.5).

Where ``email_sentiment_scores`` scores individual replies in isolation,
``ConversationAnalysis`` scores the whole arc of an email thread: is it
accelerating or stalling, who has joined the conversation, which objections were
raised but never resolved, and what the ideal next message is. Produced by
``app.services.conversation_intelligence`` from the full chronological thread.
"""

import uuid
from datetime import datetime

from sqlalchemy import Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ConversationAnalysis(Base):
    __tablename__ = "conversation_analyses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The Gmail thread this analysis summarizes.
    gmail_thread_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Momentum: 0-100 (higher = accelerating toward a deal) + a coarse label.
    momentum_score: Mapped[float] = mapped_column(Float, nullable=False, default=50.0)
    momentum_label: Mapped[str] = mapped_column(
        String(20), nullable=False, default="stable"
    )  # accelerating / stable / decelerating / stalled

    # People detected in the thread and their inferred role/engagement. Each item:
    # {"name": "...", "email": "...", "role": "...", "joined_step": 2, "signal": "..."}
    stakeholder_map: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")

    # Objections raised in the thread with resolution status. Each item:
    # {"objection": "...", "raised_by": "...", "addressed": false, "notes": "..."}
    unresolved_objections: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")

    # Free-text strategic recommendation for the rep.
    recommended_action: Mapped[str | None] = mapped_column(Text)

    # The AI's suggested next message: {"subject": "...", "body": "...", "rationale": "..."}
    next_message: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    # Supporting signals: message counts, sentiment trend, reply-latency trend.
    signals: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    model_used: Mapped[str | None] = mapped_column(String(50))

    analyzed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    company = relationship("Company")
    contact = relationship("Contact")

    def __repr__(self) -> str:
        return f"<ConversationAnalysis thread={self.gmail_thread_id} momentum={self.momentum_score}>"
