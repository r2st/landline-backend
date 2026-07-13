"""Tests for competitive intelligence (feature 2.6).

Covers the Competitor / CompetitorMention models, the string-based mention
detector (no AI needed), the sentiment_service competitor_mentions extension,
and the competitive API router (CRUD, detect, win/loss). Battle-card generation
requires an AI key, so only its no-key 503 path is asserted here.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.models.competitor import Competitor, CompetitorMention
from app.models.deal import Deal
from app.services.competitive_intelligence import get_competitive_intelligence


async def _mk_competitor(db, name="ProShop ERP", aliases=None, **kw):
    comp = Competitor(
        id=uuid.uuid4(),
        name=name,
        aliases=aliases or [],
        is_active=kw.pop("is_active", True),
        **kw,
    )
    db.add(comp)
    await db.flush()
    return comp


class TestCompetitorModel:
    def test_all_names_dedupes_and_orders_by_length(self):
        comp = Competitor(id=uuid.uuid4(), name="ProShop", aliases=["PS", "ProShop ERP", "proshop"])
        names = comp.all_names()
        # Longest first, case-insensitive dedupe of "proshop"/"ProShop".
        assert names[0] == "ProShop ERP"
        assert "PS" in names
        assert sum(1 for n in names if n.lower() == "proshop") == 1


class TestMentionDetection:
    @pytest.mark.asyncio
    async def test_detects_name_and_alias(self, db_session):
        await _mk_competitor(db_session, name="ProShop ERP", aliases=["ProShop"])
        ci = get_competitive_intelligence()

        found = await ci.detect_mentions_in_text(
            db_session,
            "We're currently using ProShop for our shop floor and it's ok.",
            source="email",
            persist=True,
        )
        assert len(found) == 1
        assert found[0]["competitor_name"] == "ProShop ERP"
        assert found[0]["matched"] == "ProShop"
        assert found[0]["sentiment"] == "using"

    @pytest.mark.asyncio
    async def test_no_false_positive_on_substring(self, db_session):
        await _mk_competitor(db_session, name="Plex")
        ci = get_competitive_intelligence()
        # "complexity" contains "plex" but must not match on a word boundary.
        found = await ci.detect_mentions_in_text(
            db_session, "This adds complexity to our process.", persist=False
        )
        assert found == []

    @pytest.mark.asyncio
    async def test_inactive_competitor_not_matched(self, db_session):
        await _mk_competitor(db_session, name="OldVendor", is_active=False)
        ci = get_competitive_intelligence()
        found = await ci.detect_mentions_in_text(db_session, "We looked at OldVendor once.", persist=False)
        assert found == []

    @pytest.mark.asyncio
    async def test_stance_evaluating(self, db_session):
        await _mk_competitor(db_session, name="Tulip")
        ci = get_competitive_intelligence()
        found = await ci.detect_mentions_in_text(
            db_session, "We're currently evaluating Tulip alongside a few others.", persist=False
        )
        assert found[0]["sentiment"] == "evaluating"

    @pytest.mark.asyncio
    async def test_persist_creates_mention_row(self, db_session, sample_company):
        comp = await _mk_competitor(db_session, name="MachineMetrics")
        ci = get_competitive_intelligence()
        await ci.detect_mentions_in_text(
            db_session,
            "MachineMetrics quoted us last week.",
            source="call",
            company_id=sample_company.id,
            persist=True,
        )
        from sqlalchemy import select
        rows = (await db_session.execute(
            select(CompetitorMention).where(CompetitorMention.competitor_id == comp.id)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].source == "call"
        assert rows[0].company_id == sample_company.id


class TestSentimentCompetitorField:
    @pytest.mark.asyncio
    async def test_analyze_reply_includes_competitor_mentions_key(self):
        from app.services.sentiment_service import SentimentAnalyzer

        analyzer = SentimentAnalyzer()
        # No AI key in tests → the except path returns the default shape, which
        # must still include the new competitor_mentions field.
        result = await analyzer.analyze_reply("We already use a competitor product.")
        assert "competitor_mentions" in result
        assert isinstance(result["competitor_mentions"], list)


class TestCompetitiveRouter:
    @pytest.mark.asyncio
    async def test_create_and_get_competitor(self, client):
        resp = await client.post(
            "/api/v1/competitive/competitors",
            json={"name": "Fulcrum", "domain": "fulcrumpro.com", "aliases": ["Fulcrum Pro"]},
        )
        assert resp.status_code == 201
        cid = resp.json()["id"]

        resp = await client.get(f"/api/v1/competitive/competitors/{cid}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Fulcrum"

    @pytest.mark.asyncio
    async def test_duplicate_name_conflict(self, client):
        await client.post("/api/v1/competitive/competitors", json={"name": "DupCo"})
        resp = await client.post("/api/v1/competitive/competitors", json={"name": "dupco"})
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_list_and_update_competitor(self, client):
        resp = await client.post("/api/v1/competitive/competitors", json={"name": "Steltix"})
        cid = resp.json()["id"]

        resp = await client.patch(
            f"/api/v1/competitive/competitors/{cid}",
            json={"weaknesses": ["expensive", "slow onboarding"], "is_active": True},
        )
        assert resp.status_code == 200
        assert resp.json()["weaknesses"] == ["expensive", "slow onboarding"]

        resp = await client.get("/api/v1/competitive/competitors")
        assert any(c["name"] == "Steltix" for c in resp.json())

    @pytest.mark.asyncio
    async def test_battle_card_without_ai_returns_503(self, client):
        resp = await client.post("/api/v1/competitive/competitors", json={"name": "E2 Shop"})
        cid = resp.json()["id"]
        resp = await client.post(f"/api/v1/competitive/competitors/{cid}/battle-card")
        # No ANTHROPIC_API_KEY configured in the test env.
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_detect_endpoint(self, client, db_session):
        await _mk_competitor(db_session, name="JobBOSS")
        await db_session.flush()
        resp = await client.post(
            "/api/v1/competitive/detect",
            json={"text": "We're on JobBOSS right now.", "source": "email", "persist": False},
        )
        assert resp.status_code == 200
        assert resp.json()["mentions_found"] == 1

    @pytest.mark.asyncio
    async def test_win_loss_correlation(self, client, db_session, sample_company):
        comp = await _mk_competitor(db_session, name="Epicor")
        db_session.add(CompetitorMention(
            id=uuid.uuid4(),
            competitor_id=comp.id,
            company_id=sample_company.id,
            source="email",
            detected_at=datetime.now(timezone.utc),
        ))
        db_session.add(Deal(
            id=uuid.uuid4(),
            company_id=sample_company.id,
            title="D",
            status="won",
        ))
        await db_session.flush()

        resp = await client.get("/api/v1/competitive/win-loss")
        assert resp.status_code == 200
        rows = resp.json()["competitors"]
        epicor = next(r for r in rows if r["competitor"] == "Epicor")
        assert epicor["deals_won"] == 1
        assert epicor["companies_mentioned"] == 1
