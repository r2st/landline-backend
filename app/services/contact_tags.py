"""Contact tag normalisation and set arithmetic.

Tags are typed by hand, so without normalisation the filter menu fills up with
``IMTS 2026``, ``imts 2026`` and ``imts 2026 `` as three separate entries that
each match a third of the contacts you meant. Everything here funnels through
:func:`normalize_tag`, which is the only definition of what makes two tags the
same tag.

Stored lowercase because the *display* casing of a free-form label is not worth
the ambiguity of two rows disagreeing about it, and because a JSONB containment
query has no case-insensitive form to fall back on.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from sqlalchemy import Text, cast

# Collapse internal runs of whitespace so "trade  show" == "trade show".
_WS = re.compile(r"\s+")

MAX_TAG_LENGTH = 60
MAX_TAGS_PER_CONTACT = 50


def normalize_tag(raw: Optional[str]) -> Optional[str]:
    """Canonical form of one tag, or ``None`` if it isn't a usable tag.

    Trim, collapse whitespace, lowercase, cap the length. Returns ``None`` for
    empty/whitespace input so callers can filter in one pass.
    """
    if raw is None:
        return None
    tag = _WS.sub(" ", str(raw).strip()).lower()
    if not tag:
        return None
    return tag[:MAX_TAG_LENGTH]


def normalize_tags(raw: Optional[Iterable[str]]) -> list[str]:
    """Normalise a collection, drop blanks and duplicates, keep first-seen order.

    Order is preserved rather than sorted so a contact's tag chips don't
    reshuffle every time someone adds one.
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        tag = normalize_tag(item)
        if tag is None or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= MAX_TAGS_PER_CONTACT:
            break
    return out


def apply_tag_delta(
    current: Optional[Iterable[str]],
    *,
    add: Optional[Iterable[str]] = None,
    remove: Optional[Iterable[str]] = None,
) -> list[str]:
    """Return the tag list after adding and removing.

    Removal is applied **after** addition, so a request that both adds and
    removes the same tag ends with it removed — the least surprising reading of a
    contradictory instruction, and stable no matter which order the client sent.

    Adding a tag that is already present is a no-op rather than a duplicate, and
    removing one that isn't there is not an error: both are what a user clicking
    around a bulk-select UI will do routinely.
    """
    result = normalize_tags(current)
    for tag in normalize_tags(add):
        if tag not in result and len(result) < MAX_TAGS_PER_CONTACT:
            result.append(tag)
    removals = set(normalize_tags(remove))
    if removals:
        result = [t for t in result if t not in removals]
    return result


def tag_clause(column, tag: str, dialect_name: str = ""):
    """A SQL predicate for "this contact carries ``tag``".

    On PostgreSQL this is JSONB containment, which the GIN index on
    ``contacts.tags`` can serve. Everywhere else (SQLite, in tests) it degrades
    to a substring match on the serialised array.

    The substring form is exact despite looking loose: tags are normalised, so
    the pattern includes the surrounding JSON quotes and ``"imts"`` therefore
    cannot match ``"imts 2026"``. It is only a fallback because it can't use an
    index, not because it's approximate.
    """
    if dialect_name == "postgresql":
        return column.contains([tag])
    return cast(column, Text).like(f'%"{tag}"%')
