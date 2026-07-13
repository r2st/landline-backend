"""AI Company Research Agent (feature 1.4).

Before any outreach, this agent autonomously researches a company by combining
several existing signal sources — the website scrape, Apollo enrichment already
stored on the company, its contacts, and recent news — and asks Claude to
synthesize them into an *actionable* research brief: named pain points, ranked
outreach angles, and competitive intel.

The brief is persisted as a ``CompanyResearch`` row and consumed by
``ai_email_generator`` and ``ai_script_generator`` so every first touch is
grounded in what actually matters to the company.

Design notes:
- Never raises into the caller. Web scrape / news failures degrade gracefully;
  the synthesis still runs on whatever signals were gathered.
- The Claude call reuses ``AIService`` (AsyncAnthropic + JSON parsing), so
  behaviour matches the rest of the codebase (model ``claude-sonnet-4-6``).
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.company_news import CompanyNews
from app.models.company_research import CompanyResearch
from app.models.contact import Contact
from app.services.ai_service import get_ai_service
from app.services.web_scraper import get_web_scraper

logger = logging.getLogger(__name__)


class ResearchAgent:
    """Orchestrates multi-source research + Claude synthesis for one company."""

    def __init__(self) -> None:
        self.ai = get_ai_service()
        self.scraper = get_web_scraper()

    # ------------------------------------------------------------------
    # Signal gathering
    # ------------------------------------------------------------------

    async def _gather_signals(self, db: AsyncSession, company: Company) -> dict:
        """Collect website, enrichment, contact, and news signals for a company.

        Returns a JSON-serialisable dict. Each source is best-effort — a failure
        in one source never blocks the others or the synthesis.
        """
        signals: dict = {
            "company": {
                "name": company.name,
                "domain": company.domain,
                "website": company.website,
                "industry_segment": company.industry_segment,
                "naics_description": company.naics_description,
                "employee_count": company.employee_count,
                "city": company.city,
                "state_province": company.state_province,
                "country": company.country,
                "pipeline_stage": company.pipeline_stage,
                "tags": list(company.tags or []),
            },
            "website_scrape": None,
            "contacts": [],
            "news": [],
        }
        sources_used: list[str] = []

        # --- Website scrape (best effort) ---
        domain = company.domain or (company.website or "").replace("https://", "").replace("http://", "").strip("/")
        if domain:
            try:
                scrape = await self.scraper.scrape_company(domain)
                if scrape and not scrape.get("error"):
                    signals["website_scrape"] = {
                        "title": scrape.get("title"),
                        "description": scrape.get("description"),
                        "about": scrape.get("about"),
                        "technologies": scrape.get("technologies", []),
                        "social_links": scrape.get("social_links", {}),
                    }
                    sources_used.append("website")
            except Exception as e:  # noqa: BLE001 — never block synthesis
                logger.warning("Research scrape failed for %s: %s", company.name, e)

        # --- Contacts (roles present tell us who to target) ---
        contacts = (
            await db.execute(
                select(Contact).where(Contact.company_id == company.id).limit(25)
            )
        ).scalars().all()
        signals["contacts"] = [
            {"name": c.full_name, "title": c.title, "department": c.department}
            for c in contacts
            if (c.title or c.first_name or c.last_name)
        ]
        if signals["contacts"]:
            sources_used.append("contacts")

        # --- Apollo enrichment already lives on the company row ---
        if company.employee_count or company.industry_segment:
            sources_used.append("apollo")

        # --- Recent news ---
        news_rows = (
            await db.execute(
                select(CompanyNews)
                .where(CompanyNews.company_id == company.id)
                .order_by(CompanyNews.discovered_at.desc())
                .limit(5)
            )
        ).scalars().all()
        signals["news"] = [
            {"title": n.title, "summary": n.summary, "category": n.category}
            for n in news_rows
        ]
        if signals["news"]:
            sources_used.append("news")

        signals["_sources_used"] = sources_used
        return signals

    # ------------------------------------------------------------------
    # Claude synthesis
    # ------------------------------------------------------------------

    async def _synthesize(self, signals: dict) -> dict:
        """Send gathered signals to Claude and parse the structured brief."""
        company = signals["company"]
        scrape = signals.get("website_scrape") or {}
        contacts_summary = (
            "; ".join(
                f"{c['name']} ({c['title']})" for c in signals["contacts"][:10] if c.get("title")
            )
            or "none discovered yet"
        )
        news_summary = (
            "\n".join(f"  - {n['title']}" for n in signals["news"][:5])
            or "none on file"
        )
        about = scrape.get("about") or scrape.get("description") or "(website text unavailable)"

        prompt = f"""You are a B2B sales research analyst for Apprend Technologies. Apprend builds
shop-floor manufacturing software — real-time press/machine monitoring, downtime
alerts, scrap/OEE dashboards, quoting tools, ERP integrations, and quality tracking —
for mid-size manufacturers (roughly 50-500 employees).

Research this company and produce an ACTIONABLE outreach brief. Be specific and
evidence-based: tie every pain point and angle to something concrete in the
signals below. Do NOT invent facts, customers, or metrics. If a signal is thin,
reason from industry norms and say so.

