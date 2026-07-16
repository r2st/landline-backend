"""Tests for DIY email pattern discovery.

Covers the properties that decide whether this helps or hurts:

* **Generation** is pure, offline and total — accents, apostrophes, hyphens and
  partial names all have to fold into something a mail server would route, or
  into nothing at all. Never a half-formed address.
* **A guess is never promoted to ``valid`` without a probe.** With no Reacher URL
  configured the address is still returned (a human can eyeball it) but stays
  ``unknown``, because handing the send path a fabricated address is how you get
  hard bounces, and hard bounces are what auto-pause halts all sending over.
* **Catch-all domains are detected and refused.** They accept every address, so
  every pattern "verifies" and the first guess wins by accident. This is the case
  that makes naive pattern generators actively harmful.
* **Fail-open** — a Reacher outage, a timeout, or a tarpitting server yields
  ``unknown``, never ``invalid``.
* **Ingestion integration** respects the global email dedup rule: a guess that
  collides with an existing contact is dropped, not assigned.

All network is faked; no test touches Reacher, SMTP or DNS.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.services import email_pattern_generator as epg
from app.services.email_pattern_generator import (
    PatternCandidate,
    generate,
    get_pattern_generator,
    reset_pattern_generator,
)
from app.services.ingestion import get_ingestion_service


# ─── fakes ───────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data if data is not None else {}

    def json(self):
        return self._data


class _FakeClient:
    """Stands in for httpx.AsyncClient.

    ``by_email`` maps an address to a Reacher ``is_reachable`` string; anything
    unlisted answers ``default``. That shape lets a test say "only flast@ is
    real" without scripting call order.
    """

    calls: list = []
    by_email: dict = {}
    default: str = "unknown"
    status_code: int = 200
    raise_exc: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        email = (json or {}).get("to_email")
        _FakeClient.calls.append({"url": url, "email": email, "headers": headers or {}})
        if _FakeClient.raise_exc is not None:
            raise _FakeClient.raise_exc
        if _FakeClient.status_code >= 400:
            return _FakeResponse(_FakeClient.status_code, {})
        reachable = _FakeClient.by_email.get(email, _FakeClient.default)
        return _FakeResponse(200, {"is_reachable": reachable, "smtp": {}})


def _probed_emails() -> list[str]:
    return [c["email"] for c in _FakeClient.calls]


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.by_email = {}
    _FakeClient.default = "unknown"
    _FakeClient.status_code = 200
    _FakeClient.raise_exc = None
    monkeypatch.setattr(epg.httpx, "AsyncClient", _FakeClient)
    yield
    _FakeClient.calls = []
    _FakeClient.by_email = {}


@pytest.fixture(autouse=True)
def _mx_valid(monkeypatch):
    """Default: MX passes, so tests reach the probe layer unless they say not to."""
    monkeypatch.setattr(epg, "check_mx", lambda d: "valid")


@pytest.fixture(autouse=True)
def _reset_generator():
    """The generator caches settings at construction; drop it around every test."""
    reset_pattern_generator()
    yield
    reset_pattern_generator()


@pytest.fixture
def reacher_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "reacher_url", "http://reacher.local")
    monkeypatch.setattr(settings, "reacher_api_key", "")
    monkeypatch.setattr(settings, "pattern_smtp_fallback_enabled", False)
    monkeypatch.setattr(settings, "pattern_max_probes", 7)
    monkeypatch.setattr(settings, "pattern_discovery_enabled", True)
    reset_pattern_generator()
    return settings


@pytest.fixture
def reacher_off(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "reacher_url", "")
    monkeypatch.setattr(settings, "pattern_smtp_fallback_enabled", False)
    monkeypatch.setattr(settings, "pattern_discovery_enabled", True)
    reset_pattern_generator()
    return settings


# ─── generation (pure, offline) ──────────────────────────────────────────────

def test_generates_all_seven_patterns():
    out = generate("Priya", "Sharma", "acme.com")
    assert [c.pattern for c in out] == [
        "first.last", "flast", "first", "firstl", "f.last", "first_last", "last",
    ]
    assert [c.email for c in out] == [
        "priya.sharma@acme.com",
        "psharma@acme.com",
        "priya@acme.com",
        "priyas@acme.com",
        "p.sharma@acme.com",
        "priya_sharma@acme.com",
        "sharma@acme.com",
    ]


def test_most_common_pattern_is_probed_first():
    """Ordering is the only thing the priors control, and it decides probe cost."""
    out = generate("Priya", "Sharma", "acme.com")
    assert out[0].pattern == "first.last"
    assert [c.prior for c in out] == sorted((c.prior for c in out), reverse=True)


def test_folds_accents_and_strips_punctuation():
    """Apollo/CSV names carry accents and apostrophes; mailboxes can't."""
    out = generate("Ana", "Rodríguez-Peña", "acme.com")
    assert out[0].email == "ana.rodriguezpena@acme.com"

    out = generate("Sean", "O'Brien", "acme.com")
    assert out[0].email == "sean.obrien@acme.com"


