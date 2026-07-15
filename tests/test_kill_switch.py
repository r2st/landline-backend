"""Tests for the global sending kill switch.

Covers the service (is/get/set), enrollment pause+resume cascade, the API
endpoints, and the dispatch-level gate that halts a real send when engaged.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.services import system_settings


async def _make_enrollment(db: AsyncSession, contact: Contact, status: str = "active") -> OutreachEnrollment:
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq", is_active=True)
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status=status
    )
    db.add(enr)
    await db.flush()
    return enr


# --- Service ---


@pytest.mark.asyncio
async def test_default_sending_enabled(db_session: AsyncSession):
    assert await system_settings.is_sending_enabled(db_session) is True
    status = await system_settings.get_sending_status(db_session)
    assert status["enabled"] is True


@pytest.mark.asyncio
async def test_set_disabled_then_enabled(db_session: AsyncSession):
    await system_settings.set_sending_enabled(db_session, False, updated_by="alice@x.com", reason="spike")
    assert await system_settings.is_sending_enabled(db_session) is False
    status = await system_settings.get_sending_status(db_session)
    assert status["enabled"] is False
    assert status["reason"] == "spike"
    assert status["updated_by"] == "alice@x.com"

    await system_settings.set_sending_enabled(db_session, True, updated_by="alice@x.com")
    assert await system_settings.is_sending_enabled(db_session) is True


@pytest.mark.asyncio
async def test_disable_pauses_active_enrollments_and_enable_resumes(
    db_session: AsyncSession, sample_contact: Contact
):
    active = await _make_enrollment(db_session, sample_contact, "active")
    # An enrollment paused for another reason must NOT be resumed by the switch.
    already_paused = await _make_enrollment(db_session, sample_contact, "paused")

    result = await system_settings.set_sending_enabled(db_session, False, updated_by="bob")
    assert result["enrollments_affected"] == 1

    await db_session.refresh(active)
    await db_session.refresh(already_paused)
    assert active.status == "paused"
    assert active.kill_switch_paused is True
    assert already_paused.status == "paused"
    assert already_paused.kill_switch_paused is False

    result = await system_settings.set_sending_enabled(db_session, True, updated_by="bob")
    assert result["enrollments_affected"] == 1

    await db_session.refresh(active)
    await db_session.refresh(already_paused)
    assert active.status == "active"
    assert active.kill_switch_paused is False
    # The other one is untouched — still paused, never kill-switch-owned.
    assert already_paused.status == "paused"


# --- API ---


@pytest.mark.asyncio
async def test_sending_status_endpoint(client: AsyncClient):
    resp = await client.get("/api/v1/system/sending-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    # Rolling stats are included for the dashboard.
    assert "daily_limit" in body


@pytest.mark.asyncio
async def test_sending_toggle_endpoint(client: AsyncClient):
    resp = await client.post(
        "/api/v1/system/sending-toggle",
        json={"enabled": False, "reason": "manual pause"},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    resp = await client.get("/api/v1/system/sending-status")
    assert resp.json()["enabled"] is False
    assert resp.json()["reason"] == "manual pause"

    resp = await client.post("/api/v1/system/sending-toggle", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True


# --- Dispatch gate ---


@pytest.mark.asyncio
async def test_send_outreach_email_halted_when_disabled(
    db_session: AsyncSession, sample_contact: Contact
):
    """When the switch is off, no transport is invoked and status is 'halted'."""
    from app.services.email_dispatch import send_outreach_email

    await system_settings.set_sending_enabled(db_session, False)

    with patch("app.services.email_dispatch._make_sender") as make_sender:
        result = await send_outreach_email(
            db_session,
            to_email="prospect@example.com",
            subject="hi",
            body_html="<p>hi</p>",
        )
    assert result["status"] == "halted"
    make_sender.assert_not_called()


@pytest.mark.asyncio
async def test_bypass_kill_switch_allows_send(db_session: AsyncSession):
    """bypass_kill_switch still sends even while paused (internal callers)."""
    from app.services.email_dispatch import send_outreach_email

    await system_settings.set_sending_enabled(db_session, False)

    mock_account = MagicMock()
    mock_account.email = "sender@apprendtechnologies.com"
    mock_account.id = uuid.uuid4()
    mock_account.status = "connected"
    mock_account.daily_send_count = 0
    mock_account.daily_count_reset_at = None
    mock_account.last_used_at = None

    with patch("app.services.email_dispatch.get_next_rotation_account", return_value=mock_account), \
         patch("app.services.email_dispatch.get_account", return_value=mock_account), \
         patch("app.services.email_dispatch._make_sender") as make_sender:
        sender = MagicMock()
        sender.send_email.return_value = {"status": "sent", "transport": "gmail"}
        make_sender.return_value = sender
        result = await send_outreach_email(
            db_session,
            to_email="prospect@example.com",
            subject="hi",
            body_html="<p>hi</p>",
            bypass_kill_switch=True,
        )
    assert result["status"] == "sent"
