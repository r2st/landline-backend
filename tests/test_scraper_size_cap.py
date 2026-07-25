"""A remote host must not get to decide how much memory we allocate.

``client.get()`` reads a whole response before returning, so a 400 MB "page" —
a misconfigured CMS, a tarball behind a text/html content-type, a deliberate
bomb — becomes a 400 MB allocation in the research worker, which scrapes a
homepage plus sub-pages per company across many companies at once. These tests
pin the byte cap, the truncate-don't-discard behaviour, and the research
agent's own per-field clipping on what it persists.
"""

import httpx
import pytest

from app.services import web_scraper as ws
from app.services.research_agent import (
    _MAX_ABOUT,
    _MAX_DESCRIPTION,
    _MAX_TITLE,
    ResearchAgent,
    _clip,
)


def _transport(body: bytes, content_type: str = "text/html", status: int = 200):
    """A transport that streams ``body`` back in small chunks."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, content=body, headers={"content-type": content_type}
        )

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def allow_all_urls(monkeypatch):
    """Bypass the SSRF resolver — this module is about body size, not hosts."""

    async def _ok(url):
        return url

    monkeypatch.setattr(ws, "_validate_public_url_async", _ok)


@pytest.mark.asyncio
async def test_oversized_body_is_truncated_at_the_cap():
    huge = b"<html><body>" + (b"x" * 5_000_000) + b"</body></html>"
    scraper = ws.WebScraper()

    async with httpx.AsyncClient(transport=_transport(huge)) as client:
        resp = await scraper._safe_get(client, "https://example.com")

    assert len(resp.content) == ws.MAX_PAGE_BYTES
    assert len(huge) > ws.MAX_PAGE_BYTES  # the page really was over the line


@pytest.mark.asyncio
async def test_truncation_keeps_the_head_so_metadata_still_parses():
    """Cutting the tail must not cost us the title/description we came for."""
    head = b"<html><head><title>Acme Tooling</title></head><body>"
    huge = head + (b"filler " * 200_000) + b"</body></html>"
    scraper = ws.WebScraper()

    async with httpx.AsyncClient(transport=_transport(huge)) as client:
        resp = await scraper._safe_get(client, "https://example.com")

    assert b"<title>Acme Tooling</title>" in resp.content


@pytest.mark.asyncio
async def test_small_page_is_returned_whole():
    body = b"<html><head><title>Small</title></head><body>hi</body></html>"
    scraper = ws.WebScraper()

    async with httpx.AsyncClient(transport=_transport(body)) as client:
        resp = await scraper._safe_get(client, "https://example.com")

    assert resp.content == body


@pytest.mark.asyncio
async def test_non_textual_body_is_not_downloaded():
    """A PDF at a company root has nothing for the HTML parser — don't pay for it."""
    pdf = b"%PDF-1.7" + b"\x00" * 100_000
    scraper = ws.WebScraper()

    async with httpx.AsyncClient(transport=_transport(pdf, "application/pdf")) as client:
        resp = await scraper._safe_get(client, "https://example.com/brochure.pdf")

    assert resp.content == b""
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_cap_is_configurable_per_call():
    body = b"y" * 10_000
    scraper = ws.WebScraper()

    async with httpx.AsyncClient(transport=_transport(body)) as client:
        resp = await scraper._safe_get(client, "https://example.com", max_bytes=1_000)

    assert len(resp.content) == 1_000


@pytest.mark.asyncio
async def test_scrape_company_survives_an_oversized_homepage(monkeypatch):
    """End to end: a giant page yields a normal result dict, not an OOM.

    The cap is dialled down for the test so the assertion is about the *cap*
    doing its job, not about how long BeautifulSoup takes on half a megabyte of
    filler (which is minutes — a separate cost the cap already bounds).
    """
    monkeypatch.setattr(ws, "MAX_PAGE_BYTES", 4_000)
    huge = (
        b"<html><head><title>Big Co</title>"
        b'<meta name="description" content="we make things">'
        b"</head><body>" + (b"z" * 2_000_000) + b"</body></html>"
    )
    scraper = ws.WebScraper()
    transport = _transport(huge)

    original = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    import app.services.web_scraper as mod

    mod.httpx.AsyncClient = _client
    try:
        result = await scraper.scrape_company("example.com")
    finally:
        mod.httpx.AsyncClient = original

    assert result.get("error") is None
    assert result["title"] == "Big Co"
    assert result["description"] == "we make things"


# --- Research agent: what gets persisted --------------------------------------


def test_clip_truncates_long_strings_and_passes_others_through():
    assert _clip("a" * 100, 10) == "a" * 10
    assert _clip("short", 10) == "short"
    assert _clip(None, 10) is None
    assert _clip(["a"], 10) == ["a"]


@pytest.mark.asyncio
async def test_research_signals_clip_hostile_scrape_fields(monkeypatch):
    """Every scraped field lands in raw_signals AND the prompt — clip both."""

    class _Scraper:
        async def scrape_company(self, domain):
            return {
                "title": "T" * 5_000,
                "description": "D" * 50_000,
                "about": "A" * 500_000,
                "technologies": [f"tech{i}" for i in range(200)],
                "social_links": {f"net{i}": "u" * 5_000 for i in range(50)},
            }

    class _Company:
        id = "00000000-0000-0000-0000-000000000001"
        name = "Acme"
        domain = "acme.com"
        website = "https://acme.com"
        industry_segment = None
        naics_description = None
        employee_count = None
        city = state_province = country = None
        pipeline_stage = "new_lead"
        tags = []

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class _DB:
        async def execute(self, *a, **k):
            return _Result()

    agent = ResearchAgent.__new__(ResearchAgent)
    agent.scraper = _Scraper()

    signals = await agent._gather_signals(_DB(), _Company())
    scrape = signals["website_scrape"]

    assert len(scrape["title"]) == _MAX_TITLE
    assert len(scrape["description"]) == _MAX_DESCRIPTION
    assert len(scrape["about"]) == _MAX_ABOUT
    assert len(scrape["technologies"]) <= 25
    assert len(scrape["social_links"]) <= 10
    assert all(len(v) <= 500 for v in scrape["social_links"].values())
