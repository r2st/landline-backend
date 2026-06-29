"""Classify a contact as a genuine business lead or internal/test data.

Mirrors the one-time backfill in migration ``031_contact_group`` so that
contacts created *after* the migration (e.g. via quick-add or the create API)
land in the same group the migration would have given them. A contact is
``'test'`` when its source starts with ``test``, its name contains ``test``, or
its email carries a known test/internal marker; otherwise it is ``'genuine'``.
"""

import re
from typing import Optional

GENUINE = "genuine"
TEST = "test"

# Unambiguous email substrings that mark a contact as test/internal data. Keep
# in sync with migration 032's LIKE patterns.
_TEST_EMAIL_MARKERS = ("example.com", "example.org", "fake", "suman", "noreply")

# "test" as a delimited token, so a real domain like "granitestateprecision.com"
# (which contains the substring "test") is NOT flagged, while "test@…",
# "john+test@…" or "@test.io" are. Keep in sync with migration 032's regex.
_TEST_TOKEN_RE = re.compile(r"(?:^|[^a-z0-9])test(?:[^a-z0-9]|$)")


def classify_group(
    email: Optional[str] = None,
    source: Optional[str] = None,
    name: Optional[str] = None,
) -> str:
    """Return ``'test'`` or ``'genuine'`` for a contact.

    ``name`` may be any free-form name string (e.g. "First Last").
    """
    if (source or "").strip().lower().startswith("test"):
        return TEST
    if "test" in (name or "").lower():
        return TEST
    email_l = (email or "").strip().lower()
    if email_l and (
        any(marker in email_l for marker in _TEST_EMAIL_MARKERS)
        or _TEST_TOKEN_RE.search(email_l)
    ):
        return TEST
    return GENUINE
