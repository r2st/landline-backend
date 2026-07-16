"""Tests for Feature 5 — Domain Health Dashboard.

Covers:
* composite score computation (weights, unknown components excluded from the
  denominator, band boundaries),
* DNSBL parsing (listed / not-listed / unknown) via a monkeypatched resolver,
* change-only alerting (no alert on an unchanged score, alert on a transition or
  a new blacklist listing, no alert on the first observation),
* the ``/deliverability/domains*`` endpoints incl. recheck cooldown.

All DNS is monkeypatched (``blacklist_check._a_records`` /
``domain_auth._txt_records`` or the check functions directly) so these run
offline and deterministically.
"""

import uuid as _uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain_health import DomainHealth
from app.models.gmail_account import GmailAccount
from app.services import blacklist_check
from app.services import domain_health_service
from app.services.domain_health_service import get_domain_health_checker


# ─── helpers ─────────────────────────────────────────────────────────────────

def _spf(status="pass", present=True, valid=True):
    return {"type": "spf", "present": present, "valid": valid, "status": status,
            "record": "v=spf1 -all", "policy": "hardfail", "issues": []}


def _dkim(status="pass", present=True, valid=True):
    return {"type": "dkim", "present": present, "valid": valid, "status": status,
            "record": "v=DKIM1; p=abc", "selector": "google", "issues": []}


def _dmarc(status="pass", present=True, valid=True, policy="reject"):
    return {"type": "dmarc", "present": present, "valid": valid, "status": status,
            "record": "v=DMARC1; p=reject", "policy": policy, "issues": []}


def _bl(status="ok", listed=None):
    return {"checked_at": "2026-07-16T00:00:00Z", "ips": [], "zones": {},
            "listed_meaningful": listed or [], "any_listed_meaningful": bool(listed),
            "status": status}


def _fake_a(mapping: dict):
    """Return an _a_records stand-in from a {name: (answers, error)} map.

    Names absent from the map resolve to ``([], None)`` — the DNSBL "not listed"
    signal.
    """
    def _lookup(name: str):
        return mapping.get(name, ([], None))
    return _lookup


# ─── score computation ───────────────────────────────────────────────────────

def test_score_all_pass_is_100():
    c = get_domain_health_checker()
    r = c.compute_score(_spf(), _dkim(), _dmarc(), _bl())
    assert r["health_score"] == 100
    assert r["band"] == "healthy"
    assert r["coverage"]["denominator"] == 95  # 20+20+25+30


def test_unknown_component_excluded_from_denominator():
    c = get_domain_health_checker()
    # DKIM unknown (lookup gap) → dropped from denominator; the rest full → 100.
    r = c.compute_score(_spf(), _dkim(status="unknown"), _dmarc(), _bl())
    assert r["coverage"]["denominator"] == 75  # 20+25+30, DKIM(20) excluded
    assert r["coverage"]["components"]["dkim"]["verified"] is False
    assert r["health_score"] == 100  # earned == denominator


def test_score_partial_credit():
    c = get_domain_health_checker()
    # SPF present-but-invalid (0.5*20=10), DMARC p=none (0.5*25=12.5),
    # DKIM valid (20), blacklist ok (30). earned=72.5, denom=95 → 76.
    spf = _spf(status="warn", present=True, valid=False)
    dmarc = _dmarc(status="warn", present=True, valid=False, policy="none")
    r = c.compute_score(spf, _dkim(), dmarc, _bl())
    assert r["health_score"] == 76
    assert r["band"] == "watch"


def test_missing_components_score_zero():
    c = get_domain_health_checker()
    spf = _spf(status="fail", present=False, valid=False)
    dkim = _dkim(status="fail", present=False, valid=False)
    dmarc = _dmarc(status="fail", present=False, valid=False, policy=None)
    r = c.compute_score(spf, dkim, dmarc, _bl())
    # Only blacklists (ok) earns → 30/95 → 32 → at_risk.
    assert r["health_score"] == 32
    assert r["band"] == "at_risk"


