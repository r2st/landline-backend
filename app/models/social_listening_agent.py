"""Social Listening Agent — models for the buying-intent listening agent.

The agent monitors *public* signals (company news, funding rounds, job
postings, leadership changes, product launches) for buying intent, rates each
against the org's ICP with an LLM, and turns high-relevance signals into warm
lead cards for the sales team — optionally auto-enrolling the hottest into a
Dialtone sequence.

Three tables, all org-scoped:

* :class:`SocialListeningAgent` — one config row per organization. Holds the
  ON/OFF toggle, the LLM model + (encrypted) API key the agent uses, the
  keywords/industries/sources it watches, and the automation thresholds.
* :class:`SignalEvent` — one discovered signal (a news item, a job posting,
  a funding announcement …), with its AI relevance score, classified signal
  type, and lifecycle status (new → reviewed/actioned/dismissed).
* :class:`MonitoredCompany` — a company the org has explicitly asked the agent
  to watch (news and/or jobs), optionally linked to an existing CRM company.

The ``llm_api_key`` is stored **encrypted at rest** (Fernet, see
:mod:`app.services.crypto`) exactly like Gmail OAuth tokens — it is a credential
that lets the agent spend money on the org's behalf. The resolution order at
call time is: the agent's own key → the org-level ``openrouter_api_key`` in
``system_settings`` → the process-wide ``OPENROUTER_API_KEY`` env var.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.mixins import OrgScopedMixin

# Free OpenRouter models the agent may be configured with. Kept as a plain
# tuple (not a DB enum) so the roster can change without a migration; the router
# validates the config against it. The first entry is the default.
ALLOWED_LLM_MODELS = (
    "meta-llama/llama-3.3-70b-instruct:free",
    "openai/gpt-oss-20b:free",
    "openai/gpt-oss-120b:free",
    "google/gemma-3-27b-it:free",
)
DEFAULT_LLM_MODEL = ALLOWED_LLM_MODELS[0]

# Source of a discovered signal — where the agent found it.
SOURCE_TYPES = (
    "news",
    "job_posting",
    "social",
    "funding",
    "company_update",
)

# The AI-classified nature of the signal — what kind of buying intent it hints.
SIGNAL_TYPES = (
    "funding",
    "hiring",
    "expansion",
    "leadership_change",
    "product_launch",
    "partnership",
)

# Lifecycle of a signal in the review feed.
SIGNAL_STATUSES = (
    "new",
    "reviewed",
    "actioned",
    "dismissed",
)


class SocialListeningAgent(Base, OrgScopedMixin):
    """Per-organization configuration for the social listening agent."""

    __tablename__ = "social_listening_agents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The master ON/OFF toggle. Off by default — the agent never runs for an org
    # that hasn't opted in.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # LLM configuration. ``llm_model`` is an OpenRouter model id; ``llm_api_key``
    # is a Fernet-encrypted OpenRouter key (nullable — falls back to the org /
    # process key when unset). Never expose the ciphertext over the API.
    llm_model: Mapped[str] = mapped_column(
        String(120), nullable=False, default=DEFAULT_LLM_MODEL, server_default=DEFAULT_LLM_MODEL
    )
    llm_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    # What to watch. Keywords/industries drive news queries; news_sources is a
    # list of extra RSS feed URLs / domains to poll on top of Google News.
    monitored_keywords: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    monitored_industries: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    news_sources: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    # Automation thresholds. A signal must score >= min_relevance_score to be
    # surfaced/actioned. auto_create_leads / auto_enroll_dialtone gate the two
    # escalation steps (create a warm lead; enroll it in a sequence).
    min_relevance_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=70, server_default="70"
    )
    auto_create_leads: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    auto_enroll_dialtone: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    scan_frequency_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24, server_default="24"
    )

    last_scan_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def has_own_key(self) -> bool:
        """True when the agent carries its own (encrypted) LLM API key."""
        return bool(self.llm_api_key)

    def __repr__(self) -> str:
        return f"<SocialListeningAgent org={self.org_id} active={self.is_active}>"


class SignalEvent(Base, OrgScopedMixin):
    """A single public buying-intent signal discovered by the agent."""

    __tablename__ = "signal_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    company_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    company_domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # Link to an existing CRM company when the signal was matched to one.
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )

    headline: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # AI-generated

    relevance_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0", index=True
    )  # 0-100
    signal_type: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)

    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="new", server_default="new", index=True
    )
    lead_created: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # The contact created/enrolled from this signal, if any.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    company = relationship("Company", lazy="raise", viewonly=True)

    def __repr__(self) -> str:
        return f"<SignalEvent {self.signal_type} {self.company_name!r} score={self.relevance_score}>"


class MonitoredCompany(Base, OrgScopedMixin):
    """A company the org has asked the agent to actively watch."""

    __tablename__ = "monitored_companies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Optional link to an existing CRM company.
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    monitor_news: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    monitor_jobs: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    company = relationship("Company", lazy="raise", viewonly=True)

    def __repr__(self) -> str:
        return f"<MonitoredCompany {self.company_name!r} news={self.monitor_news} jobs={self.monitor_jobs}>"
