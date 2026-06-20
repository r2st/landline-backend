"""Helpers for rendering email templates and building merge context.

Merge fields supported in template subject/body:
    {{first_name}}      — contact first name
    {{company}}         — company name
    {{industry_segment}}— company industry segment
    {{city}}            — company city
    {{contact_title}}   — contact job title
    {{demo_link}}       — the Apprend demo link (constant)

A few convenience aliases are also exposed: {{last_name}}, {{full_name}},
{{company_name}}, {{title}}, {{industry}}.
"""

from jinja2 import Template

# The Apprend demo link surfaced in every outreach template.
DEMO_LINK = "https://true-molding-demo.pages.dev"

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
    "demo_link": DEMO_LINK,
}


def build_merge_context(contact, company) -> dict:
    """Build the Jinja render context from a Contact (+ its Company)."""
    first_name = (getattr(contact, "first_name", None) or "") if contact else ""
    last_name = (getattr(contact, "last_name", None) or "") if contact else ""
    title = (getattr(contact, "title", None) or "") if contact else ""
    full_name = " ".join(p for p in (first_name, last_name) if p)

    company_name = (getattr(company, "name", None) or "") if company else ""
    industry = (getattr(company, "industry_segment", None) or "") if company else ""
    city = (getattr(company, "city", None) or "") if company else ""

    return {
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "company": company_name,
        "company_name": company_name,
        "industry_segment": industry,
        "industry": industry,
        "city": city,
        "contact_title": title,
        "title": title,
        "demo_link": DEMO_LINK,
    }


def render(template_str: str, context: dict) -> str:
    """Render a template string with the given context.

    Undefined merge fields render as empty strings (Jinja2 default).
    """
    return Template(template_str or "").render(**context)
