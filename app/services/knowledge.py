"""Contextual knowledge surfacing.

When a rep is composing an email (or a script) to a company in a given industry,
this service pulls the *relevant* slice of the knowledge base — pain points,
objection handling, and the industry profile (emotional triggers, decision
makers, certifications, metrics, vocabulary) — so they don't have to go look it
up. It stitches together three existing sources:

- ``industry_verticals`` — the routed vertical (name, demo link, pain language).
- ``call_scripts_data`` — the industry's objection/rebuttal pairs.
- ``ai_sales_guidelines.INDUSTRY_PROFILES`` — the deep industry profile.

Pure and read-only: :func:`surface_for_industry` needs no DB;
:func:`surface_for_company` resolves the vertical from a Company row.
"""

from __future__ import annotations

from app.models.company import Company
from app.services import call_scripts_data
from app.services import industry_verticals as iv
from app.services.outreach import ai_sales_guidelines as guidelines

# Vertical machine key → INDUSTRY_PROFILES key. Kept here (rather than imported
# from ai_script_generator) so knowledge surfacing has no dependency on the call
# script generator.
_VERTICAL_TO_PROFILE = {
    "injection_molding": "plastic_injection_molding",
    "cnc_machining": "cnc_machining",
    "metal_stamping": "metal_stamping",
    "food_beverage": "food_and_beverage",
}

# Sales-psychology principles most useful while composing an email.
_COMPOSE_PRINCIPLE_KEYS = (
    "reciprocity", "social_proof", "authority", "micro_commitments",
)


def _profile_for_vertical(vertical: iv.IndustryVertical) -> dict:
    profile_key = _VERTICAL_TO_PROFILE.get(vertical.key, "")
    return guidelines.INDUSTRY_PROFILES.get(profile_key, {})


def surface_for_industry(industry: str = "", company_name: str = "") -> dict:
    """Return the knowledge-base slice relevant to a target industry.

    ``industry`` may be a machine key (``"injection_molding"``) or free text
    (``"Plastic Injection Molding"``) — it's routed through the vertical
    resolver, which always resolves to a vertical (defaulting to the flagship).

    The returned dict is JSON-serialisable and shaped for the compose UI::

        {
          "industry": {"key", "name", "demo_link", "matched"},
          "pain_points": [...],
          "objection_handling": [{"objection", "rebuttal"}, ...],
          "profile": {
            "emotional_triggers": {"effective": [...], "avoid": [...]},
            "decision_makers": [...],
            "approach_by_size": {...},
            "key_certifications": [...],
            "key_metrics": [...],
            "technical_vocabulary": [...],
            "personalization_triggers": [...],
          },
          "principles": {key: text, ...},
        }
    """
    matched_vertical = iv.resolve_vertical(industry, company_name)
    vertical = matched_vertical or iv.resolve_vertical_or_default(industry, company_name)
    profile = _profile_for_vertical(vertical)

    # Objection/rebuttal pairs from the industry's cold-call script.
    objection_handling: list[dict] = []
    try:
        script = call_scripts_data.resolve_script(
            industry=vertical.key, company_name=company_name
        )
        objection_handling = [dict(o) for o in (script.objection_handling or [])]
    except Exception:  # noqa: BLE001 — surfacing must never raise
        objection_handling = []

    # Pain points: the vertical's, enriched with the profile's emotional triggers.
    pain_points = list(vertical.pain_points or [])

    principles = {
        k: guidelines.OUTREACH_PRINCIPLES[k]
        for k in _COMPOSE_PRINCIPLE_KEYS
        if k in guidelines.OUTREACH_PRINCIPLES
    }

    return {
        "industry": {
            "key": vertical.key,
            "name": vertical.name,
            "demo_link": vertical.demo_link,
            # Whether we matched a real vertical vs fell back to the default.
            "matched": matched_vertical is not None,
        },
        "pain_points": pain_points,
        "objection_handling": objection_handling,
        "profile": {
            "emotional_triggers": profile.get("emotional_triggers", {}),
            "decision_makers": profile.get("decision_makers", []),
            "approach_by_size": profile.get("approach_by_size", {}),
            "key_certifications": profile.get("key_certifications", []),
            "key_metrics": profile.get("key_metrics", []),
            "technical_vocabulary": profile.get("technical_vocabulary", []),
            "personalization_triggers": profile.get("personalization_triggers", []),
        },
        "principles": principles,
    }


def surface_for_company(company: Company) -> dict:
    """Surface knowledge for the industry of a specific company row.

    Adds a small ``company`` block so the compose UI can label the panel, and
    resolves the vertical from the company's ``industry_segment`` and name.
    """
    result = surface_for_industry(
        industry=company.industry_segment or "",
        company_name=company.name or "",
    )
    result["company"] = {
        "id": str(company.id) if getattr(company, "id", None) else None,
        "name": company.name,
        "industry_segment": company.industry_segment,
    }
    return result
