"""Tests for multi-channel step execution (P2 audit).

Covers the dispatcher routing, each handler's send/check_status/handle_reply,
the stub-vs-real distinction, and the API surface.
"""

import pytest

from app.services.channels import dispatcher
from app.services.channels.email_channel import EmailChannelHandler
from app.services.channels.linkedin_channel import LinkedInChannelHandler
from app.services.channels.phone_channel import PhoneChannelHandler


# --- Dispatcher registry ----------------------------------------------------

def test_registry_routes_each_channel():
    assert isinstance(dispatcher.get_handler("email"), EmailChannelHandler)
    assert isinstance(dispatcher.get_handler("linkedin"), LinkedInChannelHandler)
    assert isinstance(dispatcher.get_handler("phone"), PhoneChannelHandler)
    # SMS shares the phone (Twilio) handler.
    assert dispatcher.get_handler("sms") is dispatcher.get_handler("phone")


def test_unknown_channel_raises():
    with pytest.raises(dispatcher.UnknownChannelError):
        dispatcher.get_handler("carrier_pigeon")
    assert dispatcher.handler_for_channel("carrier_pigeon") is None


def test_stub_handlers_report_unconfigured_by_default():
    # No LinkedIn/Twilio creds in the test env → stubs, not real integrations.
    assert dispatcher.get_handler("linkedin").configured is False
    assert dispatcher.get_handler("phone").configured is False
    # Email always has a transport.
    assert dispatcher.get_handler("email").configured is True


# --- LinkedIn stub ----------------------------------------------------------

@pytest.mark.asyncio
async def test_linkedin_connection_request_simulated(db_session):
    execution = await dispatcher.dispatch_channel_step(
        db_session,
        channel="linkedin",
        action="connection_request",
        payload={"linkedin_url": "https://linkedin.com/in/jsmith"},
    )
    assert execution.status == "simulated"
    assert execution.provider == "linkedin_stub"
    assert execution.external_id and execution.external_id.startswith("linkedin-sim-")
    assert execution.sent_at is not None
    assert "[stub]" in (execution.result or {}).get("detail", "")


@pytest.mark.asyncio
async def test_linkedin_message_without_target_fails(db_session):
    execution = await dispatcher.dispatch_channel_step(
        db_session,
        channel="linkedin",
        action="message",
        payload={},  # no profile/recipient
    )
    assert execution.status == "failed"
    assert "no linkedin" in (execution.error or "")


@pytest.mark.asyncio
async def test_linkedin_bad_action_fails(db_session):
    execution = await dispatcher.dispatch_channel_step(
        db_session, channel="linkedin", action="telepathy", payload={}
    )
    assert execution.status == "failed"
    assert "unsupported" in (execution.error or "")


# --- Phone / SMS stub -------------------------------------------------------

@pytest.mark.asyncio
async def test_sms_simulated_with_twilio_shaped_sid(db_session):
    execution = await dispatcher.dispatch_channel_step(
        db_session,
        channel="sms",
        action="sms",
        payload={"to_number": "+15551234567", "message": "hi there"},
    )
    assert execution.status == "simulated"
    assert execution.provider == "twilio_stub"
    assert execution.external_id.startswith("SM")
    assert execution.sent_at is not None


@pytest.mark.asyncio
async def test_call_simulated(db_session):
    execution = await dispatcher.dispatch_channel_step(
        db_session,
        channel="phone",
        action="call",
        payload={"to_number": "+15551234567"},
    )
    assert execution.status == "simulated"
    assert execution.external_id.startswith("CA")


@pytest.mark.asyncio
async def test_sms_without_body_fails(db_session):
    execution = await dispatcher.dispatch_channel_step(
        db_session, channel="sms", action="sms", payload={"to_number": "+1555"}
    )
    assert execution.status == "failed"
    assert "message body" in (execution.error or "")


@pytest.mark.asyncio
async def test_phone_without_number_fails(db_session):
    execution = await dispatcher.dispatch_channel_step(
        db_session, channel="phone", action="call", payload={}
    )
    assert execution.status == "failed"
    assert "phone number" in (execution.error or "")


# --- Email (real path, but no connected account in tests → SES fallback) -----

@pytest.mark.asyncio
async def test_email_without_recipient_fails(db_session):
    execution = await dispatcher.dispatch_channel_step(
        db_session, channel="email", action="message", payload={"subject": "hi"}
    )
    assert execution.status == "failed"
    assert "recipient" in (execution.error or "")


@pytest.mark.asyncio
async def test_email_send_maps_dispatch_result(db_session, monkeypatch):
    async def fake_send(db, **kwargs):
        return {"status": "sent", "transport": "gmail", "gmail_message_id": "abc123"}

    monkeypatch.setattr(
        "app.services.email_dispatch.send_outreach_email", fake_send
    )
    execution = await dispatcher.dispatch_channel_step(
        db_session,
        channel="email",
        action="message",
        payload={"to_email": "x@example.com", "subject": "s", "body_html": "<p>hi</p>"},
    )
    assert execution.status == "sent"
    assert execution.provider == "gmail"
    assert execution.external_id == "abc123"


@pytest.mark.asyncio
async def test_email_soft_decline_maps_to_skipped(db_session, monkeypatch):
    async def fake_send(db, **kwargs):
        return {"status": "halted", "reason": "kill switch", "transport": "none"}

    monkeypatch.setattr(
        "app.services.email_dispatch.send_outreach_email", fake_send
    )
    execution = await dispatcher.dispatch_channel_step(
        db_session,
        channel="email",
        action="message",
        payload={"to_email": "x@example.com", "subject": "s", "body_html": "hi"},
    )
    assert execution.status == "skipped"


# --- check_status / handle_reply --------------------------------------------

@pytest.mark.asyncio
async def test_check_status_and_reply_lifecycle(db_session):
    execution = await dispatcher.dispatch_channel_step(
        db_session,
        channel="linkedin",
        action="connection_request",
        payload={"linkedin_url": "https://linkedin.com/in/x"},
    )
    handler = dispatcher.get_handler("linkedin")

    await handler.check_status(db_session, execution)
    assert execution.status_checked_at is not None
    # Status unchanged by a stub poll.
    assert execution.status == "simulated"

    await handler.handle_reply(db_session, execution, {"text": "sure, let's talk"})
    assert execution.status == "replied"
    assert execution.reply == {"text": "sure, let's talk"}


# --- API --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_endpoint_and_list(client):
    resp = await client.post(
        "/api/v1/channels/execute",
        json={
            "channel": "linkedin",
            "action": "connection_request",
            "payload": {"linkedin_url": "https://linkedin.com/in/api-test"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "simulated"
    assert body["channel"] == "linkedin"
    execution_id = body["id"]

    # It shows up in the list, filterable by channel.
    listed = await client.get("/api/v1/channels/executions?channel=linkedin")
    assert listed.status_code == 200
    ids = [e["id"] for e in listed.json()]
    assert execution_id in ids

    # Reply via the API marks it replied.
    replied = await client.post(
        f"/api/v1/channels/executions/{execution_id}/reply",
        json={"reply": {"text": "yes"}},
    )
    assert replied.status_code == 200
    assert replied.json()["status"] == "replied"


@pytest.mark.asyncio
async def test_execute_endpoint_rejects_unknown_channel(client):
    resp = await client.post(
        "/api/v1/channels/execute",
        json={"channel": "fax", "action": "message", "payload": {}},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_handlers_endpoint(client):
    resp = await client.get("/api/v1/channels/handlers")
    assert resp.status_code == 200
    channels = resp.json()["channels"]
    assert channels["email"]["configured"] is True
    assert channels["linkedin"]["configured"] is False
    assert channels["phone"]["provider"] == "twilio_stub"