def test_spamhaus_listing_zeroes_blacklist_component():
    c = get_domain_health_checker()
    bl = _bl(status="listed", listed=["dbl.spamhaus.org"])
    r = c.compute_score(_spf(), _dkim(), _dmarc(), bl)
    # blacklist fraction 0 → earned = 65, denom = 95 → 68 → at_risk.
    assert r["coverage"]["components"]["blacklists"]["fraction"] == 0.0
    assert r["health_score"] == 68
    assert r["band"] == "at_risk"


def test_non_spamhaus_single_listing_halves_component():
    c = get_domain_health_checker()
    bl = _bl(status="listed", listed=["bl.spamcop.net"])
    frac = c._blacklist_fraction(bl)
    assert frac == 0.5


def test_blacklist_unknown_excluded():
    c = get_domain_health_checker()
    r = c.compute_score(_spf(), _dkim(), _dmarc(), _bl(status="unknown"))
    assert r["coverage"]["components"]["blacklists"]["verified"] is False
    assert r["coverage"]["denominator"] == 65  # 20+20+25


def test_band_boundaries():
    c = get_domain_health_checker()
    assert c.band_for(100) == "healthy"
    assert c.band_for(90) == "healthy"
    assert c.band_for(89) == "watch"
    assert c.band_for(70) == "watch"
    assert c.band_for(69) == "at_risk"
    assert c.band_for(0) == "at_risk"


def test_no_verifiable_components_scores_zero():
    c = get_domain_health_checker()
    r = c.compute_score(_spf(status="unknown"), _dkim(status="unknown"),
                        _dmarc(status="unknown"), _bl(status="unknown"))
    assert r["health_score"] == 0
    assert r["coverage"]["verifiable"] is False
    assert r["coverage"]["denominator"] == 0


# ─── DNSBL parsing ───────────────────────────────────────────────────────────

def test_reverse_ip():
    assert blacklist_check.reverse_ip("1.2.3.4") == "4.3.2.1"
    assert blacklist_check.reverse_ip("203.0.113.5") == "5.113.0.203"
    assert blacklist_check.reverse_ip("not.an.ip") is None
    assert blacklist_check.reverse_ip("1.2.3") is None
    assert blacklist_check.reverse_ip("1.2.3.999") is None


def test_domain_blacklist_listed(monkeypatch):
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({
        "acme.com.dbl.spamhaus.org": (["127.0.1.2"], None),
    }))
    r = blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org")
    assert r["listed"] is True and r["status"] == "listed"
    assert r["answers"] == ["127.0.1.2"]


def test_domain_blacklist_not_listed(monkeypatch):
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({}))  # NXDOMAIN
    r = blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org")
    assert r["listed"] is False and r["status"] == "ok"


def test_domain_blacklist_unknown(monkeypatch):
    monkeypatch.setattr(blacklist_check, "_a_records",
                        _fake_a({"acme.com.dbl.spamhaus.org": ([], "timeout")}))
    r = blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org")
    assert r["listed"] is False and r["status"] == "unknown"
    assert r["issue"] == "timeout"


def test_ip_blacklist_listed(monkeypatch):
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({
        "4.3.2.1.zen.spamhaus.org": (["127.0.0.4"], None),
    }))
    r = blacklist_check.check_ip_blacklist("1.2.3.4", "zen.spamhaus.org")
    assert r["listed"] is True and r["status"] == "listed"
    assert r["informational"] is True  # default (Gmail shared pool)


def test_ip_blacklist_invalid_ip():
    r = blacklist_check.check_ip_blacklist("garbage", "zen.spamhaus.org")
    assert r["status"] == "unknown" and r["issue"] == "invalid_ip"


def test_check_blacklists_domain_listed_ip_informational(monkeypatch):
    # dbl (domain, meaningful) listed; zen (IP, informational) also listed but
    # must not affect the meaningful status.
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({
        "acme.com": (["9.9.9.9"], None),                      # A-record resolution
        "acme.com.dbl.spamhaus.org": (["127.0.1.2"], None),   # meaningful listing
        "9.9.9.9.zen.spamhaus.org": (["127.0.0.2"], None),    # informational listing
    }))
    r = blacklist_check.check_blacklists(
        "acme.com",
        ip_zones=["zen.spamhaus.org"],
        domain_zones=["dbl.spamhaus.org"],
    )
    assert r["status"] == "listed"
    assert r["listed_meaningful"] == ["dbl.spamhaus.org"]
    assert r["any_listed_meaningful"] is True
    # The IP zone entry is present but flagged informational.
    ip_entry = r["zones"]["zen.spamhaus.org#9.9.9.9"]
    assert ip_entry["informational"] is True and ip_entry["listed"] is True


