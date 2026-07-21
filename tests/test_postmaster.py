"""Tests for the Google Postmaster Tools integration (P2 audit)."""

from datetime import date

import pytest
from sqlalchemy import select

from app.models.postmaster import PostmasterAccount, PostmasterMetric
from app.services import postmaster_service


# --- Parsing ----------------------------------------------------------------

def test_parse_stat_date():
    assert postmaster_service._parse_stat_date(
        "domains/example.com/trafficStats/20260715"
    ) == date(2026, 7, 15)
    assert postmaster_service._parse_stat_date("domains/x/trafficStats/bad") is None
    assert postmaster_service._parse_stat_date("") is None


def test_parse_traffic_stat_maps_fields():
    raw = {
        "name": "domains/apprend.tech/trafficStats/20260715",
        "domainReputation": "HIGH",
        "userReportedSpamRatio": 0.001,
        "spfSuccessRatio": 1.0,
        "dkimSuccessRatio": 0.98,
        "dmarcSuccessRatio": 0.97,
        "inboundEncryptionRatio": 1.0,
        "outboundEncryptionRatio": 0.9,
        "ipReputations": [{"reputation": "HIGH", "ipCount": 3}],
        "deliveryErrors": [{"errorClass": "PERMANENT_ERROR", "errorRatio": 0.01}],
    }
    parsed = postmaster_service.parse_traffic_stat(raw)
    assert parsed["metric_date"] == date(2026, 7, 15)
    assert parsed["domain_reputation"] == "HIGH"
    assert parsed["spam_rate"] == 0.001
    assert parsed["spf_success_rate"] == 1.0
    assert parsed["delivery_errors"][0]["errorClass"] == "PERMANENT_ERROR"
    assert parsed["raw"] == raw


# --- pull_all guard paths ---------------------------------------------------

@pytest.mark.asyncio
async def test_pull_all_not_configured(db_session, monkeypatch):
    monkeypatch.setattr(postmaster_service, "is_configured", lambda: False)
    result = await postmaster_service.pull_all(db_session)
    assert result["status"] == "not_configured"


@pytest.mark.asyncio
async def test_pull_all_no_accounts(db_session, monkeypatch):
    monkeypatch.setattr(postmaster_service, "is_configured", lambda: True)
    result = await postmaster_service.pull_all(db_session)
    assert result["status"] == "no_accounts"


# --- pull_for_account (mocked client) ---------------------------------------

def _seed_account(db_session):
    account = PostmasterAccount(
        email="pm@apprend.tech",
        google_sub="sub-123",
        refresh_token_encrypted="unused-in-test",
        status="connected",
    )
    db_session.add(account)
    return account


def _fake_stat(domain: str, day: str = "20260715", **overrides):
    base = {
        "name": f"domains/{domain}/trafficStats/{day}",
        "domainReputation": "HIGH",
        "userReportedSpamRatio": 0.002,
        "spfSuccessRatio": 1.0,
        "dkimSuccessRatio": 1.0,
        "dmarcSuccessRatio": 0.99,
        "ipReputations": [{"reputation": "HIGH", "ipCount": 2}],
        "deliveryErrors": [],
    }
    base.update(overrides)
    return base


def _install_fake_client(monkeypatch, stats_by_domain):
    async def fake_fresh(db, account):
        return "access-token"

    monkeypatch.setattr(postmaster_service, "_fresh_access_token", fake_fresh)

    class FakeClient:
        def __init__(self, access_token):
            self.token = access_token

        async def list_domains(self):
            return list(stats_by_domain.keys())

        async def get_traffic_stats(self, domain):
            return stats_by_domain.get(domain, [])

    monkeypatch.setattr(postmaster_service, "PostmasterClient", FakeClient)


