"""Small data-normalization helpers shared across routers, schemas, and importers.

Keeping these in one place means a company created via the API, a CSV import, an
Apollo search, and a business-card quick-add all store domains the same way — which
is what makes domain-based deduplication actually work.
"""

import re
import unicodedata
from typing import Optional

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

# Legal-entity suffixes stripped from the tail of a company name when building a
# match key. "Acme Pvt Ltd", "Acme Ltd." and "Acme" are one company.
_LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "plc",
    "corp", "corporation", "co", "company", "gmbh", "ag", "sa", "sas",
    "srl", "spa", "nv", "bv", "pty", "pvt", "private", "oy", "ab", "kk",
})
# Intra-word punctuation is *deleted*, not spaced out: "S.A." must become the
# single token "sa" so the suffix stripper recognizes it, rather than "s a".
_INTRAWORD_PUNCT_RE = re.compile(r"[.’']", re.UNICODE)
# Everything else (commas, hyphens, slashes, &) separates words, so it becomes a
# space. Word characters in any script survive — folding a non-Latin name to
# nothing would collapse unrelated companies onto one key.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+", re.UNICODE)
_NON_DIGIT_RE = re.compile(r"\D")


def normalize_domain(raw: Optional[str]) -> Optional[str]:
    """Reduce a URL or domain to a bare, lowercase registrable hostname.

    ``https://www.Acme-Corp.com/about?x=1`` -> ``acme-corp.com``. Returns None
    for blank/garbage input so a unique domain index never collides on "".
    """
    if not raw:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    # Drop scheme and any leading "//".
    value = _SCHEME_RE.sub("", value)
    value = value.lstrip("/")
    # Drop path / query / fragment / port and userinfo.
    value = value.split("/", 1)[0]
    value = value.split("?", 1)[0]
    value = value.split("#", 1)[0]
    value = value.split("@")[-1]
    value = value.split(":", 1)[0]
    # Strip a leading www.
    if value.startswith("www."):
        value = value[4:]
    value = value.strip(". ")
    # A real domain has a dot and no spaces; otherwise treat as unusable.
    if not value or " " in value or "." not in value:
        return None
    return value


def normalize_name_key(raw: Optional[str]) -> Optional[str]:
    """Reduce a company name to a canonical key used **only for matching**.

    ``Acme Corp.`` / ``ACME Corporation`` / ``Acme`` -> ``acme``. Never store
    this as a display name — it is lossy on purpose.

    Deliberately *not* unique: "Precision Manufacturing" in Ohio and in Pune are
    different companies that legitimately share a key, which is why the dedup
    rule pairs ``name_key`` with geography before it will merge anything.
    """
    if not raw:
        return None
    # Fold accents ("Nestlé" -> "nestle") without discarding non-Latin scripts.
    value = unicodedata.normalize("NFKD", str(raw))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = _INTRAWORD_PUNCT_RE.sub("", value)
    value = _PUNCT_RE.sub(" ", value)
    value = _WS_RE.sub(" ", value).strip()
    if not value:
        return None
    # Strip trailing legal-entity suffixes, repeatedly ("acme pvt ltd" -> "acme").
    # Never strip down to nothing: a company actually named "Ltd" keeps its key.
    parts = value.split(" ")
    while len(parts) > 1 and parts[-1] in _LEGAL_SUFFIXES:
        parts.pop()
    return " ".join(parts).strip() or None


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Reduce a phone number to a comparable key: digits, with a ``+`` kept.

    This is a *match* key, not a dialing or display format — it does not infer a
    country code from context, so a bare national number and its E.164 form will
    not match. Good enough to corroborate a name match, deliberately not trusted
    on its own.
    """
    if not raw:
        return None
    value = str(raw).strip()
    if not value:
        return None
    plus = value.startswith("+") or value.startswith("00")
    digits = _NON_DIGIT_RE.sub("", value)
    if value.startswith("00"):
        digits = digits[2:]
    # Too short to identify anyone; an extension or a fragment.
    if len(digits) < 7:
        return None
    return ("+" + digits) if plus else digits
