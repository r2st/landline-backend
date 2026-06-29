"""AI-powered personalised cold-call script generation.

Where ``call_scripts_data`` provides one generic script per industry, this
service uses Claude to write a *bespoke* script for a single prospect — opening
with something specific about their company, speaking in the language of the
contact's role (an Owner hears ROI; a Quality Manager hears compliance; a Plant
Manager hears uptime), and weaving in how the lead has already engaged with our
email (opens/clicks/replies) and any recent company news.

It is built to never fail a request:

* If no ``ANTHROPIC_API_KEY`` is configured (e.g. the test DB, a fresh env), or
  the API errors / times out, it falls back to the canonical industry template
  rendered into the same structured shape, with ``source="template"`` and a
  human-readable ``note``.
* All AI calls are bounded by a 30-second timeout.

The public entry points are :func:`generate_for_contact` (ad-hoc, no task) and
:func:`get_or_generate_for_task` (cached on a ``CallTask``). The queue generator
calls the latter to pre-warm scripts for the day's hot/warm leads.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.call import CallTask
from app.models.company import Company
from app.models.company_news import CompanyNews
from app.models.contact import Contact
from app.models.interaction import Interaction
from app.models.lead_score import LeadScore
from app.schemas.call import CallScriptRead, CustomScript, CustomScriptResponse
from app.services import call_scripts_data, industry_verticals as iv
from app.services.call_queue import engagement_by_contact
from app.services.outreach import ai_sales_guidelines as guidelines

logger = logging.getLogger(__name__)

AI_TIMEOUT_SECONDS = 30
_MODEL = "claude-sonnet-4-6"

# Canonical call-script industry keys → ai_sales_guidelines profile keys.
_VERTICAL_TO_PROFILE = {
    "injection_molding": "plastic_injection_molding",
    "cnc_machining": "cnc_machining",
    "metal_stamping": "metal_stamping",
    "food_beverage": "food_and_beverage",
}

# Accepted call_type aliases → canonical call_scripts script_type.
_CALL_TYPE_TO_SCRIPT_TYPE = {
    "initial": "initial_cold_call",
    "initial_cold_call": "initial_cold_call",
    "follow_up": "follow_up",
    "voicemail": "voicemail",
}


# --------------------------------------------------------------------------- #
# Role-level detection — drives the tone/angle of the generated script.
# --------------------------------------------------------------------------- #

# (level, keywords, guidance for the AI on how to talk to this role)
_ROLE_RULES: list[tuple[str, tuple[str, ...], str]] = [
    (
        "executive",
        ("ceo", "owner", "president", "founder", "principal", "managing director", "gm", "general manager"),
        "Speak the language of the business: margin, growth, customer retention, "
        "competitive positioning, and ROI. Frame the platform as protecting the "
        "business that the parts pay for. Keep it strategic, not technical.",
    ),
    (
        "quality",
        ("quality", "qa", "qc", "fsqa", "food safety", "compliance"),
        "Speak the language of quality and compliance: audit readiness, reject "
        "rates, Cpk, NCRs, traceability, and standards (ISO 9001, IATF 16949, "
        "AS9100, SQF/FSMA). Frame the platform as data-rich evidence and fewer "
        "escapes to document.",
    ),
    (
        "operations",
        ("plant", "operations", "production", "ops", "manufacturing", "supervisor", "floor"),
        "Speak the language of the floor: uptime, throughput, scrap, changeover "
        "time, labour efficiency, and real-time visibility. Frame the platform "
        "as catching drift live so a run doesn't go bad.",
    ),
    (
        "engineering",
        ("engineer", "process", "technical", "r&d", "tooling", "toolroom", "die"),
        "Speak the language of the technical champion: parameter optimisation, "
        "machine/press signals, validation, and data accuracy. Be precise and "
        "credible; reference the right technical vocabulary naturally.",
    ),
]

_DEFAULT_ROLE = (
    "balanced",
    "Balance operational and business angles — lead with the concrete cost of "
    "quality escapes, then the value of catching drift in real time.",
)


def _detect_role(title: Optional[str]) -> tuple[str, str]:
    """Return ``(role_level, guidance)`` for a contact's title."""
    t = (title or "").lower()
    for level, keywords, guidance in _ROLE_RULES:
        if any(kw in t for kw in keywords):
            return level, guidance
    return _DEFAULT_ROLE


