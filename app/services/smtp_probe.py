"""SMTP probe — a free per-mailbox layer between MX and Hunter (P3a).

``email_validation`` proves a *domain* accepts mail. Hunter proves the *mailbox*
exists, but costs a credit per address and the free plan allows 50/month. An
SMTP probe answers the same question for free: open a conversation with the
domain's MX host and ask ``RCPT TO:<address>`` without ever sending a message.

Slotting this between MX and Hunter means Hunter is only paid for the addresses
the probe genuinely could not resolve.

**Outbound port 25 is the catch.** Both AWS (which is where Landline actually
runs) and Hetzner block outbound port 25 to public IPs by default, and the block
is lifted only by request. With it blocked every probe times out and returns
``unknown``, so this module is **off by default** (``SMTP_PROBE_ENABLED=false``):
enabling it before port 25 egress exists would add a connect timeout per contact
and change no outcome. Turn it on once ``nc -zv <some-mx> 25`` succeeds from the
host that runs the workers.

**Fail-open, like every layer around it.** Anything short of a clear answer —
port blocked, timeout, greylisting, a 4xx, a rejection whose text doesn't
actually say the mailbox is missing — returns ``unknown`` and lets Hunter have
the last word. This module never condemns on ambiguity.

**Two rejections look identical and are not.** A mail server can 550 a RCPT TO
because the mailbox doesn't exist, or because it doesn't like *us* (our IP has no
reputation, which is the normal state for a cloud host). Condemning on the second
would mark good contacts ``invalid`` permanently — the exact failure the
verification stack is built to avoid. So a rejection is only believed when its
enhanced status code or text positively identifies a missing mailbox; a generic
or policy-flavoured rejection is ``unknown``.

**Catch-all domains are detected, not guessed.** When the target is accepted we
probe a second, random address that cannot exist. If that is accepted too the
domain accepts everything and will bounce later, so the target is ``risky`` — the
same rule Hunter's ``accept_all`` gets, for the same reason.
"""

from __future__ import annotations

import logging
import random
import smtplib
import string
from typing import Optional

from app.config import get_settings
from app.services.email_validation import domain_of, is_valid_syntax

logger = logging.getLogger(__name__)

# Shared vocabulary with hunter_verification, so a probe verdict maps onto a
# contact exactly like a Hunter verdict does.
DELIVERABLE = "deliverable"
UNDELIVERABLE = "undeliverable"
RISKY = "risky"
UNKNOWN = "unknown"

# A 5xx is only believed when it positively identifies a missing mailbox.
# 5.1.1 is the RFC 3463 code for it; the phrases cover servers that don't send
# enhanced codes (plenty don't).
_NO_SUCH_MAILBOX = (
    "5.1.1",
    "5.1.0",
    "no such user",
    "no such recipient",
    "user unknown",
    "unknown user",
    "unknown recipient",
    "recipient not found",
    "recipient rejected",
    "user not found",
    "does not exist",
    "no mailbox",
    "invalid recipient",
    "invalid mailbox",
    "recipnotfound",
)

# Rejections that are about *us*, not the mailbox. Checked first: an IP-blocked
# 550 can still contain a word like "invalid", and treating it as a missing
# mailbox is how good contacts get wrongly condemned.
_ABOUT_US = (
    "5.7.1",
    "5.7.0",
    "5.7.26",
    "blocked",
    "blacklist",
    "block list",
    "denylist",
    "spam",
    "reputation",
    "policy",
    "not allowed",
    "access denied",
    "rate limit",
    "too many",
    "greylist",
    "graylist",
    "try again",
    "unauthenticated",
    "spf",
    "dmarc",
)


def _res(result: str, *, reason: str, detail: Optional[str] = None) -> dict:
    meta = {"reason": reason}
    if detail:
        # Truncated: server text is unbounded and lands in a JSONB column.
        meta["detail"] = detail[:200]
    return {"result": result, "meta": meta}


