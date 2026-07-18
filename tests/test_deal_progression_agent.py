"""Tests for the Deal Progression Agent — service, tasks, and API."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.company import Company
from app.models.deal import Deal
from app.models.deal_activity import DealActivity
from app.models.deal_progression_agent import DealAction, DealProgressionAgent
from app.models.user import UserRole
from app.services import deal_progression_agent as agent
from app.tasks.deal_agent import execute_pending_actions_for_session


def _ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


async def _make_deal(
    db, company_id, *, stage="discovery", status="pending", stale_days=10, value=50000
) -> Deal:
    """Create a deal whose last activity is ``stale_days`` ago."""
    ts = _ago(stale_days)
    deal = Deal(
        id=uuid.uuid4(),
        company_id=company_id,
        title="Annual contract",
        value=value,
        deal_stage=stage,
        status=status,
        created_at=ts,
        updated_at=ts,
    )
    db.add(deal)
    await db.flush()
    return deal


class _StubAI:
    """Deterministic AI stub — no network."""

    def __init__(self, model="stub", key=None):
        self.model = model
        self.key = key

    async def generate_followup(self, **kw):
        return {"subject": "quick nudge", "body": "Just floating this back up — still worth a chat?"}

    async def complete_text(self, prompt, max_tokens=120, temperature=None):
        return "Get the economic buyer on a call to unblock sign-off."


@pytest.fixture(autouse=True)
def _stub_ai(monkeypatch):
    """Route every AI touch through the stub so tests are offline + fast.

    The agent builds its AIService via ``_ai_service(model, key)`` (pinned to
    OpenRouter with the org's config), so patch that factory — capturing the
    (model, key) each call resolves to for assertions.
    """
    calls = []

    def _fake(model, key):
        calls.append((model, key))
        return _StubAI(model, key)

    monkeypatch.setattr("app.services.deal_progression_agent._ai_service", _fake)
    monkeypatch.setattr(
        "app.services.ai_service.get_ai_service", lambda: _StubAI()
    )
    return calls


# --- Config -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_or_create_config_defaults_and_idempotent(db_session):
    config = await agent.get_or_create_config(db_session)
    assert config.is_active is True
    assert config.auto_followup_enabled is True
    assert config.auto_advance_enabled is False
    assert config.max_auto_followups_per_deal == 3
    assert config.escalation_threshold_days == 14
    assert config.stale_thresholds["discovery"] == 3

    again = await agent.get_or_create_config(db_session)
    assert again.id == config.id
    count = (
        await db_session.execute(select(DealProgressionAgent))
    ).scalars().all()
    assert len(count) == 1


@pytest.mark.asyncio
async def test_threshold_falls_back_to_default(db_session):
    config = await agent.get_or_create_config(db_session)
    config.stale_thresholds = {"discovery": 1}
    assert agent._threshold_for_stage(config, "discovery") == 1
    # unset stage → module default
    assert agent._threshold_for_stage(config, "negotiation") == 7


# --- last_activity_at ---------------------------------------------------------

@pytest.mark.asyncio
async def test_last_activity_uses_most_recent_signal(db_session, sample_company):
    deal = await _make_deal(db_session, sample_company.id, stale_days=30)
    # A more recent DealActivity should win over the old updated_at.
    db_session.add(
        DealActivity(
            deal_id=deal.id, activity_type="note_added", created_at=_ago(2)
        )
    )
    await db_session.flush()
    last = await agent.last_activity_at(db_session, deal)
    assert agent._days_since(last) <= 2


# --- scan_stale_deals ---------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_ignores_fresh_deal(db_session, sample_company):
    await _make_deal(db_session, sample_company.id, stage="discovery", stale_days=1)
    summary = await agent.scan_stale_deals(db_session)
    assert summary["scanned"] == 1
    assert summary["stale"] == 0
    actions = (await db_session.execute(select(DealAction))).scalars().all()
    assert actions == []


@pytest.mark.asyncio
async def test_scan_drafts_followup_for_stale_deal(db_session, sample_company, sample_contact):
    deal = await _make_deal(db_session, sample_company.id, stage="discovery", stale_days=5)
    summary = await agent.scan_stale_deals(db_session)
    assert summary["stale"] == 1
    assert summary["followups"] == 1
    action = (await db_session.execute(select(DealAction))).scalars().one()
    assert action.action_type == "auto_followup"
    assert action.status == "pending"
    assert action.email_subject and action.email_body
    assert action.deal_id == deal.id


@pytest.mark.asyncio
async def test_scan_skips_deal_with_open_action(db_session, sample_company, sample_contact):
    await _make_deal(db_session, sample_company.id, stage="discovery", stale_days=5)
    first = await agent.scan_stale_deals(db_session)
    assert first["followups"] == 1
    # Second scan should not stack another action.
    second = await agent.scan_stale_deals(db_session)
    assert second["skipped_existing"] == 1
    assert second["followups"] == 0
    actions = (await db_session.execute(select(DealAction))).scalars().all()
    assert len(actions) == 1


@pytest.mark.asyncio
async def test_scan_flags_at_risk_when_well_past_due(db_session, sample_company):
    # discovery threshold = 3; 2x = 6; 8 days stale (< escalation 14) → at_risk.
    await _make_deal(db_session, sample_company.id, stage="discovery", stale_days=8)
    summary = await agent.scan_stale_deals(db_session)
    assert summary["at_risk"] == 1
    action = (await db_session.execute(select(DealAction))).scalars().one()
    assert action.action_type == "at_risk_flag"


@pytest.mark.asyncio
async def test_scan_escalates_beyond_threshold(db_session, sample_company):
    await _make_deal(db_session, sample_company.id, stage="negotiation", stale_days=20)
    summary = await agent.scan_stale_deals(db_session)
    assert summary["escalations"] == 1
    action = (await db_session.execute(select(DealAction))).scalars().one()
    assert action.action_type == "escalation"


@pytest.mark.asyncio
async def test_scan_suggests_when_followups_disabled(db_session, sample_company):
    config = await agent.get_or_create_config(db_session)
    config.auto_followup_enabled = False
    await db_session.flush()
    await _make_deal(db_session, sample_company.id, stage="discovery", stale_days=5)
    summary = await agent.scan_stale_deals(db_session)
    assert summary["suggestions"] == 1
    action = (await db_session.execute(select(DealAction))).scalars().one()
    assert action.action_type == "suggestion"
    assert action.suggested_action


@pytest.mark.asyncio
async def test_scan_noop_when_agent_inactive(db_session, sample_company):
    config = await agent.get_or_create_config(db_session)
    config.is_active = False
    await db_session.flush()
    await _make_deal(db_session, sample_company.id, stale_days=30)
    summary = await agent.scan_stale_deals(db_session)
    assert summary.get("inactive") is True
    assert (await db_session.execute(select(DealAction))).scalars().all() == []


@pytest.mark.asyncio
async def test_scan_respects_max_followups(db_session, sample_company, sample_contact):
    config = await agent.get_or_create_config(db_session)
    config.max_auto_followups_per_deal = 1
    await db_session.flush()
    deal = await _make_deal(db_session, sample_company.id, stage="discovery", stale_days=5)
    # Pretend one follow-up already happened and was dismissed (counts toward cap).
    db_session.add(
        DealAction(deal_id=deal.id, action_type="auto_followup", status="dismissed")
    )
    await db_session.flush()
    summary = await agent.scan_stale_deals(db_session)
    # Cap reached → falls through to a suggestion instead of another follow-up.
    assert summary["followups"] == 0
    assert summary["suggestions"] == 1


# --- auto_advance_stage -------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_advance_on_reply(db_session, sample_company):
    deal = await _make_deal(db_session, sample_company.id, stage="discovery")
    action = await agent.auto_advance_stage(db_session, deal, "reply")
    assert action is not None
    assert deal.deal_stage == "proposal"
    assert action.action_type == "stage_change"
    assert action.status == "executed"


@pytest.mark.asyncio
async def test_auto_advance_on_meeting_booked(db_session, sample_company):
    deal = await _make_deal(db_session, sample_company.id, stage="proposal")
    action = await agent.auto_advance_stage(db_session, deal, "meeting_booked")
    assert action is not None
    assert deal.deal_stage == "negotiation"


@pytest.mark.asyncio
async def test_auto_advance_noop_when_no_transition(db_session, sample_company):
    # 'meeting_booked' has no transition from 'discovery'.
    deal = await _make_deal(db_session, sample_company.id, stage="discovery")
    action = await agent.auto_advance_stage(db_session, deal, "meeting_booked")
    assert action is None
    assert deal.deal_stage == "discovery"


@pytest.mark.asyncio
async def test_auto_advance_never_backwards(db_session, sample_company):
    deal = await _make_deal(db_session, sample_company.id, stage="closing")
    assert await agent.auto_advance_stage(db_session, deal, "reply") is None
    assert deal.deal_stage == "closing"


# --- action lifecycle ---------------------------------------------------------

@pytest.mark.asyncio
async def test_dismiss_action(db_session, sample_company):
    deal = await _make_deal(db_session, sample_company.id)
    action = DealAction(deal_id=deal.id, action_type="suggestion", status="pending")
    db_session.add(action)
    await db_session.flush()
    await agent.dismiss_action(db_session, action)
    assert action.status == "dismissed"
    assert action.dismissed_at is not None


@pytest.mark.asyncio
async def test_approve_suggestion_marks_executed(db_session, sample_company):
    deal = await _make_deal(db_session, sample_company.id)
    action = DealAction(deal_id=deal.id, action_type="suggestion", status="pending")
    db_session.add(action)
    await db_session.flush()
    result = await agent.approve_action(db_session, action)
    assert result["status"] == "executed"
    assert action.status == "executed"
    assert action.executed_at is not None


@pytest.mark.asyncio
async def test_approve_followup_without_contact_skips_send(db_session, sample_company):
    # No contact on the company → nothing to send to, but still resolves.
    deal = await _make_deal(db_session, sample_company.id)
    action = DealAction(
        deal_id=deal.id, action_type="auto_followup", status="pending",
        email_subject="hi", email_body="body",
    )
    db_session.add(action)
    await db_session.flush()
    result = await agent.approve_action(db_session, action)
    assert result["status"] == "skipped"
    assert action.status == "executed"


# --- execute_pending_actions task ---------------------------------------------

@pytest.mark.asyncio
async def test_execute_expires_actions_on_closed_deal(db_session, sample_company):
    deal = await _make_deal(db_session, sample_company.id, status="won")
    action = DealAction(deal_id=deal.id, action_type="suggestion", status="pending")
    db_session.add(action)
    await db_session.flush()
    result = await execute_pending_actions_for_session(db_session)
    assert result["expired"] == 1
    assert action.status == "expired"


@pytest.mark.asyncio
async def test_execute_keeps_actions_on_open_deal(db_session, sample_company):
    deal = await _make_deal(db_session, sample_company.id, status="pending")
    action = DealAction(deal_id=deal.id, action_type="suggestion", status="pending")
    db_session.add(action)
    await db_session.flush()
    result = await execute_pending_actions_for_session(db_session)
    assert result["expired"] == 0
    assert action.status == "pending"


# --- stats --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compute_stats(db_session, sample_company):
    deal = await _make_deal(db_session, sample_company.id)
    db_session.add_all([
        DealAction(deal_id=deal.id, action_type="auto_followup", status="executed"),
        DealAction(deal_id=deal.id, action_type="stage_change", status="executed"),
        DealAction(deal_id=deal.id, action_type="suggestion", status="pending"),
        DealAction(deal_id=deal.id, action_type="escalation", status="pending"),
    ])
    await db_session.flush()
    stats = await agent.compute_stats(db_session)
    assert stats["followups_sent"] == 1
    assert stats["stage_advances"] == 1
    assert stats["suggestions"] == 1
    assert stats["escalations"] == 1
    assert stats["at_risk_count"] == 1  # the open escalation
    assert stats["pending_actions"] == 2
    assert stats["deals_nudged"] == 4


# --- API ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_get_config_creates_default(client: AsyncClient):
    resp = await client.get("/api/v1/deal-agent/config")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["is_active"] is True
    assert data["escalation_threshold_days"] == 14


@pytest.mark.asyncio
async def test_api_update_config(client: AsyncClient):
    resp = await client.put(
        "/api/v1/deal-agent/config",
        json={
            "auto_advance_enabled": True,
            "escalation_threshold_days": 21,
            "stale_thresholds": {"discovery": 2},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["auto_advance_enabled"] is True
    assert data["escalation_threshold_days"] == 21
    assert data["stale_thresholds"]["discovery"] == 2
    # Merge preserved other stage defaults.
    assert "negotiation" in data["stale_thresholds"]


@pytest.mark.asyncio
async def test_api_update_config_rejects_bad_stage(client: AsyncClient):
    resp = await client.put(
        "/api/v1/deal-agent/config",
        json={"stale_thresholds": {"bogus_stage": 3}},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_update_config_forbidden_for_agent(
    unauth_client: AsyncClient, make_user, auth_headers
):
    agent_user = await make_user(
        "agent-deal@landline.app", role=UserRole.ORG_AGENT.value
    )
    resp = await unauth_client.put(
        "/api/v1/deal-agent/config",
        json={"escalation_threshold_days": 30},
        headers=auth_headers(agent_user),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_api_list_and_filter_actions(
    client: AsyncClient, db_session, sample_company: Company
):
    deal = await _make_deal(db_session, sample_company.id)
    db_session.add_all([
        DealAction(deal_id=deal.id, action_type="suggestion", status="pending",
                   suggested_action="do X"),
        DealAction(deal_id=deal.id, action_type="auto_followup", status="executed"),
    ])
    await db_session.commit()

    resp = await client.get("/api/v1/deal-agent/actions")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    # deal metadata joined in for the queue view
    assert all(item["deal_title"] == "Annual contract" for item in body)
    assert any(item["company_name"] == sample_company.name for item in body)

    # filter by status
    pending = await client.get("/api/v1/deal-agent/actions?status=pending")
    assert len(pending.json()) == 1
    assert pending.json()[0]["action_type"] == "suggestion"

    # filter by type
    followups = await client.get("/api/v1/deal-agent/actions?action_type=auto_followup")
    assert len(followups.json()) == 1

    # invalid filters are rejected
    assert (await client.get("/api/v1/deal-agent/actions?status=nope")).status_code == 422
    assert (await client.get("/api/v1/deal-agent/actions?action_type=nope")).status_code == 422


@pytest.mark.asyncio
async def test_api_approve_and_dismiss_flow(
    client: AsyncClient, db_session, sample_company: Company
):
    deal = await _make_deal(db_session, sample_company.id)
    action = DealAction(
        deal_id=deal.id, action_type="suggestion", status="pending",
        suggested_action="do the thing",
    )
    db_session.add(action)
    await db_session.commit()
    action_id = str(action.id)

    # Approve it → executed.
    resp = await client.post(f"/api/v1/deal-agent/actions/{action_id}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "executed"

    # Dismissing an executed action is a conflict.
    dismiss = await client.post(f"/api/v1/deal-agent/actions/{action_id}/dismiss")
    assert dismiss.status_code == 409

    # Unknown action id → 404.
    missing = await client.post(f"/api/v1/deal-agent/actions/{uuid.uuid4()}/dismiss")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_api_dismiss_pending_action(
    client: AsyncClient, db_session, sample_company: Company
):
    deal = await _make_deal(db_session, sample_company.id)
    action = DealAction(deal_id=deal.id, action_type="suggestion", status="pending")
    db_session.add(action)
    await db_session.commit()
    resp = await client.post(f"/api/v1/deal-agent/actions/{action.id}/dismiss")
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"


@pytest.mark.asyncio
async def test_api_stats_shape(client: AsyncClient):
    resp = await client.get("/api/v1/deal-agent/stats")
    assert resp.status_code == 200
    data = resp.json()
    for key in (
        "deals_nudged", "followups_sent", "stage_advances", "at_risk_count",
        "pending_actions", "suggestions", "escalations",
    ):
        assert key in data


# --- LLM config: model + key resolution ---------------------------------------

from cryptography.fernet import Fernet  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services import crypto  # noqa: E402
from app.services import system_settings  # noqa: E402


@pytest.fixture
def _crypto_key():
    """Give crypto a valid Fernet key for the duration of the test."""
    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


@pytest.mark.asyncio
async def test_config_defaults_include_llm_model(db_session):
    config = await agent.get_or_create_config(db_session)
    assert config.llm_model == "meta-llama/llama-3.3-70b-instruct:free"
    assert config.llm_api_key is None


@pytest.mark.asyncio
async def test_resolve_llm_prefers_agent_key(db_session, _crypto_key):
    config = await agent.get_or_create_config(db_session)
    config.llm_model = "openai/gpt-oss-20b:free"
    config.llm_api_key = crypto.encrypt("sk-agent-key")
    await db_session.flush()

    model, key = await agent.resolve_llm(db_session, config)
    assert model == "openai/gpt-oss-20b:free"
    assert key == "sk-agent-key"


@pytest.mark.asyncio
async def test_resolve_llm_falls_back_to_org_setting(db_session):
    config = await agent.get_or_create_config(db_session)
    # No agent key → org-level system_settings OPENROUTER_API_KEY wins.
    await system_settings.set_value(db_session, "OPENROUTER_API_KEY", "sk-org-key")
    await db_session.flush()

    model, key = await agent.resolve_llm(db_session, config)
    assert key == "sk-org-key"
    # Default model when the agent hasn't overridden it.
    assert model == "meta-llama/llama-3.3-70b-instruct:free"


@pytest.mark.asyncio
async def test_resolve_llm_falls_back_to_env(db_session, monkeypatch):
    config = await agent.get_or_create_config(db_session)
    settings = get_settings()
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-env-key", raising=False)
    model, key = await agent.resolve_llm(db_session, config)
    assert key == "sk-env-key"


@pytest.mark.asyncio
async def test_scan_uses_configured_model(db_session, sample_company, sample_contact, _stub_ai):
    config = await agent.get_or_create_config(db_session)
    config.llm_model = "google/gemma-3-27b-it:free"
    await db_session.flush()
    await _make_deal(db_session, sample_company.id, stage="discovery", stale_days=5)

    await agent.scan_stale_deals(db_session)
    # The follow-up AI touch was built with the configured model.
    assert _stub_ai  # captured (model, key) tuples
    assert any(model == "google/gemma-3-27b-it:free" for model, _ in _stub_ai)


# --- LLM config: API surface --------------------------------------------------

@pytest.mark.asyncio
async def test_api_config_exposes_model_and_key_flag(client: AsyncClient):
    resp = await client.get("/api/v1/deal-agent/config")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["llm_model"] == "meta-llama/llama-3.3-70b-instruct:free"
    assert data["has_llm_api_key"] is False
    assert "meta-llama/llama-3.3-70b-instruct:free" in data["llm_model_choices"]
    # The raw key is never serialized.
    assert "llm_api_key" not in data


@pytest.mark.asyncio
async def test_api_update_model(client: AsyncClient):
    resp = await client.put(
        "/api/v1/deal-agent/config", json={"llm_model": "openai/gpt-oss-120b:free"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["llm_model"] == "openai/gpt-oss-120b:free"


@pytest.mark.asyncio
async def test_api_update_key_encrypts_and_masks(client: AsyncClient, db_session, _crypto_key):
    resp = await client.put(
        "/api/v1/deal-agent/config", json={"llm_api_key": "sk-secret-123"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_llm_api_key"] is True
    assert "llm_api_key" not in body

    # Stored ciphertext decrypts back to the original, and is not the plaintext.
    config = (await db_session.execute(select(DealProgressionAgent))).scalars().first()
    assert config.llm_api_key is not None
    assert config.llm_api_key != "sk-secret-123"
    assert crypto.decrypt(config.llm_api_key) == "sk-secret-123"


@pytest.mark.asyncio
async def test_api_clear_key_with_empty_string(client: AsyncClient, db_session, _crypto_key):
    await client.put("/api/v1/deal-agent/config", json={"llm_api_key": "sk-secret-123"})
    resp = await client.put("/api/v1/deal-agent/config", json={"llm_api_key": ""})
    assert resp.status_code == 200
    assert resp.json()["has_llm_api_key"] is False
    config = (await db_session.execute(select(DealProgressionAgent))).scalars().first()
    assert config.llm_api_key is None


@pytest.mark.asyncio
async def test_api_update_key_omitted_leaves_it(client: AsyncClient, db_session, _crypto_key):
    await client.put("/api/v1/deal-agent/config", json={"llm_api_key": "sk-keep-me"})
    # A later update that doesn't mention the key must not wipe it.
    resp = await client.put("/api/v1/deal-agent/config", json={"escalation_threshold_days": 20})
    assert resp.status_code == 200
    assert resp.json()["has_llm_api_key"] is True
    config = (await db_session.execute(select(DealProgressionAgent))).scalars().first()
    assert crypto.decrypt(config.llm_api_key) == "sk-keep-me"
