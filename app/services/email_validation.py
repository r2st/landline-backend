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
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_DNS_TIMEOUT = 5.0

# Domain → status cache ("valid" | "invalid" | "unknown"). Unknowns are cached
# too but callers treat them as "check again later" via email_status=None reset.
_MX_CACHE: dict[str, str] = {}

# Pragmatic email syntax check: one @, a dot in the domain, no spaces. Not
# RFC-5322-exhaustive on purpose — the MX check is the real gate.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_syntax(email: Optional[str]) -> bool:
    return bool(email and _EMAIL_RE.match(email.strip()))


def domain_of(email: str) -> str:
    return email.strip().rsplit("@", 1)[-1].lower()


def check_mx(domain: str) -> str:
    """Return ``"valid"`` | ``"invalid"`` | ``"unknown"`` for a domain's MX.

    ``valid``   — the domain publishes at least one MX record (or a fallback A
                  record, which RFC 5321 permits as an implicit mail host).
    ``invalid`` — the domain resolves but has neither MX nor A (can't receive
                  mail) or does not exist (NXDOMAIN).
    ``unknown`` — the lookup couldn't be completed (missing dep / timeout / DNS
                  error); never treated as a reason to suppress.
    """
    domain = (domain or "").strip().lower()
    if not domain or "." not in domain:
        return "invalid"
    if domain in _MX_CACHE:
        return _MX_CACHE[domain]

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

    _MX_CACHE[domain] = status
    return status


def validate_email(email: Optional[str]) -> str:
    """Full recipient validation: syntax then MX. Returns the status string."""
    if not is_valid_syntax(email):
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
