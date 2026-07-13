"""Tests for relationship intelligence & org-chart mapping (feature 2.3).

Covers the heuristic stakeholder classifier, engagement-based relationship
strength, title-seniority org-edge inference, the multi-thread planner, and the
/companies/{id}/stakeholder-map + multi-thread-plan endpoints. No AI key in tests
→ heuristics only, which is what these assert.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.relationship import OrgRelationship, StakeholderMap
from app.services.relationship_intelligence import get_relationship_intelligence


async def _mk_contact(db, company_id, first, last, title, **kw) -> Contact:
    c = Contact(
        id=uuid.uuid4(), company_id=company_id, first_name=first, last_name=last,
        title=title, email=kw.pop("email", f"{first.lower()}@acmestamp.com"), source="test", **kw,
    )
    db.add(c)
    await db.flush()
    return c


async def _seed_committee(db, company_id):
    ceo = await _mk_contact(db, company_id, "Carol", "Chief", "CEO")
    vp = await _mk_contact(db, company_id, "Vic", "Prez", "VP Manufacturing")
    qm = await _mk_contact(db, company_id, "Quinn", "Mgr", "Quality Manager")
    op = await _mk_contact(db, company_id, "Otto", "Ops", "Machine Operator")
    return ceo, vp, qm, op


class TestStakeholderClassification:
    @pytest.mark.asyncio
    async def test_roles_from_titles(self, db_session, sample_company):
        await _seed_committee(db_session, sample_company.id)
        ri = get_relationship_intelligence()
        rows = await ri.build_stakeholder_map(db_session, sample_company.id, use_ai=False)
        by_role = {r["inferred_role"] for r in rows}
        assert "decision_maker" in by_role   # CEO / VP
        assert "champion" in by_role          # Quality Manager
        assert "end_user" in by_role          # Operator

    @pytest.mark.asyncio
    async def test_persists_and_upserts(self, db_session, sample_company):
        await _seed_committee(db_session, sample_company.id)
        ri = get_relationship_intelligence()
        await ri.build_stakeholder_map(db_session, sample_company.id, use_ai=False)
        # Re-run: should upsert, not duplicate.
        await ri.build_stakeholder_map(db_session, sample_company.id, use_ai=False)
        rows = (
            await db_session.execute(
                select(StakeholderMap).where(StakeholderMap.company_id == sample_company.id)
            )
        ).scalars().all()
        assert len(rows) == 4

    @pytest.mark.asyncio
    async def test_engagement_drives_strength(self, db_session, sample_company):
        contact = await _mk_contact(db_session, sample_company.id, "Ed", "Engaged", "COO")
        # A replied email → strongest signal.
        db_session.add(EmailSend(
            id=uuid.uuid4(), contact_id=contact.id, to_email=contact.email,
            status="replied", sent_at=datetime.now(timezone.utc),
            opened_at=datetime.now(timezone.utc), replied_at=datetime.now(timezone.utc),
        ))
        await db_session.flush()
        ri = get_relationship_intelligence()
        rows = await ri.build_stakeholder_map(db_session, sample_company.id, use_ai=False)
        row = next(r for r in rows if r["contact_id"] == str(contact.id))
        assert row["relationship_strength"] == "strong"
        assert row["engagement_score"] > 0


class TestOrgInference:
    @pytest.mark.asyncio
    async def test_reports_to_edges(self, db_session, sample_company):
        ceo, vp, qm, op = await _seed_committee(db_session, sample_company.id)
        ri = get_relationship_intelligence()
        edges = await ri.infer_org_relationships(db_session, sample_company.id)
        reports = [e for e in edges if e["relationship_type"] == "reports_to"]
        assert reports
        # The VP should report up to the CEO (nearest more-senior).
        assert any(
            e["contact_id"] == str(vp.id) and e["related_contact_id"] == str(ceo.id)
            for e in reports
        )

    @pytest.mark.asyncio
    async def test_edges_persisted_without_dupes(self, db_session, sample_company):
        await _seed_committee(db_session, sample_company.id)
        ri = get_relationship_intelligence()
        await ri.infer_org_relationships(db_session, sample_company.id)
        await ri.infer_org_relationships(db_session, sample_company.id)
        rows = (
            await db_session.execute(
                select(OrgRelationship).where(OrgRelationship.company_id == sample_company.id)
            )
        ).scalars().all()
        types = {(r.contact_id, r.related_contact_id, r.relationship_type) for r in rows}
        assert len(types) == len(rows)  # no duplicate edges


class TestMultiThreadPlan:
    @pytest.mark.asyncio
    async def test_flags_gaps_and_single_thread(self, db_session, sample_company):
        await _seed_committee(db_session, sample_company.id)
        ri = get_relationship_intelligence()
        plan = await ri.multi_thread_plan(db_session, sample_company.id, use_ai=False)
        # All contacts cold → every target role is a gap and the deal is single-threaded.
        assert plan["single_threaded_risk"] is True
        assert set(plan["missing_roles"]) == {"decision_maker", "champion", "end_user"}
        assert plan["recommendations"]


class TestRelationshipRouter:
    @pytest.mark.asyncio
    async def test_stakeholder_map_endpoint(self, client, db_session, sample_company):
        await _seed_committee(db_session, sample_company.id)
        resp = await client.get(
            f"/api/v1/companies/{sample_company.id}/stakeholder-map?use_ai=false"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["stakeholders"]) == 4
        assert "org_relationships" in body

    @pytest.mark.asyncio
    async def test_multi_thread_plan_endpoint(self, client, db_session, sample_company):
        await _seed_committee(db_session, sample_company.id)
        resp = await client.post(
            f"/api/v1/companies/{sample_company.id}/multi-thread-plan?use_ai=false"
        )
        assert resp.status_code == 200
        assert "recommendations" in resp.json()

    @pytest.mark.asyncio
    async def test_unknown_company_404(self, client):
        resp = await client.get(f"/api/v1/companies/{uuid.uuid4()}/stakeholder-map")
        assert resp.status_code == 404


# Pure-heuristic unit tests last (DB-using classes above initialize the loop first).
class TestHeuristics:
    def test_seniority_rank_orders(self):
        ri = get_relationship_intelligence()
        assert ri._seniority_rank("CEO") > ri._seniority_rank("VP Sales")
        assert ri._seniority_rank("VP Sales") > ri._seniority_rank("Machine Operator")

    def test_role_by_title(self):
        ri = get_relationship_intelligence()
        assert ri._role_by_title("Chief Financial Officer") == "decision_maker"
        assert ri._role_by_title("Line Operator") == "end_user"
