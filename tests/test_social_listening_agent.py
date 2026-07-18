"""Tests for the Social Listening Agent (models, service, API).

Covers: config get/update (with the org_admin write gate + key masking +
encryption + key-resolution order), signal listing/filtering/action/dismiss,
monitored-company CRUD, the scan/analyze pipeline (with fetching + AI mocked),
auto lead creation + Dialtone enrollment, stats, and cross-org isolation.
"""

import uuid

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.models.social_listening_agent import (
    DEFAULT_LLM_MODEL,
    MonitoredCompany,
    SignalEvent,
    SocialListeningAgent,
)
from app.models.system_setting import SystemSetting
from app.models.tenant import Organization
from app.models.user import User, UserRole
from app.org_context import APPREND_ORG_ID, set_current_org_id
from app.services.social_listening_agent import (
    ORG_OPENROUTER_KEY_SETTING,
    SocialListeningService,
    get_social_listening_service,
)

BASE = "/api/v1/social-listening-agent"
CONFIG = f"{BASE}/config"
SIGNALS = f"{BASE}/signals"
MONITORED = f"{BASE}/monitored-companies"
STATS = f"{BASE}/stats"


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture
def crypto_key():
    """Give crypto a valid Fernet key for the duration of one test."""
    from app.config import get_settings
    from app.services import crypto

    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


@pytest_asyncio.fixture
async def org_admin(make_user) -> User:
    return await make_user(
        "owner@acme.io", role=UserRole.ORG_ADMIN.value, name="Org Owner",
        org_id=APPREND_ORG_ID,
    )


@pytest_asyncio.fixture
async def org_agent(make_user) -> User:
    return await make_user(
        "rep@acme.io", role=UserRole.ORG_AGENT.value, name="Rep One",
        org_id=APPREND_ORG_ID,
    )


@pytest_asyncio.fixture
async def org_b(db_session: AsyncSession) -> Organization:
    org = Organization(
        id=uuid.uuid4(), name="Beta Industries", slug="beta-industries",
        is_active=True, billing_plan="starter",
    )
    db_session.add(org)
    await db_session.commit()
    return org


@pytest_asyncio.fixture
async def org_b_admin(make_user, org_b) -> User:
    return await make_user(
        "owner@beta.io", role=UserRole.ORG_ADMIN.value, name="Beta Owner",
        org_id=org_b.id,
    )


class FakeAI:
    """Stand-in for AIService — returns a canned analysis for every call."""

    def __init__(self, result: dict):
        self._result = result
        self.model = "fake-model"

    async def complete_json(self, prompt, max_tokens=400, system=None):
        return dict(self._result)


def _patch_ai(monkeypatch, result: dict):
    async def fake_build_ai(self, db, agent):
        return FakeAI(result)

    monkeypatch.setattr(SocialListeningService, "_build_ai", fake_build_ai)


def _patch_fetch(monkeypatch, news_items, job_items=None):
    """Patch the two network fetchers to return canned RSS item dicts."""

    async def fake_google_news(self, query):
        # Job-intent queries carry a hiring term; route them to job_items.
        if job_items is not None and any(
            t in query for t in ("hiring", "now hiring", "job opening")
        ):
            return list(job_items)
        return list(news_items)

    async def fake_fetch_rss(self, url, validate=True):
        return list(news_items)

    monkeypatch.setattr(SocialListeningService, "_google_news", fake_google_news)
    monkeypatch.setattr(SocialListeningService, "_fetch_rss", fake_fetch_rss)


# --- Config: read/update, gating, masking -----------------------------------


@pytest.mark.asyncio
async def test_get_config_creates_default(client: AsyncClient, org_agent, auth_headers):
    resp = await client.get(CONFIG, headers=auth_headers(org_agent))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["is_active"] is False
    assert data["llm_model"] == DEFAULT_LLM_MODEL
    assert data["has_api_key"] is False
    assert data["min_relevance_score"] == 70
    assert DEFAULT_LLM_MODEL in data["available_models"]