def test_multi_word_surname_collapses():
    out = generate("Jan", "van der Berg", "acme.com")
    assert out[0].email == "jan.vanderberg@acme.com"
    assert out[1].email == "jvanderberg@acme.com"


def test_colliding_patterns_emitted_once():
    """A one-letter first name makes first.last and f.last identical."""
    out = generate("A", "Chen", "acme.com")
    emails = [c.email for c in out]
    assert len(emails) == len(set(emails))
    assert "a.chen@acme.com" in emails
    # The likelier name wins the slot.
    assert next(c.pattern for c in out if c.email == "a.chen@acme.com") == "first.last"


def test_missing_last_name_yields_only_first_patterns():
    out = generate("Priya", None, "acme.com")
    assert [c.email for c in out] == ["priya@acme.com"]


def test_missing_first_name_yields_only_last_patterns():
    out = generate(None, "Sharma", "acme.com")
    assert [c.email for c in out] == ["sharma@acme.com"]


def test_normalizes_the_domain():
    out = generate("Priya", "Sharma", "https://www.Acme.com/careers")
    assert out[0].email == "priya.sharma@acme.com"


@pytest.mark.parametrize(
    "first,last,domain",
    [
        ("Priya", "Sharma", None),
        ("Priya", "Sharma", ""),
        (None, None, "acme.com"),
        ("", "", "acme.com"),
        # Folds to no mailbox-safe characters at all — a domain-only guess would
        # be pure noise, so generate nothing rather than something wrong.
        ("李", "明", "acme.com"),
        ("...", "'''", "acme.com"),
    ],
)
def test_returns_nothing_when_there_is_nothing_to_build_from(first, last, domain):
    assert generate(first, last, domain) == []


def test_candidate_is_immutable():
    c = generate("Priya", "Sharma", "acme.com")[0]
    assert isinstance(c, PatternCandidate)
    with pytest.raises(Exception):
        c.email = "other@acme.com"  # type: ignore[misc]


# ─── domain_accepts_mail ─────────────────────────────────────────────────────

def test_domain_accepts_mail_delegates_to_check_mx(monkeypatch):
    seen = []
    monkeypatch.setattr(epg, "check_mx", lambda d: seen.append(d) or "valid")
    assert epg.domain_accepts_mail("https://www.Acme.com/") == "valid"
    # Normalized before it reaches the shared cache, so the cache stays coherent.
    assert seen == ["acme.com"]


def test_domain_accepts_mail_rejects_empty():
    assert epg.domain_accepts_mail(None) == "invalid"
    assert epg.domain_accepts_mail("   ") == "invalid"


# ─── discovery ───────────────────────────────────────────────────────────────

