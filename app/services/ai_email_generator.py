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
    # Molding & plastics → Best Molding dashboard
    "mold": "https://best-molding-demo.pages.dev",
    "molding": "https://best-molding-demo.pages.dev",
    "injection": "https://best-molding-demo.pages.dev",
    "plastic": "https://best-molding-demo.pages.dev",
    "polymer": "https://best-molding-demo.pages.dev",
    "resin": "https://best-molding-demo.pages.dev",
    # CNC & machining → CNC Process Optimizer + TrueQuote
    "cnc": "https://cnc-process-optimizer.pages.dev",
    "machin": "https://cnc-process-optimizer.pages.dev",
    "precision part": "https://cnc-process-optimizer.pages.dev",
    "turning": "https://cnc-process-optimizer.pages.dev",
    "milling": "https://cnc-process-optimizer.pages.dev",
    "swiss machin": "https://cnc-process-optimizer.pages.dev",
    # Metal stamping → StampRight AI + Stamping Defect Predictor
    "stamping": "https://stamping-defect-predictor.pages.dev",
    "stamp": "https://stampright-ai.pages.dev",
    # Fabrication & metalwork → NestFlow (sheet metal)
    "fabricat": "https://nestflow-bvf.pages.dev",
    "sheet metal": "https://nestflow-bvf.pages.dev",
    "metal fabri": "https://nestflow-bvf.pages.dev",
    "steel": "https://nestflow-bvf.pages.dev",
    # Welding & automotive → Weld & Paint Quality
    "weld": "https://automotive-quality-assistant.pages.dev",
    "automotive": "https://automotive-quality-assistant.pages.dev",
    "auto part": "https://automotive-quality-assistant.pages.dev",
    # Aerospace
    "aerospace": "https://compliance-traceability-checker.pages.dev",
    "aircraft": "https://compliance-traceability-checker.pages.dev",
    "defense": "https://compliance-traceability-checker.pages.dev",
    # Electronics & PCB → PCB Quality Analyzer
    "electron": "https://pcb-quality-analyzer.pages.dev",
    "pcb": "https://pcb-quality-analyzer.pages.dev",
    "smt": "https://pcb-quality-analyzer.pages.dev",
    "circuit": "https://pcb-quality-analyzer.pages.dev",
    # Packaging → Packaging Line Optimizer + PackForm AI
    "packag": "https://packaging-line-optimizer.pages.dev",
    "packform": "https://packform-ai.pages.dev",
    # Food & beverage → Food Safety Monitor
    "food": "https://food-safety-monitor.pages.dev",
    "dairy": "https://food-safety-monitor.pages.dev",
    "beverage": "https://food-safety-monitor.pages.dev",
    # Pharmaceutical & chemical → Pharma Batch Predictor / BatchWise
    "pharma": "https://pharma-batch-predictor.pages.dev",
    "chemical": "https://chemical-process-optimizer.pages.dev",
    "reactor": "https://chemical-process-optimizer.pages.dev",
    "batch": "https://batchwise-4qn.pages.dev",
    # Textile → FiberVision AI
    "textile": "https://textile-quality-optimizer.pages.dev",
    "weaving": "https://textile-quality-optimizer.pages.dev",
    "dyehouse": "https://textile-quality-optimizer.pages.dev",
    "fiber": "https://textile-quality-optimizer.pages.dev",
    # Tool & die → Smart Quote Generator
    "tool & die": "https://smart-quote-generator.pages.dev",
    "tool and die": "https://smart-quote-generator.pages.dev",
    "tooling": "https://smart-quote-generator.pages.dev",
    # Forging
    "forg": "https://equipment-health-predictor.pages.dev",
    # Medical devices → Compliance Traceability
    "medical": "https://compliance-traceability-checker.pages.dev",
    # Marine / petrochemical → Equipment Health
    "marine": "https://equipment-health-predictor.pages.dev",
    "petrochem": "https://equipment-health-predictor.pages.dev",
    # Solar / energy → Manufacturing Energy Optimizer
    "solar": "https://manufacturing-energy-optimizer.pages.dev",
    "energy": "https://manufacturing-energy-optimizer.pages.dev",
    # Safety / EHS → SafetyNet AI
    "safety": "https://workplace-safety-analyzer.pages.dev",
    "ehs": "https://workplace-safety-analyzer.pages.dev",
    # Supply chain
    "supply chain": "https://supply-chain-risk-monitor.pages.dev",
    "supplier": "https://supply-chain-risk-monitor.pages.dev",
    # Inventory
    "inventory": "https://inventory-intelligence-demo.pages.dev",
    # Root cause (cross-industry)
    "root cause": "https://ai-root-cause-investigator.pages.dev",
    "quality": "https://ai-root-cause-investigator.pages.dev",
    # General manufacturing fallback
    "manufactur": "https://smart-production-scheduler.pages.dev",
    "production": "https://smart-production-scheduler.pages.dev",
    "industrial": "https://equipment-health-predictor.pages.dev",
}