def classify_rejection(code: int, message: str) -> str:
    """Map a RCPT TO rejection to ``undeliverable`` or ``unknown``.

    Only a 5xx that positively identifies a missing mailbox is believed. A 4xx is
    temporary by definition (greylisting is the common case), and a 5xx that
    reads like a policy/reputation block says nothing about the mailbox.
    """
    text = (message or "").lower()

    if any(marker in text for marker in _ABOUT_US):
        return UNKNOWN
    if 400 <= code < 500:
        return UNKNOWN
    if code >= 500 and any(marker in text for marker in _NO_SUCH_MAILBOX):
        return UNDELIVERABLE
    return UNKNOWN


def mx_hosts(domain: str) -> list[str]:
    """MX hostnames for a domain, most-preferred first. Empty on any failure."""
    try:
        import dns.resolver  # noqa: PLC0415 — mirrors email_validation's optional import
    except Exception:  # pragma: no cover - dnspython is in requirements
        return []

    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = 5.0
        resolver.lifetime = 5.0
        answers = resolver.resolve(domain, "MX")
        ranked = sorted(answers, key=lambda r: r.preference)
        return [str(r.exchange).rstrip(".") for r in ranked]
    except Exception:
        # No MX / NXDOMAIN / timeout. check_mx already ran upstream and is the
        # authority on "this domain can't receive mail" — here it's just no host
        # to talk to, which is not evidence about the mailbox.
        return []


def _random_localpart() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=20))


def probe_email(address: str, *, settings=None) -> dict:
    """Ask a domain's MX host whether one mailbox exists. Never raises.

    Blocking (stdlib ``smtplib``) — call via ``asyncio.to_thread``. Returns
    ``{"result": ..., "meta": {...}}`` with result in deliverable / undeliverable
    / risky / unknown. Everything ambiguous is ``unknown``.
    """
    settings = settings or get_settings()

    if not getattr(settings, "smtp_probe_enabled", False):
        return _res(UNKNOWN, reason="disabled")

    sender = (getattr(settings, "smtp_probe_from", "") or "").strip()
    if not sender:
        # MAIL FROM must be an address at a domain we control: servers reject
        # probes from a bare or bogus sender, which would read as a mailbox
        # verdict. Refusing to probe is safer than probing badly.
        logger.warning("SMTP probe enabled but SMTP_PROBE_FROM is unset — skipping probe")
        return _res(UNKNOWN, reason="no_from_address")

    if not is_valid_syntax(address):
        return _res(UNKNOWN, reason="syntax")

    domain = domain_of(address)
    hosts = mx_hosts(domain)
    if not hosts:
        return _res(UNKNOWN, reason="no_mx_host")

    helo = (getattr(settings, "smtp_probe_helo", "") or "").strip() or domain_of(sender)
    port = getattr(settings, "smtp_probe_port", 25)
    timeout = getattr(settings, "smtp_probe_timeout", 10.0)
    host = hosts[0]

    smtp = None
    try:
        smtp = smtplib.SMTP(timeout=timeout, local_hostname=helo)
        smtp.connect(host, port)
        smtp.ehlo_or_helo_if_needed()
        smtp.mail(sender)

        code, message = smtp.rcpt(address)
        raw = message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message)

        if code in (250, 251):
            # Accepted — but a catch-all accepts anyone. Ask about an address
            # that cannot exist to tell a real mailbox from a domain that just
            # says yes to everything.
            ctrl_code, _ = smtp.rcpt(f"{_random_localpart()}@{domain}")
            if ctrl_code in (250, 251):
                return _res(RISKY, reason="catch_all")
            return _res(DELIVERABLE, reason="rcpt_accepted", detail=raw)

        verdict = classify_rejection(code, raw)
        reason = "rcpt_rejected" if verdict == UNDELIVERABLE else "rcpt_inconclusive"
        return _res(verdict, reason=reason, detail=f"{code} {raw}")

    except (smtplib.SMTPException, OSError) as exc:
        # OSError covers the blocked-port-25 timeout and connection refused.
        # Both say nothing about the mailbox.
        logger.debug("SMTP probe failed for %s via %s: %s", address, host, exc)
        return _res(UNKNOWN, reason="smtp_error", detail=f"{type(exc).__name__}: {exc}")
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass
