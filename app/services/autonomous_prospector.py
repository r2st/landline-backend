"""Autonomous prospecting orchestrator (feature 3.1).

Chains the existing sales stack into a hands-off pipeline:

    learn ICP (from won deals)
        → source candidate companies (Apollo, then Places, or supplied seeds)
        → qualify each against the ICP (fit score 0-100)
        → for high-confidence matches: persist + advance to outreach + (best-effort) enroll
        → for borderline matches: persist as a new lead in the human review queue
        → for excluded / poor fits: record and skip

Everything is wrapped so a single bad candidate never fails the run, and hard
guardrails (daily cap, enable flag, learned exclusion criteria) bound what the
agent can do autonomously.

The core is deterministic and network-free: ``run`` accepts a list of candidate
dicts (used by the API's manual trigger and by tests). When no seeds are given
it sources automatically: Apollo first, then Google Places when Apollo yields
nothing (see ``_source_candidates``).
"""

import logging
from collections import Counter
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.discovered_business import DiscoveredBusiness
from app.models.discovery_search import DiscoverySearch
from app.models.outreach import OutreachEnrollment
from app.models.prospecting import (
    IcpProfile,
    ProspectCandidate,
    ProspectingRun,
)
from app.models.sequence import OutreachSequence
from app.services.industry_verticals import matches_manufacturing, resolve_vertical
from app.services.ingestion import (
    ROUTE_AUTO_ENROLL,
    ROUTE_DISCARD,
    ROUTE_IMPORT,
    ROUTE_REVIEW,
    CandidateRecord,
    get_ingestion_service,
)

logger = logging.getLogger(__name__)

# The ICP fields a human is allowed to take ownership of. Everything here is
# scoring input; `learned_from_deal_count`, `model_used` and the timestamps
# describe how the profile was produced and are not an operator's to set.
#
# An allowlist rather than "any attribute": an override naming a field that
# doesn't exist would sit in the JSON being silently skipped forever, which is
# the same failure mode as the review queue reading fields the API never sent.
OVERRIDABLE_ICP_FIELDS = frozenset(
    {
        "industry_weights",
        "size_range",
        "location_preferences",
        "technology_signals",
        "pain_point_indicators",
        "exclusion_criteria",
    }
)


def apply_manual_overrides(profile: IcpProfile) -> None:
    """Overlay a profile's pinned fields onto its learned values, in place.

    Called after every fit, so the parts nobody pinned keep tracking what
    actually closes while explicit human choices survive the Monday re-learn.
    Unknown keys are dropped with a warning rather than ignored quietly — they
    can only come from a field that was renamed or removed, and a silently
    inert override is exactly the bug this whole mechanism exists to prevent.
    """
    overrides = profile.manual_overrides or {}
    for field, value in overrides.items():
        if field not in OVERRIDABLE_ICP_FIELDS:
            logger.warning(
                "ICP profile %s pins unknown field %r — ignoring. It was probably "
                "renamed; the override will never apply until it is corrected.",
                profile.id,
                field,
            )
            continue
        setattr(profile, field, value)

# Matches ``places_discovery.PROVIDER``. Imported lazily at call sites (the
# service pulls in httpx and the ingestion graph), so the string is pinned here
# and asserted against the real one in tests.
PLACES_SOURCE = "google_places"

# Places rows this source may still pick up. A row leaves this set once it has
# been dispositioned: 'imported' became a company, and 'skipped' was judged not
# worth importing. Both were decided by an earlier run, and re-sourcing either
# re-spends enrichment credit to reach the same verdict — which is what pinned
# every run to the same first 25 rejects and starved the ones behind them.
# 'enriched' stays in: that is a row the nightly sweep found an email for and
# nobody has ruled on yet, which makes it the best candidate on the table, not a
# spent one. Mirrors the sweep's own filter in tasks/places_discovery.py.
SOURCEABLE_PLACES_STATUSES = frozenset({"new", "enriched"})

# ---------------------------------------------------------------------------
# Places query rotation
#
# Places hard-caps every distinct textQuery at 60 results. A fixed query
# ("CNC Machining" in one state, every day) therefore re-fetches the same 60
# businesses forever: once they're imported or rejected the source goes
# permanently quiet while looking healthy. Rotating query × metro makes each
# day a *different* textQuery with its own 60-result ceiling.
# ---------------------------------------------------------------------------

