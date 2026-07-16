"""Pydantic schemas for the Hunter.io email-verification endpoints (P3).

``email_status`` is the gate a send path reads; ``result``/``score`` explain it.
Both are surfaced so the Console can show *why* an address is (un)sendable.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class VerificationResult(BaseModel):
    """One contact's verification outcome."""

    contact_id: UUID
    email: Optional[str] = None
    # About the call, not the address: 'ok' a verdict was reached and persisted;
    # 'skipped' nothing was attempted (no key / budget exhausted); 'error' a
    # layer tried and failed. Only 'ok' changes the contact.
    status: str
    # Hunter's verdict: deliverable | undeliverable | risky | unknown.
    result: Optional[str] = None
    score: Optional[int] = None  # 0-100 confidence
    source: Optional[str] = None  # 'mx' | 'hunter' | 'manual'
    # The resulting send gate: 'valid' | 'invalid' | 'unknown'.
    email_status: Optional[str] = None
    verified_at: Optional[datetime] = None
    # Why a non-'ok' status happened: no_api_key | budget_exhausted |
    # rate_limited | http_4xx/5xx | <exception>.
    reason: Optional[str] = None
    # True only for a Hunter-confirmed deliverable at/above hunter_min_score.
    auto_enroll_eligible: bool = False


class BulkVerifyRequest(BaseModel):
    contact_ids: list[UUID] = Field(..., min_length=1)


class BulkVerifyResponse(BaseModel):
    requested: int          # ids submitted
    found: int              # ids that matched a contact
    verified: int           # contacts a verdict was persisted for
    skipped: int            # no key / budget exhausted / errored — unchanged
    # Verdict (or call status where none was reached) → count.
    breakdown: dict[str, int]
    # Hunter calls left in today's budget; None when the budget is unlimited.
    budget_remaining: Optional[int] = None
    results: list[VerificationResult]


class VerificationStats(BaseModel):
    """Verification status breakdown across the contact list."""

    total_contacts: int
    # Contacts we could actually email (has an address, not suppressed or
    # unsubscribed) — the denominator that matters for coverage.
    emailable: int
    verified: int
    unverified: int
    stale: int  # verified longer ago than hunter_reverify_days
    by_email_status: dict[str, int]          # valid | invalid | unknown | unchecked
    by_result: dict[str, int]                # deliverable | undeliverable | risky | unknown
    by_source: dict[str, int]                # mx | hunter | manual
    auto_enroll_eligible: int
    hunter_enabled: bool
    hunter_calls_today: int
    daily_credit_budget: int                 # 0 = unlimited
    # Addresses proven undeliverable — each one a hard bounce that verification
    # kept off the wire.
    bounces_prevented: int
    # ``bounces_prevented`` over everything verified: the hard-bounce rate we
    # would have taken on that population had we sent to it blind. A measurement
    # of what verification has already caught, NOT a forecast of future sends.
    verified_undeliverable_rate: float
