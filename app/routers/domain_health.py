"""Domain Health Dashboard API (Feature 5).

Surfaces the persisted ``domain_health`` state: a list of monitored sending
domains with composite scores/bands, a full per-domain breakdown with
copy-paste-ready DNS remediation, and an on-demand (rate-limited) recheck.

Namespaced under ``/deliverability`` alongside the existing sender-health and
domain-auth endpoints.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.domain_health import DomainHealth
from app.schemas.domain_health import (
    DomainHealthDetail,
    DomainHealthSummary,
    DomainRecheckResult,
)
from app.services.domain_health_service import domain_of, get_domain_health_checker

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/deliverability", tags=["domain_health"])

_POSTMASTER_NOTE = (
    "Google Postmaster Tools (postmaster.google.com) shows domain reputation, "
    "spam rate, and authentication pass rates — check it for any domain sending "
    "significant Gmail volume. Not yet integrated automatically."
)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _blacklist_chip(blacklists: dict) -> str:
    """Collapse the blacklist result to a single chip status."""
    status = (blacklists or {}).get("status")
    if status in ("ok", "listed", "unknown"):
        return status
    return "unknown"


def _to_summary(row: DomainHealth) -> DomainHealthSummary:
    return DomainHealthSummary(
        domain=row.domain,
        health_score=row.health_score,
        band=row.band,
        previous_score=row.previous_score,
        last_checked=_aware(row.last_checked),
        spf=(row.spf or {}).get("status", "unknown"),
        dkim=(row.dkim or {}).get("status", "unknown"),
        dmarc=(row.dmarc or {}).get("status", "unknown"),
        blacklist=_blacklist_chip(row.blacklists or {}),
    )


def _collect_issues(row: DomainHealth) -> list[str]:
    """Human-readable remediation strings from the auth checks + blacklist hits."""
    issues: list[str] = []
    for check in (row.spf, row.dkim, row.dmarc):
        for i in (check or {}).get("issues", []) or []:
            issues.append(i)
    listed = (row.blacklists or {}).get("listed_meaningful") or []
    for zone in listed:
        issues.append(
            f"domain is listed on {zone} — request delisting at the zone's removal "
            f"page and fix the underlying cause before it tanks delivery"
        )
    return issues


def _to_detail(row: DomainHealth) -> DomainHealthDetail:
    notes = [_POSTMASTER_NOTE]
    # Note when IP-DNSBL results are informational (Gmail shared pool).
    zones = (row.blacklists or {}).get("zones") or {}
    if any(z.get("informational") for z in zones.values()):
        notes.append(
            "IP-based blacklist results are informational: Gmail-sent mail leaves "
            "Google's shared IP pool, so those listings describe Google's IPs, not "
            "yours. The domain DBL and your auth records are the meaningful signals."
        )
    return DomainHealthDetail(
        domain=row.domain,
        health_score=row.health_score,
        band=row.band,
        previous_score=row.previous_score,
        last_checked=_aware(row.last_checked),
        coverage=row.coverage or {},
        spf=row.spf or {},
        dkim=row.dkim or {},
        dmarc=row.dmarc or {},
        blacklists=row.blacklists or {},
        issues=_collect_issues(row),
        notes=notes,
    )


@router.get("/domains", response_model=list[DomainHealthSummary])
async def list_domains(db: AsyncSession = Depends(get_db)):
    """All monitored sending domains, worst health first."""
    rows = (
        await db.execute(select(DomainHealth).order_by(DomainHealth.health_score.asc()))
    ).scalars().all()
    return [_to_summary(r) for r in rows]


@router.get("/domains/{domain}", response_model=DomainHealthDetail)
async def get_domain(domain: str, db: AsyncSession = Depends(get_db)):
    """Full health detail for one domain, with remediation tips."""
    d = domain_of(domain)
    if d is None:
        raise HTTPException(status_code=400, detail="invalid domain")
    row = (
        await db.execute(select(DomainHealth).where(DomainHealth.domain == d))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"domain '{d}' is not monitored")
    return _to_detail(row)


@router.post("/domains/{domain}/recheck", response_model=DomainRecheckResult)
async def recheck_domain(domain: str, db: AsyncSession = Depends(get_db)):
    """Re-run the checks for one domain on demand.

    Rate-limited: refuses (429) if the domain was checked within the configured
    cooldown, to stay polite to the DNSBL zones.
    """
    d = domain_of(domain)
    if d is None:
        raise HTTPException(status_code=400, detail="invalid domain")

    settings = get_settings()
    existing = (
        await db.execute(select(DomainHealth).where(DomainHealth.domain == d))
    ).scalar_one_or_none()
    if existing is not None:
        last = _aware(existing.last_checked)
        if last is not None:
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            cooldown = settings.domain_health_recheck_cooldown_seconds
            if elapsed < cooldown:
                raise HTTPException(
                    status_code=429,
                    detail=f"rechecked too recently; retry in {int(cooldown - elapsed)}s",
                )

    row = await get_domain_health_checker().check_domain(db, d)
    if row is None:
        raise HTTPException(status_code=400, detail="invalid domain")
    await db.commit()
    return DomainRecheckResult(
        domain=row.domain,
        health_score=row.health_score,
        band=row.band,
        previous_score=row.previous_score,
        rechecked=True,
    )