def test_check_blacklists_clean(monkeypatch):
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({}))  # nothing listed
    r = blacklist_check.check_blacklists(
        "clean.com", domain_zones=["dbl.spamhaus.org"], ip_zones=[],
    )
    assert r["status"] == "ok"
    assert r["listed_meaningful"] == []


def test_check_blacklists_all_unknown(monkeypatch):
    monkeypatch.setattr(blacklist_check, "_a_records",
                        _fake_a({"x.com.dbl.spamhaus.org": ([], "timeout")}))
    r = blacklist_check.check_blacklists(
        "x.com", domain_zones=["dbl.spamhaus.org"], ip_zones=[],
    )
    assert r["status"] == "unknown"


# ─── DNSBL error codes vs real listings ──────────────────────────────────────
# Spamhaus answers in 127.255.255.0/24 to refuse a query (e.g. .254 = "you asked
# via a public resolver"). Reading those as listings flagged every domain as
# blacklisted and cost 30 points of health score.

def test_error_code_is_not_a_listing(monkeypatch):
    """127.255.255.254 (public-resolver refusal) must not read as listed."""
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({
        "acme.com.dbl.spamhaus.org": (["127.255.255.254"], None),
    }))
    r = blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org")
    assert r["listed"] is False
    assert r["status"] == "unknown"  # refused ≠ clean; must not score as either
    assert r["issue"] == "dnsbl_error:127.255.255.254"


@pytest.mark.parametrize("code", [
    "127.255.255.252",  # typo'd DNSBL name
    "127.255.255.254",  # query via public/open resolver
    "127.255.255.255",  # excessive queries
    "127.0.1.255",      # DBL: IP queries prohibited
])
def test_all_error_codes_are_not_listings(monkeypatch, code):
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({
        "acme.com.dbl.spamhaus.org": ([code], None),
    }))
    r = blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org")
    assert r["listed"] is False and r["status"] == "unknown"


@pytest.mark.parametrize("code,what", [
    ("127.0.0.2", "SBL"),
    ("127.0.0.3", "CSS"),
    ("127.0.0.4", "XBL/CBL"),
    ("127.0.0.9", "DROP"),
    ("127.0.0.10", "PBL ISP-maintained"),      # a real listing, NOT an error code
    ("127.0.0.11", "PBL Spamhaus-maintained"),
])
def test_real_listing_codes_still_count(monkeypatch, code, what):
    """Genuine 127.0.0.x listings must survive the error-code filter."""
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({
        "4.3.2.1.zen.spamhaus.org": ([code], None),
    }))
    r = blacklist_check.check_ip_blacklist("1.2.3.4", "zen.spamhaus.org")
    assert r["listed"] is True, f"{code} ({what}) must still read as listed"
    assert r["status"] == "listed"


def test_real_dbl_listing_still_counts(monkeypatch):
    """127.0.1.2 (DBL spam domain) is a listing — adjacent to error 127.0.1.255."""
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({
        "acme.com.dbl.spamhaus.org": (["127.0.1.2"], None),
    }))
    r = blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org")
    assert r["listed"] is True and r["status"] == "listed"


def test_real_listing_wins_over_mixed_error_code(monkeypatch):
    """A real listing alongside an error code is still a listing."""
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({
        "acme.com.dbl.spamhaus.org": (["127.255.255.254", "127.0.1.2"], None),
    }))
    r = blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org")
    assert r["listed"] is True
    assert r["answers"] == ["127.0.1.2"]  # the error code is dropped


