"""Pydantic schemas for the Domain Health Dashboard (Feature 5).

Shapes the persisted ``domain_health`` state into the operator-facing API:

* ``DomainHealthSummary`` — one row per monitored domain (score, band, per-check
  status chips) for the dashboard list.
* ``DomainHealthDetail`` — the full breakdown for one domain, including the raw
  auth records, blacklist zones, and human-readable remediation ``issues``.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class DomainCheckChip(BaseModel):
    """A single check's at-a-glance status (SPF / DKIM / DMARC / Blacklist)."""

    status: str  # pass | warn | fail | ok | listed | unknown
    detail: Optional[str] = None


class DomainHealthSummary(BaseModel):
    """One monitored domain, summarized for the dashboard list."""

    domain: str
    health_score: int
    band: str  # healthy | watch | at_risk
    previous_score: Optional[int] = None
    last_checked: Optional[datetime] = None
    # Per-check status chips.
    spf: str = "unknown"
    dkim: str = "unknown"
    dmarc: str = "unknown"
    blacklist: str = "unknown"


class DomainHealthDetail(BaseModel):
    """Full detail for one domain — records, blacklist zones, remediation."""

    domain: str
    health_score: int
    band: str
    previous_score: Optional[int] = None
    last_checked: Optional[datetime] = None
    coverage: dict[str, Any] = {}
    # Raw check outputs.
    spf: dict[str, Any] = {}
    dkim: dict[str, Any] = {}
    dmarc: dict[str, Any] = {}
    blacklists: dict[str, Any] = {}
    # Human-readable remediation strings (from domain_auth issues + blacklist hits).
    issues: list[str] = []
    # Guidance for signals we don't automate in v1 (e.g. Postmaster Tools).
    notes: list[str] = []


class DomainRecheckResult(BaseModel):
    """Result of an on-demand recheck."""

    domain: str
    health_score: int
    band: str
    previous_score: Optional[int] = None
    rechecked: bool = True
