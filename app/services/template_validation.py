"""Validate an email template before it can ship broken mail.

The failure this exists to stop is not hypothetical and not cosmetic. Merge
fields render through Jinja with its default undefined behaviour, which is to
substitute an **empty string** — so ``{{firstname}}`` (the real field is
``first_name``) does not raise, does not warn, and does not appear in any log.
It renders as nothing, the mail goes out reading ``Hi ,``, and the first symptom
is a reply rate that is quietly a fraction of what it should be. Dialtone has
already shipped a whole campaign that way once.

The merge context is a **closed set** — ``email_compose.build_merge_context``
returns a fixed dict of keys and nothing downstream adds to it — so an unknown
field is never a caller doing something clever. It is always a typo, and it is
always going to render empty. That is why an unknown field is an error here
rather than a warning: the template is not ambiguous, it is wrong, and the only
place the mistake is cheap to catch is before it is saved.

Three classes of problem, deliberately separated:

* **Syntax errors** — an unclosed ``{{`` or a malformed tag. Today these raise
  at *send* time, inside the dispatcher, on a real enrollment. That is the worst
  possible moment: the enrollment takes a send failure for what is really an
  authoring mistake made days earlier by someone who is no longer looking.
* **Unknown fields** — the silent-empty case above.
* **Warnings** — things that are legal, renderable, and usually still a mistake:
  a subject with no personalization at all, an unsubscribe token missing from a
  body, a merge field left inside a URL where an empty render breaks the link.
  These do not block a save, because each has a legitimate exception.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from jinja2 import TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment

from app.services.email_compose import SAMPLE_CONTEXT

# The fields a template may reference, taken from the sample context rather than
# hard-coded, so this cannot drift out of step with what actually gets rendered.
# Underscore-prefixed keys are internal plumbing (``_first_name_raw`` carries
# whether the greeting fallback applied) and are not offered to authors.
VALID_MERGE_FIELDS: frozenset[str] = frozenset(
    key for key in SAMPLE_CONTEXT if not key.startswith("_")
)

# ``{{ field }}``, ``{{field|filter}}``, ``{{ field.attr }}`` — the leading
# identifier is what has to be a known field. Filters and attribute access are
# left alone; they are Jinja's business, and a bad one is a syntax error caught
# by the compile below.
_FIELD_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)")

# A merge field sitting inside an href. An empty render here does not read as a
# missing word, it produces a broken or wrong link.
_FIELD_IN_URL_RE = re.compile(r"https?://[^\s\"'<>]*\{\{", re.IGNORECASE)

# Tokens the unsubscribe footer is injected with, across the send paths.
_UNSUBSCRIBE_MARKERS = ("unsubscribe", "{{unsubscribe", "opt out", "opt-out")


def referenced_fields(template_str: Optional[str]) -> set[str]:
    """Every merge field named in a template string."""
    return set(_FIELD_RE.findall(template_str or ""))


def _syntax_error(template_str: Optional[str], where: str) -> Optional[str]:
    """Compile ``template_str`` and describe the syntax error, if any.

    Compiles rather than renders: a template can be syntactically fine and still
    raise while rendering (an undefined attribute on a real value), and this
    should not report an authoring error for a data-shaped one.
    """
    env = SandboxedEnvironment()
    try:
        env.parse(template_str or "")
    except TemplateSyntaxError as exc:
        line = f" (line {exc.lineno})" if getattr(exc, "lineno", None) else ""
        return f"{where}: {exc.message}{line}"
    return None


def validate_template(
    subject: Optional[str],
    body: Optional[str],
    *,
    extra_fields: Iterable[str] = (),
) -> dict:
    """Check a template's subject and body. Returns errors, warnings and fields.

    ``extra_fields`` lets a caller with a genuinely wider render context (a
    sequence step that merges research-brief facts, say) declare it rather than
    have this reject fields it cannot see.

    Never raises: the caller decides whether a given class of problem is worth a
    400, and a validator that throws cannot report the second error.
    """
    allowed = VALID_MERGE_FIELDS | {f for f in extra_fields}
    errors: list[str] = []
    warnings: list[str] = []

    for text, where in ((subject, "subject"), (body, "body")):
        message = _syntax_error(text, where)
        if message:
            errors.append(message)

    # Unknown fields are only meaningful once both parts compile — a template
    # that fails to parse produces garbage field names.
    used: set[str] = set()
    if not errors:
        used = referenced_fields(subject) | referenced_fields(body)
        unknown = sorted(used - allowed)
        if unknown:
            errors.append(
                "Unknown merge field(s): "
                + ", ".join(f"{{{{{f}}}}}" for f in unknown)
                + ". These render as empty text in real mail. Valid fields: "
                + ", ".join(sorted(allowed))
            )

    if not (subject or "").strip():
        errors.append("subject: must not be empty")
    if not (body or "").strip():
        errors.append("body: must not be empty")

    # --- Warnings: legal, renderable, usually still a mistake ---------------
    if _FIELD_IN_URL_RE.search(body or "") or _FIELD_IN_URL_RE.search(subject or ""):
        warnings.append(
            "A merge field appears inside a URL — if it renders empty the link "
            "breaks rather than reading as a missing word."
        )
    if used and not (referenced_fields(subject) | referenced_fields(body)) & {
        "first_name", "full_name", "company", "company_name"
    }:
        warnings.append(
            "No name or company field anywhere — this will send identical copy "
            "to every recipient."
        )
    if not used:
        warnings.append(
            "No merge fields at all — this is a broadcast, not outreach."
        )
    lowered = (body or "").lower()
    if not any(marker in lowered for marker in _UNSUBSCRIBE_MARKERS):
        warnings.append(
            "No unsubscribe wording in the body. The send paths append a footer, "
            "so this is not a compliance failure on its own — but a template that "
            "does not place it explicitly cannot control where it lands."
        )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "fields_used": sorted(used),
        "valid_fields": sorted(allowed),
    }