def test_error_code_does_not_flag_domain_as_blacklisted(monkeypatch):
    """End-to-end: the exact prod failure — refusal on the only meaningful zone."""
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({
        "apprend.tech.dbl.spamhaus.org": (["127.255.255.254"], None),
    }))
    r = blacklist_check.check_blacklists(
        "apprend.tech", domain_zones=["dbl.spamhaus.org"], ip_zones=[],
    )
    assert r["listed_meaningful"] == []
    assert r["any_listed_meaningful"] is False
    assert r["status"] == "unknown"


def test_error_code_does_not_cost_blacklist_points(monkeypatch):
    """A refused zone drops out of the denominator rather than scoring 0/30."""
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({
        "apprend.tech.dbl.spamhaus.org": (["127.255.255.254"], None),
    }))
    bl = blacklist_check.check_blacklists(
        "apprend.tech", domain_zones=["dbl.spamhaus.org"], ip_zones=[],
    )
    c = get_domain_health_checker()
    r = c.compute_score(_spf(), _dkim(), _dmarc(), bl)
    # Pre-fix this scored 68/at_risk (blacklists forced to 0/30). Now blacklists
    # are excluded as unverifiable and the verified components carry the score.
    assert r["coverage"]["components"]["blacklists"]["verified"] is False
    assert r["coverage"]["denominator"] == 65  # 20+20+25, blacklists(30) excluded
    assert r["health_score"] == 100
    assert r["band"] == "healthy"


def test_clean_domain_gets_full_blacklist_points(monkeypatch):
    """A genuine NXDOMAIN (nothing listed) still earns the full 30 points."""
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({}))  # NXDOMAIN
    bl = blacklist_check.check_blacklists(
        "clean.com", domain_zones=["dbl.spamhaus.org"], ip_zones=[],
    )
    assert bl["status"] == "ok"
    c = get_domain_health_checker()
    r = c.compute_score(_spf(), _dkim(), _dmarc(), bl)
    assert r["coverage"]["components"]["blacklists"]["verified"] is True
    assert r["coverage"]["components"]["blacklists"]["fraction"] == 1.0
    assert r["coverage"]["denominator"] == 95  # blacklists counted in full
    assert r["health_score"] == 100 and r["band"] == "healthy"


def test_real_listing_still_costs_points(monkeypatch):
    """The filter must not defang a genuine listing: still 0/30 and at_risk."""
    monkeypatch.setattr(blacklist_check, "_a_records", _fake_a({
        "bad.com.dbl.spamhaus.org": (["127.0.1.2"], None),  # real DBL spam listing
    }))
    bl = blacklist_check.check_blacklists(
        "bad.com", domain_zones=["dbl.spamhaus.org"], ip_zones=[],
    )
    assert bl["listed_meaningful"] == ["dbl.spamhaus.org"]
    c = get_domain_health_checker()
    r = c.compute_score(_spf(), _dkim(), _dmarc(), bl)
    assert r["coverage"]["components"]["blacklists"]["fraction"] == 0.0
    assert r["health_score"] == 68 and r["band"] == "at_risk"


# ─── change-only alerting ────────────────────────────────────────────────────

class _FakeNotifier:
    def __init__(self):
        self.calls = []

    async def notify_domain_health_change(self, domain, summary, details=""):
        self.calls.append((domain, summary, details))


def _patch_checks(monkeypatch, *, spf, dkim, dmarc, bl):
    from app.services import domain_auth
    monkeypatch.setattr(domain_auth, "check_spf", lambda d: spf)
    monkeypatch.setattr(domain_auth, "check_dkim", lambda d, s=None: dkim)
    monkeypatch.setattr(domain_auth, "check_dmarc", lambda d: dmarc)
    monkeypatch.setattr(blacklist_check, "check_blacklists", lambda d, **kw: bl)


@pytest.mark.asyncio
async def test_no_alert_on_first_observation(monkeypatch, db_session: AsyncSession):
    notifier = _FakeNotifier()
    monkeypatch.setattr(domain_health_service, "get_notification_service", lambda: notifier)
    _patch_checks(monkeypatch, spf=_spf(), dkim=_dkim(), dmarc=_dmarc(), bl=_bl())

    row = await get_domain_health_checker().check_domain(db_session, "acme.com")
    await db_session.commit()
    assert row.health_score == 100
    assert notifier.calls == []  # nothing to compare against


