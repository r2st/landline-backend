"""AI-composed first-touch drafts awaiting human review.

The sequence path renders static Jinja mail-merge. ``ai_email_generator`` writes
genuinely personalized cold email and already accepts a research brief, and
``research_agent`` produces exactly that brief — named pain points, ranked
outreach angles — on a schedule. Neither was ever wired into outreach, so the
first touch, the single most important email in a cold sequence, was the least
personalized one. (The optimizer can rewrite, but only reactively off engagement
history, and step 1 has none.)

Turning AI copy loose on cold outreach unreviewed is how a domain gets burned,
so the first ``ai_compose_review_batch_size`` AI-composed emails per sequence
land here instead of going out. A human approves or rejects; once a sequence has
cleared its review quota, later steps compose and send without stopping.

The draft is the queue *and* the audit trail: it keeps the exact copy proposed,
whether a research brief actually grounded it, and what a reviewer decided.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

DRAFT_STATUSES = ("pending", "approved", "rejected", "sent", "failed")


class AiEmailDraft(Base):
    __tablename__ = "ai_email_drafts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','sent','failed')",
            name="ck_ai_draft_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outreach_enrollments.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outreach_sequences.id", ondelete="SET NULL")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL")
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # The AI-proposed copy, and the static copy it would replace — reviewers
    # need the comparison to judge whether the AI actually improved anything.
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    static_subject: Mapped[str | None] = mapped_column(Text)
    static_body: Mapped[str | None] = mapped_column(Text)

    # False when the generator fell back to its template (no API key, error,
    # exhausted credits). A fallback draft is not worth a reviewer's attention.
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Whether a CompanyResearch brief actually grounded the copy. Without one the
    # AI is only reworded mail-merge, which is the thing worth knowing at review.
    research_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending", index=True
    )
    review_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    send_result: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<AiEmailDraft enrollment={self.enrollment_id} step={self.step_number} {self.status}>"
