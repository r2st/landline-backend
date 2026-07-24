"""Smart meeting scheduler & pre-meeting briefings (feature 2.5).

Extends the ``call_briefing`` pattern from a single-contact cold-call sheet to a
full pre-meeting brief that synthesizes everything we know about a company into
one page:

* **background** — company facts + the latest AI research brief.
* **pain_points** — from the research brief, else the industry vertical defaults.
* **objection_prep** — likely objections for this industry/size + how to handle.
* **competitive_positioning** — any competitors mentioned at this company.
* **recommended_agenda** / **talking_points** — a suggested flow for the meeting.

It also proposes meeting times (within the configured send window) and, after a
meeting is logged, runs a post-meeting analysis producing a summary + follow-up
actions. Every AI step degrades to a deterministic heuristic so the feature works
with no Anthropic key (and in tests).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.company_news import CompanyNews
from app.models.company_research import CompanyResearch
from app.models.competitor import Competitor, CompetitorMention
from app.models.contact import Contact
from app.models.interaction import Interaction
from app.models.meeting import MeetingBrief
from app.services import industry_verticals as iv
from app.services.llm_providers import ChainClient, any_llm_available, chain_client

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"


class MeetingIntelligence:
    """Pre-meeting briefing generation, time proposal, post-meeting analysis."""

    def __init__(self) -> None:
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

    # ------------------------------------------------------------------ #
    # Briefing generation
    # ------------------------------------------------------------------ #

    async def generate_briefing(
        self,
        db: AsyncSession,
        company_id: UUID,
        *,
        contact_ids: Optional[list[UUID]] = None,
        use_ai: bool = True,
    ) -> dict:
        """Assemble the briefing_content dict for a company/meeting."""
        company = (
            await db.execute(select(Company).where(Company.id == company_id))
        ).scalar_one_or_none()
        if company is None:
            raise ValueError("Company not found")

        vertical = iv.resolve_vertical_or_default(
            company.industry_segment or "", company.name or ""
        )

        # Latest research brief (feature 1.4), if any.
        research = (
            await db.execute(
                select(CompanyResearch)
                .where(CompanyResearch.company_id == company_id)
                .order_by(CompanyResearch.researched_at.desc())
            )
        ).scalars().first()

        # Recent interactions.
        interactions = (
            await db.execute(
                select(Interaction)
                .where(Interaction.company_id == company_id)
                .order_by(Interaction.occurred_at.desc())
                .limit(5)
            )
        ).scalars().all()

        # Recent news.
        news = (
            await db.execute(
                select(CompanyNews)
                .where(CompanyNews.company_id == company_id)
                .order_by(CompanyNews.discovered_at.desc())
                .limit(3)
            )
        ).scalars().all()

        # Competitors mentioned at this company.
        comp_rows = (
            await db.execute(
                select(Competitor.name)
                .join(CompetitorMention, CompetitorMention.competitor_id == Competitor.id)
                .where(CompetitorMention.company_id == company_id)
                .distinct()
            )
        ).all()
        competitors = [name for (name,) in comp_rows]

        # Contacts on the invite.
        contacts: list[Contact] = []
        if contact_ids:
            contacts = (
                await db.execute(select(Contact).where(Contact.id.in_(contact_ids)))
            ).scalars().all()

        pain_points = list(research.pain_points) if (research and research.pain_points) else list(vertical.pain_points)

        # Deterministic baseline briefing.
        content = {
            "background": self._background(company, research, contacts),
            "pain_points": pain_points[:6],
            "objection_prep": self._default_objection_prep(company),
            "competitive_positioning": self._competitive_positioning(competitors),
            "recommended_agenda": self._default_agenda(),
            "talking_points": self._default_talking_points(vertical, news),
            "recent_news": [
                {"title": n.title, "category": n.category, "url": n.url} for n in news
            ],
            "recent_interactions": [
                {
                    "type": it.interaction_type,
                    "subject": it.subject,
                    "occurred_at": it.occurred_at.isoformat() if it.occurred_at else None,
                }
                for it in interactions
            ],
        }

        if use_ai and self.available:
            enriched = await self._ai_briefing(company, vertical, research, competitors, news, contacts)
            for key in ("objection_prep", "competitive_positioning", "recommended_agenda", "talking_points"):
                if enriched.get(key):
                    content[key] = enriched[key]
            if enriched.get("background"):
                content["background"] = enriched["background"]
        return content

    @staticmethod
    def _background(company: Company, research: Optional[CompanyResearch], contacts) -> str:
        bits = [f"{company.name}"]
        if company.industry_segment:
            bits.append(f"in {company.industry_segment}")
        if company.employee_count:
            bits.append(f"~{company.employee_count} employees")
        loc = ", ".join(p for p in (company.city, company.state_province) if p)
        if loc:
            bits.append(f"based in {loc}")
        line = " · ".join(bits)
        if research and research.research_brief:
            line += f"\n\nResearch: {research.research_brief[:600]}"
        if contacts:
            who = ", ".join(f"{c.full_name} ({c.title or 'unknown role'})" for c in contacts)
            line += f"\n\nAttendees: {who}"
        return line

    @staticmethod
    def _default_objection_prep(company: Company) -> list[dict]:
        size = company.employee_count or 0
        prep = [
            {
                "objection": "We already have a quality process / system.",
                "response": "Great — this augments it. We catch drift in real time before parts "
                            "go out of spec, so your existing checks catch less at final inspection.",
            },
            {
                "objection": "We're too busy to add another tool right now.",
                "response": "The 48-hour pilot needs almost nothing from your team — we hook into "
                            "existing machine signals and show you what it flags. No workflow change to evaluate.",
            },
        ]
        if size and size < 100:
            prep.append({
                "objection": "We're too small for something like this.",
                "response": "Smaller shops feel scrap and rework hardest per part. The pilot shows "
                            "payback on your actual numbers before any commitment.",
            })
        return prep

    @staticmethod
    def _competitive_positioning(competitors: list[str]) -> dict:
        if not competitors:
            return {
                "known_competitors": [],
                "positioning": "No competitor has surfaced at this account yet — lead with the "
                               "real-time drift detection and the free 48-hour pilot as differentiators.",
            }
        return {
            "known_competitors": competitors,
            "positioning": f"This account has referenced {', '.join(competitors)}. Ask what they "
                           "like/dislike, then position Apprend's real-time drift detection and live "
                           "demo dashboard against a static/after-the-fact approach.",
        }

    @staticmethod
    def _default_agenda() -> list[str]:
        return [
            "Rapport + confirm who's in the room and their priorities (5 min)",
            "Their current quality process and where scrap/rework hurts most (10 min)",
            "Show the live demo dashboard against a relevant scenario (10 min)",
            "Scope a free 48-hour pilot on one line/press (5 min)",
            "Agree next step + owners (5 min)",
        ]

    @staticmethod
    def _default_talking_points(vertical, news) -> list[str]:
        points = [
            f"Anchor on {vertical.name} pain: {vertical.pain_points[0] if vertical.pain_points else 'quality drift and scrap'}.",
            "Offer the free 48-hour pilot as a no-risk way to see it on their own line.",
        ]
        if news:
            points.append(f"Reference recent news: \"{news[0].title[:120]}\".")
        return points

    async def _ai_briefing(self, company, vertical, research, competitors, news, contacts) -> dict:
        research_line = (research.research_brief[:800] if research and research.research_brief else "none on file")
        news_line = "; ".join(n.title for n in news) or "none"
        who = ", ".join(f"{c.full_name} ({c.title or '?'})" for c in contacts) or "unknown attendees"
        prompt = f"""You prep a sales rep for a meeting with a manufacturing prospect for Apprend
