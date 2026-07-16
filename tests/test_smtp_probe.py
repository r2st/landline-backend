"""Tests for P3a — the free SMTP probe layer in front of Hunter.

The probe's whole value is saving Hunter credits, and its whole risk is
condemning a good contact on a rejection that was never about the mailbox. So
these cover:

* **Rejection classification** — only a 5xx that positively identifies a missing
  mailbox is believed. Greylisting, policy/reputation blocks, and generic 5xx
  text are ``unknown``, because a server that dislikes our IP says nothing about
  whether the address exists.
* **Catch-all detection** — an accepted RCPT only means "deliverable" if a
  random address at the same domain is *rejected*. Otherwise the domain accepts
  everything and bounces later → ``risky``, never ``valid``.
* **Fail-open** — disabled, unconfigured, no MX host, blocked port 25, timeout,
  or any SMTP error → ``unknown`` → Hunter still gets its say. This is the state
  on a stock AWS/Hetzner box, so it is the *normal* path, not the edge case.
* **Layering** — a decisive probe spends no Hunter credit; an indecisive one
  falls through to Hunter unchanged.

No test opens a socket or resolves DNS.
"""

import pytest

from app.config import get_settings
from app.services import hunter_verification, smtp_probe
from app.services.hunter_verification import _Budget, get_hunter_service
from app.services.smtp_probe import classify_rejection, probe_email


# ─── fakes ───────────────────────────────────────────────────────────────────

class _FakeSMTP:
    """Stands in for smtplib.SMTP.

    ``rcpt_script`` is consumed left-to-right (target first, then the catch-all
    control probe); the last entry repeats.
    """

    instances: list = []
    rcpt_script: list = []
    connect_exc: Exception | None = None

    def __init__(self, timeout=None, local_hostname=None):
        self.timeout = timeout
        self.local_hostname = local_hostname
        self.rcpts: list[str] = []
        self.sender = None
        self.host = None
        self.port = None
        self.quit_called = False
        _FakeSMTP.instances.append(self)

    def connect(self, host, port):
        if _FakeSMTP.connect_exc is not None:
            raise _FakeSMTP.connect_exc
        self.host, self.port = host, port
        return (220, b"ready")

    def ehlo_or_helo_if_needed(self):
        return None

    def mail(self, sender):
        self.sender = sender
        return (250, b"ok")

    def rcpt(self, addr):
        self.rcpts.append(addr)
        script = _FakeSMTP.rcpt_script
        if not script:
            return (250, b"ok")
        idx = len(self.rcpts) - 1
        return script[idx] if idx < len(script) else script[-1]

    def quit(self):
        self.quit_called = True


class _FakeResponse:
    def __init__(self, status_code=200, data=None, headers=None):
        self.status_code = status_code
        self._data = data or {}
        self.headers = headers or {}

    def json(self):
        return self._data


class _FakeClient:
    """Minimal httpx.AsyncClient stand-in, so we can assert Hunter was (not) called."""

    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None):
        _FakeClient.calls.append({"url": url, "params": params or {}})
        return _FakeResponse(200, {"data": {"result": "deliverable", "score": 95}})


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _FakeSMTP.instances = []
    _FakeSMTP.rcpt_script = []
    _FakeSMTP.connect_exc = None
    _FakeClient.calls = []
    monkeypatch.setattr(smtp_probe.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtp_probe, "mx_hosts", lambda d: ["mx1.acme.com"])
    monkeypatch.setattr(hunter_verification.httpx, "AsyncClient", _FakeClient)
    # MX passes by default so tests reach the probe.
    monkeypatch.setattr(hunter_verification, "check_mx", lambda d: "valid")
    yield
    _FakeSMTP.instances = []
    _FakeSMTP.rcpt_script = []
    _FakeSMTP.connect_exc = None
    _FakeClient.calls = []


@pytest.fixture
def probe_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_probe_enabled", True)
    monkeypatch.setattr(settings, "smtp_probe_from", "probe@landline.dev")
    monkeypatch.setattr(settings, "smtp_probe_helo", "")
    monkeypatch.setattr(settings, "smtp_probe_port", 25)
    monkeypatch.setattr(settings, "smtp_probe_timeout", 10.0)
    return settings


@pytest.fixture
def hunter_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "hunter_api_key", "test-key")
    monkeypatch.setattr(settings, "hunter_base_url", "https://api.hunter.io/v2")
    monkeypatch.setattr(settings, "hunter_daily_credit_budget", 100)
    return settings


# ─── classification: the line between "no mailbox" and "not about you" ───────

@pytest.mark.parametrize(
    "code,message",
    [
        (550, "5.1.1 The email account that you tried to reach does not exist"),
        (550, "No such user here"),
        (550, "5.1.1 user unknown"),
        (551, "Recipient not found"),
        (550, "RESOLVER.ADR.RecipNotFound; not found"),
        (550, "Invalid recipient <a@b.com>"),
    ],
)
def test_missing_mailbox_is_believed(code, message):
    assert classify_rejection(code, message) == smtp_probe.UNDELIVERABLE