async def test_discover_skips_when_nothing_to_generate(reacher_on):
    verdict = await get_pattern_generator().discover(None, None, "acme.com")
    assert verdict["status"] == "skipped"
    assert verdict["reason"] == "no_candidates"
    assert _probed_emails() == []


async def test_no_mx_condemns_without_spending_a_probe(reacher_on, monkeypatch):
    monkeypatch.setattr(epg, "check_mx", lambda d: "invalid")
    verdict = await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert verdict["result"] == "undeliverable"
    assert verdict["email_status"] == "invalid"
    assert verdict["meta"]["reason"] == "no_mx"
    assert _probed_emails() == []


async def test_mx_unknown_does_not_short_circuit(reacher_on, monkeypatch):
    """dnspython is optional; treating its absence as a stop would disable this
    capability silently — the same trap hunter_verification documents."""
    monkeypatch.setattr(epg, "check_mx", lambda d: "unknown")
    _FakeClient.by_email = {"priya.sharma@acme.com": "safe"}
    verdict = await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert verdict["email_status"] == "valid"


async def test_unprobed_guess_is_returned_but_never_valid(reacher_off):
    """No probe layer configured: still useful to a human, never auto-sendable."""
    verdict = await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert verdict["status"] == "ok"
    assert verdict["email"] == "priya.sharma@acme.com"
    assert verdict["email_status"] == "unknown"
    assert verdict["result"] == "unknown"
    assert verdict["meta"]["probed"] is False
    assert _probed_emails() == []


async def test_confirmed_mailbox_is_valid(reacher_on):
    _FakeClient.by_email = {"priya.sharma@acme.com": "safe"}
    verdict = await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert verdict["email"] == "priya.sharma@acme.com"
    assert verdict["result"] == "deliverable"
    assert verdict["email_status"] == "valid"
    assert verdict["pattern"] == "first.last"
    assert verdict["source"] == "pattern"


async def test_walks_patterns_until_one_confirms(reacher_on):
    """The whole point: the first guess usually misses."""
    _FakeClient.by_email = {"psharma@acme.com": "safe"}
    verdict = await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert verdict["email"] == "psharma@acme.com"
    assert verdict["pattern"] == "flast"
    assert verdict["email_status"] == "valid"
    # Stops at the hit rather than probing the rest.
    assert verdict["meta"]["tried"] == ["first.last", "flast"]


async def test_catch_all_domain_refuses_to_guess(reacher_on):
    """A catch-all accepts everything, so every pattern 'verifies'. Picking the
    first hit here would be guessing dressed up as verification."""
    _FakeClient.default = "safe"  # accepts literally any address
    verdict = await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert verdict["meta"]["reason"] == "catch_all"
    assert verdict["result"] == "risky"
    assert verdict["email_status"] == "unknown"
    # The guess is still surfaced for a human, just never as verified.
    assert verdict["email"] == "priya.sharma@acme.com"
    # It bailed after the catch-all probe instead of walking the patterns.
    assert _probed_emails() == [f"{epg._CATCH_ALL_PROBE}@acme.com"]


async def test_catch_all_probe_precedes_the_candidates(reacher_on):
    _FakeClient.by_email = {"priya.sharma@acme.com": "safe"}
    await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert _probed_emails()[0] == f"{epg._CATCH_ALL_PROBE}@acme.com"


async def test_no_confirmation_is_unknown_not_invalid(reacher_on):
    """A tarpitting or greylisting server declines to discuss real mailboxes.
    Fail-open: we learned nothing, so we condemn nothing."""
    _FakeClient.default = "unknown"
    verdict = await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert verdict["result"] == "unknown"
    assert verdict["email_status"] == "unknown"
    assert verdict["meta"]["reason"] == "no_confirmation"


async def test_reacher_outage_fails_open(reacher_on):
    _FakeClient.raise_exc = RuntimeError("connection refused")
    verdict = await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert verdict["email_status"] == "unknown"
    assert verdict["email_status"] != "invalid"


