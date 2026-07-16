"""DIY email pattern discovery — the free layer under Hunter's email-finder.

Apollo's free plan 403s both search endpoints and Hunter's ``/email-finder``
spends a credit per lookup against a 50/month free allowance, so the two paths
that turn "a name at a company" into "an address we can mail" are both gated
behind money we may not have. This module is the unpaid fallback: derive the
handful of addresses a person *plausibly* has from their name and their
employer's domain, then try to prove one of them exists.

**A generated address is a guess until something verifies it.** That distinction
is the whole design. ``generate`` is free, offline and always succeeds; it tells
you nothing about whether anyone reads the mailbox. Only the probe layer
(Reacher, or a gated SMTP check) can promote a guess, and when no probe is
available the guess stays a guess — :meth:`discover` returns it with
``email_status='unknown'``, never ``'valid'``. Writing ``'valid'`` off a pattern
match would hand the send path a fabricated address to mail, and hard bounces are
precisely what Feature 2's auto-pause reacts to by halting *all* sending.

**Catch-all domains defeat this technique outright, so we detect and refuse.**
A catch-all accepts every RCPT TO at SMTP time — ``first.last@``, ``flast@`` and
``asdfqwer@`` all come back deliverable. Probing patterns against one doesn't
find the real mailbox, it just returns whichever guess we happened to try first,
with false confidence. We probe a deliberately absent address first; if *that*
is accepted, the domain tells us nothing and we stop. This mirrors the
``accept_all → risky`` rule ``hunter_verification`` already holds to.

**Fail-open, like every other verification layer here.** No Reacher URL, a
timeout, a DNS wobble, a missing optional dependency — none of them may write
``email_status='invalid'`` or invent an address. The worst case is that a contact
stays exactly as un-emailable as they already were.
"""

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

from app.config import get_settings
from app.services.email_validation import check_mx, is_valid_syntax
from app.services.normalize import normalize_domain

logger = logging.getLogger(__name__)

# Verdict vocabulary is deliberately shared with hunter_verification: both write
# the same `email_verification_result` column, and two services describing the
# same mailbox with different words would make the column unreadable.
DELIVERABLE = "deliverable"
UNDELIVERABLE = "undeliverable"
RISKY = "risky"
UNKNOWN = "unknown"

# `email_verification_source` is String(10) — keep this under that.
SOURCE_PATTERN = "pattern"

_TIMEOUT = 15.0

# Local-part builders for the seven patterns we generate, ordered by rough
# prevalence. The weights are a **heuristic ordering prior, not measured
# statistics** — their only job is to decide which address to probe first so the
# common shapes cost the fewest probes. Nothing downstream reads them as a
# confidence score.
_PATTERNS: tuple[tuple[str, int, Callable[[str, str], Optional[str]]], ...] = (
    ("first.last", 32, lambda fn, ln: f"{fn}.{ln}" if fn and ln else None),
    ("flast", 22, lambda fn, ln: f"{fn[0]}{ln}" if fn and ln else None),
    ("first", 14, lambda fn, ln: fn or None),
    ("firstl", 9, lambda fn, ln: f"{fn}{ln[0]}" if fn and ln else None),
    ("f.last", 8, lambda fn, ln: f"{fn[0]}.{ln}" if fn and ln else None),
    ("first_last", 6, lambda fn, ln: f"{fn}_{ln}" if fn and ln else None),
    ("last", 5, lambda fn, ln: ln or None),
)

# Strip everything a mailbox can't contain. Names arrive from Apollo/CSV/Places
# with accents, apostrophes, hyphens and honorifics: "Ana Rodríguez-Peña" and
# "Sean O'Brien" must become "ana.rodriguezpena" and "sean.obrien", not addresses
# with punctuation no SMTP server will route.
_NON_MAILBOX_RE = re.compile(r"[^a-z0-9]+")

