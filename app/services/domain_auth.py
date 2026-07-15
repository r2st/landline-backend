"""Sending-domain authentication checks: SPF, DKIM, DMARC.

Part of the deliverability toolkit. Cold email from a domain without aligned
SPF/DKIM/DMARC is far more likely to land in spam or be rejected outright, so
before (or while) warming a sender we verify the domain's DNS is set up.

Everything here is **fail-open and dependency-optional**:

* If ``dnspython`` isn't installed, or a lookup errors/times out, the affected
  record is reported ``present=False, status="unknown"`` with a reason — a
  request is never allowed to raise.
* DKIM has no discoverable record name, so we probe a small set of common
  selectors (``selector._domainkey.domain``); a caller can pass an explicit
  selector when they know it.

The functions are synchronous DNS calls wrapped for use from async endpoints via
``asyncio.to_thread`` at the call site.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

# Resolver timeouts kept short so a slow/absent DNS server can't hang a request.
_DNS_TIMEOUT = 5.0


def _txt_records(name: str) -> tuple[list[str], Optional[str]]:
    """Return ``(txt_strings, error)`` for a DNS name.

    ``txt_strings`` is the list of concatenated TXT record values (each record's
    character-strings joined). ``error`` is a short reason string when the
    lookup could not be completed (missing dep, NXDOMAIN, no answer, timeout);
    an empty record set with ``error=None`` means "resolved, but no TXT here".
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
        answers = resolver.resolve(name, "TXT")
    except NXDOMAIN:
        return [], "nxdomain"
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
            # Each TXT rdata is one or more byte character-strings.
            parts = [
                s.decode("utf-8", "ignore") if isinstance(s, (bytes, bytearray)) else str(s)
                for s in rdata.strings
            ]
            out.append("".join(parts))
        except Exception:  # pragma: no cover - defensive
            out.append(str(rdata).strip('"'))
    return out, None


def check_spf(domain: str) -> dict:
    """Look up the domain's SPF record (a TXT starting ``v=spf1``)."""
    txts, err = _txt_records(domain)
    if err:
        return {"type": "spf", "present": False, "valid": False, "status": "unknown",
                "record": None, "policy": None, "issues": [err]}

    spf = next((t for t in txts if t.lower().startswith("v=spf1")), None)
    if not spf:
        return {"type": "spf", "present": False, "valid": False, "status": "fail",
                "record": None, "policy": None,
                "issues": ["no SPF record — add 'v=spf1 ... ~all'"]}

    lower = spf.lower()
    issues: list[str] = []
    # The 'all' mechanism qualifier is the enforcement policy.
    if "-all" in lower:
        policy = "hardfail"
    elif "~all" in lower:
        policy = "softfail"
    elif "?all" in lower:
        policy = "neutral"
        issues.append("SPF ends in '?all' (neutral) — provides no protection")
    elif "+all" in lower:
        policy = "pass_all"
        issues.append("SPF ends in '+all' — allows anyone to send; remove it")
    else:
        policy = "none"
        issues.append("SPF has no 'all' mechanism — add '~all' or '-all'")

    valid = policy in ("hardfail", "softfail")
    return {"type": "spf", "present": True, "valid": valid,
            "status": "pass" if valid else "warn",
            "record": spf, "policy": policy, "issues": issues}


