"""Social Listening Agent — discovers public buying-intent signals.

The agent watches public sources (company news via Google News RSS + any extra
RSS feeds the org configures, and job postings that hint at growth) for the
keywords, industries, and companies an org cares about, then uses an LLM to rate
each hit against that org's profile, summarise it, and classify the kind of
buying intent. High-relevance hits become :class:`SignalEvent` cards; the very
hottest can auto-create a warm lead and auto-enroll it into a Dialtone sequence.

Everything here is org-scoped. Background scans run under
:func:`app.tasks.org_runner.for_each_org`, so the per-org context is bound and
the global read filter isolates each tenant automatically.

LLM credential resolution (see :meth:`_build_ai`): the agent's own encrypted
``llm_api_key`` → the org-level ``openrouter_api_key`` in ``system_settings`` →
the process-wide ``OPENROUTER_API_KEY`` (handled inside ``AIService``). The
agent's ``llm_model`` is always used as the OpenRouter model id.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.models.social_listening_agent import (
    DEFAULT_LLM_MODEL,
    SIGNAL_TYPES,
    SocialListeningAgent,
    SignalEvent,
    MonitoredCompany,
)
from app.models.system_setting import SystemSetting
from app.services.ai_service import AIService
from app.services.crypto import decrypt, encrypt, TokenCryptoError
from app.services.web_scraper import validate_public_url, UnsafeURLError

logger = logging.getLogger(__name__)

# Google News RSS — free, no key. Same source the news_monitor uses.
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

# The org-level system_settings key that holds a shared OpenRouter key, used
# when an agent has no key of its own.
ORG_OPENROUTER_KEY_SETTING = "openrouter_api_key"

# How far back a news item may be published and still count as a live signal.
_MAX_SIGNAL_AGE_DAYS = 45

# Job-posting titles that signal a growing team / buying intent, appended to a
# company query to surface hiring news through the same RSS source.
_JOB_INTENT_TERMS = (
    "hiring sales manager",
    "hiring operations",
    "now hiring",
    "job opening",
)


class SocialListeningService:
    """Fetches, analyzes, and actions public buying-intent signals per org."""

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    async def get_or_create_agent(self, db: AsyncSession) -> SocialListeningAgent:
        """Return the current org's agent config, creating a default if absent.

        The read + insert are org-scoped by the global filter / before-flush
        stamp, so this always resolves *this* org's single config row.
        """
        agent = (
            await db.execute(select(SocialListeningAgent))
        ).scalars().first()
        if agent is None:
            agent = SocialListeningAgent()
            db.add(agent)
            await db.flush()
        return agent

    def set_api_key(self, agent: SocialListeningAgent, raw_key: str | None) -> None:
        """Store (encrypted) or clear the agent's OpenRouter key.

        A blank/whitespace value clears the key (fall back to org/process key);
        any other value is Fernet-encrypted before it touches the column.
        """
        cleaned = (raw_key or "").strip()
        agent.llm_api_key = encrypt(cleaned) if cleaned else None

    async def resolve_api_key(
        self, db: AsyncSession, agent: SocialListeningAgent
    ) -> str | None:
        """The OpenRouter key to use: agent's own → org-level setting → None.

        ``None`` means "let ``AIService`` fall back to the process-wide
        ``OPENROUTER_API_KEY``". Decryption failures are swallowed (logged) so a
        rotated ``TOKEN_ENCRYPTION_KEY`` degrades to the org/process key instead
        of crashing every scan.
        """
        if agent.llm_api_key:
            try:
                return decrypt(agent.llm_api_key)
            except TokenCryptoError:
                logger.warning(
                    "social-listening: could not decrypt agent key for org=%s; "
                    "falling back to org/process key", agent.org_id,
                )
        setting = (
            await db.execute(
                select(SystemSetting).where(SystemSetting.key == ORG_OPENROUTER_KEY_SETTING)
            )
        ).scalars().first()
        if setting and isinstance(setting.value, dict):
            for field in ("key", "api_key", "value"):
                candidate = setting.value.get(field)
                if candidate:
                    return str(candidate)
        return None

    async def _build_ai(
        self, db: AsyncSession, agent: SocialListeningAgent
    ) -> AIService:
        """Construct an ``AIService`` bound to this agent's model + resolved key."""
        key = await self.resolve_api_key(db, agent)
        model = agent.llm_model or DEFAULT_LLM_MODEL
        return AIService(openrouter_model=model, openrouter_api_key=key)

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------
    async def _fetch_rss(self, url: str, *, validate: bool = True) -> list[dict]:
        """Fetch and parse an RSS feed into a list of item dicts.

        ``validate`` runs the SSRF guard for *org-supplied* feed URLs
        (``news_sources``). The trusted Google News endpoint skips it. Never
        raises — a bad/unreachable/again-private feed yields ``[]``.
        """
        if validate:
            try:
                url = validate_public_url(url)
            except UnsafeURLError:
                logger.warning("social-listening: rejected unsafe feed URL %r", url)
                return []
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return []
                return self._parse_rss(resp.text)
        except Exception as exc:  # network / parse — never abort the scan
            logger.warning("social-listening: feed fetch failed for %r: %s", url, exc)
            return []

    @staticmethod
    def _parse_rss(text: str) -> list[dict]:
        """Parse RSS XML into item dicts, dropping items older than the cutoff."""
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=_MAX_SIGNAL_AGE_DAYS)
        items: list[dict] = []
        for item in root.findall(".//item")[:15]:
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            link = (item.findtext("link") or "").strip()
            description = (item.findtext("description") or "").strip()
            source = (item.findtext("source") or "").strip()
            published_at = None
            pub = item.findtext("pubDate")
            if pub:
                try:
                    published_at = parsedate_to_datetime(pub)
                except (TypeError, ValueError):
                    published_at = None
            if published_at is not None:
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
                if published_at < cutoff:
                    continue
            items.append({
                "title": title,
                "link": link,
                "description": description,
                "source": source,
                "published_at": published_at.isoformat() if published_at else None,
            })
        return items

    async def _google_news(self, query: str) -> list[dict]:
        """Query Google News RSS for *query* (a trusted, non-validated endpoint)."""
        url = GOOGLE_NEWS_RSS.format(query=quote_plus(query))
        return await self._fetch_rss(url, validate=False)

    async def scan_news_feeds(
        self, db: AsyncSession, agent: SocialListeningAgent
    ) -> list[dict]:
        """Fetch raw news signals for the agent's keywords, industries, companies.

        Returns unpersisted raw signal dicts (deduped within this call). Each
        carries ``source_type="news"`` (or ``"company_update"`` for a watched
        company) and the fields ``analyze_signal`` needs.
        """
        raw: list[dict] = []
        seen: set[str] = set()

        def _add(item: dict, *, source_type: str, company_name: str | None, domain: str | None):
            key = (item.get("link") or "").strip() or f"{company_name}|{item.get('title')}"
            if key in seen:
                return
            seen.add(key)
            raw.append({
                "source_type": source_type,
                "source_url": item.get("link") or None,
                "company_name": company_name or item.get("source") or "Unknown",
                "company_domain": domain,
                "headline": item.get("title") or "",
                "description": item.get("description") or "",
                "published_at": item.get("published_at"),
            })

        keywords = [k for k in (agent.monitored_keywords or []) if k]
        industries = [i for i in (agent.monitored_industries or []) if i]

        # Keyword × industry news across the web.
        for kw in keywords:
            for item in await self._google_news(kw):
                _add(item, source_type="news", company_name=None, domain=None)
        for industry in industries:
            query = f"{industry} " + " OR ".join(f'"{k}"' for k in keywords) if keywords else industry
            for item in await self._google_news(query):
                _add(item, source_type="news", company_name=None, domain=None)

        # Explicitly monitored companies (news only here; jobs handled separately).
        monitored = (
            await db.execute(
                select(MonitoredCompany).where(MonitoredCompany.monitor_news.is_(True))
            )
        ).scalars().all()
        for mc in monitored:
            kw_clause = " ".join(f'"{k}"' for k in keywords)
            query = f'"{mc.company_name}" {kw_clause}'.strip()
            for item in await self._google_news(query):
                _add(item, source_type="company_update",
                     company_name=mc.company_name, domain=mc.domain)

        # Extra org-configured RSS feeds (validated — org-supplied URLs).
        for feed_url in (agent.news_sources or []):
            for item in await self._fetch_rss(feed_url, validate=True):
                _add(item, source_type="news", company_name=None, domain=None)

        return raw

    async def scan_job_postings(
        self, db: AsyncSession, agent: SocialListeningAgent
    ) -> list[dict]:
        """Fetch raw job-posting signals for monitored companies.

        Uses the same free RSS source with hiring-intent queries — a growing
        team ("now hiring", "hiring sales manager") is a classic buying signal.
        Returns unpersisted raw signal dicts with ``source_type="job_posting"``.
        """
        raw: list[dict] = []
        seen: set[str] = set()
        monitored = (
            await db.execute(
                select(MonitoredCompany).where(MonitoredCompany.monitor_jobs.is_(True))
            )
        ).scalars().all()
        for mc in monitored:
            for term in _JOB_INTENT_TERMS:
                query = f'"{mc.company_name}" {term}'
                for item in await self._google_news(query):
                    key = (item.get("link") or "").strip() or f"{mc.company_name}|{item.get('title')}"
                    if key in seen:
                        continue
                    seen.add(key)
                    raw.append({
                        "source_type": "job_posting",
                        "source_url": item.get("link") or None,
                        "company_name": mc.company_name,
                        "company_domain": mc.domain,
                        "headline": item.get("title") or "",
                        "description": item.get("description") or "",
                        "published_at": item.get("published_at"),
                    })
        return raw

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------
    async def analyze_signal(
        self,
        ai: AIService,
        agent: SocialListeningAgent,
        event_data: dict,
    ) -> dict:
        """AI-rate a raw signal's relevance, summarise it, classify its type.

        Returns ``{"relevance_score": 0-100, "summary": str, "signal_type": str}``.
        Degrades gracefully: on any AI failure the signal gets score 0 (so it is
        never auto-actioned) and the headline as its summary.
        """
        industries = ", ".join(agent.monitored_industries or []) or "our target industries"
        keywords = ", ".join(agent.monitored_keywords or []) or "buying-intent events"
        headline = event_data.get("headline", "")
        description = event_data.get("description", "")
        company = event_data.get("company_name", "a company")

        prompt = f"""You are a B2B sales-intelligence analyst. Rate how strong a
BUYING-INTENT signal the item below is for a vendor whose ideal customers are in:
{industries}
and who watches for these signals: {keywords}.

SIGNAL:
Company: {company}
Headline: {headline}
Details: {description[:600]}

Score 0-100 for how relevant + actionable this is as a warm-lead trigger (100 =
a strong, timely buying signal from an ideal-fit company; 0 = irrelevant/noise).
Classify the signal into EXACTLY ONE type from this list:
{", ".join(SIGNAL_TYPES)}.
Write a one-sentence summary a salesperson can act on.

Return ONLY valid JSON:
{{"relevance_score": 0, "signal_type": "one of the types above", "summary": "..."}}"""

        try:
            result = await ai.complete_json(prompt, max_tokens=400)
        except Exception as exc:  # AIService.complete_json shouldn't raise, but be safe
            logger.warning("social-listening: analyze_signal failed: %s", exc)
            result = {}

        return self._normalize_analysis(result, headline)

    @staticmethod
    def _normalize_analysis(result: dict, headline: str) -> dict:
        """Coerce a raw AI analysis into the canonical shape (never raises)."""
        if not isinstance(result, dict) or result.get("error"):
            return {"relevance_score": 0, "signal_type": None, "summary": headline}
        try:
            score = int(float(result.get("relevance_score", 0)))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))
        signal_type = str(result.get("signal_type", "") or "").strip().lower()
        if signal_type not in SIGNAL_TYPES:
            signal_type = None
        summary = str(result.get("summary", "") or "").strip() or headline
        return {"relevance_score": score, "signal_type": signal_type, "summary": summary[:2000]}

    # ------------------------------------------------------------------
    # Persistence + actioning
    # ------------------------------------------------------------------
    async def _signal_exists(self, db: AsyncSession, raw: dict) -> bool:
        """Whether a signal for this org already exists (dedup on URL/headline)."""
        url = raw.get("source_url")
        if url:
            existing = (
                await db.execute(
                    select(SignalEvent.id).where(SignalEvent.source_url == url).limit(1)
                )
            ).scalar_one_or_none()
            return existing is not None
        existing = (
            await db.execute(
                select(SignalEvent.id).where(
                    SignalEvent.company_name == raw.get("company_name"),
                    SignalEvent.headline == raw.get("headline"),
                ).limit(1)
            )
        ).scalar_one_or_none()
        return existing is not None

    async def _match_company(self, db: AsyncSession, raw: dict) -> Company | None:
        """Best-effort link a raw signal to an existing CRM company (org-scoped)."""
        domain = (raw.get("company_domain") or "").strip().lower()
        if domain:
            company = (
                await db.execute(select(Company).where(func.lower(Company.domain) == domain))
            ).scalars().first()
            if company:
                return company
        name = (raw.get("company_name") or "").strip()
        if name and name.lower() != "unknown":
            company = (
                await db.execute(select(Company).where(func.lower(Company.name) == name.lower()))
            ).scalars().first()
            if company:
                return company
        return None

    async def persist_signal(
        self, db: AsyncSession, raw: dict, analysis: dict
    ) -> SignalEvent:
        """Create a ``SignalEvent`` from a raw hit + its AI analysis."""
        company = await self._match_company(db, raw)
        event = SignalEvent(
            source_type=raw.get("source_type", "news"),
            source_url=raw.get("source_url"),
            company_name=raw.get("company_name") or "Unknown",
            company_domain=raw.get("company_domain"),
            company_id=company.id if company else None,
            headline=raw.get("headline", ""),
            summary=analysis.get("summary"),
            relevance_score=analysis.get("relevance_score", 0),
            signal_type=analysis.get("signal_type"),
            raw_data={
                "description": raw.get("description"),
                "published_at": raw.get("published_at"),
                "source": raw.get("source"),
            },
            status="new",
        )
        db.add(event)
        await db.flush()
        return event

    async def create_warm_lead(
        self, db: AsyncSession, signal: SignalEvent
    ) -> Contact | None:
        """Turn a signal into a warm lead: ensure a company + a contact card.

        Idempotent: re-actioning a signal that already produced a lead returns
        the existing contact. Returns ``None`` only if a company couldn't be
        resolved/created. The contact is intentionally created without an email
        (there is none in a public signal) — it is a research card the team can
        enrich, tagged with the signal context in its notes.
        """
        if signal.contact_id:
            existing = (
                await db.execute(select(Contact).where(Contact.id == signal.contact_id))
            ).scalars().first()
            if existing:
                return existing

        company = None
        if signal.company_id:
            company = (
                await db.execute(select(Company).where(Company.id == signal.company_id))
            ).scalars().first()
        if company is None:
            company = await self._match_company(db, {
                "company_domain": signal.company_domain,
                "company_name": signal.company_name,
            })
        if company is None:
            if not signal.company_name or signal.company_name.lower() == "unknown":
                return None
            company = Company(
                name=signal.company_name,
                domain=(signal.company_domain or None),
                country="US",
                source="social_listening",
                source_url=signal.source_url,
                pipeline_stage="researching",
                notes=f"Discovered via Social Listening Agent: {signal.headline}",
            )
            db.add(company)
            await db.flush()

        signal.company_id = company.id

        # One research contact per company from listening — reuse if present.
        contact = (
            await db.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.source == "social_listening",
                ).order_by(Contact.created_at)
            )
        ).scalars().first()
        if contact is None:
            contact = Contact(
                company_id=company.id,
                first_name="New",
                last_name="Lead",
                title="(from buying signal)",
                source="social_listening",
                contact_group="warm",
                notes=(
                    f"Warm lead from a {signal.signal_type or 'buying'} signal — "
                    f"{signal.headline}. {signal.summary or ''}"
                ).strip(),
            )
            db.add(contact)
            await db.flush()

        signal.contact_id = contact.id
        signal.lead_created = True
        if signal.status == "new":
            signal.status = "actioned"
        return contact

    async def auto_enroll_lead(
        self, db: AsyncSession, contact: Contact, signal: SignalEvent
    ) -> bool:
        """Best-effort enroll a warm-lead contact into a matching sequence.

        Returns True only when a *new* enrollment was created. A contact with no
        email can never send, so it is skipped (mirrors the prospector). Requires
        a resolvable active sequence for the contact's company vertical.
        """
        if not contact.email or not contact.email.strip():
            return False
        company = (
            await db.execute(select(Company).where(Company.id == contact.company_id))
        ).scalars().first()
        if company is None:
            return False
        sequence = await self._pick_sequence(db, company)
        if sequence is None:
            return False
        # One matching row answers "already enrolled?". No status filter, so a
        # contact re-enrolled after completing the sequence has two rows — a
        # state the enrol endpoint explicitly allows — and asserting a single
        # one made an ordinary history abort the agent's run.
        existing = (
            await db.execute(
                select(OutreachEnrollment.id)
                .where(
                    OutreachEnrollment.contact_id == contact.id,
                    OutreachEnrollment.sequence_id == sequence.id,
                )
                .limit(1)
            )
        ).scalars().first()
        if existing is not None:
            return False
        db.add(OutreachEnrollment(
            contact_id=contact.id,
            sequence_id=sequence.id,
            current_step=1,
            status="active",
        ))
        company.pipeline_stage = "outreach"
        return True

    async def _pick_sequence(
        self, db: AsyncSession, company: Company
    ) -> OutreachSequence | None:
        """The active sequence for the company's vertical, else any active one."""
        from app.services.industry_verticals import resolve_vertical

        base = (
            OutreachSequence.is_active == True,  # noqa: E712
            OutreachSequence.status == "active",
        )
        vertical = resolve_vertical(
            industry=company.industry_segment or "", company_name=company.name or ""
        )
        if vertical is not None:
            matched = (
                await db.execute(
                    select(OutreachSequence)
                    .where(*base, OutreachSequence.industry == vertical.key)
                    .order_by(OutreachSequence.created_at)
                )
            ).scalars().first()
            if matched is not None:
                return matched
        return (
            await db.execute(
                select(OutreachSequence).where(*base).order_by(OutreachSequence.created_at)
            )
        ).scalars().first()

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    async def run_scan(
        self, db: AsyncSession, agent: SocialListeningAgent | None = None
    ) -> dict:
        """Full per-org scan: fetch → dedup → analyze → persist → (auto-action).

        Skips entirely (and says so) when the agent is toggled off. Signals that
        clear ``min_relevance_score`` are auto-actioned only when the matching
        automation flag is on. Returns a summary suitable for logging/stats.
        """
        if agent is None:
            agent = await self.get_or_create_agent(db)
        if not agent.is_active:
            return {"skipped": True, "reason": "agent inactive", "signals_found": 0}

        ai = await self._build_ai(db, agent)
        raw_signals = await self.scan_news_feeds(db, agent)
        raw_signals += await self.scan_job_postings(db, agent)

        found = leads_created = enrolled = 0
        for raw in raw_signals:
            if await self._signal_exists(db, raw):
                continue
            analysis = await self.analyze_signal(ai, agent, raw)
            signal = await self.persist_signal(db, raw, analysis)
            found += 1

            if signal.relevance_score >= agent.min_relevance_score and agent.auto_create_leads:
                contact = await self.create_warm_lead(db, signal)
                if contact is not None:
                    leads_created += 1
                    if agent.auto_enroll_dialtone and await self.auto_enroll_lead(db, contact, signal):
                        enrolled += 1

        agent.last_scan_at = datetime.now(timezone.utc)
        await db.flush()
        return {
            "skipped": False,
            "signals_found": found,
            "leads_created": leads_created,
            "enrolled": enrolled,
        }

    async def process_new_signals(self, db: AsyncSession) -> dict:
        """Review already-stored ``new`` signals above threshold and action them.

        Complements :meth:`run_scan` for orgs whose signals were created without
        auto-action (e.g. thresholds/flags changed after a scan). A no-op unless
        the agent is active with ``auto_create_leads`` on.
        """
        agent = await self.get_or_create_agent(db)
        if not agent.is_active or not agent.auto_create_leads:
            return {"skipped": True, "leads_created": 0, "enrolled": 0}

        signals = (
            await db.execute(
                select(SignalEvent).where(
                    SignalEvent.status == "new",
                    SignalEvent.lead_created.is_(False),
                    SignalEvent.relevance_score >= agent.min_relevance_score,
                ).order_by(SignalEvent.relevance_score.desc()).limit(50)
            )
        ).scalars().all()

        leads_created = enrolled = 0
        for signal in signals:
            contact = await self.create_warm_lead(db, signal)
            if contact is not None:
                leads_created += 1
                if agent.auto_enroll_dialtone and await self.auto_enroll_lead(db, contact, signal):
                    enrolled += 1
        await db.flush()
        return {"skipped": False, "leads_created": leads_created, "enrolled": enrolled}

    async def stats(self, db: AsyncSession) -> dict:
        """Aggregate counts for the dashboard: signals, leads, conversion, types."""
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        total = (
            await db.execute(select(func.count(SignalEvent.id)))
        ).scalar_one()
        this_week = (
            await db.execute(
                select(func.count(SignalEvent.id)).where(SignalEvent.created_at >= week_ago)
            )
        ).scalar_one()
        leads = (
            await db.execute(
                select(func.count(SignalEvent.id)).where(SignalEvent.lead_created.is_(True))
            )
        ).scalar_one()
        dismissed = (
            await db.execute(
                select(func.count(SignalEvent.id)).where(SignalEvent.status == "dismissed")
            )
        ).scalar_one()

        type_rows = (
            await db.execute(
                select(SignalEvent.signal_type, func.count(SignalEvent.id))
                .group_by(SignalEvent.signal_type)
                .order_by(func.count(SignalEvent.id).desc())
            )
        ).all()
        by_type = [
            {"signal_type": t or "unclassified", "count": c} for t, c in type_rows
        ]

        company_rows = (
            await db.execute(
                select(SignalEvent.company_name, func.count(SignalEvent.id))
                .group_by(SignalEvent.company_name)
                .order_by(func.count(SignalEvent.id).desc())
                .limit(5)
            )
        ).all()
        top_companies = [{"company_name": n, "count": c} for n, c in company_rows]

        conversion = round((leads / total) * 100, 1) if total else 0.0
        return {
            "total_signals": total,
            "signals_this_week": this_week,
            "leads_created": leads,
            "dismissed": dismissed,
            "conversion_rate": conversion,
            "by_signal_type": by_type,
            "top_companies": top_companies,
        }


_service: SocialListeningService | None = None


def get_social_listening_service() -> SocialListeningService:
    global _service
    if _service is None:
        _service = SocialListeningService()
    return _service