# An address no real person holds, used to unmask catch-all domains. Constant on
# purpose: a random local part would make the probe non-reproducible, and this is
# already improbable enough that a collision would be its own story.
_CATCH_ALL_PROBE = "zzz-no-such-mailbox-landline"


@dataclass(frozen=True)
class PatternCandidate:
    """One generated guess. ``prior`` orders probing; it is not a confidence."""

    email: str
    pattern: str
    prior: int


def _sanitize_name_part(raw: Optional[str]) -> str:
    """Fold one name down to the characters a local part may contain.

    Accent-folds ("Peña" → "pena") the same way ``normalize.normalize_name_key``
    does, then deletes rather than separates the intra-name punctuation, so
    "O'Brien" → "obrien" and "Rodríguez-Peña" → "rodriguezpena". Returns "" when
    nothing usable survives (a CJK name folds to empty — see ``generate``).
    """
    if not raw:
        return ""
    value = unicodedata.normalize("NFKD", str(raw))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return _NON_MAILBOX_RE.sub("", value.strip().lower())


def generate(
    first_name: Optional[str],
    last_name: Optional[str],
    domain: Optional[str],
) -> list[PatternCandidate]:
    """Generate the plausible addresses for a person at a domain, best guess first.

    Free, offline, and says nothing about deliverability — every result is a
    guess. Returns ``[]`` when there is nothing to build from: no domain, or a
    name that leaves no mailbox-safe characters behind (a Cyrillic or CJK name
    folds to empty, and a domain-shaped guess from an empty local part would be
    pure noise).

    Patterns that collapse to the same address are emitted once, under the
    likelier name — a one-letter first name makes ``first.last`` and ``f.last``
    identical, and probing "a.chen@" twice would just spend two probes.
    """
    clean_domain = normalize_domain(domain)
    if not clean_domain:
        return []

    first = _sanitize_name_part(first_name)
    last = _sanitize_name_part(last_name)
    if not first and not last:
        return []

    out: list[PatternCandidate] = []
    seen: set[str] = set()
    for name, prior, build in _PATTERNS:
        local = build(first, last)
        if not local or local in seen:
            continue
        seen.add(local)
        email = f"{local}@{clean_domain}"
        # A name of pure punctuation can still yield something syntactically
        # broken; the free check is cheaper than a wasted probe.
        if is_valid_syntax(email):
            out.append(PatternCandidate(email=email, pattern=name, prior=prior))
    return out


def domain_accepts_mail(domain: Optional[str]) -> str:
    """Return ``"valid"`` | ``"invalid"`` | ``"unknown"`` for a domain's MX.

    Thin wrapper over :func:`app.services.email_validation.check_mx` — that
    module already owns MX resolution, its per-domain cache and the RFC 5321
    A-record fallback, and a second resolver here would drift from it. Kept as a
    named entry point because "can this domain receive mail at all" is the first
    question pattern discovery asks, and it reads better than reaching through
    to the validation module at each call site.

    ``unknown`` means the lookup failed (dnspython absent, DNS timeout), never
    that the domain is bad.
    """
    clean = normalize_domain(domain)
    if not clean:
        return "invalid"
    return check_mx(clean)


