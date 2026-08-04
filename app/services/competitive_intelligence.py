"""Competitive intelligence — battle cards, mention detection, competitor news.

Three capabilities:

1. **Battle-card generation** — Claude produces per-competitor positioning:
   strengths, weaknesses, how-we-win, landmine questions, and a talk track.
2. **Mention detection** — scans free text (email replies, call notes, news) for
   any tracked competitor's name/aliases and records a ``CompetitorMention``.
3. **Competitor news monitoring** — the same key-less Google News RSS approach
   used elsewhere, but querying for the competitors themselves.

This service is intentionally cheap on the detection path (string matching, no
LLM call per email) so it can run inline during reply analysis; the LLM is used
only for battle-card generation and optional stance classification.
"""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.competitor import Competitor, CompetitorMention
from app.services.intent_monitor import parse_pub_date
from app.services.llm_providers import ChainClient, any_llm_available, chain_client

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

# Words near a competitor name that hint at the prospect's stance.
_USING_HINTS = ("we use", "using", "currently use", "already have", "already use", "switched to", "running", "we're on", "we are on")
_EVALUATING_HINTS = ("evaluating", "looking at", "considering", "comparing", "quote from", "quoted us", "demo with", "talking to")
_REJECTED_HINTS = ("dropped", "left", "moving off", "unhappy with", "problems with", "replacing", "getting rid of")