COMPANY:
  Name: {company['name']}
  Industry: {company.get('industry_segment') or company.get('naics_description') or 'unknown'}
  Employees: {company.get('employee_count') or 'unknown'}
  Location: {company.get('city') or ''} {company.get('state_province') or ''} {company.get('country') or ''}
  Website title: {scrape.get('title') or 'n/a'}

WEBSITE TEXT (scraped):
{about[:1500]}

DETECTED TECHNOLOGIES: {', '.join(scrape.get('technologies', [])) or 'none detected'}

KNOWN CONTACTS: {contacts_summary}

RECENT NEWS:
{news_summary}

Return ONLY valid JSON in exactly this shape:
{{
  "research_brief": "one tight paragraph: who they are, what they make, and why they're a fit (or not) for Apprend",
  "pain_points": [
    {{"pain": "specific operational pain", "evidence": "what in the signals implies it", "how_we_help": "the Apprend capability that addresses it"}}
  ],
  "outreach_angles": [
    {{"rank": 1, "angle": "short label", "reasoning": "why this lands for this company", "opening_line": "a first sentence Suman could actually send"}}
  ],
  "competitive_intel": {{"competitors": ["..."], "technologies": ["..."], "notes": "any systems/vendors they mention we compete with or integrate with"}}
}}

Provide 2-4 pain_points and exactly 3 ranked outreach_angles."""

        result = await self.ai._complete_json(prompt, max_tokens=1800)
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def research_company(
        self, db: AsyncSession, company: Company, *, force: bool = False
    ) -> Optional[CompanyResearch]:
        """Research a company and persist a fresh ``CompanyResearch`` brief.

        When a recent non-stale brief already exists and ``force`` is False, the
        existing brief is returned untouched (cheap idempotency for the
        on-create trigger). Returns ``None`` only if synthesis fails outright.
        """
        if not force:
            existing = await get_active_research(db, company.id)
            if existing is not None:
                return existing

        signals = await self._gather_signals(db, company)
        synthesis = await self._synthesize(signals)

        if not synthesis or synthesis.get("error"):
            logger.error(
                "Research synthesis failed for %s: %s",
                company.name, (synthesis or {}).get("error"),
            )
            return None

        # Supersede any prior briefs — mark them stale so consumers pick the new one.
        prior = (
            await db.execute(
                select(CompanyResearch).where(
                    CompanyResearch.company_id == company.id,
                    CompanyResearch.is_stale.is_(False),
                )
            )
        ).scalars().all()
        for row in prior:
            row.is_stale = True

        research = CompanyResearch(
            company_id=company.id,
            research_brief=synthesis.get("research_brief"),
            pain_points=synthesis.get("pain_points", []),
            outreach_angles=synthesis.get("outreach_angles", []),
            competitive_intel=synthesis.get("competitive_intel", {}),
            sources_used=signals.get("_sources_used", []),
            raw_signals={
                "website_scrape": signals.get("website_scrape"),
                "news": signals.get("news"),
                "contacts": signals.get("contacts"),
            },
            model_used=self.ai.model,
            is_stale=False,
            researched_at=datetime.now(timezone.utc),
        )
        db.add(research)
        await db.commit()
        await db.refresh(research)
        logger.info(
            "Researched %s — %d pain points, %d angles, sources=%s",
            company.name,
            len(research.pain_points or []),
            len(research.outreach_angles or []),
            research.sources_used,
        )
        return research


async def get_active_research(
    db: AsyncSession, company_id: UUID
) -> Optional[CompanyResearch]:
    """Return the freshest non-stale research brief for a company, or None.

    This is the read path consumers (email/script generators) use — it never
    triggers research, just serves what's already on file.
    """
    return (
        await db.execute(
            select(CompanyResearch)
            .where(
                CompanyResearch.company_id == company_id,
                CompanyResearch.is_stale.is_(False),
            )
            .order_by(CompanyResearch.researched_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def format_research_for_prompt(research: Optional[CompanyResearch]) -> str:
    """Render a research brief as a compact prompt block for content generators.

    Returns an empty string when there's no research, so callers can
    unconditionally concatenate it.
    """
    if research is None:
        return ""
    lines = ["COMPANY RESEARCH (use this to personalize — do not restate it verbatim):"]
    if research.research_brief:
        lines.append(f"Brief: {research.research_brief}")
    if research.pain_points:
        lines.append("Pain points:")
        for p in research.pain_points[:4]:
            if isinstance(p, dict):
                lines.append(f"  - {p.get('pain', '')} — {p.get('how_we_help', '')}")
    if research.outreach_angles:
        lines.append("Ranked outreach angles:")
        for a in sorted(
            [a for a in research.outreach_angles if isinstance(a, dict)],
            key=lambda x: x.get("rank", 99),
        )[:3]:
            lines.append(f"  {a.get('rank', '?')}. {a.get('angle', '')} — {a.get('reasoning', '')}")
    intel = research.competitive_intel or {}
    if isinstance(intel, dict) and (intel.get("competitors") or intel.get("technologies")):
        comp = ", ".join(intel.get("competitors", []) or [])
        tech = ", ".join(intel.get("technologies", []) or [])
        lines.append(f"Competitive intel: competitors=[{comp}] technologies=[{tech}]")
    return "\n".join(lines)


def get_research_agent() -> ResearchAgent:
    return ResearchAgent()
