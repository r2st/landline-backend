"""Tests that sender email accounts (Gmail + SMTP) are scoped by user login."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount


async def _gmail(db: AsyncSession, email: str, owner_id) -> GmailAccount:
    acct = GmailAccount(
        id=uuid.uuid4(), user_id=owner_id, email=email, google_sub=f"sub-{email}",
        display_name=email, refresh_token_encrypted="tok",
    )
    db.add(acct)
    await db.flush()
    return acct


async def _smtp(db: AsyncSession, email: str, owner_id) -> SmtpAccount:
    acct = SmtpAccount(
        id=uuid.uuid4(), user_id=owner_id, email=email, display_name=email,
        host="smtp.example.com", port=587, username=email, password_encrypted="pw",
    )
    db.add(acct)
    await db.flush()
    return acct


@pytest.mark.asyncio
async def test_gmail_accounts_scoped_by_user(
    client: AsyncClient, db_session, agent_user, admin_user, auth_headers
):
    await _gmail(db_session, "agent-box@company.com", agent_user.id)
    await _gmail(db_session, "admin-box@company.com", admin_user.id)
    await db_session.commit()

    # Agent sees only their own account.
    resp = await client.get("/api/v1/gmail/accounts", headers=auth_headers(agent_user))
    assert resp.status_code == 200
    emails = {a["email"] for a in resp.json()}
    assert emails == {"agent-box@company.com"}

    # Admin sees only theirs.
    resp2 = await client.get("/api/v1/gmail/accounts", headers=auth_headers(admin_user))
    assert {a["email"] for a in resp2.json()} == {"admin-box@company.com"}

    # No auth (API-key/back-office mode) sees all accounts.
    resp3 = await client.get("/api/v1/gmail/accounts")
    assert {a["email"] for a in resp3.json()} == {
        "agent-box@company.com", "admin-box@company.com",
    }


@pytest.mark.asyncio
async def test_smtp_accounts_scoped_by_user(
    client: AsyncClient, db_session, agent_user, admin_user, auth_headers
):
    await _smtp(db_session, "agent-smtp@company.com", agent_user.id)
    await _smtp(db_session, "admin-smtp@company.com", admin_user.id)
    await db_session.commit()

    resp = await client.get("/api/v1/smtp/accounts", headers=auth_headers(agent_user))
    assert resp.status_code == 200
    assert {a["email"] for a in resp.json()} == {"agent-smtp@company.com"}

    resp_all = await client.get("/api/v1/smtp/accounts")
    assert {a["email"] for a in resp_all.json()} == {
        "agent-smtp@company.com", "admin-smtp@company.com",
    }
