"""Tenant service — multi-tenant config resolution and usage-based billing (4.1).

Two responsibilities:

* **Config resolution** — given a user (or an explicit tenant), load the tenant
  and expose its enabled verticals, AI-prompt overrides, branding, and feature
  flags. A ``None`` org resolves to an implicit "default" tenant so first-party
  (pre-4.1) usage keeps working with no tenant row required.

* **Usage metering** — record each billable AI call (feature, model, tokens,
  estimated cost) as a ``TenantUsage`` row and aggregate current-period usage
  against the tenant's plan allowance. ``record_ai_usage`` is best-effort: a
  metering failure must never break the underlying AI feature.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant, TenantUsage

logger = logging.getLogger(__name__)

# Per-model USD price per 1M tokens (input, output). Used to estimate the cost
# of a metered AI call. Sonnet-tier default applies to unknown models.
MODEL_PRICING = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (15.0, 75.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-fable-5": (5.0, 25.0),
}
_DEFAULT_PRICING = (3.0, 15.0)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost of a call from per-model token pricing."""
    in_price, out_price = MODEL_PRICING.get(model, _DEFAULT_PRICING)
    return round(
        (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price,
        6,
    )


@dataclass
class TenantContext:
    """Resolved per-request tenant configuration.

    ``tenant`` is ``None`` for the implicit default (first-party) tenant, in
    which case all verticals are enabled, no prompt overrides apply, and usage
    is unmetered.
    """

    tenant: Optional[Tenant]

    @property
    def org_id(self) -> Optional[uuid.UUID]:
        return self.tenant.id if self.tenant else None

    @property
    def is_default(self) -> bool:
        return self.tenant is None

    @property
    def branding(self) -> dict:
        return dict(self.tenant.branding) if self.tenant and self.tenant.branding else {}

    def feature_enabled(self, flag: str, default: bool = True) -> bool:
        """Whether a tenant feature flag is on (default tenant → all on)."""
        if self.tenant is None:
            return default
        flags = self.tenant.config.get("feature_flags") if self.tenant.config else None
        if isinstance(flags, dict) and flag in flags:
            return bool(flags[flag])
        return default

    def vertical_enabled(self, key: str) -> bool:
        """Whether an industry vertical is enabled for this tenant (empty = all)."""
        if self.tenant is None:
            return True
        enabled = self.tenant.enabled_verticals()
        return not enabled or key in enabled

    def ai_prompt_prefix(self, feature: str) -> str:
        """A tenant-specific instruction to prepend to a feature's AI prompt."""
        if self.tenant is None:
            return ""
        override = self.tenant.ai_prompt_override(feature)
        return f"{override.strip()}\n\n" if override else ""


class TenantService:
    """Resolves tenant config and meters AI usage for billing."""

    async def get_tenant(
        self, db: AsyncSession, org_id: Optional[uuid.UUID]
    ) -> Optional[Tenant]:
        if org_id is None:
            return None
        return (
            await db.execute(select(Tenant).where(Tenant.id == org_id))
        ).scalar_one_or_none()

    async def get_context(
        self, db: AsyncSession, org_id: Optional[uuid.UUID]
    ) -> TenantContext:
        return TenantContext(tenant=await self.get_tenant(db, org_id))

    async def get_by_slug(self, db: AsyncSession, slug: str) -> Optional[Tenant]:
        return (
            await db.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()

    async def record_ai_usage(
        self,
        db: AsyncSession,
        org_id: Optional[uuid.UUID],
        feature: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        user_id: Optional[uuid.UUID] = None,
    ) -> Optional[TenantUsage]:
        """Record one billable AI call. No-op for the default tenant.

        Best-effort: never raises into the caller — a metering failure must not
        break the AI feature that triggered it.
        """
        if org_id is None:
            return None
        try:
            usage = TenantUsage(
                tenant_id=org_id,
                feature=feature,
                model=model,
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                estimated_cost_usd=estimate_cost(model, input_tokens or 0, output_tokens or 0),
                user_id=user_id,
            )
            db.add(usage)
            await db.flush()
            return usage
        except Exception:  # noqa: BLE001 — metering must never break the feature
            logger.exception("Failed to record tenant AI usage for org=%s", org_id)
            return None

    async def usage_summary(
        self, db: AsyncSession, tenant: Tenant, since: Optional[datetime] = None
    ) -> dict:
        """Aggregate usage for a tenant since ``since`` (default: month start)."""
        if since is None:
            now = datetime.now(timezone.utc)
            since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        totals = (
            await db.execute(
                select(
                    func.count(TenantUsage.id),
                    func.coalesce(func.sum(TenantUsage.input_tokens), 0),
                    func.coalesce(func.sum(TenantUsage.output_tokens), 0),
                    func.coalesce(func.sum(TenantUsage.estimated_cost_usd), 0),
                ).where(TenantUsage.tenant_id == tenant.id, TenantUsage.created_at >= since)
            )
        ).one()
        call_count, in_tok, out_tok, cost = totals

        # Per-feature breakdown.
        rows = (
            await db.execute(
                select(
                    TenantUsage.feature,
                    func.count(TenantUsage.id),
                    func.coalesce(func.sum(TenantUsage.estimated_cost_usd), 0),
                )
                .where(TenantUsage.tenant_id == tenant.id, TenantUsage.created_at >= since)
                .group_by(TenantUsage.feature)
            )
        ).all()
        by_feature = {
            f: {"calls": int(c), "cost_usd": float(cst)} for f, c, cst in rows
        }

        limit = tenant.effective_ai_call_limit
        call_count = int(call_count)
        return {
            "tenant_id": str(tenant.id),
            "billing_plan": tenant.billing_plan,
            "period_start": since.isoformat(),
            "ai_calls": call_count,
            "ai_call_limit": limit,
            "over_limit": limit is not None and call_count > limit,
            "remaining_calls": (max(0, limit - call_count) if limit is not None else None),
            "input_tokens": int(in_tok),
            "output_tokens": int(out_tok),
            "estimated_cost_usd": round(float(cost), 4),
            "by_feature": by_feature,
        }

    async def within_ai_limit(self, db: AsyncSession, tenant: Optional[Tenant]) -> bool:
        """Whether the tenant is still under its monthly AI-call allowance."""
        if tenant is None:
            return True
        limit = tenant.effective_ai_call_limit
        if limit is None:
            return True
        summary = await self.usage_summary(db, tenant)
        return summary["ai_calls"] < limit


_tenant_service: Optional[TenantService] = None


def get_tenant_service() -> TenantService:
    global _tenant_service
    if _tenant_service is None:
        _tenant_service = TenantService()
    return _tenant_service