# US metro areas with dense SMB manufacturing. City-level beats state-level:
# "CNC Machining in Texas" and "CNC Machining in Houston, TX" are different
# textQueries with different result sets, and the metro form surfaces shops the
# state-wide ranking buries.
PLACES_METRO_AREAS: tuple[str, ...] = (
    "Chicago, IL", "Houston, TX", "Los Angeles, CA", "Detroit, MI",
    "Cleveland, OH", "Milwaukee, WI", "Minneapolis, MN", "Dallas, TX",
    "Cincinnati, OH", "Pittsburgh, PA", "Grand Rapids, MI", "Rockford, IL",
    "Wichita, KS", "Dayton, OH", "Greenville, SC", "Hartford, CT",
    "Charlotte, NC", "Phoenix, AZ", "Portland, OR", "St. Louis, MO",
)

# Baseline query breadth beyond whatever the ICP has learned. Each is an ICP
# term by construction (the query doubles as the staged row's industry signal —
# see 183d5a9 and places_discovery._stage), so everything found under one of
# these scores as in-vertical.
DEFAULT_PLACES_QUERIES: tuple[str, ...] = (
    "CNC Machining",
    "Injection Molding",
    "Metal Stamping",
    "Precision Machining",
    "Metal Fabrication",
    "Sheet Metal Fabrication",
    "Tool and Die",
    "Plastics Manufacturing",
    "Aerospace Manufacturing",
    "Food Processing Plant",
)


