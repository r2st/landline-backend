"""Google Postmaster Tools API (P2 audit — Gmail complaint visibility).

Gmail exposes no feedback loop, so this is Landline's only window into spam
complaints and reputation for its primary transport. Endpoints:

    GET  /postmaster/oauth/authorize        -> { authorization_url, state }
    GET  /postmaster/oauth/callback         -> connect (closes popup)
    GET  /postmaster/status                 -> configured + connected accounts
    GET  /postmaster/domains                -> domains with their latest metrics
    GET  /postmaster/domains/{domain}/metrics -> time series for one domain
    POST /postmaster/pull                   -> trigger a metric pull now
    DELETE /postmaster/accounts/{id}        -> disconnect (revoke at Google)
"""

import html
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user_optional
from app.models.postmaster import PostmasterAccount, PostmasterMetric
from app.models.user import User
from app.org_context import APPREND_ORG_ID
from app.services import crypto, postmaster_oauth, postmaster_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/postmaster", tags=["postmaster"])
# The OAuth callback is hit by Google's browser redirect with no credentials, so
# it lives on a separate router mounted WITHOUT the auth dependency (mirrors
# outreach.public_router). It binds its own org via the signed state.
public_router = APIRouter(prefix="/postmaster", tags=["postmaster"])


# --- Schemas ---

class AuthorizeResponse(BaseModel):
    authorization_url: str
    state: str


class AccountRead(BaseModel):
    id: uuid.UUID
    email: str
    status: str
    last_pulled_at: Optional[datetime] = None
    connected_at: datetime

    class Config:
        from_attributes = True


class StatusResponse(BaseModel):
    configured: bool
    connected: bool
    accounts: list[AccountRead] = []


class MetricRead(BaseModel):
    domain: str
    metric_date: date
    domain_reputation: Optional[str] = None
    ip_reputations: list = []
    spam_rate: Optional[float] = None
    dkim_success_rate: Optional[float] = None
    spf_success_rate: Optional[float] = None
    dmarc_success_rate: Optional[float] = None
    inbound_encryption_ratio: Optional[float] = None
    outbound_encryption_ratio: Optional[float] = None
    delivery_errors: list = []

    class Config:
        from_attributes = True


class DomainSummary(BaseModel):
    domain: str
    latest: Optional[MetricRead] = None
    days_tracked: int = 0


def _callback_html(ok: bool, message: str) -> str:
    settings = get_settings()
    origin = settings.frontend_url.rstrip("/")
    status = "connected" if ok else "error"
    safe = html.escape(message)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Postmaster connection</title></head>
<body style="font-family: system-ui, sans-serif; padding: 2rem; text-align: center;">
  <p>{safe}</p>
  <p style="color:#6b7280;font-size:0.875rem;">You can close this window.</p>
  <script>
    (function () {{
      var payload = {{ source: "landline-postmaster-oauth", status: "{status}", message: {safe!r} }};
      try {{ if (window.opener) window.opener.postMessage(payload, "{origin}"); }} catch (e) {{}}
      setTimeout(function () {{ window.close(); }}, 1200);
    }})();
  </script>
