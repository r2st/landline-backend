"""Tests for the Referral & Warm-Intro agent.

Covers the service (candidate finding, LLM key resolution, OpenRouter transport,
AI drafting + template fallbacks, chain tracking), the API (config get/update +
org_admin gate + key masking, referral CRUD, action approve/dismiss, stats), and
the Celery task bodies (scan, follow-up, conversion detection).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.email_template import EmailSend
from app.models.interaction import Interaction
from app.models.referral import Referral, ReferralAction, ReferralAgent
from app.models.user import UserRole
from app.org_context import APPREND_ORG_ID
from app.services import crypto
from app.services import referral_agent as svc
from app.services.crypto import decrypt, encrypt

BASE = "/api/v1/referral-agent"


@pytest.fixture(autouse=True)
def _crypto_key():
    """Give crypto a valid Fernet key for the duration of each test (the agent's
    llm_api_key is stored encrypted)."""
    from cryptography.fernet import Fernet

    from app.config import get_settings

    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


def _now():
    return datetime.now(timezone.utc)


async def _make_happy_client(
    db: AsyncSession, *, name="Acme Co", email="ceo@acme.io", age_days=200
) -> tuple[Company, Contact]:
    """Seed a long-tenured client company with a recent, responsive contact so
    ``compute_company_health`` scores it high (recency + reply + meetings)."""
    company = Company(
        id=uuid.uuid4(),
        name=name,
        source="test",
        country="US",
        pipeline_stage="client",
        created_at=_now() - timedelta(days=age_days),
    )
    db.add(company)
    await db.flush()

    contact = Contact(
        id=uuid.uuid4(),
        company_id=company.id,
        first_name="Dana",
        last_name="Owner",
        email=email,
        is_primary=True,
        source="test",
    )
    db.add(contact)
    await db.flush()

    # Recent, replied email → recency 40 + responsiveness 30.
    db.add(
        EmailSend(
            id=uuid.uuid4(),
            contact_id=contact.id,
            to_email=email,
            status="sent",
            sent_at=_now() - timedelta(days=1),
            replied_at=_now() - timedelta(hours=12),
        )
    )
    # Three recent meetings → engagement 30.
    for i in range(3):
        db.add(
            Interaction(
                id=uuid.uuid4(),
                company_id=company.id,
                contact_id=contact.id,
                interaction_type="meeting",
                occurred_at=_now() - timedelta(days=i + 1),
            )
        )
    await db.flush()
    return company, contact


# =========================================================================
# Service — candidate finding
# =========================================================================
@pytest.mark.asyncio
async def test_find_candidates_surfaces_happy_long_term_client(db_session: AsyncSession):
    company, contact = await _make_happy_client(db_session)
    agent = await svc.get_or_create_agent(db_session)

    candidates = await svc.find_referral_candidates(db_session, agent)

    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.contact.id == contact.id
    assert cand.health_score >= agent.min_health_score
    assert cand.relationship_days >= agent.min_relationship_days


@pytest.mark.asyncio
async def test_find_candidates_excludes_young_relationship(db_session: AsyncSession):
    # Old enough contact history but the company itself is brand new.
    await _make_happy_client(db_session, age_days=10)
    agent = await svc.get_or_create_agent(db_session)

    candidates = await svc.find_referral_candidates(db_session, agent)

    assert candidates == []


@pytest.mark.asyncio
async def test_find_candidates_excludes_over_asked(db_session: AsyncSession):
    company, contact = await _make_happy_client(db_session)
    agent = await svc.get_or_create_agent(db_session)
    # Already at the default max of 2 asks.
    for _ in range(agent.max_asks_per_client):
        db_session.add(Referral(referrer_contact_id=contact.id, status="asked"))
    await db_session.flush()

    candidates = await svc.find_referral_candidates(db_session, agent)

    assert candidates == []


@pytest.mark.asyncio
async def test_find_candidates_excludes_suppressed_contact(db_session: AsyncSession):
    company, contact = await _make_happy_client(db_session)
    contact.suppressed = True
    await db_session.flush()
    agent = await svc.get_or_create_agent(db_session)

    candidates = await svc.find_referral_candidates(db_session, agent)

    assert candidates == []


@pytest.mark.asyncio
async def test_find_candidates_excludes_non_client_stage(db_session: AsyncSession):
    company, contact = await _make_happy_client(db_session)
    company.pipeline_stage = "engaged"
    await db_session.flush()
    agent = await svc.get_or_create_agent(db_session)

    candidates = await svc.find_referral_candidates(db_session, agent)

    assert candidates == []


# =========================================================================
# Service — API key resolution
# =========================================================================
@pytest.mark.asyncio
async def test_resolve_api_key_prefers_agent_key(db_session: AsyncSession, monkeypatch):
    agent = await svc.get_or_create_agent(db_session)
    agent.llm_api_key = encrypt("sk-agent-key")
    await db_session.flush()

    key = await svc.resolve_api_key(db_session, agent)
    assert key == "sk-agent-key"


@pytest.mark.asyncio
async def test_resolve_api_key_falls_back_to_org_setting(db_session: AsyncSession):
    from app.services import system_settings

    agent = await svc.get_or_create_agent(db_session)
    await system_settings.set_value(
        db_session, svc.ORG_OPENROUTER_KEY, {"key": "sk-org-key"}
    )
    await db_session.flush()

    key = await svc.resolve_api_key(db_session, agent)
    assert key == "sk-org-key"


@pytest.mark.asyncio
async def test_resolve_api_key_falls_back_to_global_config(db_session: AsyncSession, monkeypatch):
    agent = await svc.get_or_create_agent(db_session)

    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-global-key")

    key = await svc.resolve_api_key(db_session, agent)
    assert key == "sk-global-key"


# =========================================================================
# Service — OpenRouter transport uses the agent's model + key
# =========================================================================
@pytest.mark.asyncio
async def test_agent_complete_json_uses_agent_model_and_key(db_session: AsyncSession, monkeypatch):
    agent = await svc.get_or_create_agent(db_session)
    agent.llm_model = "google/gemma-3-27b-it:free"
    agent.llm_api_key = encrypt("sk-my-key")
    await db_session.flush()

    captured = {}

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"subject":"Hi","body":"Yo"}'}}]}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json
            return _FakeResp()

    monkeypatch.setattr(svc.httpx, "AsyncClient", _FakeClient)

    out = await svc._agent_complete_json(db_session, agent, "draft something")

    assert out == {"subject": "Hi", "body": "Yo"}
    assert captured["payload"]["model"] == "google/gemma-3-27b-it:free"
    assert captured["headers"]["Authorization"] == "Bearer sk-my-key"
    assert captured["url"].endswith("/chat/completions")


@pytest.mark.asyncio
async def test_agent_complete_json_no_key_returns_error(db_session: AsyncSession, monkeypatch):
    agent = await svc.get_or_create_agent(db_session)
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "openrouter_api_key", "")

    out = await svc._agent_complete_json(db_session, agent, "x")
    assert "error" in out


# =========================================================================
# Service — AI drafting + template fallback
# =========================================================================
@pytest.mark.asyncio
async def test_generate_referral_ask_falls_back_to_template(db_session: AsyncSession, monkeypatch):
    company, contact = await _make_happy_client(db_session)
    agent = await svc.get_or_create_agent(db_session)
    agent.referral_template = "Hey {first_name}, know anyone at a shop like {company}?"
    await db_session.flush()

    async def _fail(*a, **k):
        return {"error": "boom"}

    monkeypatch.setattr(svc, "_agent_complete_json", _fail)

    draft = await svc.generate_referral_ask(db_session, agent, contact, company)
    assert "Dana" in draft["body"]
    assert company.name in draft["body"]
    assert draft["subject"]


@pytest.mark.asyncio
async def test_generate_referral_ask_uses_ai_when_available(db_session: AsyncSession, monkeypatch):
    company, contact = await _make_happy_client(db_session)
    agent = await svc.get_or_create_agent(db_session)

    async def _ai(*a, **k):
        return {"subject": "AI subject", "body": "AI body"}

    monkeypatch.setattr(svc, "_agent_complete_json", _ai)

    draft = await svc.generate_referral_ask(db_session, agent, contact, company)
    assert draft == {"subject": "AI subject", "body": "AI body"}


# =========================================================================
# Service — referral chain tracking
# =========================================================================
@pytest.mark.asyncio
async def test_track_referral_chain_creates_and_reuses(db_session: AsyncSession):
    company, referrer = await _make_happy_client(db_session)
    referred = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="New", last_name="Lead",
        email="new@lead.io", source="test",
    )
    db_session.add(referred)
    await db_session.flush()

    # First call creates a 'received' referral.
    r1 = await svc.track_referral_chain(db_session, referrer, referred)
    assert r1.status == "received"
    assert r1.referred_contact_id == referred.id

    # An existing open referral (by referrer + referred email) is reused.
    pre = Referral(
        referrer_contact_id=referrer.id, referred_email="other@lead.io",
        referred_name="Other", status="asked",
    )
    db_session.add(pre)
    await db_session.flush()
    other = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Other", last_name="Lead",
        email="other@lead.io", source="test",
    )
    db_session.add(other)
    await db_session.flush()

    r2 = await svc.track_referral_chain(db_session, referrer, other)
    assert r2.id == pre.id
    assert r2.status == "received"
    assert r2.referred_contact_id == other.id


# =========================================================================
# API — config
# =========================================================================
@pytest.mark.asyncio
async def test_get_config_creates_default(client: AsyncClient):
    resp = await client.get(f"{BASE}/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_active"] is False
    assert body["llm_model"] == "meta-llama/llama-3.3-70b-instruct:free"
    assert body["has_api_key"] is False
    assert body["min_health_score"] == 80
    assert "meta-llama/llama-3.3-70b-instruct:free" in body["available_models"]


@pytest.mark.asyncio
async def test_update_config_masks_and_encrypts_key(client: AsyncClient, db_session: AsyncSession):
    resp = await client.put(
        f"{BASE}/config",
        json={"is_active": True, "llm_api_key": "sk-secret", "min_health_score": 90},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_active"] is True
    assert body["has_api_key"] is True
    assert body["min_health_score"] == 90
    # The raw key is never returned.
    assert "sk-secret" not in resp.text
    assert "llm_api_key" not in body

    agent = (await db_session.execute(select(ReferralAgent))).scalars().first()
    assert agent.llm_api_key is not None
    assert decrypt(agent.llm_api_key) == "sk-secret"


@pytest.mark.asyncio
async def test_update_config_clear_key(client: AsyncClient):
    await client.put(f"{BASE}/config", json={"llm_api_key": "sk-secret"})
    resp = await client.put(f"{BASE}/config", json={"llm_api_key": ""})
    assert resp.status_code == 200
    assert resp.json()["has_api_key"] is False


@pytest.mark.asyncio
async def test_update_config_rejects_unknown_model(client: AsyncClient):
    resp = await client.put(f"{BASE}/config", json={"llm_model": "gpt-4-turbo"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_config_requires_org_admin(client: AsyncClient, make_user, auth_headers):
    agent_user = await make_user(
        "rep@acme.io", role=UserRole.ORG_AGENT.value, org_id=APPREND_ORG_ID
    )
    resp = await client.put(
        f"{BASE}/config", json={"is_active": True}, headers=auth_headers(agent_user)
    )
    assert resp.status_code == 403


# =========================================================================
# API — referrals
# =========================================================================
@pytest.mark.asyncio
async def test_create_and_list_referral(client: AsyncClient):
    resp = await client.post(
        f"{BASE}/referrals",
        json={"referred_name": "Jane Prospect", "referred_company": "Prospect Inc",
              "referred_email": "jane@prospect.io", "status": "received"},
    )
    assert resp.status_code == 201
    rid = resp.json()["id"]
    assert resp.json()["received_at"] is not None

    lst = await client.get(f"{BASE}/referrals")
    assert lst.status_code == 200
    assert any(r["id"] == rid for r in lst.json())

    # Filter by status.
    filtered = await client.get(f"{BASE}/referrals", params={"status": "received"})
    assert all(r["status"] == "received" for r in filtered.json())
    none = await client.get(f"{BASE}/referrals", params={"status": "converted"})
    assert none.json() == []


@pytest.mark.asyncio
async def test_update_referral_stamps_converted(client: AsyncClient):
    rid = (await client.post(f"{BASE}/referrals", json={"referred_name": "X", "status": "asked"})).json()["id"]
    resp = await client.put(f"{BASE}/referrals/{rid}", json={"status": "converted", "referral_value": 5000000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "converted"
    assert body["converted_at"] is not None
    assert body["received_at"] is not None
    assert body["referral_value"] == 5000000


@pytest.mark.asyncio
async def test_update_referral_unknown_status_422(client: AsyncClient):
    rid = (await client.post(f"{BASE}/referrals", json={"referred_name": "X"})).json()["id"]
    resp = await client.put(f"{BASE}/referrals/{rid}", json={"status": "bogus"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_referral_unknown_referrer_404(client: AsyncClient):
    resp = await client.post(
        f"{BASE}/referrals",
        json={"referred_name": "X", "referrer_contact_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


# =========================================================================
# API — actions
# =========================================================================
@pytest_asyncio.fixture
async def pending_action(db_session: AsyncSession):
    company, contact = await _make_happy_client(db_session)
    referral = Referral(referrer_contact_id=contact.id, status="identified")
    db_session.add(referral)
    await db_session.flush()
    action = ReferralAction(
        referrer_contact_id=contact.id,
        referral_id=referral.id,
        action_type="initial_ask",
        status="pending",
        ai_subject="Quick favor",
        ai_message="Hi Dana, know anyone?",
    )
    db_session.add(action)
    await db_session.flush()
    return action, referral, contact


@pytest.mark.asyncio
async def test_list_actions_defaults_to_pending(client: AsyncClient, pending_action):
    action, _, _ = pending_action
    resp = await client.get(f"{BASE}/actions")
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()]
    assert str(action.id) in ids
    assert resp.json()[0]["referrer_name"] == "Dana Owner"


@pytest.mark.asyncio
async def test_approve_action_sends_and_advances(client: AsyncClient, db_session, pending_action, monkeypatch):
    action, referral, _ = pending_action

    async def _fake_send(*a, **k):
        return {"status": "sent", "transport": "test"}

    import app.services.email_dispatch as dispatch
    monkeypatch.setattr(dispatch, "send_outreach_email", _fake_send)

    resp = await client.post(f"{BASE}/actions/{action.id}/approve")
    assert resp.status_code == 200
    assert resp.json()["send_status"] == "sent"
    assert resp.json()["action"]["status"] == "sent"

    await db_session.refresh(action)
    await db_session.refresh(referral)
    assert action.status == "sent"
    assert action.executed_at is not None
    assert referral.status == "asked"


@pytest.mark.asyncio
async def test_approve_action_not_sent_stays_pending(client: AsyncClient, db_session, pending_action, monkeypatch):
    action, _, _ = pending_action

    async def _skipped(*a, **k):
        return {"status": "skipped", "reason": "no_ses_credentials"}

    import app.services.email_dispatch as dispatch
    monkeypatch.setattr(dispatch, "send_outreach_email", _skipped)

    resp = await client.post(f"{BASE}/actions/{action.id}/approve")
    assert resp.status_code == 200
    assert resp.json()["send_status"] == "skipped"
    await db_session.refresh(action)
    assert action.status == "pending"


@pytest.mark.asyncio
async def test_dismiss_action(client: AsyncClient, db_session, pending_action):
    action, _, _ = pending_action
    resp = await client.post(f"{BASE}/actions/{action.id}/dismiss")
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"
    # Re-dismiss now conflicts.
    again = await client.post(f"{BASE}/actions/{action.id}/dismiss")
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_action_execution_requires_org_admin(client: AsyncClient, pending_action, make_user, auth_headers):
    action, _, _ = pending_action
    rep = await make_user("rep2@acme.io", role=UserRole.ORG_AGENT.value, org_id=APPREND_ORG_ID)
    resp = await client.post(f"{BASE}/actions/{action.id}/dismiss", headers=auth_headers(rep))
    assert resp.status_code == 403


# =========================================================================
# API — stats
# =========================================================================
@pytest.mark.asyncio
async def test_stats_computes_rates_and_value(client: AsyncClient):
    await client.post(f"{BASE}/referrals", json={"referred_name": "A", "status": "asked"})
    await client.post(f"{BASE}/referrals", json={"referred_name": "B", "status": "received"})
    await client.post(f"{BASE}/referrals", json={"referred_name": "C", "status": "converted", "referral_value": 1000000})
    await client.post(f"{BASE}/referrals", json={"referred_name": "D", "status": "declined"})

    resp = await client.get(f"{BASE}/stats")
    assert resp.status_code == 200
    s = resp.json()
    assert s["total"] == 4
    assert s["converted"] == 1
    assert s["total_referral_value"] == 1000000
    # asked-denominator = asked+received+converted+declined = 4; received+ = 2.
    assert s["response_rate"] == pytest.approx(0.5)
    assert s["conversion_rate"] == pytest.approx(0.5)


# =========================================================================
# Tasks
# =========================================================================
@pytest.mark.asyncio
async def test_scan_org_candidates_queues_action(db_session: AsyncSession):
    from app.tasks.referral_agent import scan_org_candidates

    company, contact = await _make_happy_client(db_session)
    agent = await svc.get_or_create_agent(db_session)
    agent.is_active = True
    await db_session.flush()

    result = await scan_org_candidates(db_session)
    assert result["queued"] == 1

    actions = (await db_session.execute(select(ReferralAction))).scalars().all()
    assert len(actions) == 1
    assert actions[0].action_type == "initial_ask"
    assert actions[0].ai_message  # drafted (template fallback with no API key)

    referrals = (await db_session.execute(select(Referral))).scalars().all()
    assert len(referrals) == 1
    assert referrals[0].status == "identified"


@pytest.mark.asyncio
async def test_scan_skips_inactive_agent(db_session: AsyncSession):
    from app.tasks.referral_agent import scan_org_candidates

    await _make_happy_client(db_session)
    await svc.get_or_create_agent(db_session)  # is_active defaults False

    result = await scan_org_candidates(db_session)
    assert result == {"skipped": "inactive"}
    assert (await db_session.execute(select(ReferralAction))).scalars().first() is None


@pytest.mark.asyncio
async def test_scan_is_idempotent(db_session: AsyncSession):
    from app.tasks.referral_agent import scan_org_candidates

    await _make_happy_client(db_session)
    agent = await svc.get_or_create_agent(db_session)
    agent.is_active = True
    await db_session.flush()

    await scan_org_candidates(db_session)
    second = await scan_org_candidates(db_session)
    assert second["queued"] == 0
    assert len((await db_session.execute(select(ReferralAction))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_followup_queues_for_stale_ask(db_session: AsyncSession):
    from app.tasks.referral_agent import followup_org_referrals

    company, contact = await _make_happy_client(db_session)
    agent = await svc.get_or_create_agent(db_session)
    agent.is_active = True
    await db_session.flush()

    referral = Referral(
        referrer_contact_id=contact.id,
        status="asked",
        ask_sent_at=_now() - timedelta(days=agent.followup_after_days + 1),
    )
    db_session.add(referral)
    await db_session.flush()

    result = await followup_org_referrals(db_session)
    assert result["queued"] == 1

    action = (await db_session.execute(
        select(ReferralAction).where(ReferralAction.action_type == "followup")
    )).scalars().first()
    assert action is not None
    await db_session.refresh(referral)
    assert referral.status == "pending"


@pytest.mark.asyncio
async def test_followup_skips_recent_ask(db_session: AsyncSession):
    from app.tasks.referral_agent import followup_org_referrals

    company, contact = await _make_happy_client(db_session)
    agent = await svc.get_or_create_agent(db_session)
    agent.is_active = True
    await db_session.flush()
    db_session.add(Referral(
        referrer_contact_id=contact.id, status="asked",
        ask_sent_at=_now() - timedelta(days=1),
    ))
    await db_session.flush()

    result = await followup_org_referrals(db_session)
    assert result["queued"] == 0


@pytest.mark.asyncio
async def test_conversion_detection_marks_converted_and_thanks(db_session: AsyncSession):
    from app.tasks.referral_agent import check_org_conversions

    company, referrer = await _make_happy_client(db_session)
    agent = await svc.get_or_create_agent(db_session)
    agent.is_active = True
    await db_session.flush()

    # A referred contact who has since closed a won deal.
    referred = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Won", last_name="Lead",
        email="won@lead.io", source="test",
    )
    db_session.add(referred)
    await db_session.flush()
    db_session.add(Deal(
        id=uuid.uuid4(), company_id=company.id, title="Big deal",
        value=25000, status="won", close_date=_now(),
    ))
    referral = Referral(
        referrer_contact_id=referrer.id,
        referred_contact_id=referred.id,
        referred_name="Won Lead",
        status="received",
    )
    db_session.add(referral)
    await db_session.flush()

    result = await check_org_conversions(db_session)
    assert result["converted"] == 1

    await db_session.refresh(referral)
    assert referral.status == "converted"
    assert referral.converted_at is not None
    assert referral.referral_value == 2500000  # 25000 major units → paise

    thanks = (await db_session.execute(
        select(ReferralAction).where(ReferralAction.action_type == "thank_you")
    )).scalars().first()
    assert thanks is not None
