"""Google Postmaster Tools client + daily metric pull.

Pulls domain/IP reputation, user-reported spam rate, SPF/DKIM/DMARC pass rates,
TLS encryption ratios and delivery errors for every domain a connected account
has verified, and upserts one :class:`PostmasterMetric` row per (domain, date).

Everything is **fail-soft**: no connected account, an unconfigured OAuth app, or
a transient API error yields a ``{"status": ...}`` summary rather than raising,
so the daily task and the manual-trigger endpoint never crash a run.

API reference: https://developers.google.com/workspace/postmaster
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.postmaster import PostmasterAccount, PostmasterMetric
from app.services import crypto, postmaster_oauth

logger = logging.getLogger(__name__)

_BASE = "https://gmailpostmastertools.googleapis.com/v1"


def is_configured() -> bool:
    """Whether the Google OAuth app backing Postmaster is configured."""
    return postmaster_oauth.is_configured()


def _parse_stat_date(name: str) -> Optional[date]:
    """Extract the YYYYMMDD date embedded in a trafficStats resource name."""
    # name = "domains/example.com/trafficStats/20231015"
    tail = (name or "").rstrip("/").split("/")[-1]
    if len(tail) != 8 or not tail.isdigit():
        return None
    try:
        return date(int(tail[0:4]), int(tail[4:6]), int(tail[6:8]))
    except ValueError:
        return None


def parse_traffic_stat(raw: dict) -> dict:
    """Flatten a Postmaster trafficStats payload into our column shape."""
    return {
        "metric_date": _parse_stat_date(raw.get("name", "")),
        "domain_reputation": raw.get("domainReputation"),
        "ip_reputations": raw.get("ipReputations") or [],
        "spam_rate": raw.get("userReportedSpamRatio"),
        "dkim_success_rate": raw.get("dkimSuccessRatio"),
        "spf_success_rate": raw.get("spfSuccessRatio"),
        "dmarc_success_rate": raw.get("dmarcSuccessRatio"),
        "inbound_encryption_ratio": raw.get("inboundEncryptionRatio"),
        "outbound_encryption_ratio": raw.get("outboundEncryptionRatio"),
        "delivery_errors": raw.get("deliveryErrors") or [],
        "raw": raw,
    }


class PostmasterClient:
    """Thin async client over the Postmaster Tools REST API."""

    def __init__(self, access_token: str) -> None:
        self._headers = {"Authorization": f"Bearer {access_token}"}

    async def list_domains(self) -> list[str]:
        """Return the bare domain names the account has verified."""
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{_BASE}/domains", headers=self._headers)
        resp.raise_for_status()
        out = []
        for d in resp.json().get("domains", []):
            name = d.get("name", "")  # "domains/example.com"
            if name.startswith("domains/"):
                out.append(name[len("domains/"):])
        return out

    async def get_traffic_stats(self, domain: str) -> list[dict]:
        """Return the available trafficStats windows for one domain (raw)."""
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{_BASE}/domains/{domain}/trafficStats", headers=self._headers
            )
        resp.raise_for_status()
        return resp.json().get("trafficStats", [])


async def _upsert_metric(
    db: AsyncSession, org_id, domain: str, parsed: dict
) -> Optional[PostmasterMetric]:
    """Insert or update the (domain, date) metric row. Returns it, or None."""
    metric_date = parsed.get("metric_date")
    if metric_date is None:
        return None
    existing = (
        await db.execute(
            select(PostmasterMetric)
            .where(PostmasterMetric.domain == domain)
            .where(PostmasterMetric.metric_date == metric_date)
        )
    ).scalar_one_or_none()
    fields = {k: v for k, v in parsed.items() if k != "metric_date"}
    if existing is None:
        row = PostmasterMetric(domain=domain, metric_date=metric_date, **fields)
        if org_id is not None:
            row.org_id = org_id
        db.add(row)
        return row
    for key, value in fields.items():
        setattr(existing, key, value)
    return existing


async def _fresh_access_token(db: AsyncSession, account: PostmasterAccount) -> str:
    """Refresh + persist the account's access token, returning the new token."""
    refresh_token = crypto.decrypt(account.refresh_token_encrypted)
    access_token, ttl = postmaster_oauth.refresh_access_token(refresh_token)
    account.access_token_encrypted = crypto.encrypt(access_token)
    from datetime import timedelta

    account.token_expiry = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    return access_token


async def pull_for_account(db: AsyncSession, account: PostmasterAccount) -> dict:
    """Pull + upsert metrics for every domain on one connected account."""
    try:
        access_token = await _fresh_access_token(db, account)
    except postmaster_oauth.OAuthError as exc:
        account.status = "error"
        logger.warning("Postmaster token refresh failed for %s: %s", account.email, exc)
        return {"account": account.email, "status": "auth_error", "domains": 0, "rows": 0}

    client = PostmasterClient(access_token)
    try:
        domains = await client.list_domains()
    except httpx.HTTPError as exc:
        logger.warning("Postmaster list_domains failed for %s: %s", account.email, exc)
        return {"account": account.email, "status": "api_error", "domains": 0, "rows": 0}

    rows = 0
    per_domain = []
    for domain in domains:
        try:
            stats = await client.get_traffic_stats(domain)
        except httpx.HTTPError as exc:
            logger.warning("Postmaster trafficStats failed for %s: %s", domain, exc)
            per_domain.append({"domain": domain, "status": "api_error"})
            continue
        domain_rows = 0
        for raw in stats:
            parsed = parse_traffic_stat(raw)
            row = await _upsert_metric(db, account.org_id, domain, parsed)
            if row is not None:
                domain_rows += 1
        rows += domain_rows
        per_domain.append({"domain": domain, "rows": domain_rows})

    account.last_pulled_at = datetime.now(timezone.utc)
    account.status = "connected"
    return {
        "account": account.email,
        "status": "ok",
        "domains": len(domains),
        "rows": rows,
        "per_domain": per_domain,
    }


async def pull_all(db: AsyncSession) -> dict:
    """Pull metrics for every connected Postmaster account in the current org."""
    if not is_configured():
        return {"status": "not_configured", "accounts": 0, "rows": 0}
    accounts = (
        await db.execute(
            select(PostmasterAccount).where(PostmasterAccount.status != "revoked")
        )
    ).scalars().all()
    if not accounts:
        return {"status": "no_accounts", "accounts": 0, "rows": 0}

    results = []
    total_rows = 0
    for account in accounts:
        result = await pull_for_account(db, account)
        results.append(result)
        total_rows += result.get("rows", 0)
    return {
        "status": "ok",
        "accounts": len(accounts),
        "rows": total_rows,
        "results": results,
    }