class EmailPatternGenerator:
    """Generates candidate addresses and, where possible, proves one exists."""

    def __init__(self) -> None:
        settings = get_settings()
        self.reacher_url: str = (settings.reacher_url or "").rstrip("/")
        self.reacher_api_key: str = settings.reacher_api_key or ""
        self.smtp_fallback: bool = bool(settings.pattern_smtp_fallback_enabled)
        self.smtp_from: str = settings.pattern_smtp_from_address or ""
        self.max_probes: int = int(settings.pattern_max_probes)

    @property
    def can_probe(self) -> bool:
        """Whether any verification layer is actually wired up.

        False means :meth:`discover` still generates, but every result stays
        ``unknown`` — useful (a guess beats no address for a human to eyeball)
        but never auto-sendable.
        """
        return bool(self.reacher_url) or self.smtp_fallback

    # ── probes ──────────────────────────────────────────────────────────────
    async def _probe_reacher(self, email: str) -> str:
        """Ask a Reacher instance about one address.

        Returns ``deliverable`` | ``undeliverable`` | ``risky`` | ``unknown``.
        Never raises: any transport or decode failure is ``unknown``, which keeps
        a Reacher outage from condemning addresses.

        Reacher reports a catch-all as ``is_reachable: "risky"`` with
        ``smtp.is_catch_all`` set. We surface that as ``risky`` rather than
        trusting it — :meth:`_is_catch_all` makes the call that matters.
        """
        headers = {"Authorization": self.reacher_api_key} if self.reacher_api_key else {}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    f"{self.reacher_url}/v0/check_email",
                    json={"to_email": email},
                    headers=headers,
                )
            if response.status_code >= 400:
                logger.warning(
                    "Reacher returned %d for %s", response.status_code, email
                )
                return UNKNOWN
            payload = response.json() or {}
        except Exception as e:  # network, timeout, bad JSON
            logger.warning("Reacher probe errored for %s: %s", email, e)
            return UNKNOWN

        reachable = str(payload.get("is_reachable") or "").strip().lower()
        if reachable == "safe":
            return DELIVERABLE
        if reachable == "invalid":
            return UNDELIVERABLE
        if reachable == "risky":
            return RISKY
        return UNKNOWN

    def _probe_smtp_blocking(self, email: str) -> str:
        """RCPT TO probe against the domain's mail exchanger. Blocking.

        Off by default and deliberately so — see ``pattern_smtp_fallback_enabled``
        in config. A 550 is the only signal we treat as definitive; everything
        else (greylisting, tarpits, policy rejections that have nothing to do
        with the mailbox) is ``unknown``.
        """
        domain = email.rsplit("@", 1)[-1]
        try:
            import dns.resolver  # noqa: PLC0415 — optional dependency

            answers = dns.resolver.resolve(domain, "MX")
            host = str(sorted(answers, key=lambda r: r.preference)[0].exchange).rstrip(".")
        except Exception as e:
            logger.warning("SMTP probe could not resolve MX for %s: %s", domain, e)
            return UNKNOWN

        try:
            with smtplib.SMTP(host, 25, timeout=_TIMEOUT) as server:
                server.ehlo_or_helo_if_needed()
                server.mail(self.smtp_from)
                code, _ = server.rcpt(email)
        except Exception as e:
            logger.warning("SMTP probe to %s failed: %s", host, e)
            return UNKNOWN

        if code in (250, 251):
            return DELIVERABLE
        if code == 550:
            return UNDELIVERABLE
        return UNKNOWN

    async def _probe(self, email: str) -> str:
        """Probe one address via whichever layer is configured."""
        if self.reacher_url:
            return await self._probe_reacher(email)
        if self.smtp_fallback:
            if not self.smtp_from:
                logger.warning("SMTP fallback enabled without a MAIL FROM address")
                return UNKNOWN
            return await asyncio.to_thread(self._probe_smtp_blocking, email)
        return UNKNOWN

    async def _is_catch_all(self, domain: str) -> bool:
        """Does this domain accept mail for an address that cannot exist?

        If yes, every pattern we try will also be accepted, so a "deliverable"
        verdict carries no information and picking the first hit would be
        guessing dressed up as verification.
        """
        verdict = await self._probe(f"{_CATCH_ALL_PROBE}@{domain}")
        return verdict == DELIVERABLE

    # ── discovery ───────────────────────────────────────────────────────────
    async def discover(
        self,
        first_name: Optional[str],
        last_name: Optional[str],
        domain: Optional[str],
    ) -> dict:
        """Generate candidates for a person and try to prove one is real.

        Returns a verdict dict shaped like ``hunter_verification``'s:
        ``{"status", "email", "pattern", "result", "email_status", "candidates",
        "meta"}``. ``status`` describes the *call* — ``ok`` when we reached a
        conclusion worth persisting, ``skipped`` when there was nothing to try.

        ``email_status`` is only ever ``'valid'`` when a probe actually confirmed
        the mailbox. An unprobed or catch-all-blocked guess comes back
        ``'unknown'`` with ``email`` still populated, because a plausible address
        is worth showing a human even when we can't stand behind it.
        """
        candidates = generate(first_name, last_name, domain)
        if not candidates:
            return self._verdict("skipped", reason="no_candidates")

        clean_domain = normalize_domain(domain) or ""
        # Free layer first: a domain with no MX can't receive mail at all, so
        # every candidate is dead and no probe is worth spending.
        mx = await asyncio.to_thread(domain_accepts_mail, clean_domain)
        if mx == "invalid":
            return self._verdict(
                "ok",
                result=UNDELIVERABLE,
                email_status="invalid",
                candidates=candidates,
                meta={"reason": "no_mx", "domain": clean_domain},
            )

        best = candidates[0]
        if not self.can_probe:
            # Generation without verification. Honest about what it is.
            return self._verdict(
                "ok",
                email=best.email,
                pattern=best.pattern,
                result=UNKNOWN,
                email_status=UNKNOWN,
                candidates=candidates,
                meta={"reason": "unverified_guess", "probed": False, "mx": mx},
            )

        if await self._is_catch_all(clean_domain):
            logger.info(
                "%s is catch-all — pattern discovery cannot identify a mailbox",
                clean_domain,
            )
            return self._verdict(
                "ok",
                email=best.email,
                pattern=best.pattern,
                result=RISKY,
                email_status=UNKNOWN,
                candidates=candidates,
                meta={"reason": "catch_all", "probed": True, "domain": clean_domain},
            )

        tried: list[str] = []
        for candidate in candidates[: max(0, self.max_probes)]:
            tried.append(candidate.pattern)
            verdict = await self._probe(candidate.email)
            if verdict == DELIVERABLE:
                logger.info(
                    "Pattern discovery found %s (%s) for %s %s",
                    candidate.email, candidate.pattern, first_name, last_name,
                )
                return self._verdict(
                    "ok",
                    email=candidate.email,
                    pattern=candidate.pattern,
                    result=DELIVERABLE,
                    email_status="valid",
                    candidates=candidates,
                    meta={"probed": True, "tried": tried},
                )

        # Nothing confirmed. Every candidate remains a guess — say so rather than
        # condemning addresses a tarpitting server merely declined to discuss.
        return self._verdict(
            "ok",
            email=best.email,
            pattern=best.pattern,
            result=UNKNOWN,
            email_status=UNKNOWN,
            candidates=candidates,
            meta={"reason": "no_confirmation", "probed": True, "tried": tried},
        )

    @staticmethod
    def _verdict(
        status: str,
        *,
        email: Optional[str] = None,
        pattern: Optional[str] = None,
        result: Optional[str] = None,
        email_status: Optional[str] = None,
        candidates: Optional[list[PatternCandidate]] = None,
        meta: Optional[dict] = None,
        reason: Optional[str] = None,
    ) -> dict:
        return {
            "status": status,
            "email": email,
            "pattern": pattern,
            "result": result,
            "email_status": email_status,
            "source": SOURCE_PATTERN,
            "candidates": [c.email for c in (candidates or [])],
            "meta": meta or {},
            "reason": reason,
        }


_generator: Optional[EmailPatternGenerator] = None


def get_pattern_generator() -> EmailPatternGenerator:
    """Process-wide generator. Mirrors ``get_hunter_service``'s shape."""
    global _generator
    if _generator is None:
        _generator = EmailPatternGenerator()
    return _generator


def reset_pattern_generator() -> None:
    """Drop the cached generator so tests can re-read settings."""
    global _generator
    _generator = None
