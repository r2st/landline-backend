"""An RSS ``pubDate`` with no usable timezone must not take the whole feed.

``parsedate_to_datetime`` returns a **naive** datetime for the two date forms
RFC 5322 says carry no usable zone: a missing offset, and the literal ``-0000``
that means "time known, zone deliberately withheld". Both are ordinary in real
syndication output.

Every one of the three Google-News readers then compares that value against an
aware cutoff to decide whether the article is fresh, and a naive/aware
comparison raises ``TypeError``. The three failures look different from the
outside and are all silent:

* ``news_monitor._fetch_google_news`` — the comparison is inside the enclosing
  ``try``, so the exception returns ``[]``. One badly-stamped article discards
  the **whole feed**, and the company simply has no news, run after run.
* ``intent_monitor._fetch_news`` — the comparison is *outside* the try, so it
  propagates through ``scan_company`` to the per-company handler in
  ``scan_all_companies``. That company's intent scan produces nothing, for every
  one of the five source queries.
* ``competitive_intelligence._fetch_news`` — same shape, eaten by the
  per-competitor handler.

``social_listening_agent._parse_rss`` already normalises to UTC; these did not.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.competitive_intelligence import CompetitiveIntelligence
from app.services.intent_monitor import IntentMonitor, parse_pub_date
from app.services.news_monitor import NewsMonitor


def _feed(pub_date: str) -> str:
    return f"""<?xml version="1.0"?>
<rss><channel>
  <item>
    <title>Acme Stamping opens a second plant</title>
    <link>https://example.test/a</link>
    <description>Expansion</description>
    <source>Trade Weekly</source>
    <pubDate>{pub_date}</pubDate>
  </item>
</channel></rss>"""


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.status_code = 200
        self.text = text


class _FakeClient:
    """Stands in for ``httpx.AsyncClient`` as an async context manager."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        return _FakeResponse(self._text)


# ``-0000`` is the one that matters most: it is well-formed, extremely common,
# and the *only* thing wrong with it is that Python hands it back naive.
ZONELESS_DATES = ("-0000", "")


@pytest.mark.parametrize("suffix", ZONELESS_DATES)
def test_parse_pub_date_returns_an_aware_datetime(suffix):
    stamp = f"Mon, 03 Aug 2026 10:00:00 {suffix}".strip()
    parsed = parse_pub_date(stamp)
    assert parsed is not None
    assert parsed.tzinfo is not None, f"{stamp!r} parsed naive"
    # Comparing against an aware cutoff is the operation every caller performs.
    assert parsed < datetime.now(timezone.utc) + timedelta(days=1)


def test_parse_pub_date_preserves_an_explicit_offset():
    parsed = parse_pub_date("Mon, 03 Aug 2026 10:00:00 +0200")
    assert parsed == datetime(2026, 8, 3, 10, 0, tzinfo=timezone(timedelta(hours=2)))


@pytest.mark.parametrize("raw", [None, "", "not a date at all", "Mon, 99 Xxx"])
def test_parse_pub_date_returns_none_rather_than_raising(raw):
    assert parse_pub_date(raw) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ZONELESS_DATES)
async def test_intent_monitor_keeps_a_zoneless_article(monkeypatch, suffix):
    """The whole company scan used to die here, not just this article."""
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%a, %d %b %Y %H:%M:%S "
    ) + suffix
    monkeypatch.setattr(
        "app.services.intent_monitor.httpx.AsyncClient",
        lambda *a, **k: _FakeClient(_feed(recent.strip())),
    )
    articles = await IntentMonitor()._fetch_news("acme")
    assert [a["title"] for a in articles] == ["Acme Stamping opens a second plant"]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ZONELESS_DATES)
async def test_competitive_intelligence_keeps_a_zoneless_article(monkeypatch, suffix):
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%a, %d %b %Y %H:%M:%S "
    ) + suffix
    monkeypatch.setattr(
        "app.services.competitive_intelligence.httpx.AsyncClient",
        lambda *a, **k: _FakeClient(_feed(recent.strip())),
    )
    articles = await CompetitiveIntelligence()._fetch_news("Rival Corp")
    assert [a["title"] for a in articles] == ["Acme Stamping opens a second plant"]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ZONELESS_DATES)
async def test_news_monitor_keeps_a_zoneless_article(monkeypatch, suffix):
    """Here the exception returned ``[]`` — the feed, not the item, was lost."""
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%a, %d %b %Y %H:%M:%S "
    ) + suffix
    monkeypatch.setattr(
        "app.services.news_monitor.httpx.AsyncClient",
        lambda *a, **k: _FakeClient(_feed(recent.strip())),
    )
    articles = await NewsMonitor()._fetch_google_news("acme")
    assert [a["title"] for a in articles] == ["Acme Stamping opens a second plant"]


@pytest.mark.asyncio
async def test_stale_zoneless_article_is_still_dropped(monkeypatch):
    """Normalising must not turn the freshness filter off.

    The fix is about *which timezone* the comparison runs in, not about whether
    it happens. An article older than the window is still excluded — otherwise
    "don't crash on a naive date" would quietly become "treat every undated old
    article as fresh", which is the same bug pointing the other way.
    """
    stale = (datetime.now(timezone.utc) - timedelta(days=400)).strftime(
        "%a, %d %b %Y %H:%M:%S -0000"
    )
    monkeypatch.setattr(
        "app.services.intent_monitor.httpx.AsyncClient",
        lambda *a, **k: _FakeClient(_feed(stale)),
    )
    assert await IntentMonitor()._fetch_news("acme") == []
