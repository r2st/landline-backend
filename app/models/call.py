"""Cold-call models — scheduled call tasks, industry scripts, and notifications.

Three tables power the human-in-the-loop cold-calling workflow:

* ``CallTask`` — a scheduled cold call assigned to a sales rep, with a priority
  (hot/warm/cold), an outcome once the call is made, and free-form notes.
* ``CallScript`` — industry-specific call scripts (opening hook, qualifying
  questions, pain points, objection handling, CTA). Seeded from
  ``app.services.call_scripts_data``.
* ``CallNotification`` — the rep-facing notification queue (pre-call reminders,
  hot-lead alerts, missed-call follow-ups, callback reminders).
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

CALL_STATUSES = ("pending", "completed", "skipped", "rescheduled")
CALL_PRIORITIES = ("hot", "warm", "cold")
CALL_OUTCOMES = (
    "connected",
    "voicemail",
    "no_answer",
    "gatekeeper",
    "callback_scheduled",
)
CALL_SCRIPT_INDUSTRIES = (
    "injection_molding",
    "cnc_machining",
    "metal_stamping",
    "food_beverage",
)
CALL_SCRIPT_TYPES = ("initial_cold_call", "follow_up", "voicemail")
CALL_NOTIFICATION_TYPES = (
    "upcoming_call",
    "missed_call",
    "follow_up_due",
    "hot_lead_alert",
)


class CallTask(Base):
    __tablename__ = "call_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','completed','skipped','rescheduled')",
            name="ck_call_tasks_status",
        ),
        CheckConstraint(
            "priority IN ('hot','warm','cold')",
            name="ck_call_tasks_priority",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('connected','voicemail','no_answer','gatekeeper','callback_scheduled')",
            name="ck_call_tasks_outcome",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assigned_to: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="warm", index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(30))
    notes: Mapped[str | None] = mapped_column(Text)
    call_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    # AI-generated personalised call script (a JSON blob of the structured script
    # — opening, talking points, questions, objections, CTA). Cached here so the
    # rep's briefing doesn't regenerate it on every page load. ``script_context``
    # records the signals the script was built from, so it can be regenerated when
    # the lead's situation changes.
    custom_script: Mapped[dict | None] = mapped_column(JSONB)
    script_generated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    script_context: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    company: Mapped["Company"] = relationship("Company")
    contact: Mapped["Contact | None"] = relationship("Contact")
    notifications: Mapped[list["CallNotification"]] = relationship(
        "CallNotification", back_populates="call_task", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<CallTask company={self.company_id} {self.status}/{self.priority}>"


class CallScript(Base):
    __tablename__ = "call_scripts"
    __table_args__ = (
        UniqueConstraint("industry", "script_type", name="uq_call_scripts_industry_type"),
        CheckConstraint(
            "industry IN ('injection_molding','cnc_machining','metal_stamping','food_beverage')",
            name="ck_call_scripts_industry",
        ),
        CheckConstraint(
            "script_type IN ('initial_cold_call','follow_up','voicemail')",
            name="ck_call_scripts_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    industry: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    script_type: Mapped[str] = mapped_column(String(30), nullable=False, default="initial_cold_call")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    opening_hook: Mapped[str] = mapped_column(Text, nullable=False)
    qualifying_questions: Mapped[list] = mapped_column(JSONB, default=list)
    pain_points: Mapped[list] = mapped_column(JSONB, default=list)
    value_proposition: Mapped[str | None] = mapped_column(Text)
    objection_handling: Mapped[list] = mapped_column(JSONB, default=list)
    call_to_action: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<CallScript {self.industry}/{self.script_type}>"


class CallNotification(Base):
    __tablename__ = "call_notifications"
    __table_args__ = (
        CheckConstraint(
            "notification_type IN "
            "('upcoming_call','missed_call','follow_up_due','hot_lead_alert')",
            name="ck_call_notifications_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    call_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("call_tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    notification_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    call_task: Mapped["CallTask | None"] = relationship("CallTask", back_populates="notifications")

    def __repr__(self) -> str:
        return f"<CallNotification {self.notification_type} read={self.read_at is not None}>"
