"""Gmail OAuth connection endpoints.

Supports multiple connected Gmail accounts with individual sending toggles
and round-robin sender rotation across enabled accounts.

Flow:
    GET  /gmail/oauth/authorize          -> { authorization_url, state }
    GET  /gmail/oauth/callback?code&state-> upserts account, closes popup
    GET  /gmail/status                   -> connection status + all accounts
    GET  /gmail/accounts                 -> list connected accounts
    POST /gmail/accounts/{id}/set-active -> choose the primary sender
    POST /gmail/accounts/{id}/toggle-sending -> enable/disable for rotation
    DELETE /gmail/accounts/{id}          -> disconnect (revokes at Google)
"""

import asyncio
import html
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount
from app.schemas.gmail import (
    AuthorizeResponse,
    GmailAccountRead,
    GmailStatusResponse,
    ToggleSendingRequest,
)
from app.schemas.smtp import TestSendRequest, TestSendResult
from app.services import crypto, google_oauth
from app.services.gmail_sender import GmailSender

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/gmail", tags=["gmail"])


def _callback_html(ok: bool, message: str) -> str:
    """A tiny page that notifies the opener window and closes the popup."""
    settings = get_settings()
    origin = settings.frontend_url.rstrip("/")
    status = "connected" if ok else "error"
    safe_message = html.escape(message)
    return f"""<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"><title>Gmail connection</title></head>
  <body style="font-family: system-ui, sans-serif; padding: 2rem; text-align: center;">
    <p>{safe_message}</p>
    <p style="color:#6b7280;font-size:0.875rem;">You can close this window.</p>
    <script>
      (function () {{
        var payload = {{ source: "landline-gmail-oauth", status: "{status}", message: {safe_message!r} }};
        try {{
          if (window.opener) {{
            window.opener.postMessage(payload, "{origin}");
          }}
        }} catch (e) {{}}
        setTimeout(function () {{ window.close(); }}, 1200);
      }})();
    </script>
  </body>
</html>"""


@router.get("/oauth/authorize", response_model=AuthorizeResponse)
async def authorize():
    """Return the Google consent-screen URL for connecting a Gmail account."""
    if not google_oauth.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Google OAuth is not configured on the server.",
        )
    state = google_oauth.issue_state()
    url = google_oauth.build_authorization_url(state)
    return AuthorizeResponse(authorization_url=url, state=state)


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    db: AsyncSession = Depends(get_db),
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
):
    """Handle Google's redirect: exchange the code and store the connection."""
    if error:
        return HTMLResponse(_callback_html(False, f"Google returned an error: {error}"))
    if not code or not google_oauth.verify_state(state or ""):
        return HTMLResponse(_callback_html(False, "Invalid or expired authorization request."))

    try:
        connected = google_oauth.exchange_code(code)
    except google_oauth.OAuthError as exc:
        return HTMLResponse(_callback_html(False, f"Could not connect: {exc}"))
    except Exception:
        logger.exception("Unexpected OAuth callback failure")
        return HTMLResponse(_callback_html(False, "Unexpected error connecting Gmail."))

    # Upsert by google_sub
    result = await db.execute(
        select(GmailAccount).where(GmailAccount.google_sub == connected.google_sub)
    )
    account = result.scalar_one_or_none()

    expiry = datetime.now(timezone.utc) + timedelta(seconds=connected.expires_in)
    access_enc = crypto.encrypt(connected.access_token)

    if account is None:
        if not connected.refresh_token:
            return HTMLResponse(_callback_html(
                False,
                "Google did not return a refresh token. Remove Landline's access "
                "in your Google Account and try connecting again.",
            ))
        # First account becomes active automatically
        any_existing = (await db.execute(select(GmailAccount.id).limit(1))).first()
        account = GmailAccount(
            email=connected.email,
            google_sub=connected.google_sub,
            display_name=connected.display_name,
            refresh_token_encrypted=crypto.encrypt(connected.refresh_token),
            access_token_encrypted=access_enc,
            token_expiry=expiry,
            scopes=connected.scopes,
            status="connected",
            is_active=(any_existing is None),
            sending_enabled=True,  # New accounts are sending-enabled by default
        )
        db.add(account)
    else:
        account.email = connected.email
        account.display_name = connected.display_name
        account.access_token_encrypted = access_enc
        account.token_expiry = expiry
        account.scopes = connected.scopes
        account.status = "connected"
        if connected.refresh_token:
            account.refresh_token_encrypted = crypto.encrypt(connected.refresh_token)

    await db.flush()
    logger.info(
        "Connected Gmail account %s (active=%s, sending=%s)",
        account.email, account.is_active, account.sending_enabled,
    )
    return HTMLResponse(_callback_html(True, f"Connected {connected.email}."))


