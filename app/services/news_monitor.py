"""Company news monitoring — finds news about tracked companies.

Sources:
- Google News RSS (mentions of the company across the web).
- The company's OWN news/blog/press page (scraped directly — the earliest and
  most reliable signal for expansions, hires, and product launches).

When an article is judged *actionable*, a timely outreach email that references
it is auto-generated and stored on the news row so a rep (or the auto-sender)
can act while the news is fresh.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.company import Company
from app.models.company_news import CompanyNews
from app.services.ai_service import get_ai_service
from app.services.web_scraper import get_web_scraper, UnsafeURLError

logger = logging.getLogger(__name__)

# Free news API — Google News RSS feed (no API key needed)
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

# Common paths a company publishes its own announcements under.
BLOG_PATHS = ("/news", "/blog", "/press", "/newsroom", "/press-releases", "/media")

# Anchor text that signals a nav/utility link rather than an article headline.
_BLOG_SKIP_TEXT = {
    "read more", "more", "next", "previous", "subscribe", "home", "contact",
    "about", "privacy", "terms", "back", "all news", "load more", "view all",
}


def parse_blog_articles(html: str, base_url: str, limit: int = 8) -> list[dict]:
    """Extract candidate announcement headlines from a company blog/news page.

    Heuristic and dependency-light: pull anchors whose text looks like a
    headline (a handful of words, not a nav/utility label) and resolve their
    href against ``base_url``. Returns ``[{"title", "link"}, ...]`` de-duped by
    title, newest-page-order first. Pure so it can be unit-tested without a
    network round-trip.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    seen: set[str] = set()
    out: list[dict] = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not text:
            continue
        words = text.split()
        # Headlines are a phrase, not one word and not a paragraph.
        if not (3 <= len(words) <= 20):
            continue
        if text.lower() in _BLOG_SKIP_TEXT:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": text, "link": urljoin(base_url, a["href"])})
        if len(out) >= limit:
            break
    return out


