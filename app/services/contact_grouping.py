"""Classify a contact as a genuine business lead or internal/test data.

Mirrors the one-time backfill in migration ``031_contact_group`` so that
contacts created *after* the migration (e.g. via quick-add or the create API)
land in the same group the migration would have given them. A contact is
``'test'`` when its source starts with ``test``, its name contains ``test``, or
its email carries a known test/internal marker; otherwise it is ``'genuine'``.
"""

from typing import Optional

GENUINE = "genuine"
TEST = "test"

# Email substrings that mark a contact as test/internal data. Keep in sync with
# the LIKE patterns in migration 031.
_TEST_EMAIL_MARKERS = ("test", "example.com", "example.org", "fake", "suman", "noreply")


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
    if email_l and any(marker in email_l for marker in _TEST_EMAIL_MARKERS):
        return TEST
    return GENUINE