class AutonomousProspector:
    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # ICP learning
    # ------------------------------------------------------------------
    async def get_active_icp(self, db: AsyncSession) -> Optional[IcpProfile]:
        return (
            await db.execute(
                select(IcpProfile).where(IcpProfile.is_active == True)  # noqa: E712
                .order_by(IcpProfile.updated_at.desc())
            )
        ).scalars().first()

    async def learn_icp(self, db: AsyncSession, name: str = "Learned ICP") -> IcpProfile:
        """Fit (or refit) an ICP from the characteristics of won deals.

        Won deals → their companies → aggregate industry / size / location.
        Deactivates any prior active profile so exactly one stays active. Falls
        back to sensible Apprend defaults when there is not enough won history.
        """
        won_company_ids = (
            await db.execute(
                select(Deal.company_id).where(Deal.status == "won")
            )
        ).scalars().all()
        won_company_ids = [c for c in won_company_ids if c is not None]

        companies: list[Company] = []
        if won_company_ids:
            companies = list(
                (
                    await db.execute(
                        select(Company).where(Company.id.in_(won_company_ids))
                    )
                ).scalars().all()
            )

        industry_counts: Counter = Counter()
        sizes: list[int] = []
        countries: Counter = Counter()
        states: Counter = Counter()
        for c in companies:
            if c.industry_segment:
                industry_counts[c.industry_segment] += 1
            if c.employee_count:
                sizes.append(int(c.employee_count))
            if c.country:
                countries[c.country] += 1
            if c.state_province:
                states[c.state_province] += 1

        # Industry weights normalized to 0..1 by frequency; blend with Apprend's
        # known sweet-spot verticals so a thin history still steers sensibly.
        default_industries = {
            "Injection Molding": 0.8,
            "CNC Machining": 0.8,
            "Metal Stamping": 0.8,
            "Precision Machining": 0.8,
            "Metal Fabrication": 0.7,
            "Plastics": 0.7,
            "Aerospace": 0.6,
            "Food & Beverage Manufacturing": 0.6,
        }
        industry_weights = dict(default_industries)
        if industry_counts:
            top = industry_counts.most_common(1)[0][1]
            for seg, n in industry_counts.items():
                industry_weights[seg] = round(min(1.0, 0.5 + 0.5 * (n / top)), 3)

        if sizes:
            sizes.sort()
            size_range = {
                "min": max(10, int(min(sizes) * 0.6)),
                "max": int(max(sizes) * 1.4),
                "ideal": sizes[len(sizes) // 2],
            }
        else:
            size_range = {"min": 50, "max": 500, "ideal": 150}

        location_preferences = {
            "countries": [c for c, _ in countries.most_common()] or ["US", "CA"],
            "states": [s for s, _ in states.most_common(8)],
        }

        exclusion_criteria = {
            "industries": [],
            "countries": [],
            "max_employees": max(2000, size_range["max"] * 3),
            "min_employees": 5,
        }

        # Deactivate previous profiles, and inherit whatever a human pinned on
        # the one that was active. This runs weekly off the beat schedule, so
        # without the carry-forward an operator's hand-tuned ICP would revert
        # every Monday with nothing to explain why the agent started chasing the
        # wrong companies again.
        prior_overrides: dict = {}
        prior_edited_at = None
        prior_edited_by = None
        for prev in (
            await db.execute(select(IcpProfile).where(IcpProfile.is_active == True))  # noqa: E712
        ).scalars().all():
            prev.is_active = False
            if prev.manual_overrides:
                prior_overrides = dict(prev.manual_overrides)
                prior_edited_at = prev.edited_at
                prior_edited_by = prev.edited_by

        profile = IcpProfile(
            name=name,
            is_active=True,
            industry_weights=industry_weights,
            size_range=size_range,
            location_preferences=location_preferences,
            technology_signals=["spreadsheets", "paper travelers", "legacy ERP", "manual quoting"],
            pain_point_indicators=[
                "slow quoting", "quality escapes", "no shop floor visibility",
                "manual scheduling", "disconnected systems",
            ],
            exclusion_criteria=exclusion_criteria,
            learned_from_deal_count=len(companies),
            model_used="heuristic-v1",
            manual_overrides=prior_overrides,
            edited_at=prior_edited_at,
            edited_by=prior_edited_by,
        )
        # Pinned fields win over the fresh fit. Everything else keeps tracking
        # what actually closes, which is the point of re-learning at all.
        apply_manual_overrides(profile)
        db.add(profile)
        await db.flush()
        return profile

    async def ensure_icp(self, db: AsyncSession) -> IcpProfile:
        icp = await self.get_active_icp(db)
        if icp is None:
            icp = await self.learn_icp(db)
        return icp

    # ------------------------------------------------------------------
    # Qualification
    # ------------------------------------------------------------------
    def qualify(self, candidate: dict, icp: IcpProfile) -> dict:
        """Score a candidate 0-100 against the ICP and return a qualification dict.

        Deterministic and explainable: industry fit (40), size fit (30),
        location fit (20), data completeness (10). Hard exclusions zero the
        score and set ``excluded``.
        """
        industry = (candidate.get("industry") or "").strip()
        employees = candidate.get("employee_count")
        country = (candidate.get("country") or "").strip().upper()

        excl = icp.exclusion_criteria or {}
        excluded_reason = None
        if industry and industry in (excl.get("industries") or []):
            excluded_reason = f"industry '{industry}' is excluded"
        elif country and country in [c.upper() for c in (excl.get("countries") or [])]:
            excluded_reason = f"country '{country}' is excluded"
        elif employees is not None and excl.get("max_employees") and employees > excl["max_employees"]:
            excluded_reason = f"{employees} employees exceeds max {excl['max_employees']}"
        elif employees is not None and excl.get("min_employees") and employees < excl["min_employees"]:
            excluded_reason = f"{employees} employees below min {excl['min_employees']}"
        if excluded_reason:
            return {"fit_score": 0, "excluded": True, "reason": excluded_reason, "components": {}}

        # Industry fit (0-40).
        weights = icp.industry_weights or {}
        iw = 0.0
        if industry:
            # exact, else case-insensitive contains match against known segments
            if industry in weights:
                iw = weights[industry]
            else:
                for seg, w in weights.items():
                    if seg.lower() in industry.lower() or industry.lower() in seg.lower():
                        iw = max(iw, w)
            # An in-taxonomy industry that just isn't among the ICP's learned
            # keys ("Tool and Die" when the ICP only knows "CNC Machining")
            # still gets credit. Without this, a rotated Places query outside
            # the learned keys re-creates the structurally-disqualified bug the
            # query-as-industry fix (183d5a9) solved for the primary query.
            # 0.6 lands a typical Places candidate in the review queue rather
            # than auto-enrolling: manufacturing-by-taxonomy is worth a human
            # look, while auto-enroll stays reserved for learned ICP segments.
            if iw == 0.0 and matches_manufacturing(industry, candidate.get("name") or ""):
                iw = 0.6
        industry_pts = round(40 * iw, 1)

        # Size fit (0-30) — full points inside [min,max], decaying outside.
        sr = icp.size_range or {}
        size_pts = 0.0
        if employees is not None:
            lo, hi = sr.get("min", 50), sr.get("max", 500)
            if lo <= employees <= hi:
                size_pts = 30.0
            else:
                # linear decay: lose all points at 3x outside the nearest bound
                dist = (lo - employees) if employees < lo else (employees - hi)
                span = max(hi - lo, 50)
                size_pts = round(max(0.0, 30.0 * (1 - dist / (2 * span))), 1)
        else:
            size_pts = 10.0  # unknown size → partial credit

        # Location fit (0-20).
        prefs = icp.location_preferences or {}
        pref_countries = [c.upper() for c in (prefs.get("countries") or [])]
        loc_pts = 0.0
        if country and pref_countries:
            if country == (pref_countries[0] if pref_countries else ""):
                loc_pts = 20.0
            elif country in pref_countries:
                loc_pts = 14.0
        elif not country:
            loc_pts = 8.0

        # Completeness (0-10).
        present = sum(1 for k in ("name", "domain", "industry", "employee_count", "country") if candidate.get(k))
        completeness_pts = round(10 * present / 5, 1)

        fit = round(industry_pts + size_pts + loc_pts + completeness_pts)
        fit = max(0, min(100, fit))
        return {
            "fit_score": fit,
            "excluded": False,
            "reason": None,
            "components": {
                "industry": industry_pts,
                "size": size_pts,
                "location": loc_pts,
                "completeness": completeness_pts,
            },
        }

    # ------------------------------------------------------------------
    # Guardrails
    # ------------------------------------------------------------------
    async def _enrolled_today(self, db: AsyncSession) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            await db.execute(
                select(func.count(ProspectCandidate.id)).where(
                    ProspectCandidate.status == "enrolled",
                    ProspectCandidate.created_at >= start,
                )
            )
        ).scalar() or 0

    # ------------------------------------------------------------------
    # Sourcing
    # ------------------------------------------------------------------
    def _top_industry(self, icp: IcpProfile) -> str:
        if icp.industry_weights:
            return max(icp.industry_weights, key=icp.industry_weights.get)
        return "manufacturing"

    async def _source_candidates(
        self, db: AsyncSession, icp: IcpProfile, limit: int, run: ProspectingRun
    ) -> tuple[list[CandidateRecord], list[str], dict]:
        """Return (records, sources queried, sourcing notes) from Apollo, then Places.

        Neither source imports anything here — they return records and the
        ingestion service persists them. ``notes`` carries why sourcing stopped,
        so a rate-limited run can't masquerade as a run that found nothing.

        Places runs only when Apollo yields no records. That covers the free
        plan's 403 on both search endpoints, a missing key, an exhausted budget
        and a genuinely empty result — the distinction doesn't change what to do
        next, and ``notes`` keeps it legible after the fact. Places is second and
        not parallel because it is the weaker source: it carries no employee
        count or revenue (the two things ``qualify`` weighs hardest) and no
        people, so a Places run costs more per emailable contact than Apollo.
        """
        sources: list[str] = []
        notes: dict = {}

        records, apollo_notes = await self._source_from_apollo(db, icp, limit, run)
        notes.update(apollo_notes)
        if apollo_notes.get("apollo_status") != "skipped":
            sources.append("apollo")
        if records:
            return records, sources, notes

        places_records, places_notes = await self._source_from_places(db, icp, limit)
        notes.update(places_notes)
        if places_notes.get("places_status") != "skipped":
            sources.append(PLACES_SOURCE)
        return places_records, sources, notes

    async def _source_from_apollo(
        self, db: AsyncSession, icp: IcpProfile, limit: int, run: ProspectingRun
    ) -> tuple[list[CandidateRecord], dict]:
        if not self.settings.apollo_api_key:
            return [], {"apollo_status": "skipped", "apollo_error": "no_api_key"}

        from app.services.enrichment import get_enrichment_service

        svc = get_enrichment_service()
        sr = icp.size_range or {}
        search = await svc.search_companies(
            db,
            industry=self._top_industry(icp),
            min_employees=sr.get("min"),
            max_employees=sr.get("max"),
            target_volume=limit,
            run_id=run.id,
            icp_profile_id=icp.id,
        )
        notes = {
            "apollo_status": search.status,
            "apollo_pages": search.pages_fetched,
            "apollo_credits_spent": search.credits_spent,
            "apollo_truncated": search.truncated,
        }
        if search.error:
            notes["apollo_error"] = search.error
        for record in search.records:
            record.icp_profile_id = icp.id
        return search.records, notes

    def _places_query_rotation(self, icp: IcpProfile) -> list[str]:
        """The ICP's industry queries (weight desc), then the built-in breadth
        queries, deduped case-insensitively in that priority order."""
        weights = icp.industry_weights or {}
        ranked = [k for k, _ in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))]
        queries: list[str] = []
        seen: set[str] = set()
        for q in [*ranked, *DEFAULT_PLACES_QUERIES]:
            key = q.strip().lower()
            if key and key not in seen:
                seen.add(key)
                queries.append(q.strip())
        return queries

    def _places_search_from_icp(
        self, icp: IcpProfile, today: Optional[date] = None
    ) -> DiscoverySearch:
        """An unsaved ``DiscoverySearch`` describing what the ICP wants from Places.

        Transient on purpose. ``DiscoverySearch`` rows are the operator's saved,
        scheduled queries; this one is derived fresh from the ICP each run and
        persisting it would put a row nobody curated into that list. ``_stage``
        keeps the audit trail anyway — it copies ``query`` onto every staged
        business and leaves the nullable ``search_id`` NULL, which reads
        correctly as "not from a saved search".

        The (query, location) pair rotates by day: query changes daily, and the
        metro advances each time the query list wraps, so a daily run walks the
        full query × metro grid before any pair repeats. Deterministic on the
        date — a retry the same day fetches the same thing, not a fresh spend.
        """
        prefs = icp.location_preferences or {}
        countries = [c for c in (prefs.get("countries") or []) if c]
        states = [s for s in (prefs.get("states") or []) if s]
        country = countries[0].strip().upper()[:2] if countries else None

        day = (today or datetime.now(timezone.utc).date()).toordinal()
        queries = self._places_query_rotation(icp)
        query = queries[day % len(queries)]

        if country in (None, "US"):
            location = PLACES_METRO_AREAS[(day // len(queries)) % len(PLACES_METRO_AREAS)]
        elif states:
            # Non-US ICP: the metro list doesn't apply; rotate the ICP's own states.
            location = states[day % len(states)]
        else:
            location = country
        return DiscoverySearch(
            name="autonomous-prospector",
            query=query,
            # Places resolves a place name better than we can, and it costs no
            # extra call.
            location_text=location,
            country=country,
        )

    async def _source_from_places(
        self, db: AsyncSession, icp: IcpProfile, limit: int
    ) -> tuple[list[CandidateRecord], dict]:
        """Discover SMBs via Places and hand back records, never raising.

        ``discover`` stages to ``discovered_businesses`` (deduped on
        ``place_id``) and reports its own failures as a status rather than an
        exception, so a Places outage degrades this run instead of failing it.
        """
        from app.services.places_discovery import get_places_service

        svc = get_places_service()
        if not svc.enabled:
            return [], {"places_status": "skipped", "places_error": "no_api_key"}

        search = self._places_search_from_icp(icp)
        # Page to the API's own per-query ceiling rather than to ``limit``. A
        # given (query, metro) pair recurs once per rotation cycle, and a
        # limit-sized fetch would return the *same* first N businesses each
        # time; once those are imported the pair would go quiet with the rest
        # of the ceiling never looked at. Fetching the ceiling and filtering
        # costs a page or two more per run and lets successive visits walk the
        # whole result set.
        outcome = await svc.discover(db, search)
        notes = {
            "places_status": outcome.status,
            "places_query": search.query,
            "places_location": search.location_text,
            "places_staged": outcome.created,
            "places_duplicates": outcome.duplicates,
            "places_credits_spent": outcome.credits_spent,
            "places_truncated": outcome.truncated,
        }
        if outcome.error:
            notes["places_error"] = outcome.error

        # Businesses an earlier run already dispositioned are done — ingestion
        # would dedup an imported one back onto its company, but they'd crowd out
        # unseen ones under the run's target and re-report as "found" every week.
        # `company_id is None` alone doesn't express that: a rejected row keeps a
        # NULL company_id and only carries its verdict in `status`, so filtering
        # on the FK re-picked the same rejects forever. Bound to `limit` here,
        # before the expensive part: enriching a business this run won't use is
        # pure spend.
        fresh = [
            b
            for b in outcome.businesses
            if b.company_id is None
            and (b.status is None or b.status in SOURCEABLE_PLACES_STATUSES)
        ][:limit]
        await self._enrich_places_emails(db, svc, fresh, notes)

        records: list[CandidateRecord] = []
        for business in fresh:
            record = svc.to_candidate(business)
            record.icp_profile_id = icp.id
            records.append(record)

        notes["places_candidates"] = len(records)
        if outcome.status != "ok":
            logger.warning(
                "Places sourcing for '%s' ended %s: %s",
                search.query, outcome.status, outcome.error,
            )
        return records, notes

    async def _enrich_places_emails(
        self, db: AsyncSession, svc, businesses: list, notes: dict
    ) -> None:
        """Recover an email per business, in place, so there is someone to enrol.

        Places returns businesses, never people: without this every Places
        candidate reaches ingestion with no contact, and ``_auto_enroll`` has
        nobody to enrol no matter how well the company fits. ``extract_email``
        tries Hunter then a free scrape, and writes the winner onto the row.

        Sequential on purpose — these share one ``AsyncSession``, which is not
        safe to drive concurrently, so gathering them would corrupt the run's
        transaction rather than speed it up.

        Best-effort throughout: this buys contacts, and a business we can't find
        an email for still has a phone number for the call queue.
        """
        budget = self.settings.prospecting_places_enrich_max
        attempted = enriched = 0
        for business in businesses:
            if budget <= 0:
                break
            # Neither costs a call, so neither should cost budget.
            if business.email or not business.domain:
                continue
            budget -= 1
            attempted += 1
            try:
                result = await svc.extract_email(db, business)
            except Exception:
                # One unreachable site must not fail the run.
                logger.exception("Places email extraction failed for %s", business.domain)
                continue
            if result.get("status") == "enriched":
                enriched += 1

        notes["places_enrich_attempted"] = attempted
        notes["places_enriched"] = enriched
        # The honest headline: how many of this run's businesses anyone can email.
        notes["places_with_email"] = sum(1 for b in businesses if b.email)
        if attempted and budget <= 0:
            notes["places_enrich_capped"] = True
            logger.info(
                "Places inline enrichment hit its per-run cap (%d); the nightly "
                "sweep will pick up the rest",
                self.settings.prospecting_places_enrich_max,
            )

    async def _pick_sequence(
        self, db: AsyncSession, company: Company
    ) -> Optional[OutreachSequence]:
        """The active sequence whose vertical matches this company's industry.

        Routing goes through ``resolve_vertical``, the same keyword matcher the
        rest of the stack uses, so a company's free-text ``industry_segment``
        ("CNC Machining") lands on the vertical key stored in
        ``OutreachSequence.industry`` ("cnc_machining").

        Deliberately *not* ``resolve_vertical_or_default``: that defaults to
        injection molding, which is how a CNC lead would get pitched someone
        else's vertical. An unrecognised industry falls through to the generic
        fallback below instead of borrowing a wrong vertical's copy.

        The fallback (any active sequence) is ordered by ``created_at`` only to
        make it deterministic — an unordered ``.first()`` is what let this pick
        an arbitrary sequence per call.
        """
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
            # The vertical is known but nobody seeded (or activated) its
            # sequence: better a generic touch than none, but say so — this is
            # a seeding gap, not a routing decision.
            logger.warning(
                "No active sequence tagged '%s' for %s; falling back to a "
                "generic sequence",
                vertical.key, company.name,
            )
        return (
            await db.execute(
                select(OutreachSequence).where(*base).order_by(OutreachSequence.created_at)
            )
        ).scalars().first()

    async def _auto_enroll(self, db: AsyncSession, company: Company) -> bool:
        """Advance a company to outreach and best-effort enroll a primary contact.

        Returns True if a real sequence enrollment was created. Advancing the
        pipeline stage always happens for a high-confidence match.
        """
        company.pipeline_stage = "outreach"
        # An enrollment for a contact with no email address is a row that can
        # never send: the step would come due forever and the lead would look
        # worked without a single touch. Prefer the primary among the rest.
        #
        # Suppressed and unsubscribed contacts are excluded for the same reason,
        # one step further along. The send path re-checks and would stop such an
        # enrollment as ``unsubscribed`` on its first pass, so this is not what
        # stops the mail — but the row still lands in ``total_enrollments`` and
        # in the conversion denominator, so enrolling a contact we will never
        # mail quietly dilutes every campaign rate it touches. Bounces are the
        # common case: a hard bounce suppresses the address, and without this the
        # next discovery run walks the same company and enrolls the same dead
        # mailbox into whichever sequence it picks this time.
        candidates = (
            await db.execute(
                select(Contact).where(
                    Contact.company_id == company.id,
                    Contact.email.isnot(None),
                    func.trim(Contact.email) != "",
                    Contact.suppressed.is_(False),
                    Contact.unsubscribed.is_(False),
                )
                .order_by(Contact.is_primary.desc())
            )
        ).scalars().all()
        sequence = await self._pick_sequence(db, company)
        if not candidates or sequence is None:
            return False

        # The org do-not-contact list is keyed by address rather than by contact
        # row, which is what catches a re-imported contact for an address that
        # bounced under a previous row. Checked here rather than folded into the
        # query above because it is a separate table with its own org scoping.
        from app.services import suppression as _suppression

        contact = None
        for candidate in candidates:
            if not await _suppression.is_suppressed(db, candidate.email):
                contact = candidate
                break
        if contact is None:
            return False
        existing = (
            await db.execute(
                select(OutreachEnrollment).where(
                    OutreachEnrollment.contact_id == contact.id,
                    OutreachEnrollment.sequence_id == sequence.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False
        db.add(OutreachEnrollment(
            contact_id=contact.id,
            sequence_id=sequence.id,
            current_step=1,
            status="active",
        ))
        return True

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    @staticmethod
    def _record_from_dict(candidate: dict) -> CandidateRecord:
        """Adapt a legacy candidate dict to the shared record shape.

        The API's manual trigger and the test suite both pass dicts, so this
        stays the seam rather than pushing the change out to every caller.
        """
        return CandidateRecord(
            name=candidate.get("name") or "Unknown Company",
            domain=candidate.get("domain"),
            industry=candidate.get("industry"),
            employee_count=candidate.get("employee_count"),
            country=candidate.get("country"),
            city=candidate.get("city"),
            state=candidate.get("state"),
            phone=candidate.get("phone"),
            website=candidate.get("website"),
            source=candidate.get("source") or "unknown",
            company_id=candidate.get("company_id"),
            raw=candidate.get("raw") or {},
        )

    async def _record_places_imports(self, db: AsyncSession, outcomes: list) -> None:
        """Point each staged Places row at what its candidate became.

        Without this the ``company_id is None`` filter in ``_source_from_places``
        never fires: nothing else on this path writes back, so every run would
        re-source, re-enrich and re-ingest the same businesses forever and never
        reach the ones behind them.

        Mirrors the operator import endpoint's writeback (routers/discovery.py)
        so a business imported by the agent and one imported by hand end up in
        the same state. ``contact_id`` is left alone — that link is the router's
        and is not needed to keep this filter honest.
        """
        by_place: dict[str, object] = {}
        for outcome in outcomes:
            place_id = (getattr(outcome.record, "raw", None) or {}).get("place_id")
            if place_id:
                by_place[place_id] = outcome
        if not by_place:
            return

        rows = (
            await db.execute(
                select(DiscoveredBusiness).where(
                    DiscoveredBusiness.place_id.in_(list(by_place))
                )
            )
        ).scalars().all()
        now = datetime.now(timezone.utc)
        for row in rows:
            outcome = by_place[row.place_id]
            if outcome.company_id is None:
                # Routed to discard. Recording *why* stops the next run from
                # re-sourcing and re-enriching a business already judged once.
                row.status = "skipped"
                row.status_reason = (
                    outcome.error or f"not imported (route={outcome.route})"
                )[:500]
                continue
            row.company_id = outcome.company_id
            row.status = "imported"
            row.imported_at = now

    # Ingestion routes -> the disposition recorded on ProspectCandidate.
    _ROUTE_TO_STATUS = {
        ROUTE_AUTO_ENROLL: "enrolled",
        ROUTE_REVIEW: "review",
        ROUTE_DISCARD: "rejected",
        ROUTE_IMPORT: "qualified",
    }

    async def run(
        self,
        db: AsyncSession,
        target_volume: Optional[int] = None,
        seed_candidates: Optional[list[dict]] = None,
        trigger: str = "scheduled",
    ) -> ProspectingRun:
        s = self.settings
        target = target_volume or s.prospecting_target_volume
        run = ProspectingRun(trigger=trigger, status="running", target_volume=target, sources_queried=[])
        db.add(run)
        await db.flush()

        if not s.prospecting_enabled:
            run.status = "paused"
            run.error = "prospecting disabled by config"
            run.completed_at = datetime.now(timezone.utc)
            await db.flush()
            return run

        try:
            icp = await self.ensure_icp(db)
            run.icp_profile_id = icp.id

            notes: dict = {}
            if seed_candidates:
                records = [self._record_from_dict(c) for c in seed_candidates]
                run.sources_queried = ["seed"]
            else:
                records, sources, notes = await self._source_candidates(db, icp, target, run)
                run.sources_queried = sources or ["seed"]

            # Daily guardrail: how many more we may enroll today.
            enroll_budget = max(0, s.prospecting_daily_max - await self._enrolled_today(db))

            # One dedup rule, one enrichment step, one scorer, one router — for
            # every source. ``score=True`` seeds the real lead score per company;
            # a run's ~25 candidates is well within budget for an inline pass.
            result = await get_ingestion_service().ingest(
                db,
                records[:target],
                icp=icp,
                enroll_budget=enroll_budget,
                score=True,
            )
            await self._record_places_imports(db, result.outcomes)

            summaries = []
            found = qualified = enrolled = review = 0
            for outcome in result.outcomes:
                found += 1
                disposition = self._ROUTE_TO_STATUS.get(outcome.route, "rejected")
                enrolled_seq = False

                if outcome.route == ROUTE_AUTO_ENROLL and outcome.company is not None:
                    enrolled_seq = await self._auto_enroll(db, outcome.company)
                    enroll_budget = max(0, enroll_budget - 1)
                    enrolled += 1
                    qualified += 1
                elif outcome.route == ROUTE_REVIEW:
                    review += 1
                    qualified += 1

                q = outcome.qualification or {}
                db.add(ProspectCandidate(
                    run_id=run.id,
                    company_id=outcome.company_id,
                    company_name=outcome.record.name,
                    domain=outcome.record.domain,
                    source=outcome.record.source or "unknown",
                    raw_data=outcome.record.as_candidate_dict(),
                    fit_score=outcome.fit_score,
                    qualification=q,
                    dedup_confidence=outcome.dedup.confidence,
                    reasoning=(
                        outcome.error
                        or q.get("reason")
                        or f"fit {outcome.fit_score}/100 via ICP components {q.get('components')}"
                    ),
                    status=disposition,
                ))
                summaries.append({
                    "company": outcome.record.name,
                    "fit_score": outcome.fit_score,
                    "disposition": disposition,
                    "enrolled_sequence": enrolled_seq,
                    "dedup_confidence": outcome.dedup.confidence,
                })

            run.companies_found = found
            run.companies_qualified = qualified
            run.companies_enrolled = enrolled
            run.companies_review_queue = review
            run.results = {
                "candidates": summaries,
                "enroll_budget_remaining": enroll_budget,
                "icp_profile": icp.name,
                "ingestion_errors": result.errors,
                **notes,
            }
            run.status = "completed"
            run.completed_at = datetime.now(timezone.utc)
            await db.flush()
        except Exception as e:
            logger.exception("Prospecting run failed")
            try:
                # The failure may have poisoned the transaction, which would make
                # any write here fail too. Roll back first so the audit row lands
                # on a clean transaction.
                await db.rollback()
                run.status = "failed"
                run.error = str(e)[:500]
                run.completed_at = datetime.now(timezone.utc)
                db.add(run)
                await db.commit()
            except Exception as inner:
                logger.error(f"Could not record prospecting failure: {inner}")
        return run


_prospector: Optional[AutonomousProspector] = None


def get_prospector() -> AutonomousProspector:
    global _prospector
    if _prospector is None:
        _prospector = AutonomousProspector()
    return _prospector
