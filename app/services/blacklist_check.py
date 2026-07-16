"""DNSBL (blacklist) lookups for sending domains and IPs.

Companion to ``domain_auth`` (SPF/DKIM/DMARC). A domain or sending IP that lands
on a major blacklist (Spamhaus, Barracuda, SpamCop) will silently tank
deliverability, so the Domain Health Dashboard (Feature 5) checks the relevant
zones daily.

**How a DNSBL query works:** you look up an ``A`` record for a synthesized name
and decode the address it answers with:

* **IP-based** zones (``zen.spamhaus.org``, …): reverse the IP octets and prefix
  the zone — ``4.3.2.1.zen.spamhaus.org``. A ``127.0.0.x`` answer means *listed*;
  ``NXDOMAIN`` means *not listed*.
* **Domain-based** zones (``dbl.spamhaus.org``): prefix the domain itself —
  ``apprend.tech.dbl.spamhaus.org``. A ``127.0.1.x`` answer means *listed*.

**Not every answer is a listing.** Spamhaus answers in ``127.255.255.0/24`` to
report a problem with the *query* — most importantly ``127.255.255.254``, "you
asked via a public/open resolver, so I won't serve you data". Those are decoded
by ``is_dnsbl_error_code`` and reported as ``unknown``; treating them as
listings false-flags every domain checked (it did — see git history).

**DQS (Data Query Service) makes Spamhaus answers real.** ``127.255.255.254`` is
not a transient failure: our resolver *is* a public one, so every public-mirror
``spamhaus.org`` query is refused and the blacklist component is permanently
``unknown``. A DQS key authenticates the query. Set ``SPAMHAUS_DQS_KEY`` and the
Spamhaus zones are transparently rewritten at lookup time
(``dqs_zone``)::

    dbl.spamhaus.org  →  acme.com.<key>.dbl.dq.spamhaus.net
    zen.spamhaus.org  →  4.3.2.1.<key>.zen.dq.spamhaus.net

Non-Spamhaus zones (Barracuda, SpamCop) have no DQS equivalent and are left
alone. Without a key, behaviour is exactly as before (public zones, likely
refused). Free tier: https://www.spamhaus.org/free-dqs/.

The rewrite is **query-time only** — the ``zone`` we report and persist stays the
public name (``dbl.spamhaus.org``). Two reasons: the DQS name embeds a
credential and must never reach the database or a log, and the persisted zone
keys are what change-alerting diffs against, so turning DQS on must not look
like every zone was swapped out.

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

# DNSBL answers that encode a *query error* rather than a listing. Spamhaus (and
# zones following its conventions) answer in 127.255.255.0/24 when it will not
# serve the query at all:
#
#   127.255.255.252  typo'd DNSBL name
#   127.255.255.254  query arrived via a public/open resolver — refused, no data
#   127.255.255.255  excessive queries — refused
#
# plus the DBL-specific 127.0.1.255 ("IP queries prohibited" — DBL is domain-only).
# These mean *we learned nothing*, so they map to ``unknown``, never ``listed``.
#
# NB: 127.0.0.10 and 127.0.0.11 are **real PBL listings** (ISP-maintained and
# Spamhaus-maintained), not error codes — deliberately not listed here. Genuine
# listings live in 127.0.0.x (ZEN: SBL/CSS/XBL/DROP/PBL) and 127.0.1.x (DBL).
_DNSBL_ERROR_PREFIX = "127.255.255."
_DNSBL_ERROR_CODES = frozenset({"127.0.1.255"})


def is_dnsbl_error_code(answer: str) -> bool:
    """True when an ``A`` answer is a DNSBL error code rather than a listing."""
    a = (answer or "").strip()
    return a.startswith(_DNSBL_ERROR_PREFIX) or a in _DNSBL_ERROR_CODES


# Spamhaus public mirrors live under .spamhaus.org; the authenticated DQS
# equivalents under .dq.spamhaus.net, with the key as the label immediately left
# of the zone name: <query>.<key>.<name>.dq.spamhaus.net.
_SPAMHAUS_PUBLIC_SUFFIX = ".spamhaus.org"
_DQS_SUFFIX = ".dq.spamhaus.net"


def dqs_zone(zone: str, key: Optional[str]) -> str:
    """Rewrite a public Spamhaus ``zone`` to its authenticated DQS equivalent.

    ``zen.spamhaus.org`` + key → ``<key>.zen.dq.spamhaus.net``. The caller then
    prefixes the query as usual, giving Spamhaus's documented DQS form
    ``4.3.2.1.<key>.zen.dq.spamhaus.net`` / ``acme.com.<key>.dbl.dq.spamhaus.net``.

    Returns ``zone`` unchanged when there is no key, when the zone is not a
    Spamhaus public mirror (Barracuda/SpamCop have no DQS), or when it is already
    a DQS name — so this is safe to apply blindly to every configured zone.
    """
    z = (zone or "").strip().strip(".")
    k = (key or "").strip()
    if not z or not k:
        return zone
    low = z.lower()
    if low.endswith(_DQS_SUFFIX):
        return zone  # already a DQS name — assume the key is already embedded
    if not low.endswith(_SPAMHAUS_PUBLIC_SUFFIX):
        return zone  # not Spamhaus — a DQS key means nothing here
    name = z[: -len(_SPAMHAUS_PUBLIC_SUFFIX)]
    if not name:
        return zone  # bare "spamhaus.org" — no zone to authenticate
    return f"{k}.{name}{_DQS_SUFFIX}"


def _resolve_dqs_key(dqs_key: Optional[str]) -> str:
    """``None`` → read the configured key; an explicit string (incl. ``""``) wins."""
    if dqs_key is None:
        try:
            return (getattr(get_settings(), "spamhaus_dqs_key", "") or "").strip()
        except Exception:  # pragma: no cover - defensive; never break a check
            return ""
    return (dqs_key or "").strip()


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


def _query_zone(query_name: str, zone: str, *, dqs_key: Optional[str] = None) -> dict:
    """Look up ``<query_name>.<zone>`` and interpret the result as a DNSBL hit.

    Returns ``{listed, status, answers, issue, dqs}`` where ``status`` is
    ``ok`` (resolved, not listed), ``listed`` (resolved, listed), or ``unknown``
    (lookup failed, or the zone refused to answer — see ``is_dnsbl_error_code``).

    With a DQS key the *lookup* name is the authenticated DQS form, but the
    reported/persisted ``zone`` (added by the callers) stays the public name —
    the DQS name embeds a credential. ``dqs`` records whether this particular
    zone was queried authenticated, which is what distinguishes "unknown because
    the mirror refused us" from "unknown despite a key" when debugging.
    """
    lookup_zone = dqs_zone(zone, dqs_key)
    authed = lookup_zone != zone
    answers, err = _a_records(f"{query_name}.{lookup_zone}")
    if err:
        return {"listed": False, "status": "unknown", "answers": [], "issue": err,
                "dqs": authed}
    listings = [a for a in answers if not is_dnsbl_error_code(a)]
    if listings:
        return {"listed": True, "status": "listed", "answers": listings, "issue": None,
                "dqs": authed}
    if answers:
        # Every answer was an error code: the zone declined to tell us anything,
        # which is *not* evidence of being clean — stay unknown so the component
        # drops out of the score denominator instead of scoring a false 0.
        # Under DQS this most likely means the key is wrong/expired rather than
        # the public-resolver refusal, hence surfacing `dqs` alongside.
        return {"listed": False, "status": "unknown", "answers": answers,
                "issue": f"dnsbl_error:{answers[0]}", "dqs": authed}
    return {"listed": False, "status": "ok", "answers": [], "issue": None, "dqs": authed}


def check_domain_blacklist(domain: str, zone: str, *, dqs_key: Optional[str] = None) -> dict:
    """Check a single **domain-based** DNSBL zone (e.g. ``dbl.spamhaus.org``).

    ``dqs_key=None`` reads the configured ``SPAMHAUS_DQS_KEY``; pass ``""`` to
    force an unauthenticated public-mirror query.
    """
    res = _query_zone(domain, zone, dqs_key=_resolve_dqs_key(dqs_key))
    return {"zone": zone, "kind": "domain", "target": domain, "informational": False, **res}


def check_ip_blacklist(ip: str, zone: str, *, informational: bool = True,
                       dqs_key: Optional[str] = None) -> dict:
    """Check a single **IP-based** DNSBL zone for one IP (e.g. ``zen.spamhaus.org``)."""
    rev = reverse_ip(ip)
    if rev is None:
        return {"zone": zone, "kind": "ip", "target": ip, "informational": informational,
                "listed": False, "status": "unknown", "answers": [], "issue": "invalid_ip",
                "dqs": False}
    res = _query_zone(rev, zone, dqs_key=_resolve_dqs_key(dqs_key))
    return {"zone": zone, "kind": "ip", "target": ip, "informational": informational, **res}


def check_blacklists(
    domain: str,
    *,
    ips: Optional[list[str]] = None,
    ip_zones: Optional[list[str]] = None,
    domain_zones: Optional[list[str]] = None,
    ip_informational: bool = True,
    resolve_ips: bool = True,
    dqs_key: Optional[str] = None,
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
          "dqs": true/false,             # were Spamhaus zones queried authenticated
        }

    Zone keys are always the *public* zone names even when DQS is in use, so the
    shape is identical with and without a key (see ``dqs_zone``).
    """
    settings = get_settings()
    if ip_zones is None:
        ip_zones = list(getattr(settings, "dnsbl_zones", []) or [])
    if domain_zones is None:
        domain_zones = list(getattr(settings, "dnsbl_domain_zones", []) or [])
    # Resolved once here so every zone in this run agrees, and so the key is read
    # from settings exactly once rather than per zone.
    key = _resolve_dqs_key(dqs_key)

    zones: dict[str, dict] = {}

    # Domain-based zones — the meaningful signal for shared-pool (Gmail) senders.
    for zone in domain_zones:
        try:
            zones[zone] = check_domain_blacklist(domain, zone, dqs_key=key)
        except Exception as e:  # pragma: no cover - defensive; never raise
            logger.warning("blacklist domain check failed for %s/%s: %s", domain, zone, e)
            zones[zone] = {"zone": zone, "kind": "domain", "target": domain,
                           "informational": False, "listed": False, "status": "unknown",
                           "answers": [], "issue": f"error:{type(e).__name__}",
                           "dqs": False}

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
            zone_key = f"{zone}#{ip}"
            try:
                zones[zone_key] = check_ip_blacklist(ip, zone, informational=ip_informational,
                                                     dqs_key=key)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("blacklist ip check failed for %s/%s: %s", ip, zone, e)
                zones[zone_key] = {"zone": zone, "kind": "ip", "target": ip,
                                   "informational": ip_informational, "listed": False,
                                   "status": "unknown", "answers": [],
                                   "issue": f"error:{type(e).__name__}", "dqs": False}

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
        # Whether Spamhaus zones were queried authenticated. Never the key itself.
        "dqs": any(z.get("dqs") for z in zones.values()),
    }