@pytest.mark.asyncio
async def test_no_alert_on_unchanged_score(monkeypatch, db_session: AsyncSession):
    notifier = _FakeNotifier()
    monkeypatch.setattr(domain_health_service, "get_notification_service", lambda: notifier)
    _patch_checks(monkeypatch, spf=_spf(), dkim=_dkim(), dmarc=_dmarc(), bl=_bl())

    c = get_domain_health_checker()
    await c.check_domain(db_session, "acme.com")
    await c.check_domain(db_session, "acme.com")  # identical inputs
    await db_session.commit()
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_alert_on_band_transition(monkeypatch, db_session: AsyncSession):
    notifier = _FakeNotifier()
    monkeypatch.setattr(domain_health_service, "get_notification_service", lambda: notifier)
    c = get_domain_health_checker()

    # First run: healthy.
    _patch_checks(monkeypatch, spf=_spf(), dkim=_dkim(), dmarc=_dmarc(), bl=_bl())
    await c.check_domain(db_session, "acme.com")

    # Second run: SPF & DMARC now missing → score drops into at_risk.
    _patch_checks(
        monkeypatch,
        spf=_spf(status="fail", present=False, valid=False),
        dkim=_dkim(status="fail", present=False, valid=False),
        dmarc=_dmarc(status="fail", present=False, valid=False, policy=None),
        bl=_bl(),
    )
    row = await c.check_domain(db_session, "acme.com")
    await db_session.commit()

    assert row.previous_score == 100
    assert row.band == "at_risk"
    assert len(notifier.calls) == 1
    assert "acme.com" == notifier.calls[0][0]
    assert "band" in notifier.calls[0][1]


@pytest.mark.asyncio
async def test_alert_on_new_blacklist_listing(monkeypatch, db_session: AsyncSession):
    notifier = _FakeNotifier()
    monkeypatch.setattr(domain_health_service, "get_notification_service", lambda: notifier)
    c = get_domain_health_checker()

    _patch_checks(monkeypatch, spf=_spf(), dkim=_dkim(), dmarc=_dmarc(), bl=_bl())
    await c.check_domain(db_session, "acme.com")

    # Now newly listed on Spamhaus DBL.
    _patch_checks(monkeypatch, spf=_spf(), dkim=_dkim(), dmarc=_dmarc(),
                  bl=_bl(status="listed", listed=["dbl.spamhaus.org"]))
    await c.check_domain(db_session, "acme.com")
    await db_session.commit()

    assert len(notifier.calls) == 1
    assert "newly listed on dbl.spamhaus.org" in notifier.calls[0][1]


# ─── endpoints ───────────────────────────────────────────────────────────────

async def _seed_domain(db, domain, score, band, **kw):
    row = DomainHealth(
        id=_uuid.uuid4(), domain=domain, health_score=score, band=band,
        spf=kw.get("spf", _spf()), dkim=kw.get("dkim", _dkim()),
        dmarc=kw.get("dmarc", _dmarc()), blacklists=kw.get("bl", _bl()),
        coverage={}, previous_score=kw.get("previous_score"),
    )
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio
async def test_list_domains_endpoint(client: AsyncClient, db_session: AsyncSession):
    await _seed_domain(db_session, "good.com", 95, "healthy")
    await _seed_domain(db_session, "bad.com", 40, "at_risk",
                       bl=_bl(status="listed", listed=["dbl.spamhaus.org"]))

    resp = await client.get("/api/v1/deliverability/domains")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # Worst-first ordering.
    assert [d["domain"] for d in data] == ["bad.com", "good.com"]
    assert data[0]["band"] == "at_risk"
    assert data[0]["blacklist"] == "listed"
    assert data[1]["spf"] == "pass"


