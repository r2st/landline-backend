"""Integration marketplace models (feature 4.2).

An ``Integration`` is a configured connection between a tenant and an external
provider (Slack, Google Calendar, HubSpot, Salesforce, ZoomInfo, Clearbit, …).
Every provider is driven through a standardized connector interface (see
``app.services.integrations``) that abstracts the three auth styles — OAuth, API
key, and inbound webhook.

Secrets (API keys, OAuth tokens) are encrypted at rest with Fernet
(``app.services.crypto``) inside the ``credentials`` JSON blob; ``config`` holds
non-secret settings (channel ids, sync options, field mappings).

``WebhookSubscription`` and ``WebhookEvent`` implement the outbound webhook
framework: a tenant subscribes a target URL to a set of internal event types,
and each delivery attempt is recorded as a ``WebhookEvent`` for retry/audit.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Auth styles a connector can use.
AUTH_TYPES = ("oauth", "api_key", "webhook")

# Connection lifecycle states.
INTEGRATION_STATUSES = ("configured", "connected", "error", "disconnected")


class Integration(Base):
    """A tenant's configured connection to one external provider."""

    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("org_id", "provider", name="uq_integration_org_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Tenant that owns this integration. NULL = the default first-party tenant.
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Provider key from the marketplace catalog, e.g. "slack", "hubspot".
    provider: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    auth_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="configured")
    # Non-secret settings (channel id, sync direction, field mappings, …).
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    # Encrypted secrets (api_key, oauth tokens). Values are Fernet ciphertext.
    credentials: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Integration {self.provider} org={self.org_id} status={self.status}>"


class WebhookSubscription(Base):
    """A tenant's subscription delivering internal events to an external URL."""

    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    target_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    # Internal event types this subscription fires on, e.g.
    # ["lead.replied", "deal.stage_changed"]. Empty = all events.
    event_types: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # Shared secret used to HMAC-sign delivered payloads (encrypted at rest).
    secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    events = relationship("WebhookEvent", back_populates="subscription", cascade="all, delete-orphan")

    def matches(self, event_type: str) -> bool:
        """Whether this subscription should fire for ``event_type``."""
        if not self.is_active:
            return False
        return not self.event_types or event_type in self.event_types

    def __repr__(self) -> str:
        return f"<WebhookSubscription {self.target_url} events={self.event_types}>"


class WebhookEvent(Base):
    """One delivery attempt of an internal event to a webhook subscription."""

    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    # pending → delivered / failed.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    subscription = relationship("WebhookSubscription", back_populates="events")

    def __repr__(self) -> str:
        return f"<WebhookEvent {self.event_type} status={self.status} attempts={self.attempts}>"
