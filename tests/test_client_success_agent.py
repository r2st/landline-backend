"""Tests for the Client Success Agent.

Covers the config model/defaults, the API-key masking + org_admin gating on the
config endpoint, the strict per-tenant OpenRouter key resolution
(agent → org system_settings → global), the health-scan decision logic
(check-in / escalation+review-call / celebration, plus debounce and the master
off switch), the action queue (list/filter/approve/dismiss), stats, the health
overview, and the execute-pending-checkins pass.

All AI and health computation is stubbed so these run fully offline and
deterministically.
"""

import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from app.config import get_settings
from app.models.client_health import ClientHealthSnapshot
from app.models.client_success_agent import ClientSuccessAction, ClientSuccessAgent
from app.models.company import Company
from app.models.contact import Contact
from app.models.system_setting import SystemSetting
from app.models.user import UserRole
from app.org_context import APPREND_ORG_ID, set_current_org_id
from app.security import create_access_token
from app.services import client_success_agent as csa_module
from app.services import crypto
from app.services.client_success_agent import get_client_success_agent_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _crypto_and_keys():
    """Give the process a real Fernet key + a global OpenRouter key for tests.

    The app boots without TOKEN_ENCRYPTION_KEY under pytest, but the config
    endpoint encrypts the agent's API key, so install a valid key and clear the
    fernet cache. Also set a dummy global OpenRouter key so the AIService the
    service builds resolves to the (stubbed) OpenRouter backend.
    """
    settings = get_settings()
    prev_enc = settings.token_encryption_key
    prev_or = settings.openrouter_api_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    settings.openrouter_api_key = "sk-or-global-test"
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev_enc
    settings.openrouter_api_key = prev_or
    crypto._fernet.cache_clear()


def _fake_health(score, *, status=None, days_since=10, response_rate=0.2, meetings=0):
    """A stand-in ClientHealth with just the attributes the service reads."""
    if status is None:
        status = "healthy" if score >= 70 else "at_risk" if score >= 40 else "churned"
    signals = types.SimpleNamespace(
        last_contact_at=datetime.now(timezone.utc) - timedelta(days=days_since),
        days_since_last_contact=days_since,
        response_rate=response_rate,
        meetings_last_90d=meetings,
    )
    return types.SimpleNamespace(score=score, health_status=status, signals=signals)


class _FakeAI:
    """Stub AIService whose complete_text returns canned copy."""

    def __init__(self, text="AI-generated message."):
        self.text = text
        self.calls = []

    async def complete_text(self, prompt, max_tokens=1024, temperature=None):
        self.calls.append(prompt)
        return self.text


@pytest.fixture
def stub_ai(monkeypatch):
    """Force the service's per-call AIService to a deterministic stub."""
    fake = _FakeAI()

    async def _ai_for(self, db, agent):
        return fake

    monkeypatch.setattr(csa_module.ClientSuccessAgentService, "_ai_for", _ai_for)
    return fake


def _patch_health(monkeypatch, score, **kw):
    async def _compute(db, company):
        return _fake_health(score, **kw)

    monkeypatch.setattr(csa_module, "compute_company_health", _compute)


@pytest_asyncio.fixture
async def client_company(db_session):
    """A company in the 'client' pipeline stage, with a primary contact."""
    company = Company(
        id=uuid.uuid4(), name="Nimbus Plastics", country="US", source="test",
        pipeline_stage="client",
    )
    db_session.add(company)
    await db_session.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Dana", last_name="Lee",
        email="dana@nimbus.example", is_primary=True, source="test",
    )
    db_session.add(contact)
    await db_session.flush()
    return company, contact


# ---------------------------------------------------------------------------
# Model / config defaults
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_or_create_config_defaults(db_session):
    set_current_org_id(APPREND_ORG_ID)
    service = get_client_success_agent_service()
    config = await service.get_or_create_config(db_session)

    assert config.is_active is False  # OFF by default — deliberate opt-in
    assert config.llm_model == "meta-llama/llama-3.3-70b-instruct:free"
    assert config.llm_api_key is None
    assert config.health_warning_threshold == 60
    assert config.health_critical_threshold == 30
    assert config.check_frequency_days == 7
    assert config.auto_checkin_enabled is True

    # Idempotent — a second call returns the same row, not a duplicate.
    again = await service.get_or_create_config(db_session)
    assert again.id == config.id


# ---------------------------------------------------------------------------
# API key resolution precedence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resolve_api_key_precedence(db_session):
    set_current_org_id(APPREND_ORG_ID)
    service = get_client_success_agent_service()
    agent = await service.get_or_create_config(db_session)

    # 3. No per-agent, no org setting → global fallback.
    assert await service.resolve_api_key(db_session, agent) == "sk-or-global-test"

    # 2. Org-level system_settings key wins over global.
    db_session.add(SystemSetting(key="openrouter_api_key", value={"api_key": "sk-or-org"}))
    await db_session.flush()
    assert await service.resolve_api_key(db_session, agent) == "sk-or-org"

    # 1. Per-agent encrypted key wins over everything.
    agent.llm_api_key = crypto.encrypt("sk-or-agent")
    await db_session.flush()
    assert await service.resolve_api_key(db_session, agent) == "sk-or-agent"


