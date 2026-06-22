"""SMTP sender-account endpoints.

Lets users add/manage email senders that aren't connected via Google OAuth —
Outlook/Office 365, custom domains, any authenticated SMTP host. Mirrors the
Gmail router's sender-pool semantics (primary/active, sending toggle, status)
and shares the single "primary sender" flag across both account types.

Routes:
    GET    /smtp/accounts                       -> list SMTP accounts
    POST   /smtp/accounts                       -> add (verifies connection)
    PUT    /smtp/accounts/{id}                  -> edit (re-verifies)
    DELETE /smtp/accounts/{id}                  -> remove
    POST   /smtp/accounts/{id}/set-active       -> make the primary sender
    POST   /smtp/accounts/{id}/toggle-sending   -> enable/disable for rotation
    POST   /smtp/accounts/{id}/verify           -> re-test the connection
    POST   /smtp/accounts/{id}/test             -> send a test email
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount
from app.schemas.smtp import (
    SmtpAccountCreate,
    SmtpAccountRead,
    SmtpAccountUpdate,
    TestSendRequest,
    TestSendResult,
    ToggleSendingRequest,
)
from app.services import crypto
from app.services.smtp_sender import SmtpSender

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/smtp", tags=["smtp"])


async def _clear_primary_sender(db: AsyncSession) -> None:
    """Clear the ``is_active`` (primary) flag across every sender type.

    The primary sender is a single choice spanning both Gmail and SMTP accounts,
    so promoting one must demote all others regardless of type.
    """
    await db.execute(update(GmailAccount).values(is_active=False))
    await db.execute(update(SmtpAccount).values(is_active=False))


async def _get(db: AsyncSession, account_id: uuid.UUID) -> SmtpAccount:
    result = await db.execute(select(SmtpAccount).where(SmtpAccount.id == account_id))
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="SMTP account not found")
    return account


@router.get("/accounts", response_model=list[SmtpAccountRead])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    """List all configured SMTP sender accounts."""
    result = await db.execute(
        select(SmtpAccount).order_by(
            SmtpAccount.is_active.desc(), SmtpAccount.connected_at.desc()
        )
    )
    return list(result.scalars().all())


@router.post("/accounts", response_model=SmtpAccountRead, status_code=201)
async def add_account(payload: SmtpAccountCreate, db: AsyncSession = Depends(get_db)):
    """Add an SMTP sender account after verifying its credentials connect."""
    existing = await db.execute(
        select(SmtpAccount).where(SmtpAccount.email == str(payload.email))
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"An SMTP account for {payload.email} already exists.",
        )

    account = SmtpAccount(
        email=str(payload.email),
        display_name=payload.display_name,
        host=payload.host,
        port=payload.port,
        username=payload.username,
        password_encrypted=crypto.encrypt(payload.password),
        use_tls=payload.use_tls,
        use_ssl=payload.use_ssl,
        sending_enabled=True,
    )

    # Verify the connection before persisting a sender that can't actually send.
    verify = await asyncio.to_thread(SmtpSender(account).verify)
    if not verify.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=f"Could not connect to the SMTP server: {verify.get('detail', 'unknown error')}",
        )

    # First sender of any kind becomes the primary automatically.
    any_gmail = (await db.execute(select(GmailAccount.id).limit(1))).first()
    any_smtp = (await db.execute(select(SmtpAccount.id).limit(1))).first()
    account.is_active = any_gmail is None and any_smtp is None
    account.status = "connected"
    account.last_error = None

    db.add(account)
    await db.flush()
    await db.refresh(account)
    logger.info("Added SMTP sender %s (%s:%s)", account.email, account.host, account.port)
    return account


@router.put("/accounts/{account_id}", response_model=SmtpAccountRead)
async def update_account(
    account_id: uuid.UUID,
    payload: SmtpAccountUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Edit an SMTP account's settings and re-verify the connection."""
    account = await _get(db, account_id)

    data = payload.model_dump(exclude_unset=True)
    if "email" in data and data["email"] is not None:
        account.email = str(data["email"])
    for field in ("display_name", "host", "port", "username", "use_tls", "use_ssl"):
        if field in data and data[field] is not None:
            setattr(account, field, data[field])
    if data.get("password"):
        account.password_encrypted = crypto.encrypt(data["password"])

    verify = await asyncio.to_thread(SmtpSender(account).verify)
    if not verify.get("ok"):
        account.status = "error"
        account.last_error = verify.get("detail")
        await db.flush()
        raise HTTPException(
            status_code=400,
            detail=f"Saved, but could not connect: {verify.get('detail', 'unknown error')}",
        )
    account.status = "connected"
    account.last_error = None
    await db.flush()
    await db.refresh(account)
    logger.info("Updated SMTP sender %s", account.email)
    return account


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_account(account_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Remove an SMTP sender account."""
    account = await _get(db, account_id)
    was_active = account.is_active
    await db.delete(account)
    await db.flush()

    # If we removed the primary, promote another connected sender so outreach
    # keeps flowing. Prefer another SMTP account, then any Gmail account.
    if was_active:
        repl = (
            await db.execute(
                select(SmtpAccount)
                .where(SmtpAccount.status == "connected")
                .order_by(SmtpAccount.connected_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if repl is None:
            repl = (
                await db.execute(
                    select(GmailAccount)
                    .where(GmailAccount.status == "connected")
                    .order_by(GmailAccount.connected_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if repl is not None:
            repl.is_active = True
            await db.flush()
    logger.info("Removed SMTP sender %s", account.email)


@router.post("/accounts/{account_id}/set-active", response_model=SmtpAccountRead)
async def set_active(account_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Make this SMTP account the primary sender (demotes all other senders)."""
    account = await _get(db, account_id)
    if account.status != "connected":
        raise HTTPException(
            status_code=400,
            detail="This account isn't connected — fix its settings before making it primary.",
        )
    await _clear_primary_sender(db)
    account.is_active = True
    await db.flush()
    await db.refresh(account)
    logger.info("Set primary sender to SMTP account %s", account.email)
    return account


@router.post("/accounts/{account_id}/toggle-sending", response_model=SmtpAccountRead)
async def toggle_sending(
    account_id: uuid.UUID,
    body: ToggleSendingRequest,
    db: AsyncSession = Depends(get_db),
):
    """Enable or disable this account for the sending rotation pool."""
    account = await _get(db, account_id)
    if account.status != "connected" and body.sending_enabled:
        raise HTTPException(
            status_code=400,
            detail="This account isn't connected — fix its settings before enabling sending.",
        )
    account.sending_enabled = body.sending_enabled
    await db.flush()
    await db.refresh(account)
    state = "enabled" if body.sending_enabled else "disabled"
    logger.info("Sending %s for SMTP account %s", state, account.email)
    return account


@router.post("/accounts/{account_id}/verify", response_model=SmtpAccountRead)
async def verify_account(account_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Re-test the SMTP connection and update the account's status."""
    account = await _get(db, account_id)
    result = await asyncio.to_thread(SmtpSender(account).verify)
    if result.get("ok"):
        account.status = "connected"
        account.last_error = None
    else:
        account.status = "error"
        account.last_error = result.get("detail")
    await db.flush()
    await db.refresh(account)
    return account


@router.post("/accounts/{account_id}/test", response_model=TestSendResult)
async def test_send(
    account_id: uuid.UUID,
    body: TestSendRequest,
    db: AsyncSession = Depends(get_db),
):
    """Send a real test email from this account to confirm deliverability."""
    account = await _get(db, account_id)
    to_email = str(body.to_email) if body.to_email else account.email
    result = await asyncio.to_thread(
        SmtpSender(account).send_email,
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
    if result.get("status") == "sent":
        account.status = "connected"
        account.last_error = None
        account.last_used_at = datetime.now(timezone.utc)
    else:
        account.status = "error"
        account.last_error = result.get("detail")
    await db.flush()
    return TestSendResult(
        status=result.get("status", "error"),
        detail=result.get("detail"),
        sender=account.email,
        transport="smtp",
    )
