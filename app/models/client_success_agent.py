"""Client Success Agent — config + action models.

Two org-scoped tables power the autonomous Client Success Agent:

* :class:`ClientSuccessAgent` — one config row per organization. It holds the
  master ON/OFF switch, the LLM backend selection (``llm_model`` +
  ``llm_api_key``, the latter encrypted at rest and falling back to the org's
  OpenRouter key when null), the health thresholds that trigger action, the scan
  cadence, per-play auto toggles, and the editable message templates.

* :class:`ClientSuccessAction` — one row per action the agent decides to take
  (a check-in, an escalation, a celebration, or a review-call suggestion). Each
  records what triggered it, the health score at the moment of triggering, its
  lifecycle status, and the AI-generated message it would send.

The agent watches ``client_health`` scores (see
:mod:`app.models.client_health`): when a client drops below the warning
threshold it drafts a personalized check-in; below the critical threshold it
escalates to a manager and suggests a review call; when a milestone is hit it
celebrates. Everything is per-organization via :class:`OrgScopedMixin`.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin

# The action kinds the agent can produce. Kept as a module constant so the
# service, router, schemas and tests all agree on the vocabulary.
ACTION_TYPES = ("checkin", "escalation", "celebration", "review_call")
# Lifecycle of an action row.
ACTION_STATUSES = ("pending", "sent", "dismissed", "completed")

# Default OpenRouter model — a capable free-tier chat model. Matches the app's
# global default (see app.config.openrouter_model) so an org that never touches
# the config still gets a working, zero-cost backend.
DEFAULT_LLM_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


class ClientSuccessAgent(Base, OrgScopedMixin):
    """Per-organization configuration for the Client Success Agent."""

    __tablename__ = "client_success_agent"
    # One config row per org. ``org_id`` is unique so ``get_or_create`` can rely
    # on a single row and a second insert can't race two configs into existence.
    __table_args__ = (
        UniqueConstraint("org_id", name="uq_client_success_agent_org"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # --- Master switch -------------------------------------------------------
    # The agent does nothing while this is False. Defaults OFF so enabling it is
    # a deliberate, per-org opt-in.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # --- LLM backend selection ----------------------------------------------
    # The OpenRouter model id used for ALL of this agent's AI generations.
    llm_model: Mapped[str] = mapped_column(
        String(120), nullable=False, default=DEFAULT_LLM_MODEL, server_default=DEFAULT_LLM_MODEL
    )
    # Optional per-agent OpenRouter API key, ENCRYPTED at rest (Fernet). When
    # null, the service falls back to the org-level OpenRouter key stored in
    # ``system_settings`` (and finally the process-wide settings key). Never
    # returned to the client in full — the router masks it.
    llm_api_key: Mapped[str | None] = mapped_column(Text)

    # --- Trigger thresholds --------------------------------------------------
    # A health score at/below the warning threshold triggers a check-in; at/below
    # the critical threshold triggers escalation + a review call.
    health_warning_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )
    health_critical_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    # How often the periodic scan should act on a given client (days). Used to
    # debounce repeat actions so a stuck low score isn't nagged daily.
    check_frequency_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7, server_default="7"
    )

    # --- Per-play auto toggles ----------------------------------------------
    auto_checkin_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    auto_escalation_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    celebration_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # --- Editable message templates -----------------------------------------
    # Optional prompt/tone guidance woven into the AI generations. Null → the
    # service's built-in default guidance is used.
    checkin_template: Mapped[str | None] = mapped_column(Text)
    escalation_template: Mapped[str | None] = mapped_column(Text)
    celebration_template: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        state = "on" if self.is_active else "off"
        return f"<ClientSuccessAgent org={self.org_id} {state} model={self.llm_model}>"


class ClientSuccessAction(Base, OrgScopedMixin):
    """One action the Client Success Agent decided to take for a client."""

    __tablename__ = "client_success_action"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('checkin','escalation','celebration','review_call')",
            name="ck_client_success_action_type",
        ),
        CheckConstraint(
            "status IN ('pending','sent','dismissed','completed')",
            name="ck_client_success_action_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )

    action_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # Human-readable explanation of what triggered this action (e.g. "Health
    # dropped to 24 (critical); no contact in 45 days").
    trigger_reason: Mapped[str | None] = mapped_column(Text)
    # The client's health score at the moment the action was triggered.
    health_score_at_trigger: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending", index=True
    )
    # The AI-generated message body (check-in / celebration copy, or the
    # escalation summary for a manager).
    ai_message: Mapped[str | None] = mapped_column(Text)

    executed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    # Relationships (view-only; the agent never mutates the contact/company).
    contact = relationship("Contact", lazy="raise", viewonly=True)
    company = relationship("Company", lazy="raise", viewonly=True)

    def __repr__(self) -> str:
        return f"<ClientSuccessAction {self.action_type}/{self.status} company={self.company_id}>"