@pytest.mark.asyncio
async def test_update_config_as_admin(client: AsyncClient, org_admin, auth_headers):
    resp = await client.put(
        CONFIG,
        headers=auth_headers(org_admin),
        json={
            "is_active": True,
            "llm_model": "google/gemma-3-27b-it:free",
            "monitored_keywords": ["funding", "expansion"],
            "monitored_industries": ["cnc_machining"],
            "min_relevance_score": 60,
            "auto_create_leads": True,
            "scan_frequency_hours": 12,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["is_active"] is True
    assert data["llm_model"] == "google/gemma-3-27b-it:free"
    assert data["monitored_keywords"] == ["funding", "expansion"]
    assert data["min_relevance_score"] == 60
    assert data["auto_create_leads"] is True
    assert data["scan_frequency_hours"] == 12


@pytest.mark.asyncio
async def test_update_config_non_admin_forbidden(client: AsyncClient, org_agent, auth_headers):
    resp = await client.put(
        CONFIG, headers=auth_headers(org_agent), json={"is_active": True}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_config_rejects_unknown_model(client: AsyncClient, org_admin, auth_headers):
    resp = await client.put(
        CONFIG, headers=auth_headers(org_admin), json={"llm_model": "gpt-4-turbo"}
    )
    assert resp.status_code == 422
    assert "llm_model" in resp.text


@pytest.mark.asyncio
async def test_api_key_is_encrypted_and_masked(
    client: AsyncClient, org_admin, auth_headers, crypto_key, db_session
):
    resp = await client.put(
        CONFIG, headers=auth_headers(org_admin), json={"llm_api_key": "sk-or-secret-123"}
    )
    assert resp.status_code == 200, resp.text
    # Response never leaks the key, only the boolean.
    assert "sk-or-secret-123" not in resp.text
    assert resp.json()["has_api_key"] is True

    # Stored ciphertext != plaintext, and decrypts back.
    agent = (await db_session.execute(select(SocialListeningAgent))).scalars().first()
    assert agent.llm_api_key and agent.llm_api_key != "sk-or-secret-123"
    from app.services.crypto import decrypt

    assert decrypt(agent.llm_api_key) == "sk-or-secret-123"

    # Empty string clears it.
    resp = await client.put(
        CONFIG, headers=auth_headers(org_admin), json={"llm_api_key": ""}
    )
    assert resp.json()["has_api_key"] is False


# --- Key resolution order ----------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_api_key_prefers_agent_key(db_session, crypto_key):
    set_current_org_id(APPREND_ORG_ID)
    service = SocialListeningService()
    agent = await service.get_or_create_agent(db_session)
    service.set_api_key(agent, "agent-key")
    await db_session.flush()
    assert await service.resolve_api_key(db_session, agent) == "agent-key"


@pytest.mark.asyncio
async def test_resolve_api_key_falls_back_to_org_setting(db_session, crypto_key):
    set_current_org_id(APPREND_ORG_ID)
    service = SocialListeningService()
    agent = await service.get_or_create_agent(db_session)
    db_session.add(SystemSetting(
        key=ORG_OPENROUTER_KEY_SETTING, value={"key": "org-level-key"},
    ))
    await db_session.flush()
    assert await service.resolve_api_key(db_session, agent) == "org-level-key"


@pytest.mark.asyncio
async def test_resolve_api_key_none_when_unset(db_session, crypto_key):
    set_current_org_id(APPREND_ORG_ID)
    service = SocialListeningService()
    agent = await service.get_or_create_agent(db_session)
    await db_session.flush()
    assert await service.resolve_api_key(db_session, agent) is None


# --- Analysis normalization --------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_signal_normalizes(db_session):
    service = SocialListeningService()
    agent = SocialListeningAgent(monitored_industries=["cnc"], monitored_keywords=["funding"])
    ai = FakeAI({"relevance_score": 150, "signal_type": "FUNDING", "summary": "Raised $10M"})
    out = await service.analyze_signal(ai, agent, {"headline": "Acme raises $10M"})
    assert out["relevance_score"] == 100  # clamped
    assert out["signal_type"] == "funding"  # lowercased + validated
    assert out["summary"] == "Raised $10M"


@pytest.mark.asyncio
async def test_analyze_signal_degrades_on_error(db_session):
    service = SocialListeningService()
    agent = SocialListeningAgent()
    ai = FakeAI({"error": "boom"})
    out = await service.analyze_signal(ai, agent, {"headline": "H"})
    assert out["relevance_score"] == 0
    assert out["signal_type"] is None
    assert out["summary"] == "H"


# --- Scan pipeline -----------------------------------------------------------


@pytest.mark.asyncio
async def test_run_scan_inactive_is_skipped(db_session, monkeypatch):
    set_current_org_id(APPREND_ORG_ID)
    service = SocialListeningService()
    agent = await service.get_or_create_agent(db_session)  # inactive by default
    out = await service.run_scan(db_session, agent)
    assert out["skipped"] is True
    assert out["signals_found"] == 0


@pytest.mark.asyncio
async def test_run_scan_persists_and_dedups(db_session, monkeypatch):
    set_current_org_id(APPREND_ORG_ID)
    service = SocialListeningService()
    agent = await service.get_or_create_agent(db_session)
    agent.is_active = True
    agent.monitored_keywords = ["funding"]
    await db_session.flush()

    news = [{
        "title": "Acme Corp raises $20M Series B",
        "link": "https://example.com/acme-funding",
        "description": "Acme Corp announced funding.",
        "source": "TechCrunch", "published_at": None,
    }]
    _patch_fetch(monkeypatch, news, job_items=[])
    _patch_ai(monkeypatch, {"relevance_score": 85, "signal_type": "funding", "summary": "Raised $20M"})

    out = await service.run_scan(db_session, agent)
    assert out["signals_found"] == 1
    signals = (await db_session.execute(select(SignalEvent))).scalars().all()
    assert len(signals) == 1
    assert signals[0].relevance_score == 85
    assert signals[0].signal_type == "funding"
    assert agent.last_scan_at is not None

    # Second scan of the same item is deduped by URL — no new signal.
    out2 = await service.run_scan(db_session, agent)
    assert out2["signals_found"] == 0
    assert len((await db_session.execute(select(SignalEvent))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_run_scan_auto_creates_lead_above_threshold(db_session, monkeypatch):
    set_current_org_id(APPREND_ORG_ID)
    service = SocialListeningService()
    agent = await service.get_or_create_agent(db_session)
    agent.is_active = True
    agent.monitored_keywords = ["funding"]
    agent.min_relevance_score = 70
    agent.auto_create_leads = True
    await db_session.flush()

    news = [{
        "title": "Beta Manufacturing opens new plant",
        "link": "https://example.com/beta",
        "description": "Expansion.", "source": "News", "published_at": None,
    }]
    _patch_fetch(monkeypatch, news, job_items=[])
    _patch_ai(monkeypatch, {"relevance_score": 90, "signal_type": "expansion",
                            "summary": "New plant"})

    out = await service.run_scan(db_session, agent)
    assert out["signals_found"] == 1
    assert out["leads_created"] == 1

    signal = (await db_session.execute(select(SignalEvent))).scalars().first()
    assert signal.lead_created is True
    assert signal.contact_id is not None
    # A company + contact were created from the signal.
    company = (await db_session.execute(
        select(Company).where(Company.name == "Beta Manufacturing opens new plant")
    )).scalars().first() or (await db_session.execute(select(Company))).scalars().first()
    assert company is not None


@pytest.mark.asyncio
async def test_run_scan_below_threshold_no_lead(db_session, monkeypatch):
    set_current_org_id(APPREND_ORG_ID)
    service = SocialListeningService()
    agent = await service.get_or_create_agent(db_session)
    agent.is_active = True
    agent.monitored_keywords = ["funding"]
    agent.min_relevance_score = 70
    agent.auto_create_leads = True
    await db_session.flush()

    news = [{"title": "Some vaguely related blog", "link": "https://example.com/x",
             "description": "", "source": "", "published_at": None}]
    _patch_fetch(monkeypatch, news, job_items=[])
    _patch_ai(monkeypatch, {"relevance_score": 30, "signal_type": "partnership", "summary": "meh"})

    out = await service.run_scan(db_session, agent)
    assert out["signals_found"] == 1
    assert out["leads_created"] == 0
    signal = (await db_session.execute(select(SignalEvent))).scalars().first()
    assert signal.lead_created is False


@pytest.mark.asyncio
async def test_run_scan_job_postings(db_session, monkeypatch):
    set_current_org_id(APPREND_ORG_ID)
    service = SocialListeningService()
    agent = await service.get_or_create_agent(db_session)
    agent.is_active = True
    await db_session.flush()
    db_session.add(MonitoredCompany(company_name="Gamma Inc", monitor_news=False, monitor_jobs=True))
    await db_session.flush()

    jobs = [{"title": "Gamma Inc is hiring a Sales Manager", "link": "https://jobs.example.com/1",
             "description": "", "source": "", "published_at": None}]
    _patch_fetch(monkeypatch, [], job_items=jobs)
    _patch_ai(monkeypatch, {"relevance_score": 80, "signal_type": "hiring", "summary": "Hiring sales"})

    out = await service.run_scan(db_session, agent)
    assert out["signals_found"] >= 1
    sig = (await db_session.execute(
        select(SignalEvent).where(SignalEvent.source_type == "job_posting")
    )).scalars().first()
    assert sig is not None
    assert sig.signal_type == "hiring"


# --- Signals API -------------------------------------------------------------


@pytest_asyncio.fixture
async def seeded_signals(db_session) -> list[SignalEvent]:
    set_current_org_id(APPREND_ORG_ID)
    events = [
        SignalEvent(source_type="news", company_name="Acme", headline="Acme raises $5M",
                    relevance_score=90, signal_type="funding", status="new"),
        SignalEvent(source_type="job_posting", company_name="Beta", headline="Beta hiring",
                    relevance_score=55, signal_type="hiring", status="new"),
        SignalEvent(source_type="news", company_name="Delta", headline="Delta launch",
                    relevance_score=75, signal_type="product_launch", status="dismissed"),
    ]
    for e in events:
        db_session.add(e)
    await db_session.commit()
    return events


@pytest.mark.asyncio
async def test_list_signals_and_filters(client, org_agent, auth_headers, seeded_signals):
    h = auth_headers(org_agent)
    resp = await client.get(SIGNALS, headers=h)
    assert resp.status_code == 200
    assert len(resp.json()) == 3

    resp = await client.get(f"{SIGNALS}?signal_type=funding", headers=h)
    assert [s["signal_type"] for s in resp.json()] == ["funding"]

    resp = await client.get(f"{SIGNALS}?min_relevance=70", headers=h)
    assert all(s["relevance_score"] >= 70 for s in resp.json())
    assert len(resp.json()) == 2

    resp = await client.get(f"{SIGNALS}?status=dismissed", headers=h)
    assert [s["company_name"] for s in resp.json()] == ["Delta"]

    resp = await client.get(f"{SIGNALS}?source_type=job_posting", headers=h)
    assert [s["company_name"] for s in resp.json()] == ["Beta"]


@pytest.mark.asyncio
async def test_dismiss_signal(client, org_agent, auth_headers, seeded_signals):
    sid = str(seeded_signals[0].id)
    resp = await client.post(f"{SIGNALS}/{sid}/dismiss", headers=auth_headers(org_agent))
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"


@pytest.mark.asyncio
async def test_action_signal_creates_lead(client, org_agent, auth_headers, seeded_signals):
    sid = str(seeded_signals[0].id)
    resp = await client.post(f"{SIGNALS}/{sid}/action", headers=auth_headers(org_agent))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lead_created"] is True
    assert body["contact_id"] is not None
    assert body["signal"]["status"] == "actioned"


@pytest.mark.asyncio
async def test_action_unknown_company_422(client, org_agent, auth_headers, db_session):
    set_current_org_id(APPREND_ORG_ID)
    sig = SignalEvent(source_type="news", company_name="Unknown", headline="mystery",
                      relevance_score=80, status="new")
    db_session.add(sig)
    await db_session.commit()
    resp = await client.post(f"{SIGNALS}/{sig.id}/action", headers=auth_headers(org_agent))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_action_signal_not_found(client, org_agent, auth_headers):
    resp = await client.post(f"{SIGNALS}/{uuid.uuid4()}/action", headers=auth_headers(org_agent))
    assert resp.status_code == 404


# --- Auto-enroll -------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_enroll_lead(db_session, monkeypatch):
    set_current_org_id(APPREND_ORG_ID)
    service = SocialListeningService()
    # A company + an emailable contact + an active sequence.
    company = Company(name="Enroll Co", country="US", source="test",
                      industry_segment="cnc_machining")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(company_id=company.id, first_name="Jane", last_name="Doe",
                      email="jane@enrollco.com", source="social_listening")
    seq = OutreachSequence(name="Generic", is_active=True, status="active")
    db_session.add_all([contact, seq])
    await db_session.flush()

    signal = SignalEvent(source_type="news", company_name="Enroll Co",
                         company_id=company.id, headline="h", relevance_score=90, status="new")
    db_session.add(signal)
    await db_session.flush()

    enrolled = await service.auto_enroll_lead(db_session, contact, signal)
    assert enrolled is True
    row = (await db_session.execute(
        select(OutreachEnrollment).where(OutreachEnrollment.contact_id == contact.id)
    )).scalars().first()
    assert row is not None
    # A second call does not double-enroll.
    assert await service.auto_enroll_lead(db_session, contact, signal) is False


@pytest.mark.asyncio
async def test_auto_enroll_skips_contact_without_email(db_session):
    set_current_org_id(APPREND_ORG_ID)
    service = SocialListeningService()
    company = Company(name="NoEmail Co", country="US", source="test")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(company_id=company.id, first_name="No", last_name="Email",
                      source="social_listening")
    db_session.add(contact)
    await db_session.flush()
    signal = SignalEvent(source_type="news", company_name="NoEmail Co",
                         company_id=company.id, headline="h", relevance_score=90, status="new")
    db_session.add(signal)
    await db_session.flush()
    assert await service.auto_enroll_lead(db_session, contact, signal) is False


# --- process_new_signals -----------------------------------------------------


@pytest.mark.asyncio
async def test_process_new_signals_creates_leads(db_session):
    set_current_org_id(APPREND_ORG_ID)
    service = SocialListeningService()
    agent = await service.get_or_create_agent(db_session)
    agent.is_active = True
    agent.auto_create_leads = True
    agent.min_relevance_score = 70
    await db_session.flush()

    db_session.add_all([
        SignalEvent(source_type="news", company_name="High Co", headline="big news",
                    relevance_score=95, status="new"),
        SignalEvent(source_type="news", company_name="Low Co", headline="small",
                    relevance_score=40, status="new"),
    ])
    await db_session.flush()

    out = await service.process_new_signals(db_session)
    assert out["leads_created"] == 1  # only the high-relevance one
    high = (await db_session.execute(
        select(SignalEvent).where(SignalEvent.company_name == "High Co")
    )).scalars().first()
    assert high.lead_created is True


# --- Monitored companies -----------------------------------------------------


@pytest.mark.asyncio
async def test_monitored_company_crud(client, org_admin, org_agent, auth_headers):
    # Add (admin).
    resp = await client.post(
        MONITORED, headers=auth_headers(org_admin),
        json={"company_name": "Watched Corp", "domain": "watched.com", "monitor_jobs": False},
    )
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    assert resp.json()["monitor_jobs"] is False

    # List.
    resp = await client.get(MONITORED, headers=auth_headers(org_agent))
    assert resp.status_code == 200
    assert any(c["id"] == mid for c in resp.json())

    # Non-admin cannot add.
    resp = await client.post(
        MONITORED, headers=auth_headers(org_agent), json={"company_name": "Nope"}
    )
    assert resp.status_code == 403

    # Delete (admin).
    resp = await client.delete(f"{MONITORED}/{mid}", headers=auth_headers(org_admin))
    assert resp.status_code == 204
    resp = await client.delete(f"{MONITORED}/{mid}", headers=auth_headers(org_admin))
    assert resp.status_code == 404


# --- Stats -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats(client, org_agent, auth_headers, seeded_signals):
    resp = await client.get(STATS, headers=auth_headers(org_agent))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_signals"] == 3
    assert data["dismissed"] == 1
    assert isinstance(data["by_signal_type"], list)
    assert isinstance(data["top_companies"], list)
    assert data["conversion_rate"] == 0.0


# --- Cross-org isolation -----------------------------------------------------


@pytest.mark.asyncio
async def test_signals_are_org_isolated(
    client, org_admin, org_b_admin, auth_headers, seeded_signals
):
    # org A (Apprend) sees its 3 seeded signals.
    resp = await client.get(SIGNALS, headers=auth_headers(org_admin))
    assert len(resp.json()) == 3
    # org B sees none of them.
    resp = await client.get(SIGNALS, headers=auth_headers(org_b_admin))
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_config_is_per_org(client, org_admin, org_b_admin, auth_headers):
    # org A activates; org B stays default-inactive (separate config rows).
    await client.put(CONFIG, headers=auth_headers(org_admin), json={"is_active": True})
    resp = await client.get(CONFIG, headers=auth_headers(org_b_admin))
    assert resp.json()["is_active"] is False


@pytest.mark.asyncio
async def test_singleton_accessor():
    assert get_social_listening_service() is get_social_listening_service()