def _normalize_call_type(call_type: str) -> str:
    return _CALL_TYPE_TO_SCRIPT_TYPE.get(call_type, "initial_cold_call")


# --------------------------------------------------------------------------- #
# Context gathering
# --------------------------------------------------------------------------- #


async def _gather_context(
    db: AsyncSession,
    company: Company,
    contact: Optional[Contact],
    vertical: iv.IndustryVertical,
) -> dict:
    """Collect everything the AI needs to personalise a script.

    Returns a JSON-serialisable dict — also stored on the task as
    ``script_context`` so staleness can be detected later.
    """
    now = datetime.now(timezone.utc)
    profile_key = _VERTICAL_TO_PROFILE.get(vertical.key, "")
    profile = guidelines.INDUSTRY_PROFILES.get(profile_key, {})

    role_level, _ = _detect_role(contact.title if contact else None)

    ctx: dict = {
        "company": {
            "name": company.name,
            "industry_segment": company.industry_segment,
            "employee_count": company.employee_count,
            "city": company.city,
            "state_province": company.state_province,
            "website": company.website,
            "tags": list(company.tags or []),
            "pipeline_stage": company.pipeline_stage,
        },
        "contact": None,
        "industry": {
            "key": vertical.key,
            "name": vertical.name,
            "demo_link": vertical.demo_link,
            "pain_points": list(vertical.pain_points),
            "likely_certifications": profile.get("key_certifications", []),
            "key_metrics": profile.get("key_metrics", []),
            "technical_vocabulary": profile.get("technical_vocabulary", []),
        },
        "role_level": role_level,
        "engagement": {
            "emails_sent": 0, "opens": 0, "clicks": 0, "replies": 0,
            "days_since_last_contact": None,
        },
        "recent_interactions": [],
        "news": [],
        "lead_score": None,
    }

    if contact is not None:
        ctx["contact"] = {
            "name": contact.full_name,
            "first_name": contact.first_name,
            "title": contact.title,
            "department": contact.department,
        }

    # Email engagement.
    if contact is not None:
        eng = (await engagement_by_contact(db, [contact.id])).get(contact.id)
        if eng is not None:
            days_since = None
            if eng.last_contact_at is not None:
                days_since = (now - eng.last_contact_at).days
            ctx["engagement"] = {
                "emails_sent": eng.emails_sent,
                "opens": eng.opens,
                "clicks": eng.clicks,
                "replies": eng.replies,
                "days_since_last_contact": days_since,
            }

    # Recent interactions (contact-specific or company-wide).
    iquery = select(Interaction).where(Interaction.company_id == company.id)
    if contact is not None:
        iquery = select(Interaction).where(
            (Interaction.company_id == company.id)
            | (Interaction.contact_id == contact.id)
        )
    irows = (
        await db.execute(iquery.order_by(Interaction.occurred_at.desc()).limit(5))
    ).scalars().all()
    ctx["recent_interactions"] = [
        {
            "type": it.interaction_type,
            "subject": it.subject,
            "notes": it.notes or it.content,
            "occurred_at": it.occurred_at.isoformat() if it.occurred_at else None,
        }
        for it in irows
    ]

    # Latest fit score + factors.
    score_row = (
        await db.execute(
            select(LeadScore)
            .where(LeadScore.company_id == company.id)
            .order_by(LeadScore.scored_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if score_row is not None:
        ctx["lead_score"] = {
            "fit_score": score_row.fit_score,
            "factors": score_row.factors or {},
        }

    # Recent company news (most relevant / recent first).
    news_rows = (
        await db.execute(
            select(CompanyNews)
            .where(CompanyNews.company_id == company.id)
            .order_by(CompanyNews.discovered_at.desc())
            .limit(3)
        )
    ).scalars().all()
    ctx["news"] = [
        {
            "title": n.title,
            "summary": n.summary,
            "category": n.category,
        }
        for n in news_rows
    ]

    return ctx


# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #


def _summarize_engagement(eng: dict) -> str:
    if eng.get("replies"):
        return (
            f"They have REPLIED to a previous email — this is a warm conversation, "
            f"not a true cold call. Pick the thread back up naturally."
        )
    if eng.get("clicks"):
        return (
            f"They clicked a link in our email ({eng['clicks']} click(s)). Reference "
            f"that they looked at the dashboard / case study without being creepy "
            f"about tracking."
        )
    if eng.get("opens", 0) >= 3:
        return (
            f"They opened our emails {eng['opens']} times — clear interest. Name it: "
            f"'I know my emails have been landing in your inbox…'"
        )
    if eng.get("opens"):
        return f"They opened an email but haven't engaged further — light interest."
    return "No email engagement yet — a genuinely cold call. Lead with industry pain."


def _build_prompt(
    *,
    company: Company,
    contact: Optional[Contact],
    vertical: iv.IndustryVertical,
    base_script: CallScriptRead,
    context: dict,
    call_type: str,
) -> str:
    profile_key = _VERTICAL_TO_PROFILE.get(vertical.key, "")
    profile = guidelines.INDUSTRY_PROFILES.get(profile_key, {})

    role_level, role_guidance = _detect_role(contact.title if contact else None)
    first_name = (contact.first_name if contact and contact.first_name else "there")
    contact_title = (contact.title if contact and contact.title else "Unknown role")
    contact_name = (contact.full_name if contact else "Unknown contact")

    # Selected sales-psychology principles most relevant to a live call.
    principle_keys = (
        "tactical_empathy", "challenger_teaching", "spin_questioning",
        "micro_commitments", "social_proof",
    )
    principles = "\n".join(
        f"- {k.replace('_', ' ').title()}: {guidelines.OUTREACH_PRINCIPLES[k]}"
        for k in principle_keys if k in guidelines.OUTREACH_PRINCIPLES
    )

    # Emotional triggers for the vertical.
    effective = "\n".join(f"  - {t}" for t in profile.get("emotional_triggers", {}).get("effective", []))
    avoid = "\n".join(f"  - {t}" for t in profile.get("emotional_triggers", {}).get("avoid", []))

    vocab = ", ".join(profile.get("technical_vocabulary", [])[:12])
    certs = ", ".join(profile.get("key_certifications", []))
    metrics = ", ".join(profile.get("key_metrics", []))

    news_block = "None on file."
    if context.get("news"):
        news_block = "\n".join(
            f"  - {n['title']}" + (f" — {n['summary']}" if n.get("summary") else "")
            for n in context["news"]
        )

    interactions_block = "None on file."
    if context.get("recent_interactions"):
        interactions_block = "\n".join(
            f"  - [{it.get('type')}] {it.get('subject') or ''}: {(it.get('notes') or '')[:160]}"
            for it in context["recent_interactions"]
        )

    score = context.get("lead_score")
    score_block = "No fit score yet."
    if score:
        score_block = f"Fit score {score.get('fit_score')}/100."

    eng = context.get("engagement", {})
    engagement_block = _summarize_engagement(eng)

    emp = company.employee_count
    size_bucket = "unknown size"
    if emp:
        if emp < 50:
            size_bucket = f"small shop (~{emp} employees) — likely owner-led decisions"
        elif emp <= 200:
            size_bucket = f"mid-size ({emp} employees)"
        else:
            size_bucket = f"large operation ({emp} employees) — multi-threaded buying"

    # Render the base template script as a scaffold.
    base_json = json.dumps(
        {
            "opening_hook": base_script.opening_hook,
            "qualifying_questions": base_script.qualifying_questions,
            "pain_points": base_script.pain_points,
            "value_proposition": base_script.value_proposition,
            "objection_handling": base_script.objection_handling,
            "call_to_action": base_script.call_to_action,
        },
        indent=2,
    )

    call_type_guidance = {
        "initial_cold_call": (
            "This is a FIRST-TOUCH cold call. Earn 30 seconds, create curiosity, "
            "qualify, and close on a free 48-hour pilot or a 15-minute follow-up."
        ),
        "follow_up": (
            "This is a FOLLOW-UP / callback. Reference the prior touch, address what "
            "was left open, and push for a concrete next step (book the pilot date)."
        ),
        "voicemail": (
            "This is a VOICEMAIL. Keep it under 20 seconds spoken. One hook, one "
            "reason to call back, the rep's number, and the demo link mention. "
            "Put the whole voicemail in the 'voicemail' field and keep other "
            "sections short."
        ),
    }.get(call_type, "")

    return f"""You are an elite B2B cold-calling coach writing a CUSTOM call script for a
sales rep at Apprend Technologies. Apprend builds an AI quality-management
platform for manufacturers — it watches machine/press/line signals in real time
and flags quality drift before parts go out of spec, with a free 48-hour pilot
and a live demo dashboard ({vertical.demo_link}).

Write a script the rep will actually say out loud on the phone — natural,
concrete, conversational (contractions fine). NOT marketing copy. No buzzwords
(leverage, synergy, cutting-edge, revolutionary, transform, streamline). No
"I hope this finds you well". The rep's first name placeholder is {{rep_name}}
and their callback number is {{rep_phone}} — use those tokens, don't invent them.

## THE PROSPECT
- Contact: {contact_name} — {contact_title} (role level: {role_level})
- Use the first name "{first_name}" in the opening.
- Company: {company.name} — {company.industry_segment or 'manufacturer'}, {size_bucket}
- Location: {context['company'].get('city') or '?'}, {context['company'].get('state_province') or '?'}
- Industry vertical: {vertical.name}

## HOW TO TALK TO THIS ROLE
{role_guidance}

## EMAIL ENGAGEMENT (personalise the opening around this)
{engagement_block}

## RECENT COMPANY NEWS (use a real one as the opening hook if relevant)
{news_block}

## RECENT INTERACTIONS
{interactions_block}

## LEAD SCORE
{score_block}

## INDUSTRY CONTEXT — {vertical.name}
Emotional triggers that work:
{effective or '  - (use general manufacturing quality pain)'}
What to avoid:
{avoid or '  - Talking down to their expertise'}
Likely certifications: {certs or 'ISO 9001'}
Key metrics they track: {metrics or 'scrap rate, OEE'}
Technical vocabulary (use naturally, do not force): {vocab}
Real pain points: {', '.join(vertical.pain_points)}

## SALES PSYCHOLOGY TO APPLY
{principles}

## CALL TYPE
{call_type_guidance}

## BASE INDUSTRY TEMPLATE (a scaffold to personalise — do not copy verbatim)
{base_json}

## OUTPUT
Return ONLY valid JSON in exactly this shape:
{{
  "opening_hook": "what the rep says in the first 15-20 seconds — personalised to THIS company/contact/engagement",
  "personalization_notes": "1-2 sentences telling the rep WHY this script is tailored (what signal you used) — coaching, not spoken",
  "talking_points": ["3-5 short bullets the rep can glance at — role-appropriate value, framed for {role_level}"],
  "qualifying_questions": ["3-5 open discovery questions tailored to their industry + size"],
  "pain_points": ["3-5 pain points specific to their certifications/products/role"],
  "objection_handling": [{{"objection": "...", "rebuttal": "..."}}],
  "call_to_action": "the specific close, naming the right next step and the demo link",
  "voicemail": "a <20s voicemail (only fill richly if call type is voicemail; otherwise a short fallback)"
}}"""


# --------------------------------------------------------------------------- #
# Template fallback
# --------------------------------------------------------------------------- #


def _template_to_custom(base_script: CallScriptRead) -> CustomScript:
    """Render the canonical industry template into the CustomScript shape."""
    talking_points = []
    if base_script.value_proposition:
        talking_points.append(base_script.value_proposition)
    return CustomScript(
        opening_hook=base_script.opening_hook,
        personalization_notes=(
            "AI generation is unavailable — showing the generic industry template. "
            "Configure ANTHROPIC_API_KEY to enable personalised scripts."
        ),
        talking_points=talking_points,
        qualifying_questions=list(base_script.qualifying_questions or []),
        pain_points=list(base_script.pain_points or []),
        objection_handling=list(base_script.objection_handling or []),
        call_to_action=base_script.call_to_action,
        voicemail=None,
    )


def _coerce_objections(raw) -> list[dict]:
    """Normalise the AI's objection_handling into [{objection, rebuttal}]."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            obj = item.get("objection") or item.get("concern") or ""
            reb = item.get("rebuttal") or item.get("response") or item.get("answer") or ""
            if obj or reb:
                out.append({"objection": str(obj), "rebuttal": str(reb)})
    return out


def _parse_ai_script(result: dict, base_script: CallScriptRead) -> CustomScript:
    """Build a CustomScript from the AI JSON, falling back per-field."""
    def _as_list(v) -> list[str]:
        if isinstance(v, list):
            return [str(x) for x in v if x]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    return CustomScript(
        opening_hook=str(result.get("opening_hook") or base_script.opening_hook),
        personalization_notes=result.get("personalization_notes"),
        talking_points=_as_list(result.get("talking_points")),
        qualifying_questions=(
            _as_list(result.get("qualifying_questions"))
            or list(base_script.qualifying_questions or [])
        ),
        pain_points=(
            _as_list(result.get("pain_points")) or list(base_script.pain_points or [])
        ),
        objection_handling=(
            _coerce_objections(result.get("objection_handling"))
            or list(base_script.objection_handling or [])
        ),
        call_to_action=str(result.get("call_to_action") or base_script.call_to_action or ""),
        voicemail=result.get("voicemail"),
    )


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #


class AIScriptGenerator:
    """Wraps the Anthropic client; lazily created so a missing key is harmless."""

    def __init__(self):
        self._client: Optional[anthropic.AsyncAnthropic] = None
        self._model = _MODEL

    @property
    def available(self) -> bool:
        return bool(get_settings().anthropic_api_key)

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(
                api_key=get_settings().anthropic_api_key
            )
        return self._client

    async def generate_script_json(self, prompt: str) -> dict:
        """Single-shot prompt → parsed JSON dict. Bounded by AI_TIMEOUT_SECONDS.

        Returns ``{"error": ...}`` on any failure so callers can fall back.
        """
        try:
            message = await asyncio.wait_for(
                self._get_client().messages.create(
                    model=self._model,
                    max_tokens=2048,
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
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
                end = max(text.rfind("}"), text.rfind("]"))
                if start != -1 and end > start:
                    return json.loads(text[start : end + 1])
                raise
        except asyncio.TimeoutError:
            logger.error("AI script generation timed out after %ss", AI_TIMEOUT_SECONDS)
            return {"error": "timeout"}
        except Exception as e:  # noqa: BLE001 - never raise into the request
            logger.error("AI script generation failed: %s", e)
            return {"error": str(e)}


_generator: Optional[AIScriptGenerator] = None


def get_script_generator() -> AIScriptGenerator:
    global _generator
    if _generator is None:
        _generator = AIScriptGenerator()
    return _generator


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def _resolve_base_script(
    db: AsyncSession, vertical_key: str, script_type: str
) -> CallScriptRead:
    """DB script for the vertical, falling back to the in-code template."""
    from app.models.call import CallScript

    row = (
        await db.execute(
            select(CallScript).where(
                CallScript.industry == vertical_key,
                CallScript.script_type == script_type,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return CallScriptRead.model_validate(row)
    tpl = call_scripts_data.get_script(vertical_key, script_type)
    if tpl is None:
        tpl = call_scripts_data.resolve_script(industry=vertical_key, script_type=script_type)
    return CallScriptRead(**tpl.as_row())


async def generate_for_contact(
    db: AsyncSession,
    *,
    company: Company,
    contact: Optional[Contact],
    call_type: str = "initial",
) -> CustomScriptResponse:
    """Generate a personalised script for a company/contact (no persistence).

    Always returns a response — AI when possible, template fallback otherwise.
    """
    script_type = _normalize_call_type(call_type)
    vertical = iv.resolve_vertical_or_default(
        company.industry_segment or "", company.name or ""
    )
    base_script = await _resolve_base_script(db, vertical.key, script_type)
    context = await _gather_context(db, company, contact, vertical)
    now = datetime.now(timezone.utc)

    generator = get_script_generator()
    if not generator.available:
        return CustomScriptResponse(
            company_id=company.id,
            company_name=company.name,
            contact_id=contact.id if contact else None,
            contact_name=contact.full_name if contact else None,
            contact_title=contact.title if contact else None,
            industry=vertical.key,
            call_type=call_type,
            source="template",
            ai_available=False,
            note="AI generation unavailable (no API key) — showing industry template.",
            generated_at=now,
            script=_template_to_custom(base_script),
        )

    prompt = _build_prompt(
        company=company,
        contact=contact,
        vertical=vertical,
        base_script=base_script,
        context=context,
        call_type=script_type,
    )
    result = await generator.generate_script_json(prompt)

    if not isinstance(result, dict) or result.get("error"):
        note = "AI generation failed — showing industry template."
        if isinstance(result, dict) and result.get("error") == "timeout":
            note = "AI generation timed out — showing industry template."
        return CustomScriptResponse(
            company_id=company.id,
            company_name=company.name,
            contact_id=contact.id if contact else None,
            contact_name=contact.full_name if contact else None,
            contact_title=contact.title if contact else None,
            industry=vertical.key,
            call_type=call_type,
            source="template",
            ai_available=True,
            note=note,
            generated_at=now,
            script=_template_to_custom(base_script),
        )

    return CustomScriptResponse(
        company_id=company.id,
        company_name=company.name,
        contact_id=contact.id if contact else None,
        contact_name=contact.full_name if contact else None,
        contact_title=contact.title if contact else None,
        industry=vertical.key,
        call_type=call_type,
        source="ai",
        ai_available=True,
        note=None,
        generated_at=now,
        script=_parse_ai_script(result, base_script),
    )


def _context_signature(context: dict) -> dict:
    """A compact, comparable signature of the signals a script was built from.

    Stored alongside the script so we can tell whether the lead's situation has
    materially changed since it was generated.
    """
    eng = context.get("engagement", {})
    return {
        "engagement": {
            "opens": eng.get("opens", 0),
            "clicks": eng.get("clicks", 0),
            "replies": eng.get("replies", 0),
        },
        "news_count": len(context.get("news", [])),
        "interaction_count": len(context.get("recent_interactions", [])),
        "role_level": context.get("role_level"),
        "fit_score": (context.get("lead_score") or {}).get("fit_score"),
    }


async def get_or_generate_for_task(
    db: AsyncSession,
    task: CallTask,
    *,
    call_type: str = "initial",
    force: bool = False,
) -> CustomScriptResponse:
    """Return the task's cached custom script, generating + caching if missing.

    The generated script (and the context signature it was built from) is stored
    on the ``CallTask`` so the rep's briefing doesn't regenerate on every load.
    Pass ``force=True`` to regenerate regardless of cache.
    """
    if task.custom_script and not force:
        cached = dict(task.custom_script)
        cached.setdefault("call_task_id", str(task.id))
        try:
            return CustomScriptResponse.model_validate(cached)
        except Exception:  # noqa: BLE001 - stale/legacy cache shape, regenerate
            logger.warning("Cached script for task %s unreadable; regenerating", task.id)

    company = (
        await db.execute(select(Company).where(Company.id == task.company_id))
    ).scalar_one_or_none()
    if company is None:
        raise ValueError("Call task has no company")
    contact = None
    if task.contact_id is not None:
        contact = (
            await db.execute(select(Contact).where(Contact.id == task.contact_id))
        ).scalar_one_or_none()

    response = await generate_for_contact(
        db, company=company, contact=contact, call_type=call_type
    )
    response.call_task_id = task.id

    # Cache onto the task. Recompute the context signature for staleness checks.
    vertical = iv.resolve_vertical_or_default(
        company.industry_segment or "", company.name or ""
    )
    context = await _gather_context(db, company, contact, vertical)
    task.custom_script = json.loads(response.model_dump_json())
    task.script_generated_at = response.generated_at
    task.script_context = _context_signature(context)
    await db.flush()
    # Reload server-computed columns (e.g. updated_at) inside the async context so
    # later sync access (pydantic serialisation of the task) doesn't lazy-load.
    await db.refresh(task)

    return response


async def pregenerate_for_tasks(
    db: AsyncSession,
    tasks: list[CallTask],
    *,
    priorities: tuple[str, ...] = ("hot", "warm"),
    max_scripts: int = 10,
) -> int:
    """Pre-generate + cache custom scripts for the highest-priority tasks.

    Called after queue generation so the day's hot/warm leads have scripts ready
    before the rep starts dialling. Best-effort and sequential (one shared
    session): a failure on one task is logged and skipped, never raised. Returns
    the number of scripts generated. Bounded by ``max_scripts`` to keep the
    queue-generation request responsive.
    """
    generated = 0
    for task in tasks:
        if generated >= max_scripts:
            break
        if task.priority not in priorities:
            continue
        try:
            await get_or_generate_for_task(db, task, call_type="initial", force=False)
            generated += 1
        except Exception as e:  # noqa: BLE001 - never fail queue generation
            logger.warning("Pre-generating script for task %s failed: %s", task.id, e)
    logger.info("Pre-generated %d custom call scripts", generated)
    return generated
