"""Tests for the bulk suppression list (Feature 2)."""

import io
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gmail_account import GmailAccount
from app.models.suppression import SuppressionEntry
from app.services import suppression


# ─── Service ─────────────────────────────────────────────────────────────────


def test_normalize_email():
    assert suppression.normalize_email(" Foo@Bar.COM ") == "foo@bar.com"
    assert suppression.normalize_email("not-an-email") is None
    assert suppression.normalize_email("") is None
    assert suppression.normalize_email(None) is None


def test_extract_emails_shapes():
    # Header row with email + reason columns.
    rows = suppression._extract_emails("email,reason\na@b.com,bounce\nc@d.com,\n")
    assert rows == [("a@b.com", "bounce"), ("c@d.com", None)]

    # Headerless single column (plain list).
    rows = suppression._extract_emails("x@y.com\nz@w.com\n")
    assert [r[0] for r in rows] == ["x@y.com", "z@w.com"]

    # Unknown reason value is dropped (caller default applies).
    rows = suppression._extract_emails("email,reason\na@b.com,whatever")
    assert rows == [("a@b.com", None)]


@pytest.mark.asyncio
async def test_is_suppressed_and_add(db_session: AsyncSession):
    assert await suppression.is_suppressed(db_session, "nobody@x.com") is False

    entry, status = await suppression.add_entry(db_session, "Foo@Bar.com", reason="complaint")
    await db_session.commit()
    assert status == "added"
    assert entry.email == "foo@bar.com"

    # Case-insensitive match.
    assert await suppression.is_suppressed(db_session, "FOO@bar.com") is True

    # Duplicate is idempotent.
    _, status2 = await suppression.add_entry(db_session, "foo@bar.com")
    assert status2 == "duplicate"

    # Invalid never raises.
    _, status3 = await suppression.add_entry(db_session, "garbage")
    assert status3 == "invalid"


@pytest.mark.asyncio
async def test_import_csv_dedups_and_counts(db_session: AsyncSession):
    csv_text = (
        "email,reason\n"
        "a@example.com,unsubscribe\n"
        "A@EXAMPLE.COM,bounce\n"   # dup of row 1 (case-insensitive)
        "b@example.com,\n"          # blank reason → default
        "not-an-email\n"            # invalid
    )
    result = await suppression.import_csv(db_session, csv_text, default_reason="manual")
    assert result["added"] == 2
    assert result["duplicates"] == 1
    assert result["invalid"] == 1

    assert await suppression.is_suppressed(db_session, "a@example.com") is True
    assert await suppression.is_suppressed(db_session, "b@example.com") is True


# ─── API ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suppression_api_crud(client: AsyncClient):
    # Add one by hand.
    resp = await client.post("/api/v1/dialtone/suppression", json={"email": "hand@x.com", "reason": "manual"})
    assert resp.status_code == 201, resp.text
    entry_id = resp.json()["id"]

    # Duplicate → 409.
    dup = await client.post("/api/v1/dialtone/suppression", json={"email": "hand@x.com"})
    assert dup.status_code == 409

    # Bad email → 400.
    bad = await client.post("/api/v1/dialtone/suppression", json={"email": "nope"})
    assert bad.status_code == 400

    # List shows it.
    lst = await client.get("/api/v1/dialtone/suppression")
    assert lst.status_code == 200
    body = lst.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "hand@x.com"

    # Delete.
    dele = await client.delete(f"/api/v1/dialtone/suppression/{entry_id}")
    assert dele.status_code == 204
    lst2 = await client.get("/api/v1/dialtone/suppression")
    assert lst2.json()["total"] == 0


@pytest.mark.asyncio
async def test_suppression_api_import_upload(client: AsyncClient):
    csv_bytes = b"email,reason\nu1@x.com,unsubscribe\nu2@x.com,\nu1@x.com,\n"
    files = {"file": ("suppress.csv", io.BytesIO(csv_bytes), "text/csv")}
    resp = await client.post("/api/v1/dialtone/suppression/import", files=files)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["added"] == 2
    assert result["duplicates"] == 1

    lst = await client.get("/api/v1/dialtone/suppression?q=u2")
    assert lst.json()["total"] == 1


@pytest.mark.asyncio
async def test_suppression_api_search_pagination(client: AsyncClient, db_session: AsyncSession):
    for i in range(5):
        db_session.add(SuppressionEntry(email=f"lead{i}@corp.com", reason="manual", source="manual"))
    db_session.add(SuppressionEntry(email="ceo@other.com", reason="complaint", source="manual"))
    await db_session.commit()

    # Search narrows to the corp.com addresses.
    resp = await client.get("/api/v1/dialtone/suppression?q=corp.com&page_size=2")
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2  # page 1 of 3


# ─── Send-path enforcement ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_outreach_email_blocks_suppressed(db_session: AsyncSession, monkeypatch):
    from app.services.email_dispatch import send_outreach_email

    sent = {"count": 0}
    monkeypatch.setattr(
        "app.services.email_dispatch.GmailSender.send_email",
        lambda self, **kw: sent.__setitem__("count", sent["count"] + 1) or {"status": "sent"},
    )

    account = GmailAccount(
        id=uuid.uuid4(), email="from@x.com", google_sub="g",
        refresh_token_encrypted="enc", is_active=True, sending_enabled=True, status="connected",
    )
    db_session.add(account)
    db_session.add(SuppressionEntry(email="blocked@x.com", reason="unsubscribe", source="import"))
    await db_session.commit()

    result = await send_outreach_email(
        db_session, to_email="Blocked@X.com", subject="Hi", body_html="<p>Hi</p>",
        include_unsubscribe=False,
    )
    assert result["status"] == "suppressed"
    assert sent["count"] == 0
