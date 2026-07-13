"""Tests for the News Monitor quick win:

- check_company_news: actionable news auto-generates + stores an outreach email.
- parse_blog_articles: extract headlines from a company blog/press page.
- generate_news_outreach: draft a timely outreach email (AI + fallback).

DB-backed integration tests are defined first so the async DB fixtures own the
event loop before the pure/async helper tests run (the shared session-scoped
loop in conftest is otherwise order-sensitive).
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.company_news import CompanyNews
from app.services import news_monitor as nm
from app.services.news_monitor import NewsMonitor, parse_blog_articles


class _StubAI:
    def __init__(self, result):
        self._result = result

    async def _complete_json(self, prompt, max_tokens=500):
        return self._result


# ---------------------------------------------------------------------------
# check_company_news integration — actionable news triggers outreach
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actionable_news_generates_and_stores_outreach(
    db_session: AsyncSession, sample_company: Company, monkeypatch
):
    mon = NewsMonitor()

    # No Google News; one blog article.
    monkeypatch.setattr(mon, "_fetch_google_news", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        mon, "_fetch_company_blog",
        AsyncMock(return_value=[{
            "title": "Acme Stamping opens second facility",
            "link": "https://acmestamp.com/news/2",
            "source": "Acme newsroom",
            "description": "",
        }]),
    )
    # Actionable analysis.
    monkeypatch.setattr(
        mon, "_analyze_article",
        AsyncMock(return_value={
            "is_about_company": True,
            "relevance_score": 9,
            "category": "expansion",
            "summary": "Opened a second plant",
            "is_actionable": True,
            "suggested_action": "Congratulate + connect to quality visibility",
            "outreach_angle": "expansion means new lines to keep in spec",
        }),
    )
    # Stub the AI service used for outreach generation.
    monkeypatch.setattr(
        nm, "get_ai_service",
        lambda: _StubAI({"subject": "second plant", "body": "congrats - Suman"}),
    )

    results = await mon.check_company_news(db_session, sample_company)

    assert len(results) == 1
    assert results[0]["actionable"] is True
    assert results[0]["outreach_email"]["subject"] == "second plant"

    # Stored on the news row's ai_analysis.
    row = (
        await db_session.execute(
            select(CompanyNews).where(CompanyNews.company_id == sample_company.id)
        )
    ).scalar_one()
    assert row.is_actionable is True
    assert row.ai_analysis["outreach_email"]["body"] == "congrats - Suman"


@pytest.mark.asyncio
async def test_non_actionable_news_has_no_outreach(
    db_session: AsyncSession, sample_company: Company, monkeypatch
):
    mon = NewsMonitor()
    monkeypatch.setattr(mon, "_fetch_google_news", AsyncMock(return_value=[
        {"title": "Acme mentioned in a roundup", "link": "x", "source": "News"}
    ]))
    monkeypatch.setattr(mon, "_fetch_company_blog", AsyncMock(return_value=[]))
    monkeypatch.setattr(mon, "_analyze_article", AsyncMock(return_value={
        "relevance_score": 5, "category": "general", "summary": "meh",
        "is_actionable": False,
    }))
    # get_ai_service should not be needed for outreach, but stub to be safe.
    monkeypatch.setattr(nm, "get_ai_service", lambda: _StubAI({}))

    results = await mon.check_company_news(db_session, sample_company)
    assert len(results) == 1
    assert results[0]["actionable"] is False
    assert results[0]["outreach_email"] is None


# ---------------------------------------------------------------------------
# parse_blog_articles (pure)
# ---------------------------------------------------------------------------

_BLOG_HTML = """
<html><body>
  <nav><a href="/">Home</a><a href="/about">About</a></nav>
  <div class="posts">
    <a href="/news/expansion-2026">Acme opens new plant in Ohio</a>
    <a href="/news/iso-cert">Acme earns ISO 9001 recertification this year</a>
    <a href="/news/read-more">Read more</a>
    <a href="/news/expansion-2026">Acme opens new plant in Ohio</a>
  </div>
</body></html>
"""


class TestParseBlogArticles:
    def test_extracts_headlines(self):
        arts = parse_blog_articles(_BLOG_HTML, "https://acme.com/news")
        titles = [a["title"] for a in arts]
        assert "Acme opens new plant in Ohio" in titles
        assert "Acme earns ISO 9001 recertification this year" in titles

    def test_skips_nav_and_short_links(self):
        arts = parse_blog_articles(_BLOG_HTML, "https://acme.com/news")
        titles = [a["title"].lower() for a in arts]
        assert "home" not in titles
        assert "about" not in titles
        assert "read more" not in titles

    def test_dedupes_titles(self):
        arts = parse_blog_articles(_BLOG_HTML, "https://acme.com/news")
        titles = [a["title"] for a in arts]
        assert titles.count("Acme opens new plant in Ohio") == 1

    def test_resolves_relative_links(self):
        arts = parse_blog_articles(_BLOG_HTML, "https://acme.com/news")
        first = next(a for a in arts if "expansion" in a["link"])
        assert first["link"] == "https://acme.com/news/expansion-2026"

    def test_empty_html(self):
        assert parse_blog_articles("", "https://acme.com") == []


# ---------------------------------------------------------------------------
# generate_news_outreach / _fetch_company_blog (async, no DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_news_outreach_ai():
    mon = NewsMonitor()
    ai = _StubAI({"subject": "nice plant", "body": "congrats on the expansion - Suman"})
    company = Company(name="Acme", industry_segment="Metal Stamping", source="test")
    article = {"title": "Acme opens new plant"}
    out = await mon.generate_news_outreach(ai, company, article, {"category": "expansion"})
    assert out["subject"] == "nice plant"
    assert "congrats" in out["body"]
    assert out["referenced_headline"] == "Acme opens new plant"


@pytest.mark.asyncio
async def test_generate_news_outreach_fallback():
    mon = NewsMonitor()
    ai = _StubAI({"error": "boom"})
    company = Company(name="Acme", source="test")
    article = {"title": "Acme raises $5M"}
    out = await mon.generate_news_outreach(ai, company, article)
    assert "Acme" in out["body"]
    assert out["referenced_headline"] == "Acme raises $5M"


@pytest.mark.asyncio
async def test_fetch_company_blog_no_domain():
    mon = NewsMonitor()
    company = Company(name="Acme", source="test")  # no domain/website
    assert await mon._fetch_company_blog(company) == []