# ---------------------------------------------------------------------------
# Scan decision logic
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scan_inactive_agent_does_nothing(db_session, client_company, stub_ai, monkeypatch):
    set_current_org_id(APPREND_ORG_ID)
    _patch_health(monkeypatch, 10)
    service = get_client_success_agent_service()
    # Agent defaults to inactive.
    await service.get_or_create_config(db_session)
    result = await service.scan_client_health(db_session)
    assert result["active"] is False
    assert result["actions_created"] == 0


@pytest.mark.asyncio
async def test_scan_creates_checkin_on_warning(db_session, client_company, stub_ai, monkeypatch):
    set_current_org_id(APPREND_ORG_ID)
    _patch_health(monkeypatch, 45)  # between critical(30) and warning(60)
    service = get_client_success_agent_service()
    agent = await service.get_or_create_config(db_session)
    agent.is_active = True
    await db_session.flush()

    result = await service.scan_client_health(db_session)
    assert result["actions_created"] == 1
    action = (await db_session.execute(csa_module.select(ClientSuccessAction))).scalar_one()
    assert action.action_type == "checkin"
    assert action.status == "pending"
    assert action.health_score_at_trigger == 45
    assert action.ai_message == "AI-generated message."
    assert stub_ai.calls  # the AI was actually consulted


@pytest.mark.asyncio
async def test_scan_creates_escalation_and_review_call_on_critical(
    db_session, client_company, stub_ai, monkeypatch
):
    set_current_org_id(APPREND_ORG_ID)
    _patch_health(monkeypatch, 20)  # <= critical(30)
    service = get_client_success_agent_service()
    agent = await service.get_or_create_config(db_session)
    agent.is_active = True
    await db_session.flush()

    result = await service.scan_client_health(db_session)
    assert result["actions_created"] == 2
    types_created = {
        a.action_type
        for a in (await db_session.execute(csa_module.select(ClientSuccessAction))).scalars()
    }
    assert types_created == {"escalation", "review_call"}


@pytest.mark.asyncio
async def test_scan_celebrates_recovery_milestone(
    db_session, client_company, stub_ai, monkeypatch
):
    company, _ = client_company
    set_current_org_id(APPREND_ORG_ID)
    # Prior snapshot below the celebration line, now above it.
    db_session.add(ClientHealthSnapshot(
        company_id=company.id, score=55, health_status="at_risk",
        recorded_at=datetime.now(timezone.utc) - timedelta(days=3),
    ))
    await db_session.flush()
    _patch_health(monkeypatch, 88, status="healthy")
    service = get_client_success_agent_service()
    agent = await service.get_or_create_config(db_session)
    agent.is_active = True
    await db_session.flush()

    await service.scan_client_health(db_session)
    action = (await db_session.execute(csa_module.select(ClientSuccessAction))).scalar_one()
    assert action.action_type == "celebration"
    assert action.health_score_at_trigger == 88


@pytest.mark.asyncio
async def test_scan_debounces_repeat_checkins(db_session, client_company, stub_ai, monkeypatch):
    set_current_org_id(APPREND_ORG_ID)
    _patch_health(monkeypatch, 45)
    service = get_client_success_agent_service()
    agent = await service.get_or_create_config(db_session)
    agent.is_active = True
    await db_session.flush()

    first = await service.scan_client_health(db_session)
    await db_session.commit()
    assert first["actions_created"] == 1
    # Immediate re-scan: still inside check_frequency_days → no new action.
    second = await service.scan_client_health(db_session)
    assert second["actions_created"] == 0


@pytest.mark.asyncio
async def test_scan_respects_auto_checkin_toggle(db_session, client_company, stub_ai, monkeypatch):
    set_current_org_id(APPREND_ORG_ID)
    _patch_health(monkeypatch, 45)
    service = get_client_success_agent_service()
    agent = await service.get_or_create_config(db_session)
    agent.is_active = True
    agent.auto_checkin_enabled = False
    await db_session.flush()

    result = await service.scan_client_health(db_session)
    assert result["actions_created"] == 0


# ---------------------------------------------------------------------------
# execute_pending_checkins
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_execute_pending_checkins(db_session, client_company, monkeypatch):
    company, contact = client_company
    set_current_org_id(APPREND_ORG_ID)
    service = get_client_success_agent_service()
    agent = await service.get_or_create_config(db_session)
    agent.is_active = True
    await db_session.flush()

    db_session.add(ClientSuccessAction(
        action_type="checkin", company_id=company.id, contact_id=contact.id,
        status="pending", ai_message="hi",
    ))
    db_session.add(ClientSuccessAction(
        action_type="escalation", company_id=company.id, status="pending",
    ))
    await db_session.flush()

    result = await service.execute_pending_checkins(db_session)
    assert result["sent"] == 1
    rows = (await db_session.execute(csa_module.select(ClientSuccessAction))).scalars().all()
    by_type = {r.action_type: r for r in rows}
    assert by_type["checkin"].status == "sent"
    assert by_type["checkin"].executed_at is not None
    assert by_type["escalation"].status == "pending"  # untouched


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_config_get_creates_default_and_masks_key(client):
    resp = await client.get("/api/v1/client-success-agent/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_active"] is False
    assert body["llm_api_key_set"] is False
    assert "llm_api_key" not in body  # the raw key is NEVER serialized
    assert "meta-llama/llama-3.3-70b-instruct:free" in body["model_options"]


