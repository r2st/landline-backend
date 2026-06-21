"""Company news monitoring — finds news about tracked companies."""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.company import Company
from app.models.company_news import CompanyNews
from app.services.ai_service import get_ai_service

logger = logging.getLogger(__name__)

# Free news API — Google News RSS feed (no API key needed)
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"


class NewsMonitor:
    """Monitors companies for relevant news using free sources."""

    async def check_company_news(self, db: AsyncSession, company: Company) -> list[dict]:
        """Check for recent news about a specific company."""
        if not company.name:
            return []

        query = company.name.replace(" ", "+")
        if company.industry_segment:
            query += f"+{company.industry_segment.replace(' ', '+')}"

        articles = await self._fetch_google_news(query)
        
        if not articles:
            return []

        # Filter and analyze with AI
        ai = get_ai_service()
        results = []

        for article in articles[:5]:  # Max 5 articles per company per check
            # Check if we already have this article
            existing = await db.execute(
                select(CompanyNews)
                .where(CompanyNews.company_id == company.id)
                .where(CompanyNews.title == article["title"])
            )
            if existing.scalar_one_or_none():
                continue

            # Analyze relevance with AI
            analysis = await self._analyze_article(ai, article, company)
            
            if analysis.get("relevance_score", 0) < 3:
                continue  # Skip low-relevance articles

            news = CompanyNews(
                company_id=company.id,
                title=article["title"],
                summary=analysis.get("summary", article.get("description", "")),
                url=article.get("link", ""),
                source=article.get("source", "Google News"),
                published_at=article.get("published_at"),
                category=analysis.get("category", "general"),
                relevance_score=analysis.get("relevance_score", 5),
                ai_analysis=analysis,
                is_actionable=analysis.get("is_actionable", False),
            )
            db.add(news)
            results.append({
                "title": article["title"],
                "category": analysis.get("category"),
                "relevance": analysis.get("relevance_score"),
                "actionable": analysis.get("is_actionable", False),
                "suggested_action": analysis.get("suggested_action", ""),
            })

        if results:
            await db.commit()

        return results

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

            message = ai.client.messages.create(
                model=ai.model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = message.content[0].text.strip()
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
