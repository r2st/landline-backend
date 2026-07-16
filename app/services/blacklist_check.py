"""DNSBL (blacklist) lookups for sending domains and IPs.

Companion to ``domain_auth`` (SPF/DKIM/DMARC). A domain or sending IP that lands
on a major blacklist (Spamhaus, Barracuda, SpamCop) will silently tank
deliverability, so the Domain Health Dashboard (Feature 5) checks the relevant
zones daily.

**How a DNSBL query works:** you look up an ``A`` record for a synthesized name
and interpret the *presence* of an answer as "listed":

* **IP-based** zones (``zen.spamhaus.org``, …): reverse the IP octets and prefix
  the zone — ``4.3.2.1.zen.spamhaus.org``. An ``A`` answer (typically
  ``127.0.0.x``) means *listed*; ``NXDOMAIN`` means *not listed*.
* **Domain-based** zones (``dbl.spamhaus.org``): prefix the domain itself —
  ``apprend.tech.dbl.spamhaus.org``. Same answer semantics.

**Fail-open, exactly like ``domain_auth._txt_records``:** a missing
``dnspython``, a timeout, or any resolver error yields ``status="unknown"`` for
that zone — a check never raises. ``unknown`` zones are excluded from the health
score denominator upstream.

**Gmail caveat:** mail sent through Gmail leaves Google's *shared* IP pool, not
an IP we control, so IP-DNSBL results for a Gmail domain are *informational*
(they describe Google's pool). The **domain DBL** and the auth records are the
meaningful signals there. IP zones are therefore marked ``informational`` by
default and excluded from the scored blacklist component; SMTP/SES senders on
dedicated IPs can opt into meaningful IP scoring.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

# Kept short so a slow/absent DNS server can't hang the daily task.
_DNS_TIMEOUT = 5.0


def _a_records(name: str) -> tuple[list[str], Optional[str]]:
    """Return ``(ip_answers, error)`` for an ``A`` lookup of ``name``.

    ``ip_answers`` is the list of A-record addresses as strings. ``error`` is a
    short reason when the lookup could not be completed (missing dep, timeout,
    resolver error). Critically, ``NXDOMAIN`` and "no answer" return
    ``([], None)`` — a *successful* resolution with no record, which for a DNSBL
    query is the "not listed" signal (not an error).
    """
    try:
        import dns.resolver  # noqa: PLC0415 — optional dependency
        from dns.resolver import NXDOMAIN, NoAnswer, NoNameservers  # type: ignore
        from dns.exception import DNSException, Timeout  # type: ignore
    except Exception:  # pragma: no cover - only when dnspython missing
        return [], "dnspython_not_installed"

    resolver = dns.resolver.Resolver()
    resolver.lifetime = _DNS_TIMEOUT
    resolver.timeout = _DNS_TIMEOUT
    try:
        answers = resolver.resolve(name, "A")
    except NXDOMAIN:
        return [], None  # resolved: not listed / no such name
    except NoAnswer:
        return [], None
    except (Timeout,):
        return [], "timeout"
    except (NoNameservers, DNSException) as e:  # pragma: no cover - network dependent
        return [], f"dns_error:{type(e).__name__}"
    except Exception as e:  # pragma: no cover - defensive
        return [], f"error:{type(e).__name__}"

    out: list[str] = []
    for rdata in answers:
        try:
            out.append(rdata.address)  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive
            out.append(str(rdata))
    return out, None


def reverse_ip(ip: str) -> Optional[str]:
    """Reverse an IPv4 address's octets for a DNSBL query (``1.2.3.4`` → ``4.3.2.1``).

    Returns ``None`` for anything that isn't a dotted IPv4 quad (IPv6/DNSBL is out
    of scope for v1).
    """
    parts = (ip or "").strip().split(".")
    if len(parts) != 4:
        return None
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return None
    if any(o < 0 or o > 255 for o in octets):
        return None
    return ".".join(str(o) for o in reversed(octets))


def _query_zone(query_name: str, zone: str) -> dict:
    """Look up ``<query_name>.<zone>`` and interpret the result as a DNSBL hit.

    Returns ``{listed, status, answers, issue}`` where ``status`` is
    ``ok`` (resolved, not listed), ``listed`` (resolved, listed), or ``unknown``
    (lookup failed).
    """
    answers, err = _a_records(f"{query_name}.{zone}")
    if err:
        return {"listed": False, "status": "unknown", "answers": [], "issue": err}
    if answers:
        return {"listed": True, "status": "listed", "answers": answers, "issue": None}
    return {"listed": False, "status": "ok", "answers": [], "issue": None}


def check_domain_blacklist(domain: str, zone: str) -> dict:
    """Check a single **domain-based** DNSBL zone (e.g. ``dbl.spamhaus.org``)."""
    res = _query_zone(domain, zone)
    return {"zone": zone, "kind": "domain", "target": domain, "informational": False, **res}


def check_ip_blacklist(ip: str, zone: str, *, informational: bool = True) -> dict:
    """Check a single **IP-based** DNSBL zone for one IP (e.g. ``zen.spamhaus.org``)."""
    rev = reverse_ip(ip)
    if rev is None:
        return {"zone": zone, "kind": "ip", "target": ip, "informational": informational,
                "listed": False, "status": "unknown", "answers": [], "issue": "invalid_ip"}
    res = _query_zone(rev, zone)
    return {"zone": zone, "kind": "ip", "target": ip, "informational": informational, **res}


def check_blacklists(
    domain: str,
    *,
    ips: Optional[list[str]] = None,
    ip_zones: Optional[list[str]] = None,
    domain_zones: Optional[list[str]] = None,
    ip_informational: bool = True,
    resolve_ips: bool = True,
) -> dict:
    """Run every configured DNSBL zone against a domain and its sending IPs.

    * ``domain_zones`` are checked against the domain itself (meaningful).
    * ``ip_zones`` are checked against each IP in ``ips``; if ``ips`` is not given
      and ``resolve_ips`` is set, the domain's own ``A`` records are used (for a
      Gmail domain these are the website's IPs — informational only).

    Returns a structure suitable for persisting on ``DomainHealth.blacklists``::

        {
          "checked_at": "…Z",
          "ips": ["1.2.3.4", …],
          "zones": {
            "dbl.spamhaus.org": {zone, kind, target, informational, listed, status, …},
            "zen.spamhaus.org#1.2.3.4": {…},
            …
          },
          "listed_meaningful": ["dbl.spamhaus.org"],   # non-informational listings
          "any_listed_meaningful": true/false,
          "status": "ok" | "listed" | "unknown",       # over meaningful zones only
        }
    """
    settings = get_settings()
    if ip_zones is None:
        ip_zones = list(getattr(settings, "dnsbl_zones", []) or [])
    if domain_zones is None:
        domain_zones = list(getattr(settings, "dnsbl_domain_zones", []) or [])

    zones: dict[str, dict] = {}

    # Domain-based zones — the meaningful signal for shared-pool (Gmail) senders.
    for zone in domain_zones:
        try:
            zones[zone] = check_domain_blacklist(domain, zone)
        except Exception as e:  # pragma: no cover - defensive; never raise
            logger.warning("blacklist domain check failed for %s/%s: %s", domain, zone, e)
            zones[zone] = {"zone": zone, "kind": "domain", "target": domain,
                           "informational": False, "listed": False, "status": "unknown",
                           "answers": [], "issue": f"error:{type(e).__name__}"}

    # Resolve sending IPs if not supplied.
    resolved_ips = list(ips) if ips else []
    if not resolved_ips and resolve_ips and ip_zones:
        found, err = _a_records(domain)
        if err:
            logger.debug("could not resolve A records for %s: %s", domain, err)
        resolved_ips = found

    # IP-based zones — one entry per (zone, ip).
    for ip in resolved_ips:
        for zone in ip_zones:
            key = f"{zone}#{ip}"
            try:
                zones[key] = check_ip_blacklist(ip, zone, informational=ip_informational)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("blacklist ip check failed for %s/%s: %s", ip, zone, e)
                zones[key] = {"zone": zone, "kind": "ip", "target": ip,
                              "informational": ip_informational, "listed": False,
                              "status": "unknown", "answers": [], "issue": f"error:{type(e).__name__}"}

    meaningful = [z for z in zones.values() if not z.get("informational")]
    listed_meaningful = sorted({z["zone"] for z in meaningful if z.get("listed")})
    if meaningful and any(z.get("listed") for z in meaningful):
        overall = "listed"
    elif meaningful and any(z.get("status") == "ok" for z in meaningful):
        overall = "ok"
    else:
        # Nothing meaningful resolved (all unknown / no meaningful zones configured).
        overall = "unknown"

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ips": resolved_ips,
        "zones": zones,
        "listed_meaningful": listed_meaningful,
        "any_listed_meaningful": bool(listed_meaningful),
        "status": overall,
    }
