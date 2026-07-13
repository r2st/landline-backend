"""AI Workflow Builder models (feature 4.3).

A ``Workflow`` is a user-defined automation: a trigger (an internal event plus
optional match conditions) and an ordered list of action steps with optional
branching. Example: *"When a lead opens 3+ emails AND their company posts a QE
job, research the company, generate a personalized email, and enroll them in the
high-intent sequence."*

Steps are stored as a JSON list; each step is a dict:

    {
        "id": "step1",
        "action": "generate_email",      # key in the action registry
        "params": {...},                 # action-specific params
        "condition": {                   # optional gate on ctx (branching)
            "field": "lead.email_opens", "op": "gte", "value": 3
        },
        "on_success": "step2",           # optional explicit next-step id
        "on_failure": null
    }

Each execution is a ``WorkflowRun`` recording the triggering event, per-step
results, and terminal status — used for observability and debugging.

The execution engine (``app.services.workflow_engine``) is driven by the Redis
Streams event bus (``app.services.event_bus``); both degrade gracefully to an
in-process path when Redis is unavailable (tests, local dev).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Terminal + in-flight run states.
RUN_STATUSES = ("pending", "running", "completed", "failed", "skipped")


class Workflow(Base):
    """A user-defined event-triggered automation."""

    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Internal event that triggers evaluation, e.g. "lead.replied",
    # "email.opened", "deal.stage_changed", "company.created".
    trigger_event: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    # Optional match conditions ANDed against the event payload before the
    # workflow runs: {"all": [{"field": "...", "op": "gte", "value": 3}, ...]}.
    trigger_conditions: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Ordered action steps (see module docstring for the step schema).
    steps: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Guard against runaway self-triggering loops.
    max_runs_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    runs = relationship("WorkflowRun", back_populates="workflow", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Workflow {self.name!r} on={self.trigger_event} active={self.is_active}>"


class WorkflowRun(Base):
    """One execution of a workflow against a specific triggering event."""

    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    # The event payload that triggered this run.
    trigger_payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Per-step results: [{"step_id": ..., "action": ..., "status": ..., "output": ...}].
    step_results: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    workflow = relationship("Workflow", back_populates="runs")

    def __repr__(self) -> str:
        return f"<WorkflowRun workflow={self.workflow_id} status={self.status}>"