@pytest.mark.asyncio
async def test_config_put_updates_and_encrypts_key(client, db_session):
    resp = await client.put(
        "/api/v1/client-success-agent/config",
        json={
            "is_active": True,
            "llm_model": "openai/gpt-oss-20b:free",
            "llm_api_key": "sk-or-secret",
            "health_warning_threshold": 65,
            "health_critical_threshold": 25,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_active"] is True
    assert body["llm_model"] == "openai/gpt-oss-20b:free"
    assert body["llm_api_key_set"] is True
    assert body["llm_api_key_hint"]  # a masked hint, not the value

    # Persisted key is encrypted, and decrypts back to the plaintext.
    stored = (
        await db_session.execute(csa_module.select(ClientSuccessAgent))
    ).scalar_one()
    assert stored.llm_api_key != "sk-or-secret"
    assert crypto.decrypt(stored.llm_api_key) == "sk-or-secret"


@pytest.mark.asyncio
async def test_config_put_clears_key_with_empty_string(client, db_session):
    await client.put(
        "/api/v1/client-success-agent/config",
        json={"llm_api_key": "sk-or-secret"},
    )
    resp = await client.put(
        "/api/v1/client-success-agent/config",
        json={"llm_api_key": ""},
    )
    assert resp.status_code == 200
    assert resp.json()["llm_api_key_set"] is False


@pytest.mark.asyncio
async def test_config_put_rejects_bad_thresholds(client):
    resp = await client.put(
        "/api/v1/client-success-agent/config",
        json={"health_warning_threshold": 30, "health_critical_threshold": 40},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_config_put_requires_org_admin(unauth_client, make_user, auth_headers):
    agent_user = await make_user("csa-agent@landline.app", role=UserRole.ORG_AGENT.value)
    resp = await unauth_client.put(
        "/api/v1/client-success-agent/config",
        json={"is_active": True},
        headers=auth_headers(agent_user),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_actions_list_filter_approve_dismiss(client, db_session, client_company):
    company, contact = client_company
    a1 = ClientSuccessAction(
        action_type="checkin", company_id=company.id, contact_id=contact.id,
        status="pending", ai_message="hello", org_id=APPREND_ORG_ID,
    )
    a2 = ClientSuccessAction(
        action_type="escalation", company_id=company.id, status="pending",
        org_id=APPREND_ORG_ID,
    )
    db_session.add_all([a1, a2])
    await db_session.commit()

    # List all.
    resp = await client.get("/api/v1/client-success-agent/actions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    # Enrichment attached the client's display fields.
    checkin = next(a for a in body["actions"] if a["action_type"] == "checkin")
    assert checkin["company_name"] == "Nimbus Plastics"
    assert checkin["contact_name"] == "Dana Lee"

    # Filter by type.
    resp = await client.get("/api/v1/client-success-agent/actions?action_type=escalation")
    assert resp.json()["total"] == 1

    # Approve the check-in → sent.
    resp = await client.post(f"/api/v1/client-success-agent/actions/{a1.id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"

    # Dismiss the escalation.
    resp = await client.post(f"/api/v1/client-success-agent/actions/{a2.id}/dismiss")
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"


@pytest.mark.asyncio
async def test_stats_and_health_overview(client, db_session, client_company):
    company, contact = client_company
    db_session.add_all([
        ClientSuccessAction(action_type="checkin", company_id=company.id, status="sent", org_id=APPREND_ORG_ID),
        ClientSuccessAction(action_type="escalation", company_id=company.id, status="pending", org_id=APPREND_ORG_ID),
        ClientSuccessAction(action_type="celebration", company_id=company.id, status="pending", org_id=APPREND_ORG_ID),
        ClientSuccessAction(action_type="review_call", company_id=company.id, status="pending", org_id=APPREND_ORG_ID),
    ])
    await db_session.commit()

    resp = await client.get("/api/v1/client-success-agent/stats")
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["clients_monitored"] == 1
    assert stats["checkins_sent"] == 1
    assert stats["escalations"] == 1
    assert stats["celebrations"] == 1
    assert stats["review_calls"] == 1
    assert stats["pending_actions"] == 3

    resp = await client.get("/api/v1/client-success-agent/health-overview")
    assert resp.status_code == 200
    overview = resp.json()
    assert len(overview["clients"]) == 1
    row = overview["clients"][0]
    assert row["company_name"] == "Nimbus Plastics"
    assert row["trend"] in ("up", "down", "flat")