@pytest.mark.asyncio
async def test_domain_detail_endpoint(client: AsyncClient, db_session: AsyncSession):
    await _seed_domain(
        db_session, "acme.com", 60, "at_risk",
        dmarc={"type": "dmarc", "present": False, "valid": False, "status": "fail",
               "policy": None, "issues": ["no DMARC record — add 'v=DMARC1; p=none'"]},
        bl=_bl(status="listed", listed=["dbl.spamhaus.org"]),
    )
    resp = await client.get("/api/v1/deliverability/domains/acme.com")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["domain"] == "acme.com"
    assert any("DMARC" in i for i in data["issues"])
    assert any("dbl.spamhaus.org" in i for i in data["issues"])
    assert data["notes"]  # Postmaster guidance present


@pytest.mark.asyncio
async def test_domain_detail_404(client: AsyncClient):
    resp = await client.get("/api/v1/deliverability/domains/missing.com")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_recheck_cooldown(client: AsyncClient, db_session: AsyncSession, monkeypatch):
    # Seed a just-checked domain → recheck must be rate-limited.
    await _seed_domain(db_session, "fresh.com", 90, "healthy")

    resp = await client.post("/api/v1/deliverability/domains/fresh.com/recheck")
    assert resp.status_code == 429, resp.text
    assert "recently" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_recheck_runs_when_no_prior(client: AsyncClient, monkeypatch):
    from app.services import domain_auth
    monkeypatch.setattr(domain_auth, "check_spf", lambda d: _spf())
    monkeypatch.setattr(domain_auth, "check_dkim", lambda d, s=None: _dkim())
    monkeypatch.setattr(domain_auth, "check_dmarc", lambda d: _dmarc())
    monkeypatch.setattr(blacklist_check, "check_blacklists", lambda d, **kw: _bl())

    resp = await client.post("/api/v1/deliverability/domains/newdomain.com/recheck")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["domain"] == "newdomain.com"
    assert data["health_score"] == 100
    assert data["rechecked"] is True


# ─── domain discovery ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_collect_domains_dedups(db_session: AsyncSession):
    db_session.add(GmailAccount(
        id=_uuid.uuid4(), email="a@apprend.tech", google_sub="sub1",
        refresh_token_encrypted="x",
    ))
    db_session.add(GmailAccount(
        id=_uuid.uuid4(), email="b@apprend.tech", google_sub="sub2",
        refresh_token_encrypted="x",
    ))
    await db_session.commit()

    domains = await get_domain_health_checker().collect_domains(db_session)
    assert domains == ["apprend.tech"]  # deduped


# ─── Spamhaus DQS (authenticated queries) ────────────────────────────────────
# The public mirrors refuse us (127.255.255.254 — we resolve via a public
# resolver), so a DQS key is the only way the blacklist component ever becomes
# real. These lock the wire format: the key is a DNS label between the query and
# the zone, and it must never escape into what we report or persist.

_KEY = "abcdefghijklmnopqrstuvwxyz"  # 26 chars, like a real DQS key


def _capture_a(answers_for: dict | None = None):
    """An _a_records stand-in that records every name looked up.

    Returns ``(lookup, names)``; ``names`` fills in as queries are made.
    """
    names: list[str] = []
    table = answers_for or {}

    def _lookup(name: str):
        names.append(name)
        return table.get(name, ([], None))

    return _lookup, names


def _settings_with_key(key: str, **over):
    """Stand in for get_settings() — blacklist_check reads the key from it."""
    from types import SimpleNamespace
    return lambda: SimpleNamespace(
        spamhaus_dqs_key=key,
        dnsbl_zones=over.get("dnsbl_zones", ["zen.spamhaus.org"]),
        dnsbl_domain_zones=over.get("dnsbl_domain_zones", ["dbl.spamhaus.org"]),
    )


# -- zone rewriting ----------------------------------------------------------

@pytest.mark.parametrize("zone,expected", [
    ("zen.spamhaus.org", f"{_KEY}.zen.dq.spamhaus.net"),
    ("dbl.spamhaus.org", f"{_KEY}.dbl.dq.spamhaus.net"),
    ("sbl.spamhaus.org", f"{_KEY}.sbl.dq.spamhaus.net"),
    ("pbl.spamhaus.org", f"{_KEY}.pbl.dq.spamhaus.net"),
    ("zrd.spamhaus.org", f"{_KEY}.zrd.dq.spamhaus.net"),
])
def test_dqs_zone_rewrites_spamhaus_zones(zone, expected):
    assert blacklist_check.dqs_zone(zone, _KEY) == expected


