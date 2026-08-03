"""Recipient email validation — syntax + MX record pre-check.

Before spending sender reputation on an address we verify that (a) it's
syntactically an email and (b) its domain actually publishes MX records. A
domain with no MX can't receive mail, so sending guarantees a hard bounce —
exactly the signal that tanks a sending domain's reputation. Catching these
before they go out is one of the cheapest deliverability wins available.

Everything here is **fail-open and dependency-optional**: if ``dnspython`` isn't
installed, or a lookup errors/times out, the result is ``"unknown"`` (never
``"invalid"``) so a transient DNS problem can't wrongly suppress good contacts.

Results are cached per-domain in-process for the lifetime of the worker to avoid
re-resolving the same domain for every contact at that company.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_DNS_TIMEOUT = 5.0

# Domain → (status, expires_at) cache, where ``expires_at`` is a
# ``time.monotonic`` reading — a wall clock that steps backwards over an NTP
# correction would extend a verdict's life rather than end it.
#
# Two properties this needs that a bare ``dict[str, str]`` did not have:
#
# **A verdict expires, and ``unknown`` expires quickly.** ``unknown`` means "the
# lookup did not complete" — a resolver timeout, a SERVFAIL, dnspython missing
# for one deploy. Cached forever, one DNS blip during a batch permanently
# disabled the pre-bounce gate for that domain in that worker, and Celery workers
# live for days. The direction is the problem: this gate exists to stop hard
# bounces before they cost sending reputation, so its failure mode is silent —
# nothing errors, the domain is simply never checked again and its dead addresses
# go out. Note the second layer, which is why the short TTL matters even though
# ``unknown`` is fail-open: ``app.tasks.outreach`` only validates a contact when
# ``email_status is None``, so the ``unknown`` written from a poisoned cache
# entry also stops *that* contact ever being re-checked. Nothing anywhere resets
# ``email_status`` to None, contrary to what this comment used to claim.
#
# **It is bounded.** One entry per domain, never evicted, in a long-lived worker
# that the autonomous prospector feeds new domains all day.
_MX_CACHE: dict[str, tuple[str, float]] = {}

# How long each verdict is trusted. A settled answer (MX present, or NXDOMAIN)
# is stable enough to hold for a day; a failed lookup is re-tried within the
# minute, which is short enough that a resolver blip costs one batch rather than
# the worker's lifetime, and long enough that a genuine outage doesn't turn every
# contact at a domain into its own 5-second timeout.
_MX_TTL_SECONDS = {"valid": 86400.0, "invalid": 86400.0, "unknown": 60.0}

# Cache ceiling. Evicts the soonest-to-expire entries when full, so the memory is
# bounded without the cache ever handing back an answer it should have dropped.
_MX_CACHE_MAX = 10_000

# Pragmatic email syntax check: one @, a dot in the domain, no spaces. Not
# RFC-5322-exhaustive on purpose — the MX check is the real gate.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_syntax(email: Optional[str]) -> bool:
    return bool(email and _EMAIL_RE.match(email.strip()))


def domain_of(email: str) -> str:
    return email.strip().rsplit("@", 1)[-1].lower()


def _cache_get(domain: str) -> Optional[str]:
    """The cached verdict for ``domain``, or ``None`` if absent or expired."""
    entry = _MX_CACHE.get(domain)
    if entry is None:
        return None
    status, expires_at = entry
    if expires_at <= time.monotonic():
        _MX_CACHE.pop(domain, None)
        return None
    return status


def _cache_put(domain: str, status: str) -> None:
    """Cache ``status`` for ``domain`` for its TTL, keeping the cache bounded."""
    if len(_MX_CACHE) >= _MX_CACHE_MAX and domain not in _MX_CACHE:
        # Drop the tenth nearest to expiry rather than one entry per insert, so a
        # full cache doesn't pay a sort on every miss. Expiry order is the right
        # eviction order here: entries near it are the ``unknown`` retries, which
        # are the cheapest to lose and the ones we *want* re-asked.
        doomed = sorted(_MX_CACHE.items(), key=lambda kv: kv[1][1])
        for key, _ in doomed[: max(1, _MX_CACHE_MAX // 10)]:
            _MX_CACHE.pop(key, None)
    ttl = _MX_TTL_SECONDS.get(status, _MX_TTL_SECONDS["unknown"])
    _MX_CACHE[domain] = (status, time.monotonic() + ttl)


def check_mx(domain: str) -> str:
    """Return ``"valid"`` | ``"invalid"`` | ``"unknown"`` for a domain's MX.

    ``valid``   — the domain publishes at least one MX record (or a fallback A
                  record, which RFC 5321 permits as an implicit mail host).
    ``invalid`` — the domain resolves but has neither MX nor A (can't receive
                  mail) or does not exist (NXDOMAIN).
    ``unknown`` — the lookup couldn't be completed (missing dep / timeout / DNS
                  error); never treated as a reason to suppress, and re-tried
                  within the minute rather than cached for the worker's life.
    """
    domain = (domain or "").strip().lower()
    if not domain or "." not in domain:
        return "invalid"
    cached = _cache_get(domain)
    if cached is not None:
        return cached

    try:
        import dns.resolver  # noqa: PLC0415 — optional dependency
        from dns.resolver import NXDOMAIN, NoAnswer, NoNameservers  # type: ignore
        from dns.exception import DNSException, Timeout  # type: ignore
    except Exception:  # pragma: no cover - only when dnspython missing
        return "unknown"

    resolver = dns.resolver.Resolver()
    resolver.lifetime = _DNS_TIMEOUT
    resolver.timeout = _DNS_TIMEOUT

    try:
        answers = resolver.resolve(domain, "MX")
        status = "valid" if len(answers) > 0 else "invalid"
    except NXDOMAIN:
        status = "invalid"
    except NoAnswer:
        # No MX — RFC 5321 allows falling back to an A record as the mail host.
        try:
            a = resolver.resolve(domain, "A")
            status = "valid" if len(a) > 0 else "invalid"
        except NXDOMAIN:
            status = "invalid"
        except (NoAnswer,):
            status = "invalid"
        except (Timeout, NoNameservers, DNSException):  # pragma: no cover - network
            status = "unknown"
    except (Timeout,):
        status = "unknown"
    except (NoNameservers, DNSException):  # pragma: no cover - network dependent
        status = "unknown"
    except Exception:  # pragma: no cover - defensive
        status = "unknown"

    _cache_put(domain, status)
    return status


# Addresses that are syntactically fine and whose domain publishes MX — so both
# gates above wave them through — but that are never a human prospect. These are
# scraper residue: a website crawl picks up the Sentry error-reporting address
# embedded in a Wix site's JavaScript, or an asset filename that happens to look
# like an address. We really did enrol and email
# ``460ff4620fa44cba8df530afde949785@sentry.wixpress.com``.
#
# Domains whose mail is machine-only. Matched on the domain or any parent, so
# ``sentry.wixpress.com`` is caught by ``wixpress.com``.
_NON_PROSPECT_DOMAINS = frozenset({
    "sentry.io",
    "wixpress.com",
    "sentry.wixpress.com",
})

# Web-asset extensions. A "domain" ending in one of these came from a filename
# (``logo@2x.png`` — a retina asset), not a mail host.
_ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".pdf",
)

# A local part that is a bare 32-char hex blob is a machine identifier (a Sentry
# public key, a tracking id), never a person's mailbox.
_HEX_BLOB_LOCALPART_RE = re.compile(r"^[0-9a-f]{32,}$", re.IGNORECASE)

# Mailboxes that exist only to originate mail; sending to them is guaranteed
# waste and often lands in an abuse-monitored trap.
_NO_REPLY_LOCALPART_RE = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|bounce[sd]?|"
    r"mailer[-_.]?daemon|postmaster|abuse)$",
    re.IGNORECASE,
)


def is_non_prospect_address(email: Optional[str]) -> tuple[bool, str]:
    """Whether ``email`` is structurally incapable of being a real prospect.

    This is a *shape* check, not a deliverability check: every address it
    rejects would sail through both the syntax and MX gates, because the domain
    is genuinely real and genuinely accepts mail — it is just never read by a
    person who could reply. Sending to these spends sender reputation for a
    guaranteed zero and risks tripping an abuse mailbox.

    Returns ``(is_junk, reason)``; ``reason`` is "" when the address is fine.
    Conservative by design — it only rejects patterns that cannot be a human
    mailbox, so a role address like ``info@`` (a weak prospect, but a real
    inbox a person may read) is deliberately **not** rejected here.
    """
    if not email:
        return True, "empty address"
    addr = email.strip().lower()
    if "@" not in addr:
        return True, "not an address"

    local, _, domain = addr.rpartition("@")

    if domain.endswith(_ASSET_SUFFIXES):
        return True, f"asset filename, not a mail domain ({domain})"

    # Match the domain or any parent domain.
    labels = domain.split(".")
    for i in range(len(labels) - 1):
        if ".".join(labels[i:]) in _NON_PROSPECT_DOMAINS:
            return True, f"machine-only domain ({domain})"

    if _HEX_BLOB_LOCALPART_RE.match(local):
        return True, "machine-generated local part"

    if _NO_REPLY_LOCALPART_RE.match(local):
        return True, f"unattended mailbox ({local}@)"

    return False, ""


# Shared-inbox local parts. A real mailbox, read by a real person — which is why
# ``is_non_prospect_address`` deliberately lets these through — but a *worse*
# prospect than a named individual: it lands in a queue triaged by whoever is on
# rota, and cold mail is the first thing that queue deletes.
_ROLE_LOCALPART_RE = re.compile(
    r"^(info|sales|contact|contactus|admin|administrator|support|help|helpdesk|"
    r"service|customerservice|hello|hi|enquir(y|ies)|inquir(y|ies)|office|mail|"
    r"email|team|general|accounts?|accounting|billing|invoices?|finance|ar|ap|"
    r"careers?|jobs|recruiting|hr|marketing|press|media|pr|webmaster|web|"
    r"orders?|quotes?|rfq|purchasing|procurement|shipping|reception|frontdesk)"
    r"([-_.]?(us|team|dept|department|inbox))?$",
    re.IGNORECASE,
)


def is_role_address(email: Optional[str]) -> bool:
    """Whether ``email`` is a shared/role inbox rather than a named person.

    A *ranking* signal, never a rejection — the opposite of
    :func:`is_non_prospect_address`, which decides deliverability. ``info@`` is a
    perfectly real inbox and worth mailing when it is all a company exposes; it
    is simply the last address to pick when a named alternative exists, and the
    one to drop first when capping how many people at a company get touched.

    Empty or malformed input is not a role address (it is not an address at all);
    that call belongs to the validation gate.
    """
    if not email or "@" not in email:
        return False
    local, _, _ = email.strip().lower().rpartition("@")
    return bool(_ROLE_LOCALPART_RE.match(local))


def validate_email(email: Optional[str]) -> str:
    """Full recipient validation: syntax, junk shape, then MX.

    The junk-shape gate runs before the (network) MX lookup: it is free, and an
    address it rejects is undeliverable-in-practice regardless of what DNS says.
    """
    if not is_valid_syntax(email):
        return "invalid"
    is_junk, reason = is_non_prospect_address(email)
    if is_junk:
        logger.info("Rejecting non-prospect address %s: %s", email, reason)
        return "invalid"
    return check_mx(domain_of(email))


async def validate_contact(db, contact) -> str:
    """Validate a contact's email and persist the result on the contact.

    Sets ``email_status`` + ``email_checked_at``. Runs the (blocking) DNS lookup
    off the event loop. Does NOT commit — the caller owns the transaction.
    Returns the status.
    """
    import asyncio

    status = await asyncio.to_thread(validate_email, contact.email)
    contact.email_status = status
    contact.email_checked_at = datetime.now(timezone.utc)
    if status == "invalid":
        logger.info("Contact %s has an undeliverable email domain (%s)", contact.id, contact.email)
    return status


def clear_cache() -> None:
    """Clear the in-process MX cache (used by tests)."""
    _MX_CACHE.clear()
