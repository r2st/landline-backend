"""Tests for GET /system/automation.

This endpoint exists because the Settings page used to hardcode its automation
badges. Two of the three were wrong: follow-up generation and lead re-scoring
both run on the beat schedule while the UI rendered them OFF. So the thing worth
testing is that every value here traces back to real config — never a display
default.
"""

import pytest
from httpx import AsyncClient

from app.config import get_settings


@pytest.mark.asyncio
async def test_automation_reports_every_behaviour(client: AsyncClient):
    resp = await client.get("/api/v1/system/automation")
    assert resp.status_code == 200
    body = resp.json()

    keys = {f["key"] for f in body["flags"]}
    assert keys == {
        "auto_reply",
        "ai_compose",
        "auto_research",
        "auto_followups",
        "auto_scoring",
    }
    # Every flag says how it's controlled, so the UI can tell an operator whether
    # they can change it or must ask an admin.
    assert all(f["source"] in {"env", "schedule"} for f in body["flags"])
    assert all(f["label"] for f in body["flags"])


@pytest.mark.asyncio
async def test_env_flags_track_config_not_a_default(client: AsyncClient, monkeypatch):
    """The badge follows the setting. This is the whole point of the endpoint."""
    monkeypatch.setattr(get_settings(), "auto_reply_enabled", False)
    resp = await client.get("/api/v1/system/automation")
    flags = {f["key"]: f for f in resp.json()["flags"]}
    assert flags["auto_reply"]["enabled"] is False
    assert "never answered automatically" in flags["auto_reply"]["detail"]

    monkeypatch.setattr(get_settings(), "auto_reply_enabled", True)
    resp = await client.get("/api/v1/system/automation")
    flags = {f["key"]: f for f in resp.json()["flags"]}
    assert flags["auto_reply"]["enabled"] is True


@pytest.mark.asyncio
async def test_scheduled_behaviours_report_on(client: AsyncClient):
    """Follow-ups and re-scoring are beat tasks with no off switch.

    The old UI displayed both as OFF. They run every 4h and every 30m/twice-daily
    respectively, so reporting them OFF told an operator the opposite of the truth.
    """
    resp = await client.get("/api/v1/system/automation")
    flags = {f["key"]: f for f in resp.json()["flags"]}

    assert flags["auto_followups"]["enabled"] is True
    assert flags["auto_followups"]["source"] == "schedule"
    assert flags["auto_scoring"]["enabled"] is True
    assert flags["auto_scoring"]["source"] == "schedule"


@pytest.mark.asyncio
async def test_scheduled_detail_carries_the_real_cadence(client: AsyncClient, monkeypatch):
    """A bare ON is not useful — the detail must name the actual thresholds."""
    monkeypatch.setattr(get_settings(), "stale_lead_followup_days", 9)
    monkeypatch.setattr(get_settings(), "auto_followup_batch_limit", 33)
    resp = await client.get("/api/v1/system/automation")
    flags = {f["key"]: f for f in resp.json()["flags"]}

    assert "9+ days" in flags["auto_followups"]["detail"]
    assert "33 reminders" in flags["auto_followups"]["detail"]


@pytest.mark.asyncio
async def test_ai_compose_detail_reflects_review_batch_and_scope(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_compose_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_compose_review_batch_size", 7)
    monkeypatch.setattr(get_settings(), "ai_compose_all_steps", False)
    resp = await client.get("/api/v1/system/automation")
    flags = {f["key"]: f for f in resp.json()["flags"]}
    assert "First 7 per sequence" in flags["ai_compose"]["detail"]
    assert "step 1 only" in flags["ai_compose"]["detail"]

    monkeypatch.setattr(get_settings(), "ai_compose_all_steps", True)
    resp = await client.get("/api/v1/system/automation")
    flags = {f["key"]: f for f in resp.json()["flags"]}
    assert "every step is composed" in flags["ai_compose"]["detail"]

    # Disabled: the detail must describe the fallback, not the review batch.
    monkeypatch.setattr(get_settings(), "ai_compose_enabled", False)
    resp = await client.get("/api/v1/system/automation")
    flags = {f["key"]: f for f in resp.json()["flags"]}
    assert flags["ai_compose"]["enabled"] is False
    assert "static templates" in flags["ai_compose"]["detail"]


@pytest.mark.asyncio
async def test_scheduling_link_comes_from_config(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(get_settings(), "scheduling_link", "https://calendly.com/apprend/30min")
    resp = await client.get("/api/v1/system/automation")
    assert resp.json()["scheduling_link"] == "https://calendly.com/apprend/30min"


@pytest.mark.asyncio
async def test_scheduling_link_empty_never_null(client: AsyncClient, monkeypatch):
    """`scheduling_link` defaults to "" in config; the API must not emit null."""
    monkeypatch.setattr(get_settings(), "scheduling_link", "")
    resp = await client.get("/api/v1/system/automation")
    assert resp.json()["scheduling_link"] == ""
