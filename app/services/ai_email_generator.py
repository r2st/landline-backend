"""AI-powered email generation for personalized outreach.

Uses Anthropic Claude to generate highly personalized cold outreach emails
for manufacturing leads. Falls back to template-based rendering on failure.

Key features:
- Dynamic demo link selection based on target company's industry
- Manufacturing-specific system prompt for authentic cold email copy
- Short, conversational emails (<100 words) optimized for reply rates
- Graceful fallback to static templates
"""

import logging
from typing import Optional

import anthropic

from app.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dynamic demo link mapping
# ---------------------------------------------------------------------------
# Maps industry keywords to specific demo URLs.
# Right now we have one demo site; this dict is ready for expansion
# as more industry-specific demos are built.
# Keys are lowercased substrings matched against the lead's industry field.

DEMO_LINKS: dict[str, str] = {
    # Molding & plastics
    "mold": "https://true-molding-demo.pages.dev",
    "molding": "https://true-molding-demo.pages.dev",
    "injection": "https://true-molding-demo.pages.dev",
    "plastic": "https://true-molding-demo.pages.dev",
    "polymer": "https://true-molding-demo.pages.dev",
    # CNC & machining
    "cnc": "https://true-molding-demo.pages.dev",
    "machin": "https://true-molding-demo.pages.dev",
    "precision": "https://true-molding-demo.pages.dev",
    "turning": "https://true-molding-demo.pages.dev",
    "milling": "https://true-molding-demo.pages.dev",
    # Fabrication & metalwork
    "fabricat": "https://true-molding-demo.pages.dev",
    "metal": "https://true-molding-demo.pages.dev",
    "weld": "https://true-molding-demo.pages.dev",
    "sheet metal": "https://true-molding-demo.pages.dev",
    "stamping": "https://true-molding-demo.pages.dev",
    # Aerospace & automotive
    "aerospace": "https://true-molding-demo.pages.dev",
    "automotive": "https://true-molding-demo.pages.dev",
    "defense": "https://true-molding-demo.pages.dev",
    # Electronics & assembly
    "electron": "https://true-molding-demo.pages.dev",
    "assembly": "https://true-molding-demo.pages.dev",
    "pcb": "https://true-molding-demo.pages.dev",
    # General manufacturing
    "manufactur": "https://true-molding-demo.pages.dev",
    "production": "https://true-molding-demo.pages.dev",
    "industrial": "https://true-molding-demo.pages.dev",
}

DEFAULT_DEMO_LINK = "https://true-molding-demo.pages.dev"


def resolve_demo_link(industry: str = "", company_name: str = "") -> str:
    """Pick the best demo link based on industry and company name.

    Checks industry first, then company name, against the DEMO_LINKS mapping.
    Returns the default demo link if no keyword matches.
    """
    search_text = f"{industry} {company_name}".lower()
    for keyword, url in DEMO_LINKS.items():
        if keyword in search_text:
            return url
    return DEFAULT_DEMO_LINK


# ---------------------------------------------------------------------------
# System prompt for AI email generation
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a B2B sales copywriter specializing in manufacturing software.
You write cold outreach emails for Apprend Technologies, which builds shop floor management
software for manufacturers (job tracking, quoting, scheduling, quality control).

RULES:
- Write like a real person texting a colleague, NOT a marketer
- Keep the email under 80 words (shorter = higher reply rates)
- Never use buzzwords like "synergy", "leverage", "revolutionize", "cutting-edge", "game-changer"
- Never use exclamation marks
- Never start with "I hope this email finds you well" or any variation
- Reference specific manufacturing pain points: quoting speed, job tracking across departments,
  on-time delivery, shop floor visibility, spreadsheet chaos, manual data entry
- Include one clear CTA (usually a question or demo link)
- Sound curious and helpful, not pushy
- Use the recipient's first name naturally
- If a demo link is provided, weave it in naturally (not as a forced CTA)
- Do not include any email signature, footer, or company info — that's added separately
- Do not include subject line in the body

OUTPUT FORMAT:
Return ONLY a JSON object with two keys:
{"subject": "the subject line", "body": "the email body"}