async def test_reacher_http_error_fails_open(reacher_on):
    _FakeClient.status_code = 500
    verdict = await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert verdict["email_status"] == "unknown"


async def test_reacher_invalid_verdict_does_not_condemn_the_person(reacher_on):
    """Reacher saying one *pattern* is invalid doesn't mean the person has no
    address — it means that guess was wrong. Keep walking, end unknown."""
    _FakeClient.default = "invalid"
    verdict = await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert verdict["email_status"] == "unknown"
    assert verdict["result"] == "unknown"


async def test_max_probes_caps_the_conversation(reacher_on, monkeypatch):
    """Seven probes per person against a stranger's MTA looks like a dictionary
    attack; the cap is what keeps a bulk import from being one."""
    monkeypatch.setattr(get_settings(), "pattern_max_probes", 2)
    reset_pattern_generator()
    await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    # One catch-all probe + exactly two candidates.
    assert len(_probed_emails()) == 3


async def test_api_key_is_sent_when_configured(reacher_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "reacher_api_key", "secret-key")
    reset_pattern_generator()
    await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert _FakeClient.calls[0]["headers"]["Authorization"] == "secret-key"


async def test_smtp_fallback_is_off_by_default():
    """Port 25 is blocked outbound on EC2 and probing from a sending IP is what
    DNSBLs call harvesting. Opt-in only."""
    settings = get_settings()
    assert settings.pattern_smtp_fallback_enabled is False
    assert settings.reacher_url == ""
    reset_pattern_generator()
    assert get_pattern_generator().can_probe is False


async def test_smtp_fallback_used_when_reacher_absent(reacher_off, monkeypatch):
    monkeypatch.setattr(get_settings(), "pattern_smtp_fallback_enabled", True)
    monkeypatch.setattr(get_settings(), "pattern_smtp_from_address", "probe@landline.io")
    monkeypatch.setattr(get_settings(), "pattern_max_probes", 7)
    reset_pattern_generator()

    probed: list[str] = []

    def _fake_smtp(self, email):
        probed.append(email)
        return "deliverable" if email == "psharma@acme.com" else "undeliverable"

    monkeypatch.setattr(epg.EmailPatternGenerator, "_probe_smtp_blocking", _fake_smtp)
    verdict = await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert verdict["email"] == "psharma@acme.com"
    assert verdict["email_status"] == "valid"
    # No HTTP was attempted — Reacher isn't configured.
    assert _probed_emails() == []


async def test_smtp_fallback_without_a_from_address_is_unknown(reacher_off, monkeypatch):
    """A bogus MAIL FROM is itself a spam signal — refuse rather than improvise."""
    monkeypatch.setattr(get_settings(), "pattern_smtp_fallback_enabled", True)
    monkeypatch.setattr(get_settings(), "pattern_smtp_from_address", "")
    reset_pattern_generator()
    verdict = await get_pattern_generator().discover("Priya", "Sharma", "acme.com")
    assert verdict["email_status"] == "unknown"


# ─── ingestion integration ───────────────────────────────────────────────────

async def _mk_company(db: AsyncSession, **kw) -> Company:
    company = Company(
        name=kw.pop("name", "Acme Manufacturing"),
        domain=kw.pop("domain", "acme.com"),
        country=kw.pop("country", "US"),
        source="test",
        stage="new",
        pipeline_stage="new_lead",
        **kw,
    )
    db.add(company)
    await db.flush()
    return company


async def _mk_contact(db: AsyncSession, company: Company, **kw) -> Contact:
    contact = Contact(
        company_id=company.id,
        first_name=kw.pop("first_name", "Priya"),
        last_name=kw.pop("last_name", "Sharma"),
        email=kw.pop("email", None),
        source="test",
        **kw,
    )
    db.add(contact)
    await db.flush()
    return contact


