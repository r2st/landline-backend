"""AI-powered investor outreach email generation.

Where ``ai_email_generator`` writes cold *sales* emails to manufacturers, this
service writes cold *fundraising* emails to investors — pitching Apprend
Technologies as an investment. It tailors the note to the investor's thesis
(their focus sectors, the stage they lead, and their typical cheque size) so the
opening lands as "you invest in exactly this" rather than a spray-and-pray blast.

Like the rest of the platform's AI, it never fails a request: with no
``ANTHROPIC_API_KEY`` configured, or on any API error/timeout, it falls back to a
concise template-rendered email with ``source="template"`` and a human-readable
``note``. All calls are bounded by a timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional


from app.config import get_settings
from app.models.investor import Investor
from app.services.llm_providers import any_llm_available, chain_client

logger = logging.getLogger(__name__)

AI_TIMEOUT_SECONDS = 30
_MODEL = "claude-sonnet-4-6"

# What Apprend is raising for — the fixed company context every pitch draws on.
COMPANY_CONTEXT = """Apprend Technologies builds an AI quality-management platform for
manufacturers. It watches machine, press and line signals in real time and flags
quality drift before parts go out of spec — cutting scrap, rework and warranty
cost. It sells to injection molders, CNC and metal-stamping shops, and food &
beverage plants across the US and Canada, with a free 48-hour pilot as the wedge.
The market is large and underserved: mid-market manufacturers still run quality
on spreadsheets and tribal knowledge. Revenue is recurring (per-line SaaS)."""

SYSTEM_PROMPT = """You are Suman, founder of Apprend Technologies, writing a cold outreach
email to a prospective investor to open a fundraising conversation.

YOUR VOICE:
- A founder who respects the investor's time and knows their thesis
- Short sentences, plain English, specific numbers over adjectives
- Confident but not hyped — you're inviting a conversation, not closing a round in one email
- No fake urgency, no flattery-as-filler

WHAT MAKES IT LAND:
- Open by connecting THIS investor's stated focus/stage to what Apprend is. Show you
  picked them on purpose, not off a list.
- One or two concrete signals of traction or why-now (market, wedge, recurring revenue).
  Frame any metric honestly — never invent specific numbers, named customers, or results.
- One clear ask: a short intro call. Offer to send the deck.

HARD RULES:
- Body 80–160 words. Never over 200.
- No exclamation marks, no ALL-CAPS, no spam-trigger words.
- No buzzwords: "synergy", "leverage", "revolutionize", "cutting-edge", "game-changer",
  "disrupt", "10x", "unicorn", "best-in-class".
- No dead openers: "I hope this finds you well", "I wanted to reach out", "I came across".
- One ask at the end. No signature/footer in the body — that's added separately.
- Do NOT fabricate traction, customers, ARR, or valuation figures. If you cite a range,
  frame it as an estimate the investor can probe.

OUTPUT FORMAT:
Return ONLY a JSON object with two keys:
{"subject": "the subject line", "body": "the email body"}
No markdown, no explanation, no wrapper — just the raw JSON."""


def _format_check_size(min_paise: Optional[int], max_paise: Optional[int]) -> str:
    """Render a paise cheque range as a human ₹ string (₹1 = 100 paise)."""

    def _rupees(paise: int) -> str:
        rupees = paise / 100
        if rupees >= 1e7:
            return f"₹{rupees / 1e7:.2f} Cr"
        if rupees >= 1e5:
            return f"₹{rupees / 1e5:.2f} L"
        return f"₹{rupees:,.0f}"

    if min_paise is None and max_paise is None:
        return "unspecified"
    if min_paise is not None and max_paise is not None:
        return f"{_rupees(min_paise)} – {_rupees(max_paise)}"
    only = min_paise if min_paise is not None else max_paise
    return _rupees(only)  # type: ignore[arg-type]


def _build_user_prompt(investor: Investor, custom_instructions: Optional[str]) -> str:
    focus = ", ".join(investor.investment_focus or []) or "not specified"
    stage = investor.investment_stage or "not specified"
    cheque = _format_check_size(
        investor.typical_check_size_min, investor.typical_check_size_max
    )
    firm_line = f" at {investor.firm}" if investor.firm else ""

    prompt = f"""Write a cold fundraising outreach email to this investor.