def check_dmarc(domain: str) -> dict:
    """Look up the domain's DMARC record at ``_dmarc.<domain>``."""
    txts, err = _txt_records(f"_dmarc.{domain}")
    if err == "nxdomain":
        return {"type": "dmarc", "present": False, "valid": False, "status": "fail",
                "record": None, "policy": None,
                "issues": ["no DMARC record — add a TXT at _dmarc with 'v=DMARC1; p=none'"]}
    if err:
        return {"type": "dmarc", "present": False, "valid": False, "status": "unknown",
                "record": None, "policy": None, "issues": [err]}

    dmarc = next((t for t in txts if t.lower().startswith("v=dmarc1")), None)
    if not dmarc:
        return {"type": "dmarc", "present": False, "valid": False, "status": "fail",
                "record": None, "policy": None,
                "issues": ["no DMARC record — add 'v=DMARC1; p=none'"]}

    # Parse tag=value pairs.
    tags = {}
    for part in dmarc.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            tags[k.strip().lower()] = v.strip()
    policy = tags.get("p", "none").lower()
    issues: list[str] = []
    if policy == "none":
        issues.append("DMARC policy is 'p=none' (monitor only) — tighten to quarantine/reject once aligned")
    if "rua" not in tags:
        issues.append("no aggregate reporting address (rua=) — you won't receive DMARC reports")

    # Present + enforcing (quarantine/reject) = pass; monitor-only = warn.
    valid = policy in ("quarantine", "reject")
    return {"type": "dmarc", "present": True, "valid": valid,
            "status": "pass" if valid else "warn",
            "record": dmarc, "policy": policy, "issues": issues}


def check_dkim(domain: str, selectors: Optional[list[str]] = None) -> dict:
    """Probe common DKIM selectors at ``<selector>._domainkey.<domain>``.

    DKIM has no canonical record name, so without an explicit selector we try a
    configured list. Reports the first selector that resolves to a ``v=DKIM1``
    (or public-key ``p=``) record.
    """
    if not selectors:
        selectors = list(get_settings().dkim_default_selectors)

    checked: list[str] = []
    any_unknown = False
    for sel in selectors:
        checked.append(sel)
        txts, err = _txt_records(f"{sel}._domainkey.{domain}")
        if err == "dnspython_not_installed":
            return {"type": "dkim", "present": False, "valid": False, "status": "unknown",
                    "record": None, "selector": None, "selectors_checked": checked,
                    "issues": [err]}
        if err and err != "nxdomain":
            any_unknown = True
            continue
        rec = next(
            (t for t in txts if t.lower().startswith("v=dkim1") or "p=" in t.lower()),
            None,
        )
        if rec:
            has_key = "p=" in rec.lower() and not rec.lower().rstrip().endswith("p=")
            return {"type": "dkim", "present": True, "valid": has_key,
                    "status": "pass" if has_key else "warn",
                    "record": rec, "selector": sel, "selectors_checked": checked,
                    "issues": [] if has_key else ["DKIM record has an empty public key (p=) — key revoked?"]}

    status = "unknown" if any_unknown else "fail"
    issue = (
        "could not resolve DKIM for any probed selector"
        if status == "unknown"
        else f"no DKIM record found for selectors {checked} — publish one or pass the correct selector"
    )
    return {"type": "dkim", "present": False, "valid": False, "status": status,
            "record": None, "selector": None, "selectors_checked": checked,
            "issues": [issue]}


def check_domain_auth(domain: str, dkim_selectors: Optional[list[str]] = None) -> dict:
    """Run all three checks and roll up an overall verdict.

    Overall status:
      * ``fail``    — any of SPF/DKIM/DMARC is definitively missing/misconfigured
      * ``warn``    — all present but at least one not fully enforcing (or unknown)
      * ``pass``    — SPF + DKIM + DMARC all present and valid
    """
    domain = (domain or "").strip().lower().lstrip("@")
    # Accept an email address and use its domain part.
    if "@" in domain:
        domain = domain.split("@", 1)[1]
    if not domain or "." not in domain:
        return {"domain": domain, "overall": "invalid",
                "issues": ["not a valid domain"], "spf": None, "dkim": None, "dmarc": None}

    spf = check_spf(domain)
    dkim = check_dkim(domain, dkim_selectors)
    dmarc = check_dmarc(domain)
    parts = [spf, dkim, dmarc]

    statuses = {p["status"] for p in parts}
    if "fail" in statuses:
        overall = "fail"
    elif statuses == {"pass"}:
        overall = "pass"
    else:
        overall = "warn"

    recommendations: list[str] = []
    for p in parts:
        recommendations.extend(p.get("issues") or [])

    return {
        "domain": domain,
        "overall": overall,
        "spf": spf,
        "dkim": dkim,
        "dmarc": dmarc,
        "recommendations": recommendations,
    }
