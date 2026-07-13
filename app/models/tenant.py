"""Tenant model — multi-tenant white-label platform (feature 4.1).

A ``Tenant`` is an organization that uses Landline as a white-labeled B2B CRM.
Each tenant gets org-level data isolation (every user belongs to a tenant via
``User.org_id``), tenant-specific configuration (industry verticals, AI prompt
customization, feature flags), branding (logo, colors, custom domain), and a
billing plan with a usage-based AI-call allowance.

Isolation is enforced at the application layer through the existing
``UserScope`` pattern, extended here with ``org_id`` (see
``app.dependencies``). A ``NULL`` ``org_id`` on a user means "the default
first-party tenant" (Apprend itself), preserving backward compatibility with
every pre-4.1 user and row.

``TenantUsage`` records per-call AI usage (feature, model, tokens, estimated
cost) so AI features can be metered and billed per tenant.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Billing plans and their default monthly AI-call allowances. Kept as a plain
# mapping (not a DB enum) so plans can be re-priced without a migration.
BILLING_PLANS = ("free", "starter", "growth", "enterprise")

PLAN_AI_CALL_LIMITS = {
    "free": 100,
    "starter": 2_000,
    "growth": 10_000,
    "enterprise": None,  # unlimited
}


class Tenant(Base):
    """An organization using the white-label platform."""

    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(
            "billing_plan IN ('free','starter','growth','enterprise')",
            name="ck_tenants_billing_plan",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # URL-safe slug used for subdomain / white-label routing (e.g. "acme").
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Default industry vertical key (see services/industry_verticals.py).
    industry: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Tenant-specific config: enabled_verticals (list[str]), ai_prompts
    # (dict of prompt overrides keyed by feature), feature_flags (dict[str,bool]).
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    # White-label branding: logo_url, primary_color, secondary_color,
    # company_name, support_email, custom_domain.
    branding: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    # Billing.
    billing_plan: Mapped[str] = mapped_column(String(20), nullable=False, default="free")
    # Monthly cap on billable AI calls; NULL = unlimited (enterprise). Falls back
    # to PLAN_AI_CALL_LIMITS[billing_plan] when NULL and plan is not enterprise.
    ai_monthly_call_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    users = relationship("User", back_populates="tenant", foreign_keys="User.org_id")

    @property
    def effective_ai_call_limit(self) -> int | None:
        """Resolved monthly AI-call allowance (explicit override or plan default)."""
        if self.ai_monthly_call_limit is not None:
            return self.ai_monthly_call_limit
        return PLAN_AI_CALL_LIMITS.get(self.billing_plan, PLAN_AI_CALL_LIMITS["free"])

    def enabled_verticals(self) -> list[str]:
        """The industry-vertical keys this tenant has enabled (empty = all)."""
        v = self.config.get("enabled_verticals") if self.config else None
        return list(v) if isinstance(v, list) else []

    def ai_prompt_override(self, feature: str) -> str | None:
        """A tenant-specific AI prompt prefix/override for a feature, if set."""
        prompts = self.config.get("ai_prompts") if self.config else None
        if isinstance(prompts, dict):
            val = prompts.get(feature)
            return val if isinstance(val, str) and val.strip() else None
        return None

    def __repr__(self) -> str:
        return f"<Tenant {self.slug} plan={self.billing_plan} active={self.is_active}>"


class TenantUsage(Base):
    """One metered AI call attributed to a tenant, for usage-based billing."""

    __tablename__ = "tenant_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Which AI feature drove the call (e.g. "email_generation", "workflow").
    feature: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Estimated USD cost of the call (input + output priced per model).
    estimated_cost_usd: Mapped[float] = mapped_column(
        Numeric(12, 6), nullable=False, default=0, server_default="0"
    )
    # Optional attribution to the user that triggered the call.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    tenant = relationship("Tenant")

    def __repr__(self) -> str:
        return f"<TenantUsage tenant={self.tenant_id} feature={self.feature} cost={self.estimated_cost_usd}>"