DEFAULT_DEMO_LINK = "https://best-molding-demo.pages.dev"


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

EMOTIONAL TARGETING (this is what makes the email land):
- Emotion comes from SPECIFICITY, never from hype. Name the real pain by its real
  name. Make the reader feel seen, then offer relief.
- Use loss aversion honestly: the cost of doing nothing (scrap bins, the 2 AM call,
  the angry customer, the air-freight bill, the corrective-action report with their
  name on it) is real — let them feel it before you offer the fix.
- For plastic injection molders specifically, the nerves to touch are: short shots,
  flash, sink marks, warpage, core-pin and check-ring wear, cavity-to-cavity
  variation, the retiring 22-year operator who reads a press by feel, PPAP/quality
  rejections, and scrap-and-rework eating margin.
- Aim for: they recognize themselves in line one, feel the weight by the middle,
  and feel a way out by the end.
- Emotion is EARNED, not performed. Naming their problem accurately IS the empathy —
  you don't also need to announce that you understand ("I know how frustrating that
  must be", "I can only imagine"). Telling someone you get it is what people do when
  they don't. Show it by being right about their day instead.
- The strongest honest move you have is a question only someone who knows the job
  would think to ask. "Who notices first when a cavity starts drifting — the
  operator or the CMM?" earns more than any claim, because it can't be faked and it
  costs them nothing to answer.
- Write to ONE person, not a segment. If a sentence would read identically to 500
  other shops, it is filler — cut it or make it theirs.

TRUTHFULNESS (non-negotiable):
- Never invent a named customer, a quote, or a specific result presented as fact.
  No "a shop in Michigan cut scrap to 0.9%." That is a fake testimonial and it is
  not allowed.
