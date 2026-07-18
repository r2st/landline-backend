"""Referral & Warm-Intro agent models.

Three org-scoped tables power the autonomous referral agent:

* :class:`ReferralAgent` — the per-organization config + ON/OFF toggle. Exactly
  one row per org (get-or-create). Holds the agent's own LLM model + (encrypted)
  API key so a tenant can point the agent at any OpenRouter model with its own
  key, falling back to the org-level key when unset.
* :class:`Referral` — a single referral in its lifecycle (identified → asked →
  pending → received → converted / declined), linking the referrer (a happy
  client contact) to the referred person, and — once they become a contact and
  close a deal — to the resulting revenue.
* :class:`ReferralAction` — a unit of autonomous work the agent proposes (an
  initial ask, a follow-up, or a thank-you) with the AI-drafted message, awaiting
  approval (or auto-executed when ``auto_ask_enabled``).

All three mix in :class:`OrgScopedMixin` so reads/writes are tenant-isolated by
the global org filter.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin

# Lifecycle a referral moves through. ``identified`` — the agent spotted a happy
# client worth asking. ``asked`` — an ask went out. ``pending`` — the client
# agreed / we're awaiting the intro. ``received`` — a concrete referral landed.
# ``converted`` — the referred party closed a deal. ``declined`` — dead end.
REFERRAL_STATUSES = (
    "identified",
    "asked",
    "pending",
    "received",
    "converted",
    "declined",
)

# What kind of message an action carries.
REFERRAL_ACTION_TYPES = ("initial_ask", "followup", "thank_you")

# An action's own state: awaiting approval, executed (sent), or dismissed.
REFERRAL_ACTION_STATUSES = ("pending", "sent", "dismissed")

# Model ids offered in the config dropdown (all free-tier on OpenRouter). Kept
# here so the API can validate a chosen model against the same list the UI shows.
REFERRAL_LLM_MODELS = (
    "meta-llama/llama-3.3-70b-instruct:free",
    "openai/gpt-oss-20b:free",
    "openai/gpt-oss-120b:free",
    "google/gemma-3-27b-it:free",
)

DEFAULT_LLM_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


class ReferralAgent(Base, OrgScopedMixin):
    """Per-org configuration + toggle for the autonomous referral agent."""

    __tablename__ = "referral_agents"
    __table_args__ = (
        # One config row per organization.
        CheckConstraint("min_health_score >= 0 AND min_health_score <= 100",
                        name="ck_referral_agents_health_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Master ON/OFF. When off, the scan/follow-up/conversion tasks skip this org
    # entirely and the API refuses to auto-execute actions.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # --- LLM routing (per agent) ---
    llm_model: Mapped[str] = mapped_column(
        String(120), nullable=False, default=DEFAULT_LLM_MODEL,
        server_default=DEFAULT_LLM_MODEL,
    )
    # Fernet-encrypted OpenRouter API key. NULL → fall back to the org-level key
    # (system_settings ``openrouter_api_key``) and then the global config key.
    llm_api_key: Mapped[str | None] = mapped_column(Text)

    # --- Candidate selection ---
    min_health_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=80, server_default="80"
    )
    min_relationship_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default="90"
    )
    max_asks_per_client: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    followup_after_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7, server_default="7"
    )

    # When true the scan task auto-approves + sends initial asks; otherwise every
    # ask waits in the action queue for a human to approve.
    auto_ask_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # --- Message guidance (optional style/base templates passed to the LLM) ---
    referral_template: Mapped[str | None] = mapped_column(Text)
    followup_template: Mapped[str | None] = mapped_column(Text)
    thankyou_template: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<ReferralAgent org={self.org_id} active={self.is_active}>"


class Referral(Base, OrgScopedMixin):
    """One referral in its lifecycle, linking a referrer to a referred party."""

    __tablename__ = "referrals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('identified','asked','pending','received','converted','declined')",
            name="ck_referrals_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # The happy client who is (or will be) asked for the referral. SET NULL so a
    # referral survives the contact being deleted.
    referrer_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The referred person (free-text until they become a contact of ours).
    referred_name: Mapped[str | None] = mapped_column(String(200))
    referred_email: Mapped[str | None] = mapped_column(String(255), index=True)
    referred_company: Mapped[str | None] = mapped_column(String(255))
    # Set once the referred party is created as a Contact in our CRM.
    referred_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="identified",
        server_default="identified", index=True,
    )

    ask_sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    converted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Deal value credited to this referral, in paise (INR subunit), once the
    # referred party converts. NULL until then.
    referral_value: Mapped[int | None] = mapped_column(BigInteger)

    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    referrer: Mapped["Contact"] = relationship(
        "Contact", foreign_keys=[referrer_contact_id], lazy="raise", viewonly=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Referral {self.referred_name!r} status={self.status}>"


class ReferralAction(Base, OrgScopedMixin):
    """An autonomous action the agent proposes: an ask, follow-up, or thank-you."""

    __tablename__ = "referral_actions"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('initial_ask','followup','thank_you')",
            name="ck_referral_actions_type",
        ),
        CheckConstraint(
            "status IN ('pending','sent','dismissed')",
            name="ck_referral_actions_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Whom the message is addressed to (the referrer). SET NULL to survive delete.
    referrer_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # The referral this action advances (follow-ups / thank-yous), when applicable.
    referral_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("referrals.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    action_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending",
        server_default="pending", index=True,
    )

    # AI-drafted subject + body for the message.
    ai_subject: Mapped[str | None] = mapped_column(Text)
    ai_message: Mapped[str | None] = mapped_column(Text)

    executed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<ReferralAction {self.action_type} status={self.status}>"
