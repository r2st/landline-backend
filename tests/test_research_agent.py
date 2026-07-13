"""Tests for the AI Company Research Agent (feature 1.4).

The web scraper and Claude are mocked so these run offline. Covers signal
gathering + synthesis, brief supersession, the prompt formatter, and the
company research endpoints.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.models.company import Company
from app.models.company_news import CompanyNews
from app.services.research_agent import (
    format_research_for_prompt,
    get_active_research,
    get_research_agent,
)

_FAKE_SYNTHESIS = {
    "research_brief": "Acme makes precision stamped parts for automotive.",
    "pain_points": [
        {"pain": "cavity-to-cavity variation", "evidence": "ISO badge on site", "how_we_help": "real-time SPC"},
    ],
    "outreach_angles": [
        {"rank": 1, "angle": "scrap cost", "reasoning": "high-volume shop", "opening_line": "saw you run 24/7"},
        {"rank": 2, "angle": "compliance", "reasoning": "automotive", "opening_line": "IATF audits are brutal"},
    ],
    "competitive_intel": {"competitors": ["FactoryFoo"], "technologies": ["SAP"], "notes": "mentions SAP ERP"},
}


@pytest.fixture
def _mock_research(monkeypatch):
    """Patch the scraper + Claude so research runs offline and deterministically."""
    async def fake_scrape(self, domain):
        return {"title": "Acme", "description": "Precision stamping", "about": "We are ISO 9001 certified.", "technologies": ["SAP"], "social_links": {}}

    async def fake_complete_json(self, prompt, max_tokens=1024):
        fake_complete_json.last_prompt = prompt
        return dict(_FAKE_SYNTHESIS)

    import app.services.ai_service as ai_mod
    from app.services.ai_service import AIService
    from app.services.web_scraper import WebScraper

    # Reset the AIService singleton so a leaked instance-attribute from another
    # test file can't shadow the class-level patch below.
    monkeypatch.setattr(ai_mod, "_ai_service", None)
    monkeypatch.setattr(WebScraper, "scrape_company", fake_scrape)
    monkeypatch.setattr(AIService, "_complete_json", fake_complete_json)
    return fake_complete_json


# --------------------------------------------------------------------------- #
# format_research_for_prompt
# --------------------------------------------------------------------------- #


def test_format_research_none_is_empty():
    assert format_research_for_prompt(None) == ""


def test_format_research_renders_sections():
    research = SimpleNamespace(
        research_brief="They make widgets.",
        pain_points=[{"pain": "scrap", "how_we_help": "monitoring"}],
        outreach_angles=[{"rank": 1, "angle": "cost", "reasoning": "big shop"}],
        competitive_intel={"competitors": ["FooCorp"], "technologies": ["SAP"]},
    )
    text = format_research_for_prompt(research)
    assert "They make widgets." in text
    assert "scrap" in text
    assert "cost" in text
    assert "FooCorp" in text


# --------------------------------------------------------------------------- #
# research_company
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_research_company_creates_brief(db_session, sample_company, _mock_research):
    # Seed a news item so the news source is exercised.
    db_session.add(
        CompanyNews(id=uuid.uuid4(), company_id=sample_company.id, title="Acme opens new plant")
    )
    await db_session.flush()

    agent = get_research_agent()
    research = await agent.research_company(db_session, sample_company, force=True)

    assert research is not None
    assert research.research_brief.startswith("Acme makes")
    assert len(research.pain_points) == 1
    assert len(research.outreach_angles) == 2
    assert research.is_stale is False
    # Sources: website (scrape), news, and apollo (company has employee_count).
    assert "website" in research.sources_used
    assert "news" in research.sources_used

    # The scraped 'about' text must have reached the synthesis prompt.
    assert "ISO 9001" in _mock_research.last_prompt


@pytest.mark.asyncio
async def test_research_company_supersedes_prior(db_session, sample_company, _mock_research):
    agent = get_research_agent()
    first = await agent.research_company(db_session, sample_company, force=True)
    second = await agent.research_company(db_session, sample_company, force=True)

    assert first.id != second.id
    # The old brief is marked stale; only the newest is active.
    await db_session.refresh(first)
    assert first.is_stale is True
    assert second.is_stale is False

    active = await get_active_research(db_session, sample_company.id)
    assert active.id == second.id


@pytest.mark.asyncio
async def test_research_company_idempotent_without_force(db_session, sample_company, _mock_research):
    agent = get_research_agent()
    first = await agent.research_company(db_session, sample_company, force=True)
    # Without force, an existing non-stale brief is returned unchanged.
    again = await agent.research_company(db_session, sample_company, force=False)
    assert again.id == first.id


@pytest.mark.asyncio
async def test_research_company_handles_ai_failure(db_session, sample_company, monkeypatch):
    async def fake_scrape(self, domain):
        return {"about": "text"}

    async def failing_json(self, prompt, max_tokens=1024):
        return {"error": "AI down"}

    from app.services.ai_service import AIService
    from app.services.web_scraper import WebScraper

    monkeypatch.setattr(WebScraper, "scrape_company", fake_scrape)
    monkeypatch.setattr(AIService, "_complete_json", failing_json)

    agent = get_research_agent()
    research = await agent.research_company(db_session, sample_company, force=True)
    assert research is None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_research_none_then_generate(client, db_session, _mock_research):
    # Create a company directly in the DB the client uses.
    company = Company(id=uuid.uuid4(), name="Widgets Inc", source="test", country="US", domain="widgets.example")
    db_session.add(company)
    await db_session.commit()

    # No brief yet.
    r = await client.get(f"/api/v1/companies/{company.id}/research")
    assert r.status_code == 200
    assert r.json()["status"] == "none"

    # Generate one.
    r2 = await client.post(f"/api/v1/companies/{company.id}/research")
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "ok"
    assert body["research_brief"].startswith("Acme makes")
    assert len(body["outreach_angles"]) == 2

    # Now GET returns it.
    r3 = await client.get(f"/api/v1/companies/{company.id}/research")
    assert r3.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_get_research_unknown_company_404(client):
    r = await client.get(f"/api/v1/companies/{uuid.uuid4()}/research")
    assert r.status_code == 404
