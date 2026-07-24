"""Relationship intelligence & org-chart mapping (feature 2.3).

Builds, per company:

* a **stakeholder map** — each contact's inferred buying-committee role
  (decision maker / champion / coach / blocker / end user), relationship strength
  (cold / warm / strong), engagement score and trend;
* an **org chart** — inferred reports_to / peers_with edges between contacts;
* a **multi-thread plan** — which committee roles are covered vs. missing and
  concrete recommendations to fill the gaps.

The baseline is deterministic and needs no LLM: seniority is read from titles,
relationship strength from email engagement, and reporting edges from the title
hierarchy within a company. When an Anthropic key is configured the classifier is
enriched by Claude (better role calls + tailored multi-thread advice), but every
path degrades cleanly to the heuristic so the feature works on a fresh DB and in
tests without a key.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.relationship import OrgRelationship, StakeholderMap
from app.services.call_queue import engagement_by_contact
from app.services.llm_providers import ChainClient, any_llm_available, chain_client

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

# Seniority ladder — higher rank = more senior. Used for role inference and to
# infer reports_to edges (a lower rank in the same department reports up).
_SENIORITY = [
    (("ceo", "chief executive", "founder", "owner", "president", "principal"), 100),
    (("cfo", "chief financial", "coo", "cto", "chief "), 95),
    (("vp", "vice president", "svp", "evp", "head of"), 80),
    (("director", "dir "), 65),
    (("manager", "supervisor", "lead", "foreman"), 45),
    (("engineer", "operator", "technician", "analyst", "specialist", "coordinator", "buyer"), 25),
]

# Title keywords → likely committee role (heuristic baseline before AI).
_ROLE_HINTS = {
    "decision_maker": ("ceo", "cfo", "coo", "owner", "founder", "president", "vp", "vice president", "chief", "head of", "director of operations", "plant manager", "general manager"),
    "end_user": ("operator", "technician", "engineer", "foreman", "supervisor", "quality inspector", "machinist"),
    "champion": ("continuous improvement", "process", "quality manager", "operations manager", "production manager"),
    "coach": ("assistant", "coordinator", "analyst", "buyer", "procurement"),
}

# Committee roles a healthy multi-threaded deal should cover.
_TARGET_ROLES = ("decision_maker", "champion", "end_user")


class RelationshipIntelligence:
    """Stakeholder classification, org-chart inference, multi-thread planning."""

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
            start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
            end = max(text.rfind("}"), text.rfind("]"))
            if start != -1 and end > start:
                return json.loads(text[start:end + 1])
            raise

    # ------------------------------------------------------------------ #
    # Heuristics
    # ------------------------------------------------------------------ #

    @staticmethod
    def _seniority_rank(title: Optional[str]) -> int:
        t = (title or "").lower()
        if not t.strip():
            return 10
        for keywords, rank in _SENIORITY:
            if any(k in t for k in keywords):
                return rank
        return 15

    @classmethod
    def _role_by_title(cls, title: Optional[str]) -> str:
        t = (title or "").lower()
        if not t.strip():
            return "unknown"
        # Most-senior signal wins for decision_maker; otherwise first match.
        for role in ("decision_maker", "champion", "end_user", "coach"):
            if any(k in t for k in _ROLE_HINTS[role]):
                return role
        return "unknown"

    @staticmethod
    def _strength_and_score(eng) -> tuple[str, int]:
        """Map email engagement to a (strength, 0-100 score)."""
        if eng is None:
            return "cold", 0
        score = 0
        score += min(eng.opens, 5) * 6          # up to 30
        score += min(eng.clicks, 3) * 12        # up to 36
        score += min(eng.replies, 3) * 18       # up to 54 (replies matter most)
        score = min(score, 100)
        if eng.replies > 0 or score >= 60:
            strength = "strong"
        elif eng.clicks > 0 or eng.opens >= 3 or score >= 25:
            strength = "warm"
        else:
            strength = "cold"
        return strength, score

    @staticmethod
    def _trend(eng, now: datetime) -> str:
        if eng is None or eng.last_contact_at is None:
            return "unknown"
        days = (now - eng.last_contact_at).total_seconds() / 86400.0
        if eng.replies > 0 and days <= 14:
            return "rising"
        if days > 45:
            return "declining"
        return "flat"

    # ------------------------------------------------------------------ #
    # Stakeholder map
    # ------------------------------------------------------------------ #

    async def build_stakeholder_map(
        self, db: AsyncSession, company_id: UUID, *, persist: bool = True, use_ai: bool = True
    ) -> list[dict]:
        """Classify every contact at a company; upsert StakeholderMap rows."""
        contacts = (
            await db.execute(select(Contact).where(Contact.company_id == company_id))
        ).scalars().all()
        if not contacts:
            return []

        now = datetime.now(timezone.utc)
        eng_by_contact = await engagement_by_contact(db, [c.id for c in contacts])

        # Heuristic baseline for each contact.
        baseline: dict[UUID, dict] = {}
        for c in contacts:
            eng = eng_by_contact.get(c.id)
            strength, score = self._strength_and_score(eng)
            baseline[c.id] = {
                "contact_id": c.id,
                "name": c.full_name,
                "title": c.title,
                "inferred_role": self._role_by_title(c.title),
                "relationship_strength": strength,
                "engagement_score": score,
                "engagement_trend": self._trend(eng, now),
                "last_interaction": eng.last_contact_at if eng else None,
                "rationale": f"Title-based role; engagement score {score}.",
                "ai_analysis": {},
            }

        # Optional AI enrichment of the role calls (relationship strength/score
        # stay engagement-derived — the AI can't see engagement better than we do).
        if use_ai and self.available:
            valid_roles = set(_ROLE_HINTS) | {"blocker"}
            ai_roles = await self._ai_classify_roles(contacts)
            for cid, info in ai_roles.items():
                if cid in baseline and info.get("role") in valid_roles:
                    baseline[cid]["inferred_role"] = info["role"]
                    baseline[cid]["rationale"] = info.get("rationale") or baseline[cid]["rationale"]
                    baseline[cid]["ai_analysis"] = info

        results = [baseline[c.id] for c in contacts]

        if persist:
            existing = {
                s.contact_id: s
                for s in (
                    await db.execute(
                        select(StakeholderMap).where(StakeholderMap.company_id == company_id)
                    )
                ).scalars().all()
            }
            for row in results:
                sm = existing.get(row["contact_id"])
                if sm is None:
                    sm = StakeholderMap(company_id=company_id, contact_id=row["contact_id"])
                    db.add(sm)
                sm.inferred_role = row["inferred_role"]
                sm.relationship_strength = row["relationship_strength"]
                sm.engagement_score = row["engagement_score"]
                sm.engagement_trend = row["engagement_trend"]
                sm.last_interaction = row["last_interaction"]
                sm.rationale = row["rationale"]
                sm.ai_analysis = row["ai_analysis"]
            await db.flush()

        # Serialize contact_id/last_interaction for API friendliness.
        for row in results:
            row["contact_id"] = str(row["contact_id"])
            if row["last_interaction"] is not None:
                row["last_interaction"] = row["last_interaction"].isoformat()
        return results

    async def _ai_classify_roles(self, contacts: list[Contact]) -> dict:
        """Claude classifies each contact's committee role. Returns {contact_id: {...}}."""
        roster = "\n".join(
            f'- id={c.id} | {c.full_name} | title="{c.title or "unknown"}" | dept="{c.department or ""}"'
            for c in contacts
        )
        prompt = f"""You map B2B buying committees for Apprend Technologies (manufacturing
quality software). Classify each contact into ONE role:
- decision_maker: signs the check / owns the budget
- champion: advocates for us internally
- coach: feeds us intel but isn't the buyer
- blocker: resists change / defends the incumbent
- end_user: operates the tool day to day

CONTACTS:
{roster}

Return ONLY a JSON array; one object per contact:
[{{"id": "<id>", "role": "decision_maker|champion|coach|blocker|end_user", "rationale": "one short reason"}}]"""
        try:
            message = await self._get_client().messages.create(
                model=self.model,
                max_tokens=1200,
                messages=[{"role": "user", "content": prompt}],
            )
            data = self._parse_json(message.content[0].text)
        except Exception as e:  # noqa: BLE001 — enrichment is best-effort
            logger.warning("Stakeholder AI classification failed: %s", e)
            return {}

        out: dict = {}
        rows = data if isinstance(data, list) else data.get("contacts", [])
        valid_ids = {str(c.id): c.id for c in contacts}
        for item in rows or []:
            rid = str(item.get("id", ""))
            if rid in valid_ids and item.get("role"):
                out[valid_ids[rid]] = {"role": item["role"], "rationale": item.get("rationale")}
        return out

    # ------------------------------------------------------------------ #
    # Org chart (reporting edges)
    # ------------------------------------------------------------------ #

    async def infer_org_relationships(
        self, db: AsyncSession, company_id: UUID, *, persist: bool = True
    ) -> list[dict]:
        """Infer reports_to / peers_with edges from title seniority within a company."""
        contacts = (
            await db.execute(select(Contact).where(Contact.company_id == company_id))
        ).scalars().all()
        if len(contacts) < 2:
            return []

        ranked = sorted(
            ({"c": c, "rank": self._seniority_rank(c.title)} for c in contacts),
            key=lambda x: x["rank"],
            reverse=True,
        )
        edges: list[dict] = []
        top_rank = ranked[0]["rank"]
        # Peers: contacts within the same seniority band.
        for i, a in enumerate(ranked):
            for b in ranked[i + 1:]:
                if a["rank"] == b["rank"] and a["rank"] >= 25:
                    edges.append({
                        "contact_id": a["c"].id,
                        "related_contact_id": b["c"].id,
                        "relationship_type": "peers_with",
                        "confidence": 45,
                        "source": "title_inference",
                    })
        # Reports_to: each non-top contact reports to the nearest more-senior person.
        for idx, node in enumerate(ranked):
            if node["rank"] == top_rank:
                continue
            # Nearest senior above this node.
            senior = next((s for s in ranked[:idx][::-1] if s["rank"] > node["rank"]), None)
            if senior is not None:
                gap = senior["rank"] - node["rank"]
                edges.append({
                    "contact_id": node["c"].id,
                    "related_contact_id": senior["c"].id,
                    "relationship_type": "reports_to",
                    # Closer ranks → higher confidence in a direct report line.
                    "confidence": max(30, 75 - gap // 2),
                    "source": "title_inference",
                })

        if persist and edges:
            existing = {
                (e.contact_id, e.related_contact_id, e.relationship_type)
                for e in (
                    await db.execute(
                        select(OrgRelationship).where(OrgRelationship.company_id == company_id)
                    )
                ).scalars().all()
            }
            for edge in edges:
                key = (edge["contact_id"], edge["related_contact_id"], edge["relationship_type"])
                if key in existing:
                    continue
                db.add(OrgRelationship(company_id=company_id, **edge))
            await db.flush()

        for edge in edges:
            edge["contact_id"] = str(edge["contact_id"])
            edge["related_contact_id"] = str(edge["related_contact_id"])
        return edges

    # ------------------------------------------------------------------ #
    # Multi-thread plan
    # ------------------------------------------------------------------ #

    async def multi_thread_plan(
        self, db: AsyncSession, company_id: UUID, *, use_ai: bool = True
    ) -> dict:
        """Assess committee coverage and recommend outreach to fill the gaps."""
        company = (
            await db.execute(select(Company).where(Company.id == company_id))
        ).scalar_one_or_none()
        if company is None:
            raise ValueError("Company not found")

        stakeholders = await self.build_stakeholder_map(db, company_id, persist=True, use_ai=use_ai)

        covered_roles = {
            s["inferred_role"]
            for s in stakeholders
            if s["inferred_role"] != "unknown" and s["relationship_strength"] != "cold"
        }
        missing = [r for r in _TARGET_ROLES if r not in covered_roles]

        # Single-threaded risk: only one warm/strong relationship.
        engaged = [s for s in stakeholders if s["relationship_strength"] in ("warm", "strong")]
        single_threaded = len(engaged) <= 1

        recommendations = self._heuristic_recommendations(stakeholders, missing, single_threaded)

        plan = {
            "company_id": str(company_id),
            "company_name": company.name,
            "stakeholder_count": len(stakeholders),
            "engaged_count": len(engaged),
            "single_threaded_risk": single_threaded,
            "covered_roles": sorted(covered_roles),
            "missing_roles": missing,
            "recommendations": recommendations,
        }

        if use_ai and self.available:
            ai_plan = await self._ai_multi_thread(company, stakeholders, missing, single_threaded)
            if ai_plan.get("recommendations"):
                plan["recommendations"] = ai_plan["recommendations"]
            if ai_plan.get("summary"):
                plan["summary"] = ai_plan["summary"]
        return plan

    @staticmethod
    def _heuristic_recommendations(stakeholders, missing, single_threaded) -> list[dict]:
        recs: list[dict] = []
        if single_threaded:
            recs.append({
                "priority": "high",
                "type": "reduce_single_thread_risk",
                "detail": "Deal is single-threaded — engaging only one stakeholder. "
                          "Widen coverage before it stalls if that person goes quiet.",
            })
        role_label = {
            "decision_maker": "the economic buyer (budget owner)",
            "champion": "an internal champion to advocate for us",
            "end_user": "an end user who feels the day-to-day pain",
        }
        for role in missing:
            recs.append({
                "priority": "high" if role == "decision_maker" else "medium",
                "type": "fill_role_gap",
                "role": role,
                "detail": f"No warm contact for {role_label.get(role, role)}. "
                          "Identify and reach out to fill this gap.",
            })
        # Nudge cold-but-senior contacts.
        for s in stakeholders:
            if s["inferred_role"] == "decision_maker" and s["relationship_strength"] == "cold":
                recs.append({
                    "priority": "medium",
                    "type": "warm_up_decision_maker",
                    "contact_id": s["contact_id"],
                    "detail": f"{s['name']} looks like a decision maker but is still cold — prioritize warming them up.",
                })
        if not recs:
            recs.append({
                "priority": "low",
                "type": "maintain",
                "detail": "Coverage looks healthy across roles — keep all threads warm.",
            })
        return recs

    async def _ai_multi_thread(self, company, stakeholders, missing, single_threaded) -> dict:
        roster = "\n".join(
            f'- {s["name"]} | role={s["inferred_role"]} | strength={s["relationship_strength"]} '
            f'| engagement={s["engagement_score"]}'
            for s in stakeholders
        )
        prompt = f"""You advise Apprend Technologies reps on multi-threading a manufacturing deal.

COMPANY: {company.name} ({company.industry_segment or "manufacturing"})
STAKEHOLDERS:
{roster}
MISSING COMMITTEE ROLES: {missing or "none"}
SINGLE-THREADED: {single_threaded}

Give a short assessment and 2-4 concrete, prioritized recommendations to
multi-thread this deal (who to engage next and why). Be specific to what's
missing above.

Return ONLY valid JSON:
{{
  "summary": "one-paragraph assessment",
  "recommendations": [
    {{"priority": "high|medium|low", "type": "short_slug", "detail": "concrete action"}}
  ]
}}"""
        try:
            message = await self._get_client().messages.create(
                model=self.model,
                max_tokens=900,
                messages=[{"role": "user", "content": prompt}],
            )
            return self._parse_json(message.content[0].text)
        except Exception as e:  # noqa: BLE001 — best-effort; heuristic already set
            logger.warning("Multi-thread AI planning failed: %s", e)
            return {}


_ri: RelationshipIntelligence | None = None


def get_relationship_intelligence() -> RelationshipIntelligence:
    global _ri
    if _ri is None:
        _ri = RelationshipIntelligence()
    return _ri
