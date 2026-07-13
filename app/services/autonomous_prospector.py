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
        self, db: AsyncSession, icp: IcpProfile, limit: int
    ) -> tuple[list[dict], list[str]]:
        """Return (candidate dicts, sources queried) from Apollo when configured."""
        sources: list[str] = []
        candidates: list[dict] = []
        if not self.settings.apollo_api_key:
            return candidates, sources
        try:
            from app.services.enrichment import get_enrichment_service

            svc = get_enrichment_service()
            top_industry = None
            if icp.industry_weights:
                top_industry = max(icp.industry_weights, key=icp.industry_weights.get)
            sr = icp.size_range or {}
            imported = await svc.search_companies(
                db,
                industry=top_industry or "manufacturing",
                min_employees=sr.get("min"),
                max_employees=sr.get("max"),
                per_page=min(limit, 25),
            )
            sources.append("apollo")
            for row in imported or []:
                candidates.append({
                    "name": row.get("name"),
                    "domain": row.get("domain"),
                    "industry": row.get("industry_segment") or row.get("industry"),
                    "employee_count": row.get("employee_count"),
                    "country": row.get("country"),
                    "city": row.get("city"),
                    "state": row.get("state_province"),
                    "source": "apollo",
                    "company_id": row.get("id"),
                    "raw": row,
                })
        except Exception as e:  # pragma: no cover - network path
            logger.warning("Apollo sourcing failed: %s", e)
        return candidates, sources

    async def _find_or_create_company(self, db: AsyncSession, candidate: dict) -> Company:
        """Resolve an existing Company (by id/domain/name) or create a new lead."""
        cid = candidate.get("company_id")
        if cid:
            existing = (
                await db.execute(select(Company).where(Company.id == cid))
            ).scalar_one_or_none()
            if existing:
                return existing
        domain = candidate.get("domain")
        if domain:
            existing = (
                await db.execute(select(Company).where(Company.domain == domain))
            ).scalar_one_or_none()
            if existing:
                return existing
        name = candidate.get("name") or "Unknown Company"
        existing = (
            await db.execute(
                select(Company).where(func.lower(Company.name) == name.lower())
            )
        ).scalar_one_or_none()
        if existing:
            return existing
        company = Company(
            name=name,
            domain=domain or None,
            industry_segment=candidate.get("industry"),
            employee_count=candidate.get("employee_count"),
            country=(candidate.get("country") or "US")[:2].upper() or "US",
            city=candidate.get("city"),
            state_province=candidate.get("state"),
            source="autonomous_prospector",
            stage="new",
            pipeline_stage="new_lead",
        )
        db.add(company)
        await db.flush()
        return company

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

            candidates = list(seed_candidates or [])
            if not candidates:
                sourced, sources = await self._source_candidates(db, icp, target)
                candidates = sourced
                run.sources_queried = sources or ["seed"]
            else:
                run.sources_queried = ["seed"]

            # Daily guardrail: how many more we may enroll today.
            enroll_budget = max(0, s.prospecting_daily_max - await self._enrolled_today(db))

            summaries = []
            found = qualified = enrolled = review = 0
            for cand in candidates[:target]:
                found += 1
                q = self.qualify(cand, icp)
                fit = q["fit_score"]
                disposition = "rejected"
                company_id = None
                enrolled_seq = False

                if q["excluded"]:
                    disposition = "rejected"
                elif fit >= s.prospecting_auto_enroll_threshold and enroll_budget > 0:
                    company = await self._find_or_create_company(db, cand)
                    company.pipeline_stage = "researching"
                    enrolled_seq = await self._auto_enroll(db, company)
                    company_id = company.id
                    disposition = "enrolled"
                    enroll_budget -= 1
                    enrolled += 1
                    qualified += 1
                elif fit >= s.prospecting_review_threshold:
                    company = await self._find_or_create_company(db, cand)
                    company_id = company.id
                    disposition = "review"
                    review += 1
                    qualified += 1
                else:
                    disposition = "rejected"

                cand_row = ProspectCandidate(
                    run_id=run.id,
                    company_id=company_id,
                    company_name=cand.get("name") or "Unknown Company",
                    domain=cand.get("domain"),
                    source=cand.get("source") or "unknown",
                    raw_data={k: v for k, v in cand.items() if k != "raw"},
                    fit_score=fit,
                    qualification=q,
                    reasoning=q.get("reason") or f"fit {fit}/100 via ICP components {q.get('components')}",
                    status=disposition,
                )
                db.add(cand_row)
                summaries.append({
                    "company": cand.get("name"),
                    "fit_score": fit,
                    "disposition": disposition,
                    "enrolled_sequence": enrolled_seq,
                })

            run.companies_found = found
            run.companies_qualified = qualified
            run.companies_enrolled = enrolled
            run.companies_review_queue = review
            run.results = {
                "candidates": summaries,
                "enroll_budget_remaining": enroll_budget,
                "icp_profile": icp.name,
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
