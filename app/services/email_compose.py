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
