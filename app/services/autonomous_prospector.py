"""Autonomous prospecting orchestrator (feature 3.1).

Chains the existing sales stack into a hands-off pipeline:

    learn ICP (from won deals)
        → source candidate companies (Apollo, or supplied seeds)
        → qualify each against the ICP (fit score 0-100)
        → for high-confidence matches: persist + advance to outreach + (best-effort) enroll
        → for borderline matches: persist as a new lead in the human review queue
        → for excluded / poor fits: record and skip

Everything is wrapped so a single bad candidate never fails the run, and hard
guardrails (daily cap, enable flag, learned exclusion criteria) bound what the
agent can do autonomously.

The core is deterministic and network-free: ``run`` accepts a list of candidate
dicts (used by the API's manual trigger and by tests). When no seeds are given
and Apollo is configured, it sources candidates from Apollo automatically.
"""

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.outreach import OutreachEnrollment
from app.models.prospecting import (
    IcpProfile,
    ProspectCandidate,
    ProspectingRun,
)
from app.models.sequence import OutreachSequence
from app.services.ingestion import (
    ROUTE_AUTO_ENROLL,
    ROUTE_DISCARD,
    ROUTE_IMPORT,
    ROUTE_REVIEW,
    CandidateRecord,
    get_ingestion_service,
)

logger = logging.getLogger(__name__)


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
            "Plastics": 0.7,
            "Aerospace": 0.6,
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

        # Deactivate previous profiles.
        for prev in (
            await db.execute(select(IcpProfile).where(IcpProfile.is_active == True))  # noqa: E712
        ).scalars().all():
            prev.is_active = False

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
        )
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
    async def _source_candidates(
        self, db: AsyncSession, icp: IcpProfile, limit: int, run: ProspectingRun
    ) -> tuple[list[CandidateRecord], list[str], dict]:
        """Return (records, sources queried, sourcing notes) from Apollo.

        Apollo no longer imports anything here — it returns records and the
        ingestion service persists them. ``notes`` carries why sourcing stopped,
        so a rate-limited run can't masquerade as a run that found nothing.
        """
        sources: list[str] = []
        notes: dict = {}
        if not self.settings.apollo_api_key:
            return [], sources, notes

        from app.services.enrichment import get_enrichment_service

        svc = get_enrichment_service()
        top_industry = None
        if icp.industry_weights:
            top_industry = max(icp.industry_weights, key=icp.industry_weights.get)
        sr = icp.size_range or {}
        search = await svc.search_companies(
            db,
            industry=top_industry or "manufacturing",
            min_employees=sr.get("min"),
            max_employees=sr.get("max"),
            target_volume=limit,
            run_id=run.id,
            icp_profile_id=icp.id,
        )
        sources.append("apollo")
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
        return search.records, sources, notes

    async def _auto_enroll(self, db: AsyncSession, company: Company) -> bool:
        """Advance a company to outreach and best-effort enroll a primary contact.

        Returns True if a real sequence enrollment was created. Advancing the
        pipeline stage always happens for a high-confidence match.
        """
        company.pipeline_stage = "outreach"
        contact = (
            await db.execute(
                select(Contact).where(Contact.company_id == company.id)
                .order_by(Contact.is_primary.desc())
            )
        ).scalars().first()
        sequence = (
            await db.execute(
                select(OutreachSequence).where(
                    OutreachSequence.is_active == True,  # noqa: E712
                    OutreachSequence.status == "active",
                )
            )
        ).scalars().first()
        if contact is None or sequence is None:
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
            run.status = "failed"
            run.error = str(e)
            run.completed_at = datetime.now(timezone.utc)
            await db.flush()
        return run


_prospector: Optional[AutonomousProspector] = None


def get_prospector() -> AutonomousProspector:
    global _prospector
    if _prospector is None:
        _prospector = AutonomousProspector()
    return _prospector