@pytest.mark.parametrize(
    "code,message",
    [
        # About us, not the mailbox — condemning on these marks good contacts invalid.
        (550, "5.7.1 Service unavailable, client host blocked using Spamhaus"),
        (550, "5.7.1 Your IP has poor reputation"),
        (554, "Message rejected due to policy"),
        (550, "Access denied"),
        # Temporary by definition.
        (450, "4.2.0 Greylisted, try again later"),
        (451, "Too many concurrent connections"),
        (421, "Service not available"),
        # Generic 5xx that names no cause.
        (550, "Requested action not taken"),
        (500, "Syntax error"),
    ],
)
def test_ambiguous_rejection_is_unknown(code, message):
    assert classify_rejection(code, message) == smtp_probe.UNKNOWN


def test_reputation_wording_wins_over_mailbox_wording():
    """A block can borrow mailbox vocabulary. The safe reading must win.

    Believing this 550 would mark a perfectly good contact permanently invalid.
    """
    verdict = classify_rejection(550, "5.7.1 blocked - invalid recipient policy")
    assert verdict == smtp_probe.UNKNOWN


# ─── probe verdicts ──────────────────────────────────────────────────────────

def test_accepted_rcpt_with_rejected_control_is_deliverable(probe_on):
    _FakeSMTP.rcpt_script = [(250, b"ok"), (550, b"5.1.1 no such user")]
    out = probe_email("ceo@acme.com")
    assert out["result"] == smtp_probe.DELIVERABLE
    # Target first, then a random control address at the same domain.
    probe = _FakeSMTP.instances[0]
    assert probe.rcpts[0] == "ceo@acme.com"
    assert probe.rcpts[1].endswith("@acme.com") and probe.rcpts[1] != "ceo@acme.com"
    assert probe.quit_called


def test_catch_all_domain_is_risky_not_deliverable(probe_on):
    """Accepting everything is not the same as the mailbox existing."""
    _FakeSMTP.rcpt_script = [(250, b"ok"), (250, b"ok")]
    out = probe_email("ceo@acme.com")
    assert out["result"] == smtp_probe.RISKY
    assert out["meta"]["reason"] == "catch_all"


def test_missing_mailbox_is_undeliverable_without_control_probe(probe_on):
    """A discriminating rejection is decisive — no second round trip needed."""
    _FakeSMTP.rcpt_script = [(550, b"5.1.1 no such user")]
    out = probe_email("ghost@acme.com")
    assert out["result"] == smtp_probe.UNDELIVERABLE
    assert len(_FakeSMTP.instances[0].rcpts) == 1


def test_blocked_rejection_does_not_condemn(probe_on):
    _FakeSMTP.rcpt_script = [(550, b"5.7.1 client host blocked")]
    out = probe_email("ceo@acme.com")
    assert out["result"] == smtp_probe.UNKNOWN
    assert out["meta"]["reason"] == "rcpt_inconclusive"


def test_uses_configured_sender_and_helo(probe_on, monkeypatch):
    monkeypatch.setattr(probe_on, "smtp_probe_helo", "mail.landline.dev")
    _FakeSMTP.rcpt_script = [(250, b"ok"), (550, b"5.1.1 no such user")]
    probe_email("ceo@acme.com")
    probe = _FakeSMTP.instances[0]
    assert probe.sender == "probe@landline.dev"
    assert probe.local_hostname == "mail.landline.dev"
    assert (probe.host, probe.port) == ("mx1.acme.com", 25)


def test_helo_defaults_to_sender_domain(probe_on):
    _FakeSMTP.rcpt_script = [(250, b"ok"), (550, b"5.1.1 no such user")]
    probe_email("ceo@acme.com")
    assert _FakeSMTP.instances[0].local_hostname == "landline.dev"


# ─── fail-open: every one of these is the normal state on a stock host ───────

def test_disabled_never_opens_a_socket():
    out = probe_email("ceo@acme.com")
    assert out["result"] == smtp_probe.UNKNOWN
    assert out["meta"]["reason"] == "disabled"
    assert _FakeSMTP.instances == []


def test_enabled_without_sender_refuses_to_probe(monkeypatch):
    """Probing from a bogus MAIL FROM earns rejections that look like verdicts."""
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_probe_enabled", True)
    monkeypatch.setattr(settings, "smtp_probe_from", "")
    out = probe_email("ceo@acme.com")
    assert out["result"] == smtp_probe.UNKNOWN
    assert out["meta"]["reason"] == "no_from_address"
    assert _FakeSMTP.instances == []


def test_blocked_port_25_is_unknown_not_invalid(probe_on):
    """The default state on AWS/Hetzner: connect times out. Must not condemn."""
    _FakeSMTP.connect_exc = TimeoutError("timed out")
    out = probe_email("ceo@acme.com")
    assert out["result"] == smtp_probe.UNKNOWN
    assert out["meta"]["reason"] == "smtp_error"