class CompetitiveIntelligence:
    """Battle cards, competitor mention detection, and competitor news."""

    def __init__(self):
        settings = get_settings()
        self.model = _MODEL
        self._api_key = settings.anthropic_api_key
        self._client: ChainClient | None = None

    @property
    def available(self) -> bool:
        # Any chain provider counts, not just the Anthropic key: this service is
        # chain-backed now, and gating on one credential would keep it dark on a
        # deployment that carries only a Gemini or Groq key.
        return any_llm_available()

    def _get_client(self) -> ChainClient:
        if self._client is None:
            self._client = chain_client(self._api_key)
        return self._client

    def _parse_json(self, text: str) -> dict:
        import json
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end > start:
                return json.loads(text[start:end + 1])
            raise

    # ---------------------------------------------------------------- #
    # Battle-card generation
    # ---------------------------------------------------------------- #

    async def generate_battle_card(
        self,
        competitor_name: str,
        domain: Optional[str] = None,
        products: Optional[list] = None,
        notes: Optional[str] = None,
    ) -> dict:
        """Generate battle-card content for a competitor via Claude.

        Returns ``{"strengths": [...], "weaknesses": [...], "products": [...],
        "battle_card": {positioning, how_we_win, landmine_questions, talk_track}}``.
        Never raises — returns ``{"error": ...}`` on failure so callers can fall
        back to whatever they have on file.
        """
        if not self.available:
            return {"error": "AI unavailable (no API key)"}

        prod_line = f"Known products: {', '.join(products)}" if products else ""
        notes_line = f"What we know: {notes}" if notes else ""
        prompt = f"""You are a competitive-intelligence analyst for Apprend Technologies.
Apprend sells an AI quality-management platform for mid-size manufacturers —
real-time monitoring of machine/press/line signals that flags quality drift
before parts go out of spec, with a free 48-hour pilot and a live demo dashboard.

Build a competitive battle card against this competitor:
COMPETITOR: {competitor_name}
{f'DOMAIN: {domain}' if domain else ''}
{prod_line}
{notes_line}

Be concrete and honest — a rep will use this live against a prospect who is
evaluating both. No marketing fluff. If you're unsure of a fact, frame it as a
question the rep should ask rather than stating it.

Return ONLY valid JSON:
{{
  "products": ["their main products/modules"],
  "strengths": ["where they genuinely win — be honest"],
  "weaknesses": ["where they fall short vs Apprend"],
  "battle_card": {{
    "positioning": "one-paragraph how to position Apprend against them",
    "how_we_win": ["specific angles that beat them"],
    "landmine_questions": ["questions to ask the prospect that expose their weaknesses"],
    "talk_track": "what the rep says when the prospect mentions this competitor"
  }}
}}"""
        try:
            message = await self._get_client().messages.create(
                model=self.model,
                max_tokens=1400,
                messages=[{"role": "user", "content": prompt}],
            )
            result = self._parse_json(message.content[0].text)
            return {
                "products": result.get("products", products or []),
                "strengths": result.get("strengths", []),
                "weaknesses": result.get("weaknesses", []),
                "battle_card": result.get("battle_card", {}),
                "model_used": self.model,
            }
        except Exception as e:
            logger.error("Battle card generation failed for %s: %s", competitor_name, str(e))
            return {"error": str(e)}

    async def refresh_battle_card(self, db: AsyncSession, competitor: Competitor) -> Competitor:
        """Generate and persist battle-card content onto a Competitor row."""
        result = await self.generate_battle_card(
            competitor.name,
            competitor.domain,
            list(competitor.products or []),
            competitor.notes,
        )
        if not result.get("error"):
            if result.get("products"):
                competitor.products = result["products"]
            competitor.strengths = result.get("strengths", competitor.strengths)
            competitor.weaknesses = result.get("weaknesses", competitor.weaknesses)
            competitor.battle_card = result.get("battle_card", competitor.battle_card)
            competitor.last_updated = datetime.now(timezone.utc)
            await db.flush()
        return competitor

    # ---------------------------------------------------------------- #
    # Mention detection
    # ---------------------------------------------------------------- #

    @staticmethod
    def _find_match(text_lower: str, competitor: Competitor) -> Optional[str]:
        """Return the alias that matched in ``text_lower`` (word-boundary), or None."""
        for name in competitor.all_names():
            if not name:
                continue
            pattern = r"\b" + re.escape(name.lower()) + r"\b"
            if re.search(pattern, text_lower):
                return name
        return None

    @staticmethod
    def _infer_stance(text_lower: str, matched: str) -> Optional[str]:
        """Cheap keyword heuristic for the prospect's stance toward a competitor."""
        idx = text_lower.find(matched.lower())
        window = text_lower[max(0, idx - 60): idx + 60]
        if any(h in window for h in _REJECTED_HINTS):
            return "rejected"
        if any(h in window for h in _USING_HINTS):
            return "using"
        if any(h in window for h in _EVALUATING_HINTS):
            return "evaluating"
        return "neutral"

    async def detect_mentions_in_text(
        self,
        db: AsyncSession,
        text: str,
        source: str = "email",
        company_id: Optional[UUID] = None,
        contact_id: Optional[UUID] = None,
        deal_id: Optional[UUID] = None,
        persist: bool = True,
    ) -> list[dict]:
        """Scan text for tracked competitors; record + return any mentions.

        Returns a list of ``{competitor_id, competitor_name, matched, sentiment,
        context}`` dicts. When ``persist`` is True, a ``CompetitorMention`` row is
        created for each (used by the sentiment pipeline and manual detection).
        """
        if not text or not text.strip():
            return []
        text_lower = text.lower()

        competitors = (
            await db.execute(select(Competitor).where(Competitor.is_active.is_(True)))
        ).scalars().all()

        found = []
        for comp in competitors:
            matched = self._find_match(text_lower, comp)
            if not matched:
                continue
            idx = text_lower.find(matched.lower())
            context = text[max(0, idx - 80): idx + 80].strip()
            stance = self._infer_stance(text_lower, matched)
            if persist:
                mention = CompetitorMention(
                    competitor_id=comp.id,
                    company_id=company_id,
                    contact_id=contact_id,
                    deal_id=deal_id,
                    source=source,
                    context=context,
                    sentiment=stance,
                )
                db.add(mention)
            found.append({
                "competitor_id": str(comp.id),
                "competitor_name": comp.name,
                "matched": matched,
                "sentiment": stance,
                "context": context,
            })
        if found and persist:
            await db.flush()
        return found

    # ---------------------------------------------------------------- #
    # Competitor news monitoring
    # ---------------------------------------------------------------- #

    async def monitor_competitor_news(self, db: AsyncSession, limit: int = 10) -> list[dict]:
        """Check Google News for each active competitor; record news mentions."""
        competitors = (
            await db.execute(
                select(Competitor).where(Competitor.is_active.is_(True)).limit(limit)
            )
        ).scalars().all()

        results = []
        for comp in competitors:
            try:
                articles = await self._fetch_news(comp.name)
                new_mentions = 0
                for article in articles[:3]:
                    title = article["title"]
                    # Dedup on (competitor, news title) using the context field.
                    exists = await db.execute(
                        select(CompetitorMention.id)
                        .where(CompetitorMention.competitor_id == comp.id)
                        .where(CompetitorMention.source == "news")
                        .where(CompetitorMention.context == title)
                        .limit(1)
                    )
                    # Best-effort dedup on an unconstrained triple; two stored
                    # copies must skip the article, not abort the sweep.
                    if exists.scalars().first():
                        continue
                    db.add(CompetitorMention(
                        competitor_id=comp.id,
                        source="news",
                        context=title,
                        sentiment="neutral",
                    ))
                    new_mentions += 1
                if new_mentions:
                    await db.commit()
                    results.append({"competitor": comp.name, "news_mentions": new_mentions})
            except Exception as e:
                logger.error("Competitor news check failed for %s: %s", comp.name, str(e))
        return results

    async def _fetch_news(self, competitor_name: str) -> list[dict]:
        query = f'"{competitor_name}" (manufacturing OR software OR quality OR launch OR pricing)'.replace(" ", "+")
        url = GOOGLE_NEWS_RSS.format(query=query)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return []
                root = ET.fromstring(resp.text)
        except Exception as e:
            logger.error("Competitor news fetch failed: %s", str(e))
            return []

        articles = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=45)
        for item in root.findall(".//item")[:8]:
            title = item.findtext("title", "")
            if not title:
                continue
            # Normalised to UTC — a naive ``pubDate`` (a missing offset, or the
            # literal ``-0000``) compared against an aware cutoff raises
            # ``TypeError``, and this comparison sits outside the try above, so
            # one such article aborted the whole competitor's news check.
            published_at = parse_pub_date(item.findtext("pubDate", ""))
            if published_at and published_at < cutoff:
                continue
            articles.append({"title": title, "link": item.findtext("link", "")})
        return articles


_ci: CompetitiveIntelligence | None = None


def get_competitive_intelligence() -> CompetitiveIntelligence:
    global _ci
    if _ci is None:
        _ci = CompetitiveIntelligence()
    return _ci
