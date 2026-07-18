"""Referral & Warm-Intro agent service.

The brains of the referral agent, kept transport-thin so it is unit-testable
without Celery or FastAPI:

* **Candidate finding** — :func:`find_referral_candidates` surfaces happy,
  long-tenured clients we haven't over-asked, ranked by health.
* **AI drafting** — :func:`generate_referral_ask` / :func:`generate_followup` /
  :func:`generate_thankyou` craft personalized copy. Every AI call routes through
  :func:`_agent_complete_json`, which sends the request to **OpenRouter's
  OpenAI-compatible chat/completions endpoint** using *the agent's own*
  ``llm_model`` and ``llm_api_key`` — falling back to the org-level
  ``openrouter_api_key`` (system_settings) and finally the global config key.
* **Chain tracking** — :func:`track_referral_chain` links a referred contact
  back to the referrer once they enter the CRM.

All AI helpers degrade gracefully: on any transport/parse failure they fall back
to the org's templates (or a sensible default) so the agent never hard-fails a
scan because a free-tier model rate-limited.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.referral import (
    DEFAULT_LLM_MODEL,
    Referral,
    ReferralAgent,
)
from app.services import system_settings
from app.services.crypto import TokenCryptoError, decrypt
from app.services.health import compute_company_health

logger = logging.getLogger(__name__)

# Company pipeline stage that marks an active paying client.
_CLIENT_STAGE = "client"

# system_settings key holding the org-level OpenRouter key (fallback for the
# agent's own key). Stored as {"key": "sk-..."}.
ORG_OPENROUTER_KEY = "openrouter_api_key"


# ---------------------------------------------------------------------------
# Config get-or-create
# ---------------------------------------------------------------------------
async def get_or_create_agent(db: AsyncSession) -> ReferralAgent:
    """Return the current org's referral-agent config, creating a default row.

    Reads are org-filtered globally, so this always resolves to the caller's own
    config. Exactly one row exists per org (enforced by a unique index); the
    first access lazily creates it with the documented defaults.
    """
    agent = (await db.execute(select(ReferralAgent))).scalars().first()
    if agent is None:
        agent = ReferralAgent()
        db.add(agent)
        await db.flush()
    return agent


# ---------------------------------------------------------------------------
# LLM key resolution + OpenRouter transport
# ---------------------------------------------------------------------------
async def resolve_api_key(db: AsyncSession, agent: ReferralAgent) -> Optional[str]:
    """Resolve the OpenRouter key for *agent*: own key → org key → global config.

    The agent's ``llm_api_key`` is stored Fernet-encrypted; a decrypt failure is
    logged and treated as "no agent key" so a stale ciphertext degrades to the
    org/global key instead of breaking every AI call.
    """
    if agent.llm_api_key:
        try:
            return decrypt(agent.llm_api_key)
        except TokenCryptoError:
            logger.warning("Referral agent: failed to decrypt llm_api_key; falling back")

    # Org-level key from system_settings.
    org_val = await system_settings.get_value(db, ORG_OPENROUTER_KEY, default=None)
    if isinstance(org_val, dict) and org_val.get("key"):
        return org_val["key"]

    # Global config key (the platform default).
    return get_settings().openrouter_api_key or None


async def _agent_complete_json(
    db: AsyncSession,
    agent: ReferralAgent,
    prompt: str,
    *,
    max_tokens: int = 700,
    system: Optional[str] = None,
) -> dict:
    """Send a single-shot prompt to OpenRouter using the agent's model + key.

    Returns the parsed JSON object, or ``{"error": ...}`` on any failure — never
    raises, so a scan over many candidates isn't aborted by one bad response.
    """
    api_key = await resolve_api_key(db, agent)
    if not api_key:
        return {"error": "No OpenRouter API key configured (agent, org, or global)."}

    settings = get_settings()
    base = settings.openrouter_base_url.rstrip("/")
    model = agent.llm_model or DEFAULT_LLM_MODEL

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {"model": model, "max_tokens": max_tokens, "messages": messages}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://apprend.tech",
        "X-Title": "Landline Referral Agent",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            resp = await http.post(
                f"{base}/chat/completions", headers=headers, json=payload
            )
            resp.raise_for_status()
            data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        return _parse_json(text)
    except Exception as exc:  # noqa: BLE001 — degrade, never raise into a scan
        logger.warning("Referral agent AI call failed (model=%s): %s", model, exc)
        return {"error": str(exc)}


def _parse_json(text: str) -> dict:
    """Parse a JSON object out of an LLM reply, tolerating markdown fences/prose."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("```")[1]
        if body.startswith("json"):
            body = body[4:]
        body = body.strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        start = body.find("{")
        end = body.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(body[start : end + 1])
            except json.JSONDecodeError:
                pass
    return {"error": "Failed to parse AI response", "raw": text[:500]}


# ---------------------------------------------------------------------------
# Candidate finding
# ---------------------------------------------------------------------------
@dataclass
class ReferralCandidate:
    """A happy client the agent could ask for a referral."""

    contact: Contact
    company: Company
    health_score: int
    relationship_days: int
    asks_so_far: int


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def find_referral_candidates(
    db: AsyncSession, agent: Optional[ReferralAgent] = None, *, limit: int = 50
) -> list[ReferralCandidate]:
    """Find happy, long-tenured clients under the per-client ask cap.

    A candidate is the primary contact of a company in the ``client`` pipeline
    stage whose health score ≥ ``min_health_score``, whose relationship (company
    age) ≥ ``min_relationship_days``, and who has fewer than
    ``max_asks_per_client`` referral records already. Results are ranked by
    health (happiest first). Reads are org-filtered globally, so this only ever
    sees the caller's / the current task's org.
    """
    if agent is None:
        agent = await get_or_create_agent(db)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=agent.min_relationship_days)

    # Client companies old enough to have a real relationship.
    companies = (
        await db.execute(
            select(Company).where(
                Company.pipeline_stage == _CLIENT_STAGE,
                Company.created_at <= cutoff,
            )
        )
    ).scalars().all()

    candidates: list[ReferralCandidate] = []
    for company in companies:
        # Pick the primary contact (fall back to the earliest-created contact).
        contact = (
            await db.execute(
                select(Contact)
                .where(Contact.company_id == company.id)
                .order_by(Contact.is_primary.desc(), Contact.created_at.asc())
            )
        ).scalars().first()
        if contact is None or not contact.email:
            continue
        # Skip anyone who has opted out / been suppressed.
        if contact.suppressed or contact.unsubscribed:
            continue

        # Don't over-ask: count referrals already tied to this contact.
        asks = (
            await db.execute(
                select(func.count(Referral.id)).where(
                    Referral.referrer_contact_id == contact.id
                )
            )
        ).scalar_one() or 0
        if asks >= agent.max_asks_per_client:
            continue

        health = await compute_company_health(db, company)
        if health.score < agent.min_health_score:
            continue

        created = _aware(company.created_at) or now
        candidates.append(
            ReferralCandidate(
                contact=contact,
                company=company,
                health_score=health.score,
                relationship_days=max(0, (now - created).days),
                asks_so_far=asks,
            )
        )

    candidates.sort(key=lambda c: c.health_score, reverse=True)
    return candidates[:limit]


# ---------------------------------------------------------------------------
# AI drafting
# ---------------------------------------------------------------------------
def _contact_name(contact: Optional[Contact]) -> str:
    if contact is None:
        return "there"
    parts = [contact.first_name or "", contact.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or "there"


def _first_name(contact: Optional[Contact]) -> str:
    if contact and contact.first_name:
        return contact.first_name
    full = _contact_name(contact)
    return full.split()[0] if full and full != "there" else "there"


_VOICE = (
    "VOICE: warm, human, concise (3-5 sentences). Contractions are good. NO "
    "buzzwords (leverage, synergy, cutting-edge, transform, streamline), NO "
    '"I hope this finds you well" openers, NO exclamation marks. One clear ask.'
)


async def generate_referral_ask(
    db: AsyncSession,
    agent: ReferralAgent,
    contact: Contact,
    company: Company,
    relationship_summary: str = "",
) -> dict:
    """Craft a personalized referral-ask email to a happy client.

    Returns ``{"subject", "body"}``. Falls back to the agent's
    ``referral_template`` (or a built-in default) if the LLM is unavailable, so a
    usable draft is always produced.
    """
    first = _first_name(contact)
    tmpl = (agent.referral_template or "").strip()
    tmpl_line = f"\nBase this on our template (adapt, personalize, don't copy verbatim):\n{tmpl}\n" if tmpl else ""
    summary = relationship_summary or f"{company.name} has been a happy client of ours."

    prompt = f"""You're Suman from Apprend Technologies. Write a short referral-request email
to a happy client, asking if they know anyone who'd benefit from what we do.

CLIENT:
Name: {_contact_name(contact)}
Company: {company.name}
Industry: {company.industry_segment or 'manufacturing'}
Context: {summary}
{tmpl_line}
{_VOICE}
- Open by genuinely appreciating the relationship — be specific, not generic.
- Make the ask easy and low-pressure: "no worries if not".
- Ask them to think of 1-2 people or shops like theirs who might find us useful.
- Address them by first name ({first}). Sign off as "Suman".

Return ONLY valid JSON: {{"subject": "...", "body": "..."}}"""

    result = await _agent_complete_json(db, agent, prompt, max_tokens=700)
    if isinstance(result, dict) and result.get("subject") and result.get("body"):
        return {"subject": str(result["subject"]), "body": str(result["body"])}
    return _fallback_ask(agent, contact, company)


async def generate_followup(
    db: AsyncSession,
    agent: ReferralAgent,
    referral: Referral,
    contact: Optional[Contact],
    company: Optional[Company] = None,
) -> dict:
    """Craft a gentle follow-up on an unanswered referral ask."""
    first = _first_name(contact)
    tmpl = (agent.followup_template or "").strip()
    tmpl_line = f"\nBase this on our template (adapt, don't copy verbatim):\n{tmpl}\n" if tmpl else ""
    company_name = (company.name if company else None) or referral.referred_company or "your company"

    prompt = f"""You're Suman from Apprend Technologies. You asked {_contact_name(contact)} a
little while ago whether they knew anyone who might benefit from what we build.
They haven't replied. Write a very short, friendly nudge.
{tmpl_line}
{_VOICE}
- Under 40 words. This is a bump, not a new pitch.
- No guilt, no pressure — "totally fine if nothing comes to mind".
- Address them by first name ({first}). Sign off as "Suman".

Return ONLY valid JSON: {{"subject": "...", "body": "..."}}"""

    result = await _agent_complete_json(db, agent, prompt, max_tokens=400)
    if isinstance(result, dict) and result.get("subject") and result.get("body"):
        return {"subject": str(result["subject"]), "body": str(result["body"])}
    return _fallback_followup(agent, contact)


async def generate_thankyou(
    db: AsyncSession,
    agent: ReferralAgent,
    referral: Referral,
    contact: Optional[Contact],
    deal_value: Optional[int] = None,
) -> dict:
    """Craft a warm thank-you when a referral converts into a deal."""
    first = _first_name(contact)
    tmpl = (agent.thankyou_template or "").strip()
    tmpl_line = f"\nBase this on our template (adapt, don't copy verbatim):\n{tmpl}\n" if tmpl else ""
    referred = referral.referred_name or "the person you introduced us to"

    prompt = f"""You're Suman from Apprend Technologies. A referral from {_contact_name(contact)}
just became a customer — {referred} signed on. Write a genuine, warm thank-you.
{tmpl_line}
{_VOICE}
- Be specific and sincere; their intro directly led to real business.
- Keep it short. No coupon-speak, no over-the-top flattery.
- Address them by first name ({first}). Sign off as "Suman".

Return ONLY valid JSON: {{"subject": "...", "body": "..."}}"""

    result = await _agent_complete_json(db, agent, prompt, max_tokens=500)
    if isinstance(result, dict) and result.get("subject") and result.get("body"):
        return {"subject": str(result["subject"]), "body": str(result["body"])}
    return _fallback_thankyou(agent, contact, referred)


# ---------------------------------------------------------------------------
# Template fallbacks (used when the LLM is unavailable)
# ---------------------------------------------------------------------------
def _apply_template(template: Optional[str], **vars: str) -> Optional[str]:
    """Fill ``{placeholder}`` tokens in a template; unknown tokens are left as-is."""
    if not template:
        return None
    out = template
    for key, val in vars.items():
        out = out.replace("{" + key + "}", val)
    return out


def _fallback_ask(agent: ReferralAgent, contact: Optional[Contact], company: Company) -> dict:
    first = _first_name(contact)
    body = _apply_template(
        agent.referral_template,
        first_name=first,
        contact_name=_contact_name(contact),
        company=company.name,
    ) or (
        f"Hi {first},\n\n"
        f"It's been a pleasure working with {company.name} — thank you for your trust in us. "
        "Quick favor: if anyone comes to mind who runs a shop like yours and might find what we build "
        "useful, I'd be grateful for an intro. No worries at all if nothing comes to mind.\n\n"
        "Suman"
    )
    return {"subject": f"A quick favor, {first}?", "body": body}


def _fallback_followup(agent: ReferralAgent, contact: Optional[Contact]) -> dict:
    first = _first_name(contact)
    body = _apply_template(
        agent.followup_template, first_name=first, contact_name=_contact_name(contact)
    ) or (
        f"Hi {first},\n\n"
        "Just floating this back up — totally fine if no one comes to mind. Appreciate you either way.\n\n"
        "Suman"
    )
    return {"subject": "Following up", "body": body}


def _fallback_thankyou(agent: ReferralAgent, contact: Optional[Contact], referred: str) -> dict:
    first = _first_name(contact)
    body = _apply_template(
        agent.thankyou_template,
        first_name=first,
        contact_name=_contact_name(contact),
        referred=referred,
    ) or (
        f"Hi {first},\n\n"
        f"Just wanted to say thank you — your introduction to {referred} turned into a real partnership. "
        "It means a lot that you thought of us. Truly grateful.\n\n"
        "Suman"
    )
    return {"subject": "Thank you", "body": body}


# ---------------------------------------------------------------------------
# Referral-chain tracking
# ---------------------------------------------------------------------------
async def track_referral_chain(
    db: AsyncSession,
    referrer: Contact,
    referred: Contact,
    *,
    referral_value: Optional[int] = None,
    notes: Optional[str] = None,
) -> Referral:
    """Link a newly-created contact back to whoever referred them.

    Reuses an existing open referral (matched by referrer + referred email) if
    one is already on file — e.g. one the agent created at ask time — attaching
    the concrete ``referred_contact_id`` to it; otherwise creates a fresh
    ``received`` referral. Idempotent on (referrer, referred email).
    """
    existing = None
    if referred.email:
        existing = (
            await db.execute(
                select(Referral).where(
                    Referral.referrer_contact_id == referrer.id,
                    func.lower(Referral.referred_email) == referred.email.lower(),
                )
            )
        ).scalars().first()

    now = datetime.now(timezone.utc)
    if existing is not None:
        existing.referred_contact_id = referred.id
        if existing.status in ("identified", "asked", "pending"):
            existing.status = "received"
            existing.received_at = existing.received_at or now
        if referral_value is not None:
            existing.referral_value = referral_value
        if notes:
            existing.notes = notes
        await db.flush()
        return existing

    referral = Referral(
        referrer_contact_id=referrer.id,
        referred_contact_id=referred.id,
        referred_name=_contact_name(referred),
        referred_email=referred.email,
        status="received",
        received_at=now,
        referral_value=referral_value,
        notes=notes,
    )
    db.add(referral)
    await db.flush()
    return referral


def relationship_summary(candidate: ReferralCandidate) -> str:
    """A short human-readable relationship blurb for the ask prompt."""
    years = candidate.relationship_days // 365
    months = (candidate.relationship_days % 365) // 30
    if years >= 1:
        tenure = f"{years} year{'s' if years != 1 else ''}"
    elif months >= 1:
        tenure = f"{months} month{'s' if months != 1 else ''}"
    else:
        tenure = f"{candidate.relationship_days} days"
    return (
        f"{candidate.company.name} has been a client for about {tenure} with a "
        f"health score of {candidate.health_score}/100 — a strong, happy relationship."
    )
