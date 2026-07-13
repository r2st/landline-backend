"""Intent signal monitoring — detects buyer-intent signals for tracked companies.

Scans multiple free sources for events that indicate a company is entering a
buying cycle, then uses Claude to score each raw signal for *relevance* to
Apprend's value proposition and *urgency* (how soon the buying window opens):

* **Job postings** — hiring for Quality Engineer / Plant Manager / Production
  Supervisor roles signals capacity building and imminent quality pain.
* **Funding / M&A** — a raise or acquisition means fresh procurement budget.
* **Technology signals** — ERP implementations, digital-transformation and
  automation mentions signal an active modernization effort.

All sourcing uses the same key-less Google News RSS approach as
``news_monitor.py`` — the AI analysis is the differentiator, the data
acquisition is deliberately cheap. Detected signals are persisted as
``IntentSignal`` rows and fold into lead scoring via ``services/scoring.py``.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.intent_signal import IntentSignal
from app.services.ai_service import get_ai_service

logger = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

# Default buying-window (days) per signal type when the AI can't infer one.
_DEFAULT_WINDOW_DAYS = {
    "hiring": 90,
    "expansion": 120,
    "funding": 90,
    "m&a": 120,
    "technology": 60,
    "leadership": 90,
    "regulatory": 60,
    "general": 45,
}

# (signal_type, query-suffix) — each becomes one targeted news search.
_SOURCE_QUERIES: list[tuple[str, str]] = [
    ("hiring", "hiring OR job OR careers (quality OR \"plant manager\" OR production OR operations)"),
    ("funding", "funding OR raised OR investment OR \"series\" OR acquisition OR merger"),
    ("technology", "ERP OR \"digital transformation\" OR automation OR MES OR \"smart factory\""),
    ("expansion", "expansion OR \"new plant\" OR \"new facility\" OR growth OR opening"),
    ("leadership", "\"new CEO\" OR \"appoints\" OR \"names\" OR \"joins as\" OR \"hires\""),
]


class IntentMonitor:
    """Scans free sources for buyer-intent signals and scores them with Claude."""

    async def scan_company(self, db: AsyncSession, company: Company) -> list[dict]:
        """Detect and persist intent signals for one company. Returns summaries."""
        if not company.name:
            return []

        ai = get_ai_service()
        results: list[dict] = []
        now = datetime.now(timezone.utc)

        for signal_type, suffix in _SOURCE_QUERIES:
            query = f'"{company.name}" {suffix}'.replace(" ", "+")
            articles = await self._fetch_news(query)
            for article in articles[:3]:  # cap per source to bound API spend
                title = article["title"]
                # Dedup: skip a signal we already have for this company + title.
                exists = await db.execute(
                    select(IntentSignal.id)
                    .where(IntentSignal.company_id == company.id)
                    .where(IntentSignal.title == title)
                )
                if exists.scalar_one_or_none():
                    continue

                analysis = await self._analyze_signal(ai, article, company, signal_type)
                relevance = int(analysis.get("relevance_score", 0) or 0)
                if relevance < 30:
                    continue  # not relevant enough to keep

                resolved_type = analysis.get("signal_type", signal_type) or signal_type
                window = analysis.get("buying_window_days")
                if not isinstance(window, int) or window <= 0:
                    window = _DEFAULT_WINDOW_DAYS.get(resolved_type, 60)
                expires_at = now + timedelta(days=window)

                signal = IntentSignal(
                    company_id=company.id,
                    signal_type=resolved_type,
                    source=article.get("source") or "Google News",
                    title=title,
                    description=analysis.get("summary") or article.get("description", "")[:1000],
                    url=article.get("link"),
                    raw_data={"article": {k: v for k, v in article.items() if k != "published_at"}},
                    ai_analysis=analysis,
                    relevance_score=max(0, min(100, relevance)),
                    urgency_score=max(0, min(100, int(analysis.get("urgency_score", 0) or 0))),
                    buying_window_days=window,
                    recommended_action=analysis.get("recommended_action"),
                    outreach_angle=analysis.get("outreach_angle"),
                    detected_at=now,
                    expires_at=expires_at,
                )
                db.add(signal)
                results.append({
                    "signal_type": resolved_type,
                    "title": title,
                    "relevance_score": signal.relevance_score,
                    "urgency_score": signal.urgency_score,
                    "buying_window_days": window,
                    "recommended_action": signal.recommended_action,
                })

        if results:
            await db.commit()
        return results

    async def scan_all_companies(self, db: AsyncSession, limit: int = 10) -> list[dict]:
        """Scan active-pipeline companies for intent signals."""
        # Prioritise companies actually in play — no point scanning disqualified ones.
        active_pipeline = ["new_lead", "researching", "outreach", "engaged", "negotiating"]
        result = await db.execute(
            select(Company)
            .where(Company.pipeline_stage.in_(active_pipeline))
            .order_by(Company.name)
            .limit(limit)
        )
        companies = result.scalars().all()

        all_results = []
        for company in companies:
            try:
                signals = await self.scan_company(db, company)
                if signals:
                    all_results.append({
                        "company": company.name,
                        "company_id": str(company.id),
                        "signals_found": len(signals),
                        "signals": signals,
                    })
            except Exception as e:  # never let one company kill the batch
                logger.error("Intent scan failed for %s: %s", company.name, str(e))
        return all_results

    async def _fetch_news(self, query: str) -> list[dict]:
        """Fetch recent articles from Google News RSS for a query."""
        url = GOOGLE_NEWS_RSS.format(query=query)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return []
                root = ET.fromstring(resp.text)
        except Exception as e:
            logger.error("Intent news fetch failed: %s", str(e))
            return []

        articles = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        for item in root.findall(".//item")[:8]:
            title = item.findtext("title", "")
            if not title:
                continue
            pub_date = item.findtext("pubDate", "")
            published_at = None
            if pub_date:
                try:
                    published_at = parsedate_to_datetime(pub_date)
                except Exception:
                    pass
            if published_at and published_at < cutoff:
                continue  # intent signals must be fresh
            articles.append({
                "title": title,
                "link": item.findtext("link", ""),
                "description": item.findtext("description", ""),
                "source": item.findtext("source", ""),
                "published_at": published_at,
            })
        return articles

    async def _analyze_signal(self, ai, article: dict, company: Company, signal_type: str) -> dict:
        """Use Claude to score a raw signal's relevance and urgency for us."""
        prompt = f"""You are a B2B buyer-intent analyst for Apprend Technologies, which sells
AI quality-management and manufacturing software to mid-size manufacturers
(plastics, CNC machining, injection molding, metal stamping, aerospace/marine parts).

We monitor tracked companies for signals that they are entering a buying cycle.
Analyze this candidate signal and score how much it means a real opportunity.

COMPANY: {company.name}
INDUSTRY: {company.industry_segment or 'Unknown'}
CANDIDATE SIGNAL TYPE: {signal_type}

ARTICLE TITLE: {article['title']}
SOURCE: {article.get('source', 'Unknown')}
DESCRIPTION: {article.get('description', '')[:400]}

Consider:
- Is this actually about THIS company (not a same-named company elsewhere)?
- Relevance: does this signal a problem Apprend solves? "Hiring 50 operators"
  (scaling → quality problems) is high; "hiring a marketing intern" is near zero.
- Urgency: how soon does the buying window open? A new plant manager has ~90 days
  to make changes; a funding round means procurement budget in 60-90 days; a
  finished ERP rollout is late.

Return ONLY valid JSON:
{{
  "is_about_company": true,
  "signal_type": "hiring|expansion|funding|m&a|technology|leadership|regulatory|general",
  "relevance_score": 0-100,
  "urgency_score": 0-100,
  "buying_window_days": <int estimate of days until the window opens/closes>,
  "summary": "one-sentence plain summary of the signal",
  "recommended_action": "specific outreach action to take",
  "outreach_angle": "how to reference this signal in a first-touch message"
}}

If the article is NOT about this company, set is_about_company false and
relevance_score 0."""
        try:
            result = await ai._complete_json(prompt, max_tokens=600)
            if not isinstance(result, dict) or result.get("error"):
                return {"relevance_score": 0}
            if not result.get("is_about_company", True):
                result["relevance_score"] = 0
            return result
        except Exception as e:  # pragma: no cover — defensive
            logger.error("Intent signal analysis failed: %s", str(e))
            return {"relevance_score": 0}


_intent_monitor: IntentMonitor | None = None


def get_intent_monitor() -> IntentMonitor:
    global _intent_monitor
    if _intent_monitor is None:
        _intent_monitor = IntentMonitor()
    return _intent_monitor
