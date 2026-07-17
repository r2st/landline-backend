"""Pydantic schemas for organization self-service management (Phase 3).

These back the ``/org/*`` routes an ``org_admin`` uses to manage their *own*
organization — distinct from the platform-admin ``/tenants`` CRUD, which
manages every org. The org here is always resolved from the caller's JWT
``org_id`` claim; it never appears in a request path or body.
"""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.auth import UserRead

# Roles an org_admin may assign within their org. ``platform_admin`` is
# deliberately excluded — only the platform itself grants that.
ORG_ASSIGNABLE_ROLES = ("org_admin", "org_manager", "org_agent")


# --- Org settings ------------------------------------------------------------


class OrgSettingsRead(BaseModel):
    """The caller's organization settings, as shown on the Settings page."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    industry: Optional[str] = None
    billing_plan: str
    config: dict = Field(default_factory=dict)
    branding: dict = Field(default_factory=dict)
    ai_monthly_call_limit: Optional[int] = None
    effective_ai_call_limit: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class OrgSettingsUpdate(BaseModel):
    """Fields an org_admin may change on their own org.

    Notably absent: ``slug`` (identity / routing key), ``billing_plan`` and
    ``ai_monthly_call_limit`` (billing — platform-admin only), and
    ``is_active`` (deactivating your own org is a platform action). An org_admin
    edits presentation and configuration, not the org's commercial terms.
    """

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    industry: Optional[str] = Field(None, max_length=80)
    config: Optional[dict] = None
    branding: Optional[dict] = None


# --- Members -----------------------------------------------------------------


class MemberInvite(BaseModel):
    """Invite a new user into the caller's org with a temporary password."""

    email: EmailStr
    name: str = Field(..., min_length=1, max_length=255)
    role: str = "org_agent"
    # Optional: when omitted a random temporary password is generated and the
    # invitee must reset it on first login. Provided passwords are still
    # flagged must_change_password so the admin never sets a lasting secret.
    password: Optional[str] = Field(None, min_length=6, max_length=128)

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        if v not in ORG_ASSIGNABLE_ROLES:
            raise ValueError(f"role must be one of {ORG_ASSIGNABLE_ROLES}")
        return v


class MemberInviteResponse(UserRead):
    """A newly-invited member plus the one-time temporary password.

    ``temporary_password`` is returned exactly once, at creation, so the admin
    can relay it to the invitee out-of-band (no email delivery is assumed). It
    is never persisted in plaintext or exposed by any subsequent read.
    """

    temporary_password: str


class MemberRoleUpdate(BaseModel):
    """Change a member's role within the org."""

    role: str

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        if v not in ORG_ASSIGNABLE_ROLES:
            raise ValueError(f"role must be one of {ORG_ASSIGNABLE_ROLES}")
        return v


# --- Usage -------------------------------------------------------------------


class UsageTrendPoint(BaseModel):
    """One month's activity, for the usage dashboard's trend charts."""

    month: str  # "YYYY-MM"
    emails_sent: int
    ai_calls: int


class OrgUsageRead(BaseModel):
    """Aggregated usage for the caller's org, current billing period + trends."""

    org_id: uuid.UUID
    billing_plan: str
    period_start: datetime

    # Resource counts.
    contacts: int
    members: int
    emails_sent_this_month: int
    emails_sent_total: int

    # AI metering (from tenant_usage), current period.
    ai_calls: int
    ai_call_limit: Optional[int] = None
    ai_calls_remaining: Optional[int] = None
    over_limit: bool = False
    estimated_cost_usd: float = 0.0
    by_feature: dict = Field(default_factory=dict)

    # Last-6-months trend for the charts.
    trend: list[UsageTrendPoint] = Field(default_factory=list)