Technologies (real-time quality-monitoring software; free 48-hour pilot; live demo dashboard).

COMPANY: {company.name} | industry: {company.industry_segment or vertical.name} | employees: {company.employee_count or "?"}
ATTENDEES: {who}
RESEARCH: {research_line}
RECENT NEWS: {news_line}
COMPETITORS SEEN AT ACCOUNT: {competitors or "none"}
INDUSTRY PAIN POINTS: {list(vertical.pain_points)[:5]}

Produce a tight, specific pre-meeting brief. No fluff — a rep reads this 5 minutes before the call.

Return ONLY valid JSON:
{{
  "background": "2-3 sentence situation summary",
  "objection_prep": [{{"objection": "...", "response": "..."}}],
  "competitive_positioning": {{"known_competitors": [...], "positioning": "..."}},
  "recommended_agenda": ["agenda item with time", "..."],
  "talking_points": ["specific point", "..."]
}}"""
        try:
            message = await self._get_client().messages.create(
                model=self.model,
                max_tokens=1400,
                messages=[{"role": "user", "content": prompt}],
            )
            return self._parse_json(message.content[0].text)
        except Exception as e:  # noqa: BLE001 — best-effort; heuristic already built
            logger.warning("Meeting briefing AI generation failed: %s", e)
            return {}

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    async def build_and_store_brief(
        self,
        db: AsyncSession,
        company_id: UUID,
        *,
        contact_ids: Optional[list[UUID]] = None,
        meeting_at: Optional[datetime] = None,
        title: Optional[str] = None,
        user_id: Optional[UUID] = None,
        use_ai: bool = True,
    ) -> MeetingBrief:
        content = await self.generate_briefing(
            db, company_id, contact_ids=contact_ids, use_ai=use_ai
        )
        brief = MeetingBrief(
            company_id=company_id,
            user_id=user_id,
            contact_ids=[str(c) for c in (contact_ids or [])],
            title=title or "Pre-meeting briefing",
            meeting_at=meeting_at,
            briefing_content=content,
            status="ready",
            model_used=self.model if (use_ai and self.available) else None,
        )
        db.add(brief)
        await db.flush()
        return brief

    # ------------------------------------------------------------------ #
    # Time proposal
    # ------------------------------------------------------------------ #

    def propose_meeting_times(self, *, count: int = 3, now: Optional[datetime] = None) -> dict:
        """Propose the next ``count`` business-day slots within the send window.

        Deterministic (no AI): returns ISO timestamps plus the configured
        scheduling link, if any, so an auto-reply can offer concrete options.
        """
        settings = get_settings()
        now = now or datetime.now(timezone.utc)
        start_hour = getattr(settings, "send_window_start_hour", 9)
        end_hour = getattr(settings, "send_window_end_hour", 17)
        # Two candidate slots per day: near the start and mid-window.
        slot_hours = [max(start_hour + 1, 10), min(end_hour - 3, 14)]

        slots: list[str] = []
        day = now.date()
        while len(slots) < count:
            day = day + timedelta(days=1)
            if day.weekday() >= 5:  # Sat/Sun
                continue
            for h in slot_hours:
                if len(slots) >= count:
                    break
                dt = datetime.combine(day, time(hour=h), tzinfo=timezone.utc)
                slots.append(dt.isoformat())
        return {
            "proposed_slots": slots,
            "scheduling_link": getattr(settings, "scheduling_link", "") or None,
            "timezone": "UTC",
        }

    # ------------------------------------------------------------------ #
    # Post-meeting analysis
    # ------------------------------------------------------------------ #

    async def analyze_post_meeting(
        self,
        db: AsyncSession,
        *,
        company_id: UUID,
        notes: str,
        brief: Optional[MeetingBrief] = None,
        use_ai: bool = True,
    ) -> dict:
        """Summarize a logged meeting and produce follow-up action items."""
        analysis = self._heuristic_post_meeting(notes)
        if use_ai and self.available and notes.strip():
            ai = await self._ai_post_meeting(notes)
            if ai:
                analysis = ai

        if brief is not None:
            brief.post_meeting_analysis = analysis
            brief.status = "completed"
            await db.flush()
        return analysis

    @staticmethod
    def _heuristic_post_meeting(notes: str) -> dict:
        text = (notes or "").lower()
        positive = any(w in text for w in ("interested", "pilot", "next step", "demo", "follow up", "send"))
        negative = any(w in text for w in ("not interested", "no budget", "pass", "later", "not now"))
        sentiment = "positive" if positive and not negative else ("negative" if negative else "neutral")
        actions = []
        if "pilot" in text:
            actions.append("Send the 48-hour pilot scope and confirm the line/press to run it on.")
        if "pricing" in text or "price" in text or "quote" in text:
            actions.append("Prepare and send pricing / ROI math on their numbers.")
        if not actions:
            actions.append("Send a recap email with agreed next steps and a proposed follow-up date.")
        return {
            "summary": (notes or "").strip()[:400] or "No notes captured.",
            "sentiment": sentiment,
            "action_items": actions,
            "next_steps": actions[:1],
        }

    async def _ai_post_meeting(self, notes: str) -> dict:
        prompt = f"""Analyze these post-meeting notes from a sales meeting (Apprend Technologies,
manufacturing quality software). Extract a concise summary, overall sentiment,
and concrete follow-up actions.

NOTES:
{notes[:2000]}

Return ONLY valid JSON:
{{
  "summary": "2-3 sentence summary",
  "sentiment": "positive|neutral|negative",
  "action_items": ["specific follow-up action", "..."],
  "next_steps": ["the single most important next step"]
}}"""
        try:
            message = await self._get_client().messages.create(
                model=self.model,
                max_tokens=700,
                messages=[{"role": "user", "content": prompt}],
            )
            return self._parse_json(message.content[0].text)
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.warning("Post-meeting AI analysis failed: %s", e)
            return {}


_mi: MeetingIntelligence | None = None


def get_meeting_intelligence() -> MeetingIntelligence:
    global _mi
    if _mi is None:
        _mi = MeetingIntelligence()
    return _mi
