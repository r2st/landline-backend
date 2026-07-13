"""Tests for buyer-intent signal detection (feature 2.1).

Covers the IntentSignal model, its integration into rule-based lead scoring, and
the intent API router. Network/AI paths (the actual news scan) are not exercised
here — those depend on external sources and are covered by the service's
defensive fallbacks.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.intent_signal import IntentSignal
from app.services.scoring import score_company_rule_based


def _mk_signal(company_id, **kw):
    now = datetime.now(timezone.utc)
    defaults = dict(
        company_id=company_id,
        signal_type="hiring",
        source="Google News",
        title="Acme hiring 50 quality engineers",
        description="Expansion signal",
        relevance_score=90,
        urgency_score=80,
        buying_window_days=90,
        detected_at=now,
        expires_at=now + timedelta(days=90),
        actioned=False,
    )
    defaults.update(kw)
    return IntentSignal(**defaults)


class TestIntentSignalModel:
    def test_combined_score_weights_relevance_higher(self):
        sig = _mk_signal(uuid.uuid4(), relevance_score=100, urgency_score=0)
        # 100*0.6 + 0*0.4 = 60
        assert sig.combined_score == 60.0

    def test_is_active_respects_expiry(self):
        now = datetime.now(timezone.utc)
        past = _mk_signal(uuid.uuid4(), expires_at=now - timedelta(days=1))
        future = _mk_signal(uuid.uuid4(), expires_at=now + timedelta(days=1))
        never = _mk_signal(uuid.uuid4(), expires_at=None)
        assert past.is_active(now) is False
        assert future.is_active(now) is True
        assert never.is_active(now) is True


class TestIntentScoringIntegration:
    @pytest.mark.asyncio
    async def test_active_intent_signal_boosts_fit_score(self, db_session, sample_company):
        # Baseline score with no intent signals.
        base = await score_company_rule_based(db_session, sample_company)
        base_fit = base.fit_score

        # Add a strong, active intent signal and rescore.
        db_session.add(_mk_signal(sample_company.id, relevance_score=95, urgency_score=90))
        await db_session.flush()

        boosted = await score_company_rule_based(db_session, sample_company)
        assert boosted.fit_score > base_fit
        intent = boosted.factors["intent_signals"]
        assert intent["bonus_points"] > 0
        assert intent["active_signals"] == 1
        assert "hiring" in intent["types"]

    @pytest.mark.asyncio
    async def test_expired_signal_does_not_boost(self, db_session, sample_company):
        base = await score_company_rule_based(db_session, sample_company)

        now = datetime.now(timezone.utc)
        db_session.add(_mk_signal(sample_company.id, expires_at=now - timedelta(days=5)))
        await db_session.flush()

        after = await score_company_rule_based(db_session, sample_company)
        assert after.fit_score == base.fit_score
        assert after.factors["intent_signals"]["bonus_points"] == 0

    @pytest.mark.asyncio
    async def test_intent_bonus_is_capped(self, db_session, sample_company):
        # Many max-strength signals must not run the score past the cap.
        for i in range(8):
            db_session.add(_mk_signal(sample_company.id, title=f"signal {i}", relevance_score=100, urgency_score=100))
        await db_session.flush()

        scored = await score_company_rule_based(db_session, sample_company)
        assert scored.factors["intent_signals"]["bonus_points"] <= 15
        assert scored.fit_score <= 100


class TestIntentRouter:
    @pytest.mark.asyncio
    async def test_list_and_filter_signals(self, client, db_session, sample_company):
        db_session.add(_mk_signal(sample_company.id, signal_type="funding", title="Acme raises Series B", relevance_score=70))
        db_session.add(_mk_signal(sample_company.id, signal_type="hiring", title="Acme hiring ops", relevance_score=60))
        await db_session.flush()

        resp = await client.get("/api/v1/intent/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Sorted by relevance desc.
        assert data[0]["relevance_score"] >= data[1]["relevance_score"]

        resp = await client.get("/api/v1/intent/signals", params={"signal_type": "funding"})
        assert resp.status_code == 200
        assert all(s["signal_type"] == "funding" for s in resp.json())

    @pytest.mark.asyncio
    async def test_min_relevance_filter(self, client, db_session, sample_company):
        db_session.add(_mk_signal(sample_company.id, title="weak", relevance_score=35))
        db_session.add(_mk_signal(sample_company.id, title="strong", relevance_score=88))
        await db_session.flush()

        resp = await client.get("/api/v1/intent/signals", params={"min_relevance": 50})
        assert resp.status_code == 200
        titles = [s["title"] for s in resp.json()]
        assert "strong" in titles and "weak" not in titles

    @pytest.mark.asyncio
    async def test_active_only_excludes_expired(self, client, db_session, sample_company):
        now = datetime.now(timezone.utc)
        db_session.add(_mk_signal(sample_company.id, title="live", expires_at=now + timedelta(days=10)))
        db_session.add(_mk_signal(sample_company.id, title="dead", expires_at=now - timedelta(days=10)))
        await db_session.flush()

        resp = await client.get("/api/v1/intent/signals", params={"active_only": True})
        titles = [s["title"] for s in resp.json()]
        assert "live" in titles and "dead" not in titles

        resp = await client.get("/api/v1/intent/signals", params={"active_only": False})
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_company_signals_endpoint(self, client, db_session, sample_company):
        db_session.add(_mk_signal(sample_company.id))
        await db_session.flush()
        resp = await client.get(f"/api/v1/intent/companies/{sample_company.id}/signals")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_mark_actioned(self, client, db_session, sample_company):
        sig = _mk_signal(sample_company.id)
        db_session.add(sig)
        await db_session.flush()

        resp = await client.post(f"/api/v1/intent/signals/{sig.id}/actioned")
        assert resp.status_code == 200
        assert resp.json()["actioned"] is True

    @pytest.mark.asyncio
    async def test_scan_unknown_company_404(self, client):
        resp = await client.post(f"/api/v1/intent/scan/{uuid.uuid4()}")
        assert resp.status_code == 404
