"""Small data-normalization helpers shared across routers, schemas, and importers.

Keeping these in one place means a company created via the API, a CSV import, an
Apollo search, and a business-card quick-add all store domains the same way — which
is what makes domain-based deduplication actually work.
"""

import re
from typing import Optional

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


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