class NewsMonitor:
    """Monitors companies for relevant news using free sources."""

    async def check_company_news(self, db: AsyncSession, company: Company) -> list[dict]:
        """Check for recent news about a specific company.

        Pulls from Google News RSS *and* the company's own blog/press page, then
        analyses each new article. When an article is actionable, a timely
        outreach email referencing it is generated and stored on the news row.
        """
        if not company.name:
            return []

        query = company.name.replace(" ", "+")
        if company.industry_segment:
            query += f"+{company.industry_segment.replace(' ', '+')}"

        articles = await self._fetch_google_news(query)
        # Also scrape the company's own newsroom/blog — often the earliest signal.
        articles = articles + await self._fetch_company_blog(company)

        if not articles:
            return []

        # Filter and analyze with AI
        ai = get_ai_service()
        results = []

        seen_titles: set[str] = set()
        for article in articles[:8]:  # Max 8 articles per company per check
            title = article.get("title", "")
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)

            # Check if we already have this article
            existing = await db.execute(
                select(CompanyNews)
                .where(CompanyNews.company_id == company.id)
                .where(CompanyNews.title == title)
            )
            if existing.scalar_one_or_none():
                continue

            # Analyze relevance with AI
            analysis = await self._analyze_article(ai, article, company)

            if analysis.get("relevance_score", 0) < 3:
                continue  # Skip low-relevance articles

            is_actionable = analysis.get("is_actionable", False)

            # Actionable news → generate a timely outreach email referencing it.
            outreach = None
            if is_actionable:
                outreach = await self.generate_news_outreach(ai, company, article, analysis)
                analysis = {**analysis, "outreach_email": outreach}

            news = CompanyNews(
                company_id=company.id,
                title=title,
                summary=analysis.get("summary", article.get("description", "")),
                url=article.get("link", ""),
                source=article.get("source", "Google News"),
                published_at=article.get("published_at"),
                category=analysis.get("category", "general"),
                relevance_score=analysis.get("relevance_score", 5),
                ai_analysis=analysis,
                is_actionable=is_actionable,
            )
            db.add(news)
            results.append({
                "title": title,
                "category": analysis.get("category"),
                "relevance": analysis.get("relevance_score"),
                "actionable": is_actionable,
                "suggested_action": analysis.get("suggested_action", ""),
                "outreach_email": outreach,
            })

        if results:
            await db.commit()

        return results

    async def _fetch_company_blog(self, company: Company) -> list[dict]:
        """Scrape the company's own news/blog/press page for announcements.

        Tries a handful of common paths (``/news``, ``/blog``, ``/press``, …)
        under the company domain, SSRF-validated via the shared web scraper.
        Returns ``[{"title", "link", "source"}, ...]``; never raises.
        """
        domain = (company.domain or company.website or "").strip()
        if not domain:
            return []
        if not domain.startswith("http"):
            domain = f"https://{domain}"
        base = f"{urlparse(domain).scheme}://{urlparse(domain).netloc}"
        if not urlparse(base).netloc:
            return []

        scraper = get_web_scraper()
        articles: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
                for path in BLOG_PATHS:
                    url = urljoin(base, path)
                    try:
                        resp = await scraper._safe_get(client, url)
                    except UnsafeURLError as e:
                        logger.warning("Blocked blog scrape of %s: %s", url, e)
                        continue
                    except Exception:
                        continue
                    if resp.status_code != 200:
                        continue
                    parsed = parse_blog_articles(resp.text, url)
                    for a in parsed:
                        articles.append({
                            "title": a["title"],
                            "link": a["link"],
                            "source": f"{company.name} newsroom",
                            "description": "",
                        })
                    if articles:
                        # First page that yields headlines is enough for one check.
                        break
        except Exception as e:  # noqa: BLE001 — best-effort scraping
            logger.warning("Company blog scrape failed for %s: %s", company.name, e)
        return articles[:5]

    async def generate_news_outreach(
        self, ai, company: Company, article: dict, analysis: Optional[dict] = None
    ) -> dict:
        """Draft a short, timely outreach email that references a news item.

        Never raises: returns a deterministic fallback draft if the API is
        unavailable or the response can't be parsed.
        """
        analysis = analysis or {}
        angle = analysis.get("outreach_angle") or analysis.get("suggested_action") or ""
        try:
            prompt = f"""A company we're prospecting just had newsworthy activity. Write a short,
timely cold email from Suman at Apprend Technologies (manufacturing software —
quality monitoring, quoting, shop-floor dashboards) that references the news
naturally as the reason for reaching out.

Company: {company.name}
Industry: {company.industry_segment or 'manufacturing'}
News headline: {article.get('title', '')}
Category: {analysis.get('category', 'general')}
Angle to use: {angle or 'congratulate/observe and connect it to our value'}

Rules:
- Under 90 words. Open with the news, not with us. Contractions fine.
- No buzzwords, no exclamation marks. One ask: a 15-minute call.
- Subject line: short, lowercase.

Return ONLY valid JSON: {{"subject": "...", "body": "..."}}"""
            result = await ai._complete_json(prompt, max_tokens=500)
            if isinstance(result, dict) and result.get("body"):
                return {
                    "subject": result.get("subject") or f"about {company.name}'s news",
                    "body": result["body"],
                    "referenced_headline": article.get("title", ""),
                }
        except Exception as e:  # noqa: BLE001 — never fail the caller
            logger.warning("News outreach generation failed: %s", e)
        return self._fallback_news_outreach(company, article)

    @staticmethod
    def _fallback_news_outreach(company: Company, article: dict) -> dict:
        headline = article.get("title", "your recent news")
        body = (
            f"Saw the news about {company.name} — {headline}. Congrats. "
            "We build quality-monitoring software for shops like yours that catches "
            "drift before parts ship, and moments like this are usually when it pays "
            "to have that visibility in place. Worth a quick 15-minute call to see if "
            "it fits?\n\n- Suman"
        )
        return {
            "subject": f"about {company.name}'s news",
            "body": body,
            "referenced_headline": headline,
        }

    async def check_all_companies(self, db: AsyncSession, limit: int = 10) -> list[dict]:
        """Check news for companies that haven't been checked recently."""
        # Get companies ordered by least recently checked
        # Check companies in engaged, qualified, contacted stages first
        priority_stages = ["engaged", "qualified", "contacted", "scored", "researched", "new"]
        
        result = await db.execute(
            select(Company)
            .where(Company.stage.in_(priority_stages))
            .order_by(Company.name)
            .limit(limit)
        )
        companies = result.scalars().all()

        all_results = []
        for company in companies:
            try:
                news = await self.check_company_news(db, company)
                if news:
                    all_results.append({
                        "company": company.name,
                        "company_id": str(company.id),
                        "news_found": len(news),
                        "articles": news,
                    })
            except Exception as e:
                logger.error("News check failed for %s: %s", company.name, str(e))

        return all_results

    async def _fetch_google_news(self, query: str) -> list[dict]:
        """Fetch news from Google News RSS."""
        import xml.etree.ElementTree as ET

        url = GOOGLE_NEWS_RSS.format(query=query)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return []

                root = ET.fromstring(resp.text)
                articles = []

                for item in root.findall(".//item")[:10]:
                    title = item.findtext("title", "")
                    link = item.findtext("link", "")
                    pub_date = item.findtext("pubDate", "")
                    source = item.findtext("source", "")
                    description = item.findtext("description", "")

                    # Parse date
                    published_at = None
                    if pub_date:
                        try:
                            from email.utils import parsedate_to_datetime
                            published_at = parsedate_to_datetime(pub_date)
                        except Exception:
                            pass

                    # Only include articles from last 30 days
                    if published_at and published_at < datetime.now(timezone.utc) - timedelta(days=30):
                        continue

                    articles.append({
                        "title": title,
                        "link": link,
                        "description": description,
                        "source": source,
                        "published_at": published_at,
                    })

                return articles

        except Exception as e:
            logger.error("Google News fetch failed: %s", str(e))
            return []

    async def _analyze_article(self, ai, article: dict, company: Company) -> dict:
        """Use AI to analyze article relevance to our outreach goals."""
        try:
            import json
            prompt = f"""Analyze this news article about or related to a company we're tracking for B2B outreach.

Company: {company.name}
Industry: {company.industry_segment or 'Unknown'}
Current Stage: {company.stage}

Article Title: {article['title']}
Article Source: {article.get('source', 'Unknown')}
Description: {article.get('description', '')[:300]}

Analyze:
1. Is this actually about this specific company (not a different company with a similar name)?
2. How relevant is this for our B2B outreach? (1-10 scale)
3. What category? (funding, expansion, leadership_change, acquisition, product_launch, partnership, award, hiring, general)
4. Should we take action? (e.g., reach out because of expansion, congratulate on funding, etc.)
5. What should we do?

Return ONLY valid JSON:
{{
  "is_about_company": true/false,
  "relevance_score": 1-10,
  "category": "...",
  "summary": "one-sentence summary",
  "is_actionable": true/false,
  "suggested_action": "what to do (e.g., 'Send congratulations email about expansion')",
  "outreach_angle": "how to reference this in outreach"
}}"""

            # Go through AIService's own completion path, not ``ai.client``.
            # Reaching into ``.client`` grabbed the raw Anthropic SDK handle,
            # which on the OpenRouter/chain backend is constructed with the
            # literal key "unused" — so this call could only ever work on the
            # Anthropic backend, and silently 401'd everywhere else.
            response_text = (await ai.complete_text(prompt, max_tokens=512)).strip()
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]
                response_text = response_text.strip()

            result = json.loads(response_text)
            
            # If it's not actually about this company, mark as low relevance
            if not result.get("is_about_company", True):
                result["relevance_score"] = 1
                result["is_actionable"] = False
            
            return result

        except Exception as e:
            logger.error("AI news analysis failed: %s", str(e))
            return {"relevance_score": 5, "category": "general", "summary": article["title"]}


def get_news_monitor() -> NewsMonitor:
    return NewsMonitor()
