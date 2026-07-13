"""Tests for the AI sales coach (feature 2.4).

Exercises per-rep metric aggregation, snapshot generation (with the rule-based
fallback that runs when no AI key is configured — the test env has none), and
the coaching API router including role gating on team analytics.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.call import CallTask
from app.models.deal import Deal
from app.models.email_template import EmailSend
from app.models.interaction import Interaction
from app.models.rep_performance import RepPerformance
from app.services.sales_coach import (
    compute_rep_metrics,
    generate_rep_performance,
    period_bounds,
)


async def _seed_rep_activity(db, user_id, company, contact):
    """Seed a rep's email/call/interaction/deal activity within the last few days."""
    now = datetime.now(timezone.utc)
    recent = now - timedelta(days=2)

    # 4 emails: 3 opened, 2 replied.
    for i in range(4):
        db.add(EmailSend(
            id=uuid.uuid4(),
            contact_id=contact.id,
            user_id=user_id,
            to_email=contact.email,
            subject=f"Hi {i}",
            status="sent",
            sent_at=recent,
            opened_at=recent if i < 3 else None,
            replied_at=recent if i < 2 else None,
        ))

    # 3 completed calls, 1 a live connect.
    for i in range(3):
        db.add(CallTask(
            id=uuid.uuid4(),
            user_id=user_id,
            company_id=company.id,
            contact_id=contact.id,
            status="completed",
            outcome="connected" if i == 0 else "voicemail",
            completed_at=recent,
        ))

    # 2 interactions.
    for i in range(2):
        db.add(Interaction(
            id=uuid.uuid4(),
            user_id=user_id,
            company_id=company.id,
            contact_id=contact.id,
            interaction_type="call",
            occurred_at=recent,
        ))

    # A won deal at the touched company.
    db.add(Deal(
        id=uuid.uuid4(),
        company_id=company.id,
        title="Deal",
        value=10000,
        status="won",
        created_at=recent,
        updated_at=recent,
    ))
    await db.flush()


class TestRepMetrics:
    @pytest.mark.asyncio
    async def test_compute_rep_metrics(self, db_session, make_user, sample_company, sample_contact):
        user = await make_user("rep1@landline.app")
        await _seed_rep_activity(db_session, user.id, sample_company, sample_contact)

        start, end = period_bounds("weekly")
        m = await compute_rep_metrics(db_session, user.id, start, end)

        assert m["emails_sent"] == 4
        assert m["emails_opened"] == 3
        assert m["emails_replied"] == 2
        assert m["reply_rate"] == 50.0
        assert m["open_rate"] == 75.0
        assert m["calls_completed"] == 3
        assert m["call_connects"] == 1
        assert m["connect_rate"] == pytest.approx(33.3, abs=0.1)
        assert m["interactions_total"] == 2
        # Deal attributed via the interaction/call company link.
        assert m["deals_won"] == 1
        assert m["won_value"] == 10000.0

    @pytest.mark.asyncio
    async def test_metrics_empty_for_idle_rep(self, db_session, make_user):
        user = await make_user("idle@landline.app")
        start, end = period_bounds("weekly")
        m = await compute_rep_metrics(db_session, user.id, start, end)
        assert m["emails_sent"] == 0
        assert m["reply_rate"] == 0.0
        assert m["deals_won"] == 0


class TestGenerateSnapshot:
    @pytest.mark.asyncio
    async def test_generate_creates_snapshot_with_fallback_coaching(
        self, db_session, make_user, sample_company, sample_contact
    ):
        user = await make_user("rep2@landline.app")
        await _seed_rep_activity(db_session, user.id, sample_company, sample_contact)

        snap = await generate_rep_performance(db_session, user, period_type="weekly")
        assert isinstance(snap, RepPerformance)
        assert snap.metrics["emails_sent"] == 4
        # No AI key in tests → rule-based fallback, model_used is None.
        assert snap.model_used is None
        assert "items" in snap.coaching_recommendations
        assert "summary" in snap.ai_insights

    @pytest.mark.asyncio
    async def test_generate_is_idempotent_upsert(self, db_session, make_user, sample_company, sample_contact):
        user = await make_user("rep3@landline.app")
        await _seed_rep_activity(db_session, user.id, sample_company, sample_contact)

        first = await generate_rep_performance(db_session, user, period_type="weekly")
        first_id = first.id
        second = await generate_rep_performance(db_session, user, period_type="weekly")
        # Same period → updated in place, not a new row.
        assert second.id == first_id


class TestCoachingRouter:
    @pytest.mark.asyncio
    async def test_my_performance(self, client, db_session, make_user, auth_headers, sample_company, sample_contact):
        user = await make_user("rep4@landline.app")
        await _seed_rep_activity(db_session, user.id, sample_company, sample_contact)
        await db_session.flush()

        resp = await client.get("/api/v1/coaching/my-performance", headers=auth_headers(user))
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == str(user.id)
        assert body["metrics"]["emails_sent"] == 4

    @pytest.mark.asyncio
    async def test_recommendations_endpoint(self, client, db_session, make_user, auth_headers):
        user = await make_user("rep5@landline.app")
        await db_session.flush()
        resp = await client.get("/api/v1/coaching/recommendations", headers=auth_headers(user))
        assert resp.status_code == 200
        assert "recommendations" in resp.json()

    @pytest.mark.asyncio
    async def test_team_requires_privileged_role(
        self, client, db_session, make_user, admin_user, auth_headers
    ):
        agent = await make_user("rep6@landline.app")
        await db_session.flush()

        # Agent is forbidden.
        resp = await client.get("/api/v1/coaching/team", headers=auth_headers(agent))
        assert resp.status_code == 403

        # Admin allowed.
        resp = await client.get("/api/v1/coaching/team", headers=auth_headers(admin_user))
        assert resp.status_code == 200
        assert "benchmarks" in resp.json()
        assert "reps" in resp.json()

    @pytest.mark.asyncio
    async def test_generate_for_other_requires_privilege(
        self, client, db_session, make_user, admin_user, auth_headers
    ):
        agent = await make_user("rep7@landline.app")
        other = await make_user("rep8@landline.app")
        await db_session.flush()

        # Agent cannot generate for someone else.
        resp = await client.post(
            "/api/v1/coaching/generate",
            params={"target_user_id": str(other.id)},
            headers=auth_headers(agent),
        )
        assert resp.status_code == 403

        # Admin can.
        resp = await client.post(
            "/api/v1/coaching/generate",
            params={"target_user_id": str(other.id)},
            headers=auth_headers(admin_user),
        )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == str(other.id)