No markdown, no explanation, no wrapper — just the raw JSON."""


# ---------------------------------------------------------------------------
# Email generation
# ---------------------------------------------------------------------------

# Template type to prompt hints mapping
TEMPLATE_HINTS: dict[str, str] = {
    "first_touch": "This is the FIRST email to this person. Be curious, not salesy. Lead with a specific observation about their company or industry.",
    "follow_up_1": "This is a FOLLOW-UP — they haven't replied to the first email. Be brief. Offer a new angle or piece of value. Don't guilt-trip them.",
    "follow_up_2": "This is a SECOND FOLLOW-UP. Try a completely different angle — maybe share a relevant case study or stat. Keep it very short.",
    "follow_up_3": "This is the FINAL follow-up (breakup email). Be gracious. Let them know you'll stop emailing. Leave the door open.",
    "warm_intro": "This is a WARM INTRO — you have some context about them or their company. Lead with what caught your eye.",
    "decision_maker": "This person is a senior decision maker (VP/C-level). Be concise and respect their time. Focus on business outcomes, not features.",
    "referral": "You're asking this person to REFER you to the right contact at their company. Be polite and make it easy for them.",
    "re_engage": "You've spoken before but it's been a while. Reference the passage of time naturally. Share something new.",
    "event_invite": "You're inviting them to a live session/webinar. Make it sound valuable, not salesy. Mention it's practical, not a pitch.",
    "post_meeting": "You just had a meeting with them. Thank them, reference something specific from the conversation, and outline next steps.",
}


async def generate_email(
    lead_data: dict,
    template_type: str = "first_touch",
    custom_instructions: Optional[str] = None,
) -> dict:
    """Generate a personalized outreach email using AI.

    Args:
        lead_data: Dict with keys like first_name, company_name, industry,
                   title, city, and any enrichment data.
        template_type: One of the TEMPLATE_HINTS keys (e.g. "first_touch").
        custom_instructions: Optional extra instructions for the AI.

    Returns:
        Dict with "subject", "body", "demo_link", and "ai_generated" keys.
        On failure, returns a fallback with "ai_generated": False.
    """
    settings = get_settings()

    if not settings.anthropic_api_key:
        logger.warning("No Anthropic API key — falling back to template")
        return _fallback_response(lead_data, template_type)

    # Resolve the best demo link for this lead
    industry = lead_data.get("industry", "") or lead_data.get("industry_segment", "") or ""
    company_name = lead_data.get("company_name", "") or lead_data.get("company", "") or ""
    demo_link = resolve_demo_link(industry, company_name)

    # Build the user prompt
    first_name = lead_data.get("first_name", "there")
    title = lead_data.get("title", "") or lead_data.get("contact_title", "")
    city = lead_data.get("city", "")

    hint = TEMPLATE_HINTS.get(template_type, TEMPLATE_HINTS["first_touch"])

    user_prompt = f"""Write a cold outreach email with these details:

RECIPIENT:
- Name: {first_name}
- Company: {company_name}
- Industry: {industry}
- Title: {title}
- City: {city}

DEMO LINK TO INCLUDE (if appropriate for this email type): {demo_link}

EMAIL TYPE: {template_type}
{hint}
"""

    if custom_instructions:
        user_prompt += f"\nADDITIONAL INSTRUCTIONS: {custom_instructions}\n"

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        response_text = message.content[0].text.strip()

        # Parse JSON response
        import json
        # Handle potential markdown wrapping
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1]
            response_text = response_text.rsplit("```", 1)[0].strip()

        result = json.loads(response_text)

        return {
            "subject": result.get("subject", f"Quick thought for {company_name}"),
            "body": result.get("body", ""),
            "demo_link": demo_link,
            "ai_generated": True,
            "model": "claude-sonnet-4-20250514",
            "template_type": template_type,
        }

    except json.JSONDecodeError as e:
        logger.error("Failed to parse AI response as JSON: %s — raw: %s", e, response_text[:200])
        return _fallback_response(lead_data, template_type)
    except anthropic.APIError as e:
        logger.error("Anthropic API error generating email: %s", e)
        return _fallback_response(lead_data, template_type)
    except Exception as e:
        logger.error("Unexpected error generating AI email: %s", e)
        return _fallback_response(lead_data, template_type)


def _fallback_response(lead_data: dict, template_type: str) -> dict:
    """Return a basic template-based email when AI generation fails."""
    first_name = lead_data.get("first_name", "there")
    company_name = lead_data.get("company_name", "") or lead_data.get("company", "")
    industry = lead_data.get("industry", "") or lead_data.get("industry_segment", "")
    demo_link = resolve_demo_link(industry, company_name)

    return {
        "subject": f"{first_name}, quick question about {company_name}",
        "body": (
            f"Hi {first_name},\n\n"
            f"I work with {industry} manufacturers on a problem that comes up a lot "
            f"— jobs slip through the cracks because quoting, scheduling, and shop floor "
            f"tracking live in different places.\n\n"
            f"We built something that pulls it together. Here's a quick demo: {demo_link}\n\n"
            f"Worth a look?"
        ),
        "demo_link": demo_link,
        "ai_generated": False,
        "template_type": template_type,
    }