@pytest.mark.parametrize("zone", [
    "b.barracudacentral.org",   # not Spamhaus — no DQS equivalent
    "bl.spamcop.net",
])
def test_dqs_zone_leaves_non_spamhaus_zones_alone(zone):
    """A DQS key authenticates Spamhaus only; other zones must be untouched."""
    assert blacklist_check.dqs_zone(zone, _KEY) == zone


@pytest.mark.parametrize("key", ["", None, "   "])
def test_dqs_zone_without_key_is_identity(key):
    assert blacklist_check.dqs_zone("dbl.spamhaus.org", key) == "dbl.spamhaus.org"


def test_dqs_zone_does_not_double_wrap_an_already_dqs_zone():
    z = f"{_KEY}.dbl.dq.spamhaus.net"
    assert blacklist_check.dqs_zone(z, _KEY) == z


# -- query format ------------------------------------------------------------

def test_domain_query_uses_dqs_format_when_key_set(monkeypatch):
    """<domain>.<key>.dbl.dq.spamhaus.net — the documented DQS domain form."""
    lookup, names = _capture_a()
    monkeypatch.setattr(blacklist_check, "_a_records", lookup)
    blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org", dqs_key=_KEY)
    assert names == [f"acme.com.{_KEY}.dbl.dq.spamhaus.net"]


def test_ip_query_uses_dqs_format_when_key_set(monkeypatch):
    """<reversed-ip>.<key>.zen.dq.spamhaus.net — the documented DQS IP form."""
    lookup, names = _capture_a()
    monkeypatch.setattr(blacklist_check, "_a_records", lookup)
    blacklist_check.check_ip_blacklist("1.2.3.4", "zen.spamhaus.org", dqs_key=_KEY)
    assert names == [f"4.3.2.1.{_KEY}.zen.dq.spamhaus.net"]


def test_domain_query_uses_legacy_format_without_key(monkeypatch):
    """No key → unchanged public-mirror behaviour."""
    lookup, names = _capture_a()
    monkeypatch.setattr(blacklist_check, "_a_records", lookup)
    blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org", dqs_key="")
    assert names == ["acme.com.dbl.spamhaus.org"]


def test_ip_query_uses_legacy_format_without_key(monkeypatch):
    lookup, names = _capture_a()
    monkeypatch.setattr(blacklist_check, "_a_records", lookup)
    blacklist_check.check_ip_blacklist("1.2.3.4", "zen.spamhaus.org", dqs_key="")
    assert names == ["4.3.2.1.zen.spamhaus.org"]


def test_key_read_from_settings_when_not_passed(monkeypatch):
    """The daily task calls check_blacklists(domain) with no key — settings supply it."""
    lookup, names = _capture_a()
    monkeypatch.setattr(blacklist_check, "_a_records", lookup)
    monkeypatch.setattr(blacklist_check, "get_settings", _settings_with_key(_KEY))
    blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org")
    assert names == [f"acme.com.{_KEY}.dbl.dq.spamhaus.net"]


def test_unset_key_in_settings_falls_back_to_public(monkeypatch):
    lookup, names = _capture_a()
    monkeypatch.setattr(blacklist_check, "_a_records", lookup)
    monkeypatch.setattr(blacklist_check, "get_settings", _settings_with_key(""))
    blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org")
    assert names == ["acme.com.dbl.spamhaus.org"]


def test_check_blacklists_mixes_dqs_and_plain_zones(monkeypatch):
    """Spamhaus zones get the key; Barracuda/SpamCop in the same run do not."""
    lookup, names = _capture_a()
    monkeypatch.setattr(blacklist_check, "_a_records", lookup)
    monkeypatch.setattr(blacklist_check, "get_settings", _settings_with_key(
        _KEY, dnsbl_zones=["zen.spamhaus.org", "b.barracudacentral.org"]))
    blacklist_check.check_blacklists("acme.com", ips=["1.2.3.4"])
    assert f"acme.com.{_KEY}.dbl.dq.spamhaus.net" in names
    assert f"4.3.2.1.{_KEY}.zen.dq.spamhaus.net" in names
    assert "4.3.2.1.b.barracudacentral.org" in names  # untouched by the key


