"""Building a ``LIKE``/``ILIKE`` pattern out of something a user typed.

Every search box in this codebase spelled the same line::

    Company.name.ilike(f"%{q}%")

which is safe from SQL injection — ``q`` is a bound parameter, not concatenated
into the statement — and is *not* safe from the pattern language. ``%`` and
``_`` are wildcards inside that parameter, so the user's literal text is being
read as a glob:

* **It answers the wrong question.** ``q=50%`` matches every row rather than the
  companies with "50%" in their name. ``q=a_b`` matches ``axb``. Searching the
  suppression list for ``%`` returns the whole list, which — on a screen whose
  entire job is "is this address suppressed?" — is a confirmation for an address
  nobody suppressed.
* **It is a cheap way to make the database work very hard.** A leading-wildcard
  ``ILIKE`` is already a sequential scan; a pattern like ``%a%a%a%a%a%a%a%a%z``
  makes each row's comparison backtracking rather than linear, and the cost
  multiplies by the row count. That is an authenticated request holding a worker
  and a connection for as long as the caller cares to make it, from a query
  parameter with no other way to reach the planner.

:func:`escape_like` neutralises the three pattern characters — ``%``, ``_`` and
the escape character itself — and the ``*_pattern`` helpers wrap the result in
the shape the call site wanted. Every one of them must be paired with
``escape="\\\\"`` on the SQLAlchemy side, which is what
:func:`contains_pattern` and friends are for: they return the pattern, and the
caller passes ``escape=LIKE_ESCAPE``.

Deliberately *not* handled here: trimming, lower-casing, or rejecting an empty
term. Those are per-call-site decisions (``ILIKE`` is already case-insensitive;
one caller lower-cases because its column is stored lower-cased) and folding
them in would quietly change what several existing searches match.
"""

from __future__ import annotations

# The character used to escape a wildcard. Backslash is the conventional choice
# and the one Postgres uses by default, but ``LIKE`` has *no* default escape in
# the SQL standard — it has to be named on the statement — so every call site
# passes this as ``escape=``.
LIKE_ESCAPE = "\\"


def escape_like(term: str) -> str:
    """Neutralise ``LIKE`` wildcards in *term* so it matches literally.

    The escape character is escaped first; doing it last would double-escape the
    backslashes this function had just inserted.
    """
    return (
        term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )


def contains_pattern(term: str) -> str:
    """Pattern for "the column contains *term* literally"."""
    return f"%{escape_like(term)}%"


def endswith_pattern(term: str) -> str:
    """Pattern for "the column ends with *term* literally"."""
    return f"%{escape_like(term)}"


def startswith_pattern(term: str) -> str:
    """Pattern for "the column starts with *term* literally"."""
    return f"{escape_like(term)}%"