@router.get("/status", response_model=GmailStatusResponse)
async def status(db: AsyncSession = Depends(get_db)):
    """Connection status for the settings UI and compose 'From' line."""
    settings = get_settings()
    result = await db.execute(
        select(GmailAccount).order_by(
            GmailAccount.is_active.desc(), GmailAccount.connected_at.desc()
        )
    )
    accounts = list(result.scalars().all())
    active = next((a for a in accounts if a.is_active and a.status == "connected"), None)
    sending_count = sum(
        1 for a in accounts if a.sending_enabled and a.status == "connected"
    )
    return GmailStatusResponse(
        configured=google_oauth.is_configured(),
        connected=any(a.status == "connected" for a in accounts),
        active_email=active.email if active else None,
        active_account_id=active.id if active else None,
        sending_enabled_count=sending_count,
        fallback_sender=settings.ses_sender_email,
        accounts=accounts,
    )


@router.get("/accounts", response_model=list[GmailAccountRead])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    """List all connected Gmail accounts."""
    result = await db.execute(
        select(GmailAccount).order_by(
            GmailAccount.is_active.desc(), GmailAccount.connected_at.desc()
        )
    )
    return list(result.scalars().all())


@router.post("/accounts/{account_id}/set-active", response_model=GmailAccountRead)
async def set_active(account_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Make a connected account the primary sender (clears the flag on others)."""
    result = await db.execute(select(GmailAccount).where(GmailAccount.id == account_id))
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Gmail account not found")
    if account.status != "connected":
        raise HTTPException(
            status_code=400,
            detail="This account is not connected — reconnect it before making it active.",
        )
    # The primary sender spans Gmail + SMTP, so demote every other sender.
    await db.execute(update(GmailAccount).values(is_active=False))
    await db.execute(update(SmtpAccount).values(is_active=False))
    account.is_active = True
    await db.flush()
    await db.refresh(account)
    logger.info("Set primary Gmail sender to %s", account.email)
    return account


@router.post("/accounts/{account_id}/toggle-sending", response_model=GmailAccountRead)
async def toggle_sending(
    account_id: uuid.UUID,
    body: ToggleSendingRequest,
    db: AsyncSession = Depends(get_db),
):
    """Enable or disable an account for the sending rotation pool."""
    result = await db.execute(select(GmailAccount).where(GmailAccount.id == account_id))
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Gmail account not found")
    if account.status != "connected":
        raise HTTPException(
            status_code=400,
            detail="This account is not connected — reconnect it first.",
        )
    account.sending_enabled = body.sending_enabled
    await db.flush()
    await db.refresh(account)
    state = "enabled" if body.sending_enabled else "disabled"
    logger.info("Sending %s for Gmail account %s", state, account.email)
    return account


@router.delete("/accounts/{account_id}", status_code=204)
async def disconnect(account_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Disconnect an account: revoke its token at Google and delete it locally."""
    result = await db.execute(select(GmailAccount).where(GmailAccount.id == account_id))
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Gmail account not found")

    was_active = account.is_active
    try:
        refresh_token = crypto.decrypt(account.refresh_token_encrypted)
        google_oauth.revoke_token(refresh_token)
    except Exception as exc:
        logger.warning("Could not revoke token for %s: %s", account.email, exc)

    await db.delete(account)
    await db.flush()

    if was_active:
        result = await db.execute(
            select(GmailAccount)
            .where(GmailAccount.status == "connected")
            .order_by(GmailAccount.connected_at.desc())
            .limit(1)
        )
        replacement = result.scalar_one_or_none()
        if replacement is not None:
            replacement.is_active = True
            await db.flush()
    logger.info("Disconnected Gmail account %s", account.email)


@router.post("/accounts/{account_id}/test", response_model=TestSendResult)
async def test_send(
    account_id: uuid.UUID,
    body: TestSendRequest,
    db: AsyncSession = Depends(get_db),
):
    """Send a real test email from this Gmail account to confirm sending works."""
    result = await db.execute(select(GmailAccount).where(GmailAccount.id == account_id))
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Gmail account not found")
    if account.status != "connected":
        raise HTTPException(
            status_code=400,
            detail="This account isn't connected — reconnect it before testing.",
        )
    to_email = str(body.to_email) if body.to_email else account.email
    send_result = await asyncio.to_thread(
        GmailSender(account).send_email,
        to_email=to_email,
        subject="Landline test email",
        body_html=(
            "<p>This is a test email from <strong>Landline</strong>.</p>"
            f"<p>If you received this, sending from <strong>{account.email}</strong> "
            "is working correctly.</p>"
        ),
        body_text=(
            "This is a test email from Landline. If you received this, sending "
            f"from {account.email} is working correctly."
        ),
    )
    if send_result.get("status") == "sent":
        account.last_used_at = datetime.now(timezone.utc)
    else:
        account.status = "error"
    await db.flush()
    return TestSendResult(
        status=send_result.get("status", "error"),
        detail=send_result.get("detail"),
        sender=account.email,
        transport="gmail",
    )