async def test_ingestion_fills_a_verified_email(db_session, reacher_on):
    company = await _mk_company(db_session)
    contact = await _mk_contact(db_session, company)
    _FakeClient.by_email = {"priya.sharma@acme.com": "safe"}

    filled = await get_ingestion_service().discover_contact_emails(
        db_session, company, [contact]
    )
    assert filled == 1
    assert contact.email == "priya.sharma@acme.com"
    assert contact.email_status == "valid"
    assert contact.email_verification_source == "pattern"
    assert contact.email_verified_at is not None


async def test_ingestion_unverified_guess_stays_unknown_and_unverified(
    db_session, reacher_off
):
    """The row becomes contactable-by-a-human without claiming the address is
    good, and email_verified_at stays NULL so the Hunter sweep still re-checks it."""
    company = await _mk_company(db_session)
    contact = await _mk_contact(db_session, company)

    filled = await get_ingestion_service().discover_contact_emails(
        db_session, company, [contact]
    )
    assert filled == 1
    assert contact.email == "priya.sharma@acme.com"
    assert contact.email_status == "unknown"
    assert contact.email_verified_at is None


async def test_ingestion_never_reassigns_an_email_that_already_exists(
    db_session, reacher_on
):
    """The global email dedup rule applies to guesses too: assigning a colliding
    address would mail one mailbox as two people."""
    company = await _mk_company(db_session)
    incumbent = await _mk_contact(
        db_session, company, first_name="Other", last_name="Person",
        email="priya.sharma@acme.com",
    )
    newcomer = await _mk_contact(db_session, company)
    _FakeClient.by_email = {"priya.sharma@acme.com": "safe"}

    filled = await get_ingestion_service().discover_contact_emails(
        db_session, company, [newcomer]
    )
    assert filled == 0
    assert newcomer.email is None
    assert incumbent.email == "priya.sharma@acme.com"


async def test_ingestion_skips_company_without_a_domain(db_session, reacher_on):
    company = await _mk_company(db_session, domain=None)
    contact = await _mk_contact(db_session, company)

    filled = await get_ingestion_service().discover_contact_emails(
        db_session, company, [contact]
    )
    assert filled == 0
    assert contact.email is None
    assert _probed_emails() == []


async def test_ingestion_skips_contact_that_already_has_an_email(db_session, reacher_on):
    company = await _mk_company(db_session)
    contact = await _mk_contact(db_session, company, email="real@acme.com")

    filled = await get_ingestion_service().discover_contact_emails(
        db_session, company, [contact]
    )
    assert filled == 0
    assert contact.email == "real@acme.com"
    assert _probed_emails() == []


async def test_ingestion_skips_contact_with_only_a_first_name(db_session, reacher_on):
    """'first@' would collide across every junior at the company."""
    company = await _mk_company(db_session)
    contact = await _mk_contact(db_session, company, last_name=None)

    filled = await get_ingestion_service().discover_contact_emails(
        db_session, company, [contact]
    )
    assert filled == 0
    assert contact.email is None
    assert _probed_emails() == []


async def test_ingestion_respects_the_kill_switch(db_session, reacher_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "pattern_discovery_enabled", False)
    company = await _mk_company(db_session)
    contact = await _mk_contact(db_session, company)

    filled = await get_ingestion_service().discover_contact_emails(
        db_session, company, [contact]
    )
    assert filled == 0
    assert contact.email is None
    assert _probed_emails() == []


async def test_ingestion_leaves_the_row_alone_on_a_dead_domain(
    db_session, reacher_on, monkeypatch
):
    """A domain with no MX says nothing about the person — don't stamp a contact
    'invalid' over an address we invented for them."""
    monkeypatch.setattr(epg, "check_mx", lambda d: "invalid")
    company = await _mk_company(db_session)
    contact = await _mk_contact(db_session, company)

    filled = await get_ingestion_service().discover_contact_emails(
        db_session, company, [contact]
    )
    assert filled == 0
    assert contact.email is None
    assert contact.email_status is None