# -- response parsing (same codes as public, minus the refusals) --------------

@pytest.mark.parametrize("code,what", [
    ("127.0.0.2", "SBL"),
    ("127.0.0.4", "XBL/CBL"),
    ("127.0.0.10", "PBL ISP-maintained"),
    ("127.0.0.11", "PBL Spamhaus-maintained"),
])
def test_dqs_ip_listing_parsed_like_public(monkeypatch, code, what):
    lookup, _ = _capture_a({f"2.0.0.127.{_KEY}.zen.dq.spamhaus.net": ([code], None)})
    monkeypatch.setattr(blacklist_check, "_a_records", lookup)
    # 127.0.0.2 is Spamhaus's documented DQS test point (listed in all IP zones).
    r = blacklist_check.check_ip_blacklist("127.0.0.2", "zen.spamhaus.org", dqs_key=_KEY)
    assert r["listed"] is True, f"{code} ({what}) must read as listed under DQS"
    assert r["status"] == "listed" and r["dqs"] is True


def test_dqs_domain_listing_parsed_like_public(monkeypatch):
    lookup, _ = _capture_a({f"acme.com.{_KEY}.dbl.dq.spamhaus.net": (["127.0.1.2"], None)})
    monkeypatch.setattr(blacklist_check, "_a_records", lookup)
    r = blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org", dqs_key=_KEY)
    assert r["listed"] is True and r["status"] == "listed"


def test_dqs_not_listed(monkeypatch):
    """NXDOMAIN under DQS is a real 'clean' verdict — the whole point of the key."""
    lookup, _ = _capture_a()  # everything NXDOMAINs
    monkeypatch.setattr(blacklist_check, "_a_records", lookup)
    r = blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org", dqs_key=_KEY)
    assert r["listed"] is False
    assert r["status"] == "ok"  # not "unknown" — DQS answered, and said clean
    assert r["dqs"] is True


@pytest.mark.parametrize("code", [
    "127.255.255.252",  # typo'd zone name
    "127.255.255.254",  # unauthenticated/public resolver — a bad key looks like this
    "127.255.255.255",  # excessive queries / over quota
])
def test_dqs_error_codes_still_map_to_unknown(monkeypatch, code):
    """A wrong or expired key must degrade to unknown, never to clean or listed."""
    lookup, _ = _capture_a({f"acme.com.{_KEY}.dbl.dq.spamhaus.net": ([code], None)})
    monkeypatch.setattr(blacklist_check, "_a_records", lookup)
    r = blacklist_check.check_domain_blacklist("acme.com", "dbl.spamhaus.org", dqs_key=_KEY)
    assert r["listed"] is False
    assert r["status"] == "unknown"
    assert r["issue"] == f"dnsbl_error:{code}"
    assert r["dqs"] is True  # tells us the key was in play, so suspect the key


# -- the key is a credential -------------------------------------------------

def test_reported_zone_stays_public_and_key_never_leaks(monkeypatch):
    """We persist this dict; it must not carry the key, and zone keys must be stable.

    Change-alerting diffs against the persisted zone names, so enabling DQS must
    not look like every zone was swapped for a new one.
    """
    lookup, _ = _capture_a({f"acme.com.{_KEY}.dbl.dq.spamhaus.net": (["127.0.1.2"], None)})
    monkeypatch.setattr(blacklist_check, "_a_records", lookup)
    monkeypatch.setattr(blacklist_check, "get_settings", _settings_with_key(_KEY))
    out = blacklist_check.check_blacklists("acme.com", ips=["1.2.3.4"])

    assert "dbl.spamhaus.org" in out["zones"]           # public name, not the DQS one
    assert out["listed_meaningful"] == ["dbl.spamhaus.org"]
    assert out["dqs"] is True
    assert _KEY not in repr(out), "DQS key leaked into the persisted blacklist blob"
