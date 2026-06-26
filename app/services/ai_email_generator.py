"""AI-powered email generation for personalized outreach.

Uses Anthropic Claude to generate highly personalized cold outreach emails
for manufacturing leads. Falls back to template-based rendering on failure.

Key features:
- Dynamic demo link selection based on target company's industry
- Manufacturing-specific system prompt for authentic cold email copy
- Short, conversational emails (<100 words) optimized for reply rates
- Graceful fallback to static templates
"""

import json
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

SYSTEM_PROMPT = """You are Suman from Apprend Technologies, writing outreach emails to manufacturing
companies. Apprend builds shop floor monitoring software — real-time press tracking, downtime
alerts, scrap monitoring, OEE dashboards.

YOUR VOICE:
- You sound like a real person who happens to sell software, not a marketing team
- Short sentences. Contractions. The way you'd actually talk at a trade show
- You're genuinely curious about their shop, not performing curiosity
- Warm but not fake. Professional but not stiff
- You write like someone dashing off a quick note between meetings

HARD RULES:
- Under 80 words for the body. Shorter is better
- No exclamation marks
- No buzzwords: "synergy", "leverage", "revolutionize", "cutting-edge", "game-changer",
  "streamline", "transform", "innovative", "state-of-the-art", "best-in-class"
- No dead openers: "I hope this finds you well", "I came across your company",
  "I wanted to reach out", "I noticed that"
- Lead with something specific about their company, industry, or role
- One ask at the end — a question or a demo link, not both
- Subject lines: short, lowercase, like you'd text a coworker. Not a marketing campaign
- No signature/footer in the body — that's added separately
- No subject line in the body

OUTPUT FORMAT:
Return ONLY a JSON object with two keys:
{"subject": "the subject line", "body": "the email body"}

No markdown, no explanation, no wrapper — just the raw JSON."""


# ---------------------------------------------------------------------------
# Email generation
# ---------------------------------------------------------------------------

# Template type to prompt hints mapping
TEMPLATE_HINTS: dict[str, str] = {
    "first_touch": "First email ever to this person. Open with something specific about their shop or industry — show you did 30 seconds of homework. Be curious, not salesy. End with a low-pressure question.",
    "follow_up_1": "They didn't reply to email #1. Bump the thread with 'hey, floating this back up' energy. Add ONE new thing — a relevant number, a quick insight, or the demo link if you didn't include it before. No guilt trips. Under 50 words.",
    "follow_up_2": "Second follow-up, different angle entirely. Maybe mention what a similar shop saw after switching, or a specific pain point you've heard from their industry. Very short — 3 sentences max.",
    "follow_up_3": "Final email. Breakup email — 'totally fine if the timing isn't right, just didn't want to keep pinging you.' Leave the door open. Two sentences. No drama.",
    "warm_intro": "You have context about them — maybe someone mentioned them, or you saw them at an event, or their company was in the news. Lead with that connection. Feels personal, not templated.",
    "decision_maker": "Senior person (VP/C-level/Owner). They're busy and they've seen every pitch. Be direct — what outcome do you deliver, in one sentence. No feature lists. Respect their time.",
    "referral": "You're asking them to point you to the right person at their company. Make it dead simple — 'who handles X at your shop?' One ask. Don't over-explain what you do.",
    "re_engage": "You talked a while back but it went quiet. Reference the time gap naturally ('it's been a few months'). Share something new — a feature, a case study, a relevant trend. No 'just checking in.'",
    "event_invite": "Inviting them to something practical — a live demo, a session, a quick webinar. Make it sound useful, not like a sales pitch. Mention what they'll actually learn or see.",
    "post_meeting": "You just met with them. Reference something specific from the conversation (not generic 'great meeting'). Outline the next step clearly. Keep it warm and brief.",
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
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        response_text = message.content[0].text.strip()

        # Parse JSON response
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
            "model": "claude-sonnet-4-6",
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
            f"I've been talking to a lot of {industry} shops lately and the same thing "
            f"keeps coming up — tracking what's happening on the floor in real time "
            f"without the spreadsheet juggling.\n\n"
            f"We built a tool that handles that. Easier to just show you: {demo_link}\n\n"
            f"Worth 2 minutes?"
        ),
        "demo_link": demo_link,
        "ai_generated": False,
        "template_type": template_type,
    }