</body></html>"""


@router.get("/oauth/authorize", response_model=AuthorizeResponse)
async def authorize(current_user: Optional[User] = Depends(get_current_user_optional)):
    """Return the Google consent-screen URL for connecting Postmaster Tools."""
    if not postmaster_oauth.is_configured():
        raise HTTPException(status_code=503, detail="Google OAuth is not configured on the server.")
    user_id = str(current_user.id) if current_user else None
    state = postmaster_oauth.issue_state(user_id=user_id)
    return AuthorizeResponse(
        authorization_url=postmaster_oauth.build_authorization_url(state), state=state
    )


@public_router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    db: AsyncSession = Depends(get_db),
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
):
    """Handle Google's redirect: exchange the code and store the connection."""
    if error:
        return HTMLResponse(_callback_html(False, f"Google returned an error: {error}"))
    state_payload = postmaster_oauth.read_state(state or "")
    if not code or state_payload is None:
        return HTMLResponse(_callback_html(False, "Invalid or expired authorization request."))

    connecting_user_id: uuid.UUID | None = None
    raw_user_id = state_payload.get("u")
    if raw_user_id:
        try:
            connecting_user_id = uuid.UUID(str(raw_user_id))
        except (ValueError, TypeError):
            connecting_user_id = None

    connecting_org_id: uuid.UUID | None = None
    if connecting_user_id is not None:
        connecting_org_id = (
            await db.execute(
                select(User.org_id)
                .where(User.id == connecting_user_id)
                .execution_options(skip_org_filter=True)
            )
        ).scalar_one_or_none()

    try:
        connected = postmaster_oauth.exchange_code(code)
    except postmaster_oauth.OAuthError as exc:
        return HTMLResponse(_callback_html(False, f"Could not connect: {exc}"))
    except Exception:
        logger.exception("Unexpected Postmaster OAuth callback failure")
        return HTMLResponse(_callback_html(False, "Unexpected error connecting Postmaster."))

    account = (
        await db.execute(
            select(PostmasterAccount)
            .where(PostmasterAccount.google_sub == connected.google_sub)
            .execution_options(skip_org_filter=True)
        )
    ).scalar_one_or_none()

    expiry = datetime.now(timezone.utc) + timedelta(seconds=connected.expires_in)
    access_enc = crypto.encrypt(connected.access_token)

    if account is None:
        if not connected.refresh_token:
            return HTMLResponse(_callback_html(
                False,
                "Google did not return a refresh token. Remove Landline's access "
                "in your Google Account and try connecting again.",
            ))
        account = PostmasterAccount(
            org_id=connecting_org_id or APPREND_ORG_ID,
            user_id=connecting_user_id,
            email=connected.email,
            google_sub=connected.google_sub,
            display_name=connected.display_name,
            refresh_token_encrypted=crypto.encrypt(connected.refresh_token),
            access_token_encrypted=access_enc,
            token_expiry=expiry,
            scopes=connected.scopes,
            status="connected",
        )
        db.add(account)
    else:
        account.email = connected.email
        account.display_name = connected.display_name
        account.access_token_encrypted = access_enc
        account.token_expiry = expiry
        account.scopes = connected.scopes
        account.status = "connected"
        if connecting_user_id is not None:
            account.user_id = connecting_user_id
        if connected.refresh_token:
            account.refresh_token_encrypted = crypto.encrypt(connected.refresh_token)

    await db.flush()
    logger.info("Connected Postmaster account %s", connected.email)
    return HTMLResponse(_callback_html(True, f"Connected {connected.email}."))


@router.get("/status", response_model=StatusResponse)
async def status(db: AsyncSession = Depends(get_db)):
    """Connection status + connected accounts for the settings UI."""
    accounts = (
        await db.execute(
            select(PostmasterAccount).order_by(PostmasterAccount.connected_at.desc())
        )
    ).scalars().all()
    return StatusResponse(
        configured=postmaster_oauth.is_configured(),
        connected=any(a.status == "connected" for a in accounts),
        accounts=list(accounts),
    )


@router.get("/domains", response_model=list[DomainSummary])
async def list_domains(db: AsyncSession = Depends(get_db)):
    """All monitored domains with their most recent metric snapshot."""
    rows = (
        await db.execute(
            select(PostmasterMetric).order_by(
                PostmasterMetric.domain.asc(), PostmasterMetric.metric_date.desc()
            )
        )
    ).scalars().all()
    by_domain: dict[str, list[PostmasterMetric]] = {}
    for r in rows:
        by_domain.setdefault(r.domain, []).append(r)
    summaries = []
    for domain, metrics in sorted(by_domain.items()):
        summaries.append(
            DomainSummary(
                domain=domain,
                latest=MetricRead.model_validate(metrics[0]),
                days_tracked=len(metrics),
            )
        )
    return summaries


@router.get("/domains/{domain}/metrics", response_model=list[MetricRead])
async def domain_metrics(
    domain: str,
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Metric time series for one domain, most recent first."""
    cutoff = date.today() - timedelta(days=days)
    rows = (
        await db.execute(
            select(PostmasterMetric)
            .where(PostmasterMetric.domain == domain)
            .where(PostmasterMetric.metric_date >= cutoff)
            .order_by(PostmasterMetric.metric_date.desc())
        )
    ).scalars().all()
    return [MetricRead.model_validate(r) for r in rows]


@router.post("/pull", response_model=dict)
async def pull_now(db: AsyncSession = Depends(get_db)):
    """Trigger an immediate Postmaster metric pull for connected accounts."""
    result = await postmaster_service.pull_all(db)
    return result


@router.delete("/accounts/{account_id}", status_code=204)
async def disconnect(
    account_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Disconnect a Postmaster account: revoke at Google and delete locally."""
    account = (
        await db.execute(
            select(PostmasterAccount).where(PostmasterAccount.id == account_id)
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Postmaster account not found")
    try:
        refresh_token = crypto.decrypt(account.refresh_token_encrypted)
        postmaster_oauth.revoke_token(refresh_token)
    except Exception as exc:
        logger.warning("Could not revoke Postmaster token for %s: %s", account.email, exc)
    await db.delete(account)
    await db.flush()