def test_connection_refused_is_unknown(probe_on):
    _FakeSMTP.connect_exc = ConnectionRefusedError("refused")
    assert probe_email("ceo@acme.com")["result"] == smtp_probe.UNKNOWN


def test_smtp_protocol_error_is_unknown(probe_on):
    _FakeSMTP.connect_exc = smtp_probe.smtplib.SMTPServerDisconnected("bye")
    assert probe_email("ceo@acme.com")["result"] == smtp_probe.UNKNOWN


def test_no_mx_host_is_unknown(probe_on, monkeypatch):
    monkeypatch.setattr(smtp_probe, "mx_hosts", lambda d: [])
    out = probe_email("ceo@acme.com")
    assert out["result"] == smtp_probe.UNKNOWN
    assert out["meta"]["reason"] == "no_mx_host"


def test_bad_syntax_is_unknown_here(probe_on):
    """Syntax is the caller's gate; the probe just declines to guess."""
    assert probe_email("not-an-email")["result"] == smtp_probe.UNKNOWN


# ─── layering: the probe must save credits, never cost coverage ──────────────

@pytest.mark.asyncio
async def test_probe_deliverable_skips_hunter(probe_on, hunter_on):
    _FakeSMTP.rcpt_script = [(250, b"ok"), (550, b"5.1.1 no such user")]
    verdict = await get_hunter_service().verify_email("ceo@acme.com")
    assert verdict["status"] == "ok"
    assert verdict["result"] == "deliverable"
    assert verdict["source"] == "smtp"
    assert verdict["email_status"] == "valid"
    assert _FakeClient.calls == []  # the point: no credit spent


@pytest.mark.asyncio
async def test_probe_undeliverable_skips_hunter(probe_on, hunter_on):
    _FakeSMTP.rcpt_script = [(550, b"5.1.1 no such user")]
    verdict = await get_hunter_service().verify_email("ghost@acme.com")
    assert verdict["result"] == "undeliverable"
    assert verdict["source"] == "smtp"
    assert verdict["email_status"] == "invalid"
    assert _FakeClient.calls == []


@pytest.mark.asyncio
async def test_probe_catch_all_skips_hunter_and_stays_sendable(probe_on, hunter_on):
    """Hunter would only report accept_all → risky too, so the credit is waste."""
    _FakeSMTP.rcpt_script = [(250, b"ok"), (250, b"ok")]
    verdict = await get_hunter_service().verify_email("ceo@acme.com")
    assert verdict["result"] == "risky"
    assert verdict["source"] == "smtp"
    assert verdict["email_status"] == "unknown"  # sendable, never 'invalid'
    assert _FakeClient.calls == []


@pytest.mark.asyncio
async def test_probe_unknown_falls_through_to_hunter(probe_on, hunter_on):
    _FakeSMTP.connect_exc = TimeoutError("timed out")
    verdict = await get_hunter_service().verify_email("ceo@acme.com")
    assert len(_FakeClient.calls) == 1
    assert verdict["source"] == "hunter"


@pytest.mark.asyncio
async def test_probe_disabled_leaves_hunter_path_untouched(hunter_on):
    """Regression guard: off by default must mean today's behaviour, exactly."""
    verdict = await get_hunter_service().verify_email("ceo@acme.com")
    assert len(_FakeClient.calls) == 1
    assert verdict["source"] == "hunter"
    assert _FakeSMTP.instances == []


@pytest.mark.asyncio
async def test_mx_invalid_short_circuits_before_the_probe(probe_on, hunter_on, monkeypatch):
    monkeypatch.setattr(hunter_verification, "check_mx", lambda d: "invalid")
    verdict = await get_hunter_service().verify_email("ceo@nomx.example")
    assert verdict["source"] == "mx"
    assert _FakeSMTP.instances == []
    assert _FakeClient.calls == []


@pytest.mark.asyncio
async def test_decisive_probe_consumes_no_hunter_budget(probe_on, hunter_on):
    """The budget gates the paid layer; a free verdict must not draw it down."""
    _FakeSMTP.rcpt_script = [(250, b"ok"), (550, b"5.1.1 no such user")]
    budget = _Budget(5)
    await get_hunter_service().verify_email("ceo@acme.com", budget=budget)
    assert budget.remaining == 5


@pytest.mark.asyncio
async def test_probe_verdict_persists_onto_contact(probe_on, hunter_on):
    """An 'ok' probe verdict is written exactly like a Hunter one."""
    _FakeSMTP.rcpt_script = [(550, b"5.1.1 no such user")]
    verdict = await get_hunter_service().verify_email("ghost@acme.com")

    class _C:
        email = "ghost@acme.com"
        id = 1
        email_status = None
        email_checked_at = None
        email_verification_result = None
        email_verification_score = None
        email_verification_source = None
        email_verification_meta = None
        email_verified_at = None

    contact = _C()
    assert get_hunter_service()._apply(contact, verdict) is True
    assert contact.email_status == "invalid"
    assert contact.email_verification_source == "smtp"
    assert contact.email_verified_at is not None