- You MAY cite industry-typical ranges if you frame them as estimates the reader
  can check ("for a shop your size the math usually lands around…", "run your own
  numbers"). You MAY describe the pattern you hear across the industry. You may not
  fabricate proof.
- Attribution is not a loophole. "A client of ours saw…", "one shop told us…",
  "we've seen shops cut…" are the same fabrication with a hedge in front. If you
  did not read it in the research brief below, you do not know it.
- THIS RULE OUTRANKS EVERY OTHER INSTRUCTION YOU ARE GIVEN. If the email-type hint
  or the additional instructions ask for a customer story, a case study, or a
  specific result, they are asking for something you do not have. Write the honest
  version — the pattern across the industry, framed as a pattern — and never
  invent the proof to satisfy a hint.

TONE:
- The email type below carries a TONE. It bends the voice above — it never
  replaces it, and it never loosens the truthfulness or deliverability rules.
  You are the same person in every tone; you're just reading the room.

HARD RULES (deliverability):
- Body 60–160 words. Shorter is usually better; never over 200.
- No exclamation marks. No ALL-CAPS words. No spam triggers ("act now", "free money",
  "limited time", "guarantee", "click here", "winner", "urgent").
- The word "free" is fine ONLY in "free pilot" / "free audit" / "free assessment".
- No buzzwords: "synergy", "leverage", "revolutionize", "cutting-edge", "game-changer",
  "streamline", "transform", "innovative", "state-of-the-art", "best-in-class".
- No dead openers: "I hope this finds you well", "I came across your company",
  "I wanted to reach out", "I noticed that".
- No images, no raw URLs in body text — wrap any link in an <a> tag.
- Lead with something specific about their company, industry, or role.
- One ask at the end — a question, or the demo link, or the free-pilot reply CTA.
- Subject lines: short (under 60 chars), lowercase, like you'd text a coworker.
- No signature/footer in the body — that's added separately. No subject line in the body.

OUTPUT FORMAT:
Return ONLY a JSON object with two keys:
{"subject": "the subject line", "body": "the email body"}

No markdown, no explanation, no wrapper — just the raw JSON."""


# ---------------------------------------------------------------------------
# Tone
# ---------------------------------------------------------------------------
# Prompt guidance for each tone in ``app.models.sequence.SEQUENCE_TONES``. The
# model owns the storage vocabulary; this dict owns what each word means to the
# copywriter. ``test_email_tone`` pins the two together so a tone can never be
# storable but meaningless.
#
# Each entry bends the house voice. None of them may loosen the truthfulness or
# deliverability rules, and each is written to stay inside them — "urgent" is the
# one that has to work hardest at that, see below.

DEFAULT_TONE = "conversational"

TONE_GUIDANCE: dict[str, str] = {
    "conversational": (
        "TONE — conversational: the house voice. Like you'd talk at a trade show, "
        "between meetings. Contractions, short sentences, one clear thought per line. "
        "Slightly informal is right; sloppy is not."
    ),
    "friendly": (
        "TONE — friendly: warmer and lighter than the house voice. You're glad to be "
        "writing to them. Allowed to be a little wry about the parts of the job "
        "everyone hates. Stay professional — friendly means human, not chummy, and "
        "you have never met this person, so no forced familiarity ('Hey buddy', "
        "'my friend') and no comedy at their expense."
    ),
    "professional": (
        "TONE — professional: measured and precise. Full sentences, less banter, no "
        "wryness. Respect their time by making every line carry information. This is "
        "the register for someone evaluating vendors on the merits — plain, credible, "
        "specific. Professional does NOT mean corporate: the buzzword ban still "
        "applies, and stiff formality ('Please be advised', 'Pursuant to') reads as "
        "a mail-merge, which is the opposite of what you want."
    ),
    "urgent": (
        "TONE — urgent: the pressure is THEIRS and it is real — a cost compounding "
        "every day it goes unfixed, a problem that gets more expensive the longer it "
        "runs. Name the clock that is actually ticking on their floor: scrap shipping "
        "daily, the audit already scheduled, the operator who retires in March. "
        "Direct, tight, no throat-clearing. "
        "Manufactured urgency is FORBIDDEN and it is the exact thing this tone gets "
        "misread as: no fake deadlines, no scarcity ('only taking 3 shops this "
        "quarter'), no pressure on the reply itself. The spam-trigger ban still "
        "applies in full — you may not write 'act now', 'urgent', or 'limited time'. "
        "Urgency you invent is a sales tactic. Urgency you point at is a favour."
    ),
    "empathetic": (
        "TONE — empathetic: you have sat with this problem and you know what it costs "
        "the person, not just the P&L — the 2 AM call, defending the scrap number in "
        "a meeting where it isn't your fault, the operator who can read the press by "
        "feel and retires next year. Softer, slower, more room. One ask, gently made. "
        "Empathy is precision about their day, NOT announcements of feeling: never "
        "write 'I understand how hard that is' or 'I can only imagine'. Show it by "
        "being right. Do not pity them — they are good at a hard job, not a victim of "
        "it, and condescension is the failure mode of this tone."
    ),
}


def resolve_tone(tone: Optional[str]) -> str:
    """Normalize a tone to one the prompt builder has guidance for.

    Fails soft on purpose: tone is a quality lever, not a routing decision, so an
    unknown or missing value degrades to the house voice rather than raising. The
    DB CHECK constraint is what keeps junk out of storage; by the time copy is
    being written the send is already in flight and refusing to write it would
    turn a cosmetic problem into a dropped touch.
    """
    key = (tone or "").strip().lower()
    if key in TONE_GUIDANCE:
        return key
    if key:
        logger.warning("Unknown tone %r — falling back to %r", tone, DEFAULT_TONE)
    return DEFAULT_TONE


# ---------------------------------------------------------------------------
# Email generation
# ---------------------------------------------------------------------------

# Template type to prompt hints mapping
TEMPLATE_HINTS: dict[str, str] = {
    "first_touch": "First email ever to this person. Open with something specific about their shop or industry — show you did 30 seconds of homework. Be curious, not salesy. End with a low-pressure question.",
    "follow_up_1": "They didn't reply to email #1. Bump the thread with 'hey, floating this back up' energy. Add ONE new thing — a number they can run against their own shop (framed as an estimate, never as a result you got for someone), a quick insight, or the demo link if you didn't include it before. No guilt trips. Under 50 words.",
    "follow_up_2": "Second follow-up, different angle entirely. Describe the pattern you hear across their industry — the failure mode that keeps coming up in these conversations — and frame it as a pattern, not as a result someone got. No customer stories, named or anonymous. Very short — 3 sentences max.",
    "follow_up_3": "Final email. Breakup email — 'totally fine if the timing isn't right, just didn't want to keep pinging you.' Leave the door open. Two sentences. No drama.",
    "warm_intro": "Lead with a real connection — a referral, an event, company news — but ONLY if it appears in the research brief or the additional instructions below. A connection you invent is a lie you'll be caught in on the reply, so if you have no such context, write a strong first_touch instead: lead with something specific about their shop or industry and skip the connection entirely.",
    "decision_maker": "Senior person (VP/C-level/Owner). They're busy and they've seen every pitch. Be direct — what outcome do you deliver, in one sentence. No feature lists. Respect their time.",
    "referral": "You're asking them to point you to the right person at their company. Make it dead simple — 'who handles X at your shop?' One ask. Don't over-explain what you do.",
    "re_engage": "You talked a while back but it went quiet. Reference the time gap naturally ('it's been a few months'). Share something genuinely new — a feature that now exists, or a shift you're seeing across the industry. Not a case study: you have no results to report and inventing one here would be a fake testimonial. No 'just checking in.'",
    "event_invite": "Inviting them to something practical — a live demo, a session, a quick webinar. Make it sound useful, not like a sales pitch. Mention what they'll actually learn or see.",
    "post_meeting": "You just met with them. Reference something specific from the conversation (not generic 'great meeting'). Outline the next step clearly. Keep it warm and brief.",
    "pain_point": "Lead with the emotional weight of one specific failure mode on their floor — the 2 AM call, the scrap bin, the part that ships bad. Make them feel it in two sentences, then offer that our system catches the drift before the bad part. End with one low-pressure question.",
    "scrap_math": "Walk the honest cost of scrap and rework as math they can check against their own shop ('annual resin spend x scrap rate'). Frame any number as an estimate, never a fabricated result. Close by offering to walk their numbers in 15 minutes.",
    "free_pilot": "This is the breakup / final email. Offer the genuine FREE 48-hour process pilot on one press — connect sensors, run two days, hand them a report of anomalies and drift. Ask them to reply with a single word ('pilot'). Leave the door open with no pressure.",
}


async def generate_email(
    lead_data: dict,
    template_type: str = "first_touch",
    custom_instructions: Optional[str] = None,
    research_context: str = "",
    tone: Optional[str] = None,
) -> dict:
    """Generate a personalized outreach email using AI.

    Args:
        lead_data: Dict with keys like first_name, company_name, industry,
                   title, city, and any enrichment data.
        template_type: One of the TEMPLATE_HINTS keys (e.g. "first_touch").
        custom_instructions: Optional extra instructions for the AI.
        research_context: Optional pre-formatted company research brief (from
            ``research_agent.format_research_for_prompt``). When present, the AI
            grounds the email in named pain points and outreach angles (1.4).
        tone: One of ``TONE_GUIDANCE``'s keys — usually the sending sequence's
            ``tone`` column. Unknown or None degrades to the house voice; see
            ``resolve_tone``.

    Returns:
        Dict with "subject", "body", "demo_link", "tone", and "ai_generated"
        keys. On failure, returns a fallback with "ai_generated": False.
    """
    settings = get_settings()
    resolved_tone = resolve_tone(tone)

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
    tone_guidance = TONE_GUIDANCE[resolved_tone]

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

{tone_guidance}
"""

    if research_context:
        user_prompt += f"\n{research_context}\n"

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
            "tone": resolved_tone,
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
    """Return a basic template-based email when AI generation fails.

    Reports ``tone: None`` rather than the requested tone — this copy is a static
    string that no tone was applied to, and claiming otherwise would make the
    field a lie exactly where a caller is most likely to be debugging why the
    voice looks wrong.
    """
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
        "tone": None,
    }
