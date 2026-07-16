"""Domain health orchestration (Feature 5 — Domain Health Dashboard).

Ties together the existing SPF/DKIM/DMARC checks (``domain_auth``) and the new
DNSBL blacklist lookups (``blacklist_check``) into a per-domain composite health
score, persists the latest state to ``domain_health`` (upsert, unique per
domain), and alerts **only on a change** (score/band transition or a new
blacklist listing) to avoid daily alert fatigue.

The heavy lifting is DNS I/O, which is blocking — every lookup is dispatched off
the event loop with ``asyncio.to_thread`` so the daily task never blocks the
loop.

Scoring (weights configurable in ``settings``)::

    SPF        20   present+valid → full · present+invalid → half · missing → 0
    DKIM       20   valid selector → full · none → 0 · unknown → excluded
    DMARC      25   reject/quarantine → full · p=none → half · missing → 0
    Blacklists 30   not listed → full · Spamhaus hit → 0 · other listing → half

``unknown`` components are **excluded from the denominator** so the score
reflects only what could actually be verified; ``coverage`` records which
components counted. Band: ``>=90 healthy``, ``70-89 watch``, ``<70 at_risk``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.domain_health import DomainHealth
from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount
from app.services import blacklist_check, domain_auth
from app.services.notifications import get_notification_service

logger = logging.getLogger(__name__)


def domain_of(email_or_domain: str) -> Optional[str]:
    """Extract a normalized bare domain from an email address or domain string."""
    val = (email_or_domain or "").strip().lower().lstrip("@")
    if "@" in val:
        val = val.split("@", 1)[1]
    val = val.strip()
    if not val or "." not in val:
        return None
    return val


class DomainHealthChecker:
    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # Domain discovery
    # ------------------------------------------------------------------
    async def collect_domains(self, db: AsyncSession) -> list[str]:
        """Unique sending domains across every Gmail and SMTP account."""
        emails: list[str] = []
        emails += list(
            (await db.execute(select(GmailAccount.email))).scalars().all()
        )
        emails += list(
            (await db.execute(select(SmtpAccount.email))).scalars().all()
        )
        domains: list[str] = []
        seen: set[str] = set()
        for e in emails:
            d = domain_of(e)
            if d and d not in seen:
                seen.add(d)
                domains.append(d)
        return domains

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------
    def band_for(self, score: int) -> str:
        s = self.settings
        if score >= s.domain_health_band_healthy_min:
            return "healthy"
        if score >= s.domain_health_band_watch_min:
            return "watch"
        return "at_risk"

    def _spf_fraction(self, spf: dict) -> Optional[float]:
        status = (spf or {}).get("status")
        if status == "unknown":
            return None
        if spf.get("present") and spf.get("valid"):
            return 1.0
        if spf.get("present"):
            return 0.5
        return 0.0  # missing / fail

    def _dkim_fraction(self, dkim: dict) -> Optional[float]:
        status = (dkim or {}).get("status")
        if status == "unknown":
            return None  # don't penalize a lookup gap
        if dkim.get("present") and dkim.get("valid"):
            return 1.0
        return 0.0

    def _dmarc_fraction(self, dmarc: dict) -> Optional[float]:
        status = (dmarc or {}).get("status")
        if status == "unknown":
            return None
        policy = (dmarc or {}).get("policy")
        if policy in ("reject", "quarantine"):
            return 1.0
        if dmarc.get("present"):  # p=none (monitor only)
            return 0.5
        return 0.0

    def _blacklist_fraction(self, blacklists: dict) -> Optional[float]:
        """Score the *meaningful* (non-informational) blacklist zones.

        Returns ``None`` when nothing meaningful could be verified (all unknown /
        no meaningful zones configured) so blacklists drop out of the denominator.
        """
        status = (blacklists or {}).get("status")
        if status == "unknown":
            return None
        listed = (blacklists or {}).get("listed_meaningful") or []
        if not listed:
            return 1.0
        # A Spamhaus listing (ZEN or DBL) is near-fatal for deliverability.
        if any("spamhaus" in z.lower() for z in listed):
            return 0.0
        # Otherwise each listing halves the component, floored at 0.
        return max(0.0, 1.0 - 0.5 * len(listed))

    def compute_score(self, spf: dict, dkim: dict, dmarc: dict, blacklists: dict) -> dict:
        """Composite 0-100 score, band, and coverage over verifiable components."""
        s = self.settings
        components = [
            ("spf", self._spf_fraction(spf), s.domain_health_weight_spf),
            ("dkim", self._dkim_fraction(dkim), s.domain_health_weight_dkim),
            ("dmarc", self._dmarc_fraction(dmarc), s.domain_health_weight_dmarc),
            ("blacklists", self._blacklist_fraction(blacklists), s.domain_health_weight_blacklists),
        ]
        earned = 0.0
        denom = 0
        coverage: dict[str, dict] = {}
        for name, frac, weight in components:
            if frac is None:
                coverage[name] = {"verified": False, "weight": weight}
                continue
            coverage[name] = {"verified": True, "weight": weight, "fraction": round(frac, 3)}
            earned += frac * weight
            denom += weight

        if denom <= 0:
            # Nothing verifiable — report 0 with an explicit no-coverage note.
            return {"health_score": 0, "band": self.band_for(0),
                    "coverage": {"components": coverage, "denominator": 0, "verifiable": False}}

        score = int(round(100 * earned / denom))
        score = max(0, min(100, score))
        return {
            "health_score": score,
            "band": self.band_for(score),
            "coverage": {"components": coverage, "denominator": denom, "verifiable": True},
        }

    # ------------------------------------------------------------------
    # Per-domain check
    # ------------------------------------------------------------------
    async def check_domain(self, db: AsyncSession, domain: str) -> Optional[DomainHealth]:
        """Run all checks for one domain, upsert the record, alert on change.

        Returns the persisted (flushed) ``DomainHealth`` row, or ``None`` if the
        domain string is invalid.
        """
        d = domain_of(domain)
        if d is None:
            return None

        # All lookups are blocking DNS — run them off the loop.
        spf = await asyncio.to_thread(domain_auth.check_spf, d)
        dkim = await asyncio.to_thread(domain_auth.check_dkim, d, None)
        dmarc = await asyncio.to_thread(domain_auth.check_dmarc, d)
        blacklists = await asyncio.to_thread(blacklist_check.check_blacklists, d)

        scored = self.compute_score(spf, dkim, dmarc, blacklists)

        row = (
            await db.execute(select(DomainHealth).where(DomainHealth.domain == d))
        ).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        prior_score = row.health_score if row is not None else None
        prior_blacklists = row.blacklists if row is not None else None
        is_new = row is None
        if row is None:
            row = DomainHealth(domain=d)
            db.add(row)

        row.spf = spf
        row.dkim = dkim
        row.dmarc = dmarc
        row.blacklists = blacklists
        row.previous_score = prior_score
        row.health_score = scored["health_score"]
        row.band = scored["band"]
        row.coverage = scored["coverage"]
        row.last_checked = now
        await db.flush()

        # Change-only alerting (skip the very first observation — nothing to
        # compare against, and we don't want a burst of alerts on first rollout).
        if not is_new:
            await self._alert_on_change(
                d, prior_score, prior_blacklists, scored, blacklists, row.band
            )
        return row

    async def _alert_on_change(
        self,
        domain: str,
        prior_score: Optional[int],
        prior_blacklists: Optional[dict],
        scored: dict,
        blacklists: dict,
        band: str,
    ) -> None:
        """Fire a notification only when something materially changed."""
        new_score = scored["health_score"]
        prior_band = self.band_for(prior_score) if prior_score is not None else None

        reasons: list[str] = []

        # New blacklist listing appeared (meaningful zones only).
        prior_listed = set((prior_blacklists or {}).get("listed_meaningful") or [])
        new_listed = set(blacklists.get("listed_meaningful") or [])
        newly_listed = sorted(new_listed - prior_listed)
        if newly_listed:
            reasons.append(f"newly listed on {', '.join(newly_listed)}")

        # Band boundary crossing.
        if prior_band is not None and band != prior_band:
            reasons.append(f"health band {prior_band} → {band}")
        elif prior_score is not None and new_score != prior_score:
            # Same band but score moved — worth a quieter note.
            reasons.append(f"health score {prior_score} → {new_score}")

        if not reasons:
            return

        summary = "; ".join(reasons)
        logger.warning("Domain health changed for %s: %s (score %s → %s)",
                       domain, summary, prior_score, new_score)
        try:
            details = (
                f"Score: {prior_score} → {new_score} (band {band}).\n"
                f"Blacklist status: {blacklists.get('status')}"
            )
            await get_notification_service().notify_domain_health_change(
                domain, summary, details
            )
        except Exception:  # pragma: no cover - notification must never break the run
            logger.exception("Failed to send domain-health change notification for %s", domain)

    # ------------------------------------------------------------------
    # Full run
    # ------------------------------------------------------------------
    async def check_all(self, db: AsyncSession) -> dict:
        """Check every monitored domain. Returns a run summary."""
        domains = await self.collect_domains(db)
        results = []
        for domain in domains:
            try:
                row = await self.check_domain(db, domain)
                if row is not None:
                    results.append({
                        "domain": row.domain,
                        "health_score": row.health_score,
                        "band": row.band,
                        "previous_score": row.previous_score,
                    })
            except Exception:
                logger.exception("Domain health check failed for %s", domain)
        return {"domains_checked": len(results), "results": results}


_checker: Optional[DomainHealthChecker] = None


def get_domain_health_checker() -> DomainHealthChecker:
    global _checker
    if _checker is None:
        _checker = DomainHealthChecker()
    return _checker
