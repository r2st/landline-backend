"""Tests for the SPF/DKIM/DMARC domain-authentication checker.

DNS is fully monkeypatched via ``domain_auth._txt_records`` so these run
offline and deterministically.
"""

from app.services import domain_auth


def _fake_dns(mapping: dict):
    """Return a _txt_records stand-in driven by a {name: (txts, error)} map.

    Names not in the map resolve to an empty record set (no error).
    """
    def _lookup(name: str):
        return mapping.get(name, ([], None))
    return _lookup


# --- SPF ---

def test_spf_hardfail(monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records",
                        _fake_dns({"acme.com": (["v=spf1 include:_spf.google.com -all"], None)}))
    r = domain_auth.check_spf("acme.com")
    assert r["present"] and r["valid"]
    assert r["policy"] == "hardfail"
    assert r["status"] == "pass"


def test_spf_softfail_is_valid(monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records",
                        _fake_dns({"acme.com": (["v=spf1 ~all"], None)}))
    r = domain_auth.check_spf("acme.com")
    assert r["policy"] == "softfail" and r["valid"]


def test_spf_neutral_warns(monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records",
                        _fake_dns({"acme.com": (["v=spf1 ?all"], None)}))
    r = domain_auth.check_spf("acme.com")
    assert r["policy"] == "neutral" and not r["valid"]
    assert r["status"] == "warn" and r["issues"]


def test_spf_missing_fails(monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records",
                        _fake_dns({"acme.com": (["some-other-txt"], None)}))
    r = domain_auth.check_spf("acme.com")
    assert not r["present"] and r["status"] == "fail"


def test_spf_unknown_on_dns_error(monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records",
                        _fake_dns({"acme.com": ([], "timeout")}))
    r = domain_auth.check_spf("acme.com")
    assert r["status"] == "unknown" and "timeout" in r["issues"]


# --- DMARC ---

def test_dmarc_enforcing(monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records", _fake_dns({
        "_dmarc.acme.com": (["v=DMARC1; p=reject; rua=mailto:d@acme.com"], None),
    }))
    r = domain_auth.check_dmarc("acme.com")
    assert r["present"] and r["valid"]
    assert r["policy"] == "reject" and r["status"] == "pass"


def test_dmarc_monitor_only_warns(monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records", _fake_dns({
        "_dmarc.acme.com": (["v=DMARC1; p=none"], None),
    }))
    r = domain_auth.check_dmarc("acme.com")
    assert r["present"] and not r["valid"]
    assert r["status"] == "warn"
    assert any("p=none" in i for i in r["issues"])


def test_dmarc_missing_fails(monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records",
                        _fake_dns({"_dmarc.acme.com": ([], "nxdomain")}))
    r = domain_auth.check_dmarc("acme.com")
    assert not r["present"] and r["status"] == "fail"


# --- DKIM ---

def test_dkim_found_selector(monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records", _fake_dns({
        "google._domainkey.acme.com": (["v=DKIM1; k=rsa; p=MIGfMA0GCS"], None),
    }))
    r = domain_auth.check_dkim("acme.com", ["google"])
    assert r["present"] and r["valid"]
    assert r["selector"] == "google"


def test_dkim_none_found_fails(monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records",
                        _fake_dns({}))  # every selector → empty (no error)
    r = domain_auth.check_dkim("acme.com", ["google", "default"])
    assert not r["present"] and r["status"] == "fail"
    assert r["selectors_checked"] == ["google", "default"]


# --- Overall ---

def test_check_domain_auth_all_pass(monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records", _fake_dns({
        "acme.com": (["v=spf1 -all"], None),
        "google._domainkey.acme.com": (["v=DKIM1; p=MIGABC"], None),
        "_dmarc.acme.com": (["v=DMARC1; p=quarantine; rua=mailto:d@acme.com"], None),
    }))
    r = domain_auth.check_domain_auth("acme.com", ["google"])
    assert r["overall"] == "pass"
    assert r["spf"]["valid"] and r["dkim"]["valid"] and r["dmarc"]["valid"]


def test_check_domain_auth_fails_when_spf_missing(monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records", _fake_dns({
        "google._domainkey.acme.com": (["v=DKIM1; p=MIGABC"], None),
        "_dmarc.acme.com": (["v=DMARC1; p=reject"], None),
    }))
    r = domain_auth.check_domain_auth("acme.com", ["google"])
    assert r["overall"] == "fail"
    assert r["recommendations"]


def test_check_domain_auth_accepts_email_address(monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records", _fake_dns({
        "acme.com": (["v=spf1 ~all"], None),
    }))
    r = domain_auth.check_domain_auth("sales@acme.com")
    assert r["domain"] == "acme.com"


def test_check_domain_auth_invalid_domain():
    r = domain_auth.check_domain_auth("notadomain")
    assert r["overall"] == "invalid"


# --- Endpoint ---

async def test_domain_auth_endpoint(client, monkeypatch):
    monkeypatch.setattr(domain_auth, "_txt_records", _fake_dns({
        "acme.com": (["v=spf1 -all"], None),
        "google._domainkey.acme.com": (["v=DKIM1; p=MIGABC"], None),
        "_dmarc.acme.com": (["v=DMARC1; p=reject; rua=mailto:d@acme.com"], None),
    }))
    r = await client.get("/api/v1/deliverability/domain-auth?domain=acme.com&dkim_selector=google")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall"] == "pass"
    assert body["domain"] == "acme.com"


async def test_domain_auth_endpoint_requires_domain(client):
    r = await client.get("/api/v1/deliverability/domain-auth?domain=")
    assert r.status_code == 400
