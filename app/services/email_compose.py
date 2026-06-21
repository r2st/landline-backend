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
    first_name = (getattr(contact, "first_name", None) or "") if contact else ""
    last_name = (getattr(contact, "last_name", None) or "") if contact else ""
    title = (getattr(contact, "title", None) or "") if contact else ""
    full_name = " ".join(p for p in (first_name, last_name) if p)

    company_name = (getattr(company, "name", None) or "") if company else ""
    industry = (getattr(company, "industry_segment", None) or "") if company else ""
    city = (getattr(company, "city", None) or "") if company else ""

    # Dynamically resolve demo link based on industry/company
    demo_link = resolve_demo_link(industry, company_name)

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
