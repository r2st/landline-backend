"""Helpers for rendering email templates and building merge context.

Merge fields supported in template subject/body:
    {{first_name}}      — contact first name
    {{company}}         — company name
    {{industry_segment}}— company industry segment
    {{city}}            — company city
    {{contact_title}}   — contact job title
    {{demo_link}}       — dynamically resolved demo link based on industry

A few convenience aliases are also exposed: {{last_name}}, {{full_name}},
{{company_name}}, {{title}}, {{industry}}.
"""

import re

from jinja2.sandbox import SandboxedEnvironment

from app.services.ai_email_generator import resolve_demo_link, DEFAULT_DEMO_LINK

# Placeholder values used when previewing a template with no real contact.
SAMPLE_CONTEXT = {
    # Mirrors build_merge_context so ``subject_context`` treats the sample as a
    # named contact and previews keep showing the name.
    "_first_name_raw": "Jordan",
    "first_name": "Jordan",
    "last_name": "Avery",
    "full_name": "Jordan Avery",
    "company": "Acme Precision Manufacturing",
    "company_name": "Acme Precision Manufacturing",
    "industry_segment": "CNC Machining",
    "industry": "CNC Machining",
    "city": "Cleveland",
    "contact_title": "VP of Operations",
    "title": "VP of Operations",
    "demo_link": DEFAULT_DEMO_LINK,
}

# Keep backward compat
DEMO_LINK = DEFAULT_DEMO_LINK


def build_merge_context(contact, company) -> dict:
    """Build the Jinja render context from a Contact (+ its Company)."""
    raw_first = (getattr(contact, "first_name", None) or "").strip() if contact else ""
    last_name = (getattr(contact, "last_name", None) or "") if contact else ""
    title = (getattr(contact, "title", None) or "") if contact else ""
    # ``full_name`` uses the *real* names only — never the "there" fallback — so a
    # nameless contact renders as their last name (or empty), not "there Avery".
    full_name = " ".join(p for p in (raw_first, last_name) if p)

    company_name = (getattr(company, "name", None) or "") if company else ""
    industry = (getattr(company, "industry_segment", None) or "") if company else ""
    city = (getattr(company, "city", None) or "") if company else ""

    # Dynamically resolve demo link based on industry/company
    demo_link = resolve_demo_link(industry, company_name)

    # A missing/blank first name must render "Hi there," not "Hi ,". The greeting
    # field defaults to a friendly generic; every other field keeps its real (or
    # empty) value.
    return {
        # The raw value, kept for callers that need to know whether the greeting
        # fallback was applied (see ``subject_context``). Underscore-prefixed so
        # it can never collide with a real merge field.
        "_first_name_raw": raw_first,
        "first_name": raw_first or "there",
        "last_name": last_name,
        "full_name": full_name,
        "company": company_name,
        "company_name": company_name,
        "industry_segment": industry,
        "industry": industry,
        "city": city,
        "contact_title": title,
        "title": title,
        "demo_link": demo_link,
    }


def subject_context(context: dict) -> dict:
    """A merge context suited to rendering a **subject line**.

    The ``"there"`` greeting fallback is right for a body — ``"Hi there,"`` reads
    like a human wrote it. It is wrong for a subject: templates lead with the
    name (``"{{first_name}}, who eats it when a part runs out of tol?"``), and
    the fallback turns that into ``"there, who eats it when a part runs out of
    tol?"``, which reads like nobody proof-read it.

    So for subjects the field renders **empty** and :func:`clean_subject` strips
    the punctuation it leaves behind, giving the clean ``"who eats it when a
    part runs out of tol?"``. A contact with a real first name is unaffected.
    """
    if not context.get("_first_name_raw"):
        return {**context, "first_name": ""}
    return context


def render(template_str: str, context: dict) -> str:
    """Render a template string with the given context.

    Uses a SandboxedEnvironment to prevent server-side template injection.
    Undefined merge fields render as empty strings (Jinja2 default).
    """
    env = SandboxedEnvironment()
    template = env.from_string(template_str or "")
    return template.render(**context)


# Leading punctuation an empty merge field leaves behind at the start of a
# subject, e.g. "{{first_name}}, who eats it?" with no first name → ", who eats
# it?". A comma/semicolon/colon/dash plus any surrounding whitespace.
_LEADING_MERGE_ARTIFACT_RE = re.compile(r"^[\s,;:–—-]+")
_COLLAPSE_WS_RE = re.compile(r"[ \t]{2,}")


def clean_subject(subject: str) -> str:
    """Tidy a rendered subject line after merge fields have been substituted.

    A subject template that opens with a merge field (``{{first_name}}, who eats
    it?``) renders as ``, who eats it?`` when that field is empty. Strip the
    dangling leading punctuation/whitespace so the subject never starts with a
    comma, and collapse the double space an emptied mid-line field can leave.

    Defensive and idempotent: a subject with no artifact is returned unchanged
    (aside from trimming), so it is safe to call on every rendered subject.
    """
    if not subject:
        return subject or ""
    cleaned = _LEADING_MERGE_ARTIFACT_RE.sub("", subject)
    cleaned = _COLLAPSE_WS_RE.sub(" ", cleaned)
    return cleaned.strip()


# A greeting left dangling by an empty {{first_name}}: "Hi ,", "Hello  ,",
# "Hey ,"  — optionally wrapped in the tags the HTML body uses ("<p>Hi ,</p>").
#
# Anchored to the start of the body, a line, or an HTML tag boundary, because a
# greeting only *is* a greeting in that position. Matching the bare word
# anywhere would rewrite ordinary prose — "We said hi, then left." must survive
# untouched. The greeting word is captured so its casing and choice are kept.
_EMPTY_GREETING_RE = re.compile(
    r"(^|>|\n|\r)([ \t]*)(Hi|Hello|Hey|Dear)[ \t]*,",
    re.IGNORECASE,
)

# " ," / " ;" left mid-sentence by an emptied merge field.
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+([,;:])")


def clean_body(body: str, fallback_name: str = "there") -> str:
    """Repair merge artifacts left in a rendered email **body**.

    :func:`clean_subject` only ever fixed subject lines, so a body whose
    ``{{first_name}}`` rendered empty still went out opening with a literal
    ``"Hi ,"`` — which is the single most obvious "this is broken automated
    mail" tell a cold email can carry, and it led every first-touch send to a
    contact with no first name on record.

    ``build_merge_context`` now defaults the greeting field to ``"there"``, so
    this is the belt-and-braces layer: it repairs bodies rendered from a context
    built elsewhere (an older cached draft, a caller that assembles its own
    context, a template with a differently-named field) rather than trusting
    every producer to have got the fallback right.

    Two safe, targeted repairs — nothing that could mangle legitimate copy:

    * ``"Hi ,"`` → ``"Hi there,"`` (greeting word and casing preserved)
    * a space stranded before a comma/semicolon is closed up

    Idempotent: a body with no artifact comes back unchanged.
    """
    if not body:
        return body or ""
    cleaned = _EMPTY_GREETING_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)} {fallback_name},", body
    )
    cleaned = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", cleaned)
    return cleaned