@pytest.mark.asyncio
async def test_pull_for_account_upserts_metrics(db_session, monkeypatch):
    account = _seed_account(db_session)
    await db_session.flush()
    _install_fake_client(monkeypatch, {"apprend.tech": [_fake_stat("apprend.tech")]})

    result = await postmaster_service.pull_for_account(db_session, account)
    assert result["status"] == "ok"
    assert result["rows"] == 1

    row = (await db_session.execute(select(PostmasterMetric))).scalars().one()
    assert row.domain == "apprend.tech"
    assert row.domain_reputation == "HIGH"
    assert row.metric_date == date(2026, 7, 15)
    assert row.spam_rate == 0.002
    assert account.last_pulled_at is not None


@pytest.mark.asyncio
async def test_pull_for_account_is_idempotent(db_session, monkeypatch):
    account = _seed_account(db_session)
    await db_session.flush()
    _install_fake_client(monkeypatch, {"apprend.tech": [_fake_stat("apprend.tech")]})

    await postmaster_service.pull_for_account(db_session, account)
    # Second pull of the same (domain, date) updates in place, not a new row.
    _install_fake_client(
        monkeypatch,
        {"apprend.tech": [_fake_stat("apprend.tech", domainReputation="MEDIUM")]},
    )
    await postmaster_service.pull_for_account(db_session, account)

    rows = (await db_session.execute(select(PostmasterMetric))).scalars().all()
    assert len(rows) == 1
    assert rows[0].domain_reputation == "MEDIUM"


@pytest.mark.asyncio
async def test_pull_for_account_multiple_domains_and_days(db_session, monkeypatch):
    account = _seed_account(db_session)
    await db_session.flush()
    _install_fake_client(
        monkeypatch,
        {
            "apprend.tech": [
                _fake_stat("apprend.tech", "20260714"),
                _fake_stat("apprend.tech", "20260715"),
            ],
            "apprendtechnologies.com": [_fake_stat("apprendtechnologies.com", "20260715")],
        },
    )
    result = await postmaster_service.pull_for_account(db_session, account)
    assert result["rows"] == 3
    rows = (await db_session.execute(select(PostmasterMetric))).scalars().all()
    assert len(rows) == 3


# --- Router -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_endpoint_unconfigured(client):
    resp = await client.get("/api/v1/postmaster/status")
    assert resp.status_code == 200
    body = resp.json()
    # No Google creds in the test env → not configured, no accounts.
    assert body["configured"] is False
    assert body["accounts"] == []


@pytest.mark.asyncio
async def test_authorize_503_when_unconfigured(client):
    resp = await client.get("/api/v1/postmaster/oauth/authorize")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_pull_endpoint_reports_not_configured(client):
    resp = await client.post("/api/v1/postmaster/pull")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_configured"


@pytest.mark.asyncio
async def test_domains_and_metrics_endpoints(client, db_session):
    # Seed two days for one domain directly.
    for day, rep, spam in [(date(2026, 7, 14), "MEDIUM", 0.01), (date(2026, 7, 15), "HIGH", 0.001)]:
        db_session.add(
            PostmasterMetric(
                domain="apprend.tech",
                metric_date=day,
                domain_reputation=rep,
                spam_rate=spam,
                ip_reputations=[],
                delivery_errors=[],
                raw={},
            )
        )
    await db_session.commit()

    domains = await client.get("/api/v1/postmaster/domains")
    assert domains.status_code == 200
    data = domains.json()
    assert len(data) == 1
    assert data[0]["domain"] == "apprend.tech"
    assert data[0]["days_tracked"] == 2
    # Latest is the most recent date (HIGH).
    assert data[0]["latest"]["domain_reputation"] == "HIGH"

    series = await client.get("/api/v1/postmaster/domains/apprend.tech/metrics?days=365")
    assert series.status_code == 200
    dates = [m["metric_date"] for m in series.json()]
    # Most recent first.
    assert dates == ["2026-07-15", "2026-07-14"]


@pytest.mark.asyncio
async def test_domains_endpoint_empty(client):
    resp = await client.get("/api/v1/postmaster/domains")
    assert resp.status_code == 200
    assert resp.json() == []