INVESTOR:
- Name: {investor.name}{firm_line}
- Focus sectors: {focus}
- Stage they invest at: {stage}
- Typical cheque size: {cheque}
- Location: {investor.location or 'unknown'}

COMPANY WE'RE RAISING FOR:
{COMPANY_CONTEXT}

Connect the investor's focus/stage to Apprend specifically. Ask for a short intro
call and offer to send the deck.
"""
    if custom_instructions:
        prompt += f"\nADDITIONAL INSTRUCTIONS: {custom_instructions}\n"
    return prompt


def _fallback(investor: Investor) -> dict:
    """A concise, non-AI template email when generation is unavailable."""
    first_name = (investor.name or "there").split()[0]
    focus = (investor.investment_focus or ["B2B software"])[0]
    subject = f"apprend — real-time quality for manufacturers ({focus})"
    body = (
        f"Hi {first_name},\n\n"
        f"I'm building Apprend Technologies — an AI quality platform that catches "
        f"manufacturing defects in real time, before bad parts ship. Given your focus "
        f"on {focus}, I thought it might be a fit.\n\n"
        f"Mid-market shops still run quality on spreadsheets. We land with a free "
        f"48-hour pilot and convert to per-line recurring revenue.\n\n"
        f"Open to a 20-minute intro call? Happy to send the deck first."
    )
    return {"subject": subject, "body": body}


def _available() -> bool:
    return any_llm_available()


async def _generate_json(prompt: str) -> dict:
    """Single-shot prompt → parsed JSON. Returns ``{"error": ...}`` on failure."""
    try:
        client = chain_client(get_settings().anthropic_api_key)
        message = await asyncio.wait_for(
            client.messages.create(
                model=_MODEL,
                max_tokens=600,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=AI_TIMEOUT_SECONDS,
        )
        text = message.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
    except asyncio.TimeoutError:
        logger.error("Investor outreach generation timed out after %ss", AI_TIMEOUT_SECONDS)
        return {"error": "timeout"}
    except Exception as e:  # noqa: BLE001 - never raise into the request
        logger.error("Investor outreach generation failed: %s", e)
        return {"error": str(e)}


async def generate_outreach(
    investor: Investor,
    *,
    custom_instructions: Optional[str] = None,
) -> dict:
    """Generate a personalised investor outreach email.

    Always returns a dict with ``subject``, ``body``, ``source`` ("ai" or
    "template"), ``ai_available``, ``note``, and ``generated_at``.
    """
    now = datetime.now(timezone.utc)

    if not _available():
        draft = _fallback(investor)
        return {
            **draft,
            "source": "template",
            "ai_available": False,
            "note": "AI generation unavailable (no API key) — showing template draft.",
            "generated_at": now,
        }

    prompt = _build_user_prompt(investor, custom_instructions)
    result = await _generate_json(prompt)

    if not isinstance(result, dict) or result.get("error") or not result.get("body"):
        note = "AI generation failed — showing template draft."
        if isinstance(result, dict) and result.get("error") == "timeout":
            note = "AI generation timed out — showing template draft."
        draft = _fallback(investor)
        return {
            **draft,
            "source": "template",
            "ai_available": True,
            "note": note,
            "generated_at": now,
        }

    fallback = _fallback(investor)
    return {
        "subject": str(result.get("subject") or fallback["subject"]),
        "body": str(result.get("body") or fallback["body"]),
        "source": "ai",
        "ai_available": True,
        "note": None,
        "generated_at": now,
    }
