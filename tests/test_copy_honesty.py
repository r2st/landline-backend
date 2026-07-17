"""The copy we send must not invent customers, quotes, or results.

``outreach_templates``' docstring has always said this — numbers are estimates
the reader checks against their own shop, never fabricated customer outcomes;
social proof is the pattern across the industry, never a named case study — and
the sequences seeded from code honour it. Nothing enforced it. Six sequences
built by hand through the API drifted the other way and were fixed in migration
074; one of them ("Final v6") was active and asserting a $340K -> $87K result to
live prospects when it was found.

This file is the enforcement. It scans the copy we actually ship — the template
constants and the migration's replacement bodies, not their docstrings, which
legitimately quote the fabrications in order to explain them.

Scope, honestly stated: this pins the specific fabrications that were removed and
the shapes they came in. It cannot decide whether a sentence is true — no test
can. It catches the copy-paste that brings a known lie back and the obvious new
one in the same mould; it is a floor, not a proof.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from app.services import industry_verticals, outreach_templates

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic" / "versions" / "074_honest_legacy_sequence_copy.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("m074", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _shipped_copy() -> list[tuple[str, str]]:
    """Every subject and body we send, as (source label, text).

    Deliberately reads the constants rather than the files: a module docstring
    that says "we do not invent a 12-press shop in Michigan" is the rule, not a
    breach of it, and scanning source text cannot tell the two apart.
    """
    out: list[tuple[str, str]] = []

    for _, _, subject, body in outreach_templates.MOLDING_SEQUENCE_STEPS:
        out.append(("outreach_templates", subject))
        out.append(("outreach_templates", body))
    for step_number, variants in outreach_templates.SUBJECT_AB_VARIANTS.items():
        for variant in variants:
            out.append((f"outreach_templates A/B step {step_number}", variant))

    for vertical in industry_verticals.VERTICALS:
        for _, _, subject, body in vertical.steps:
            out.append((f"vertical {vertical.key}", subject))
            out.append((f"vertical {vertical.key}", body))
        for step_number, variants in vertical.subject_ab.items():
            for variant in variants:
                out.append((f"vertical {vertical.key} A/B step {step_number}", variant))

    for name, step_number, subject, body in _load_migration().REWRITES:
        label = f"074 {name} step {step_number}"
        if subject is not None:
            out.append((label, subject))
        out.append((label, body))

    return out


# The exact inventions removed by migration 074. Every one was presented to real
# prospects as something that happened. None of it did.
BANNED_FABRICATIONS = [
    "8.2%", "0.9%",            # "cut their scrap from 8.2% to 0.9% in the first week"
    "6.1%", "1.4%",            # a before/after that never happened
    "$47,000", "$47K",         # "the $47,000 mistake"
    "$340,000", "$340K",       # the invented Michigan testimonial
    "$87,000", "$87K",
    "$31K", "$31,000",         # "saved them $31K in the first month"
    "$40K", "$40,000",         # "before it cost $40K"
    "$2.1M", "$2.1 million",   # the account an invented Indiana shop lost
    "$300K+",                  # "{{company_name}} is probably losing $300K+"
]

# Shapes a fabrication arrives in. These are the moulds the removed copy used —
# an anonymous customer is still an invented customer under 16 CFR 255.
FABRICATION_SHAPES = [
    (
        re.compile(r"\b(?:a|one)\s+(?:\d+-press\s+)?(?:shop|molder|operation)\s+in\s+[A-Z][a-z]+", re.I),
        "a case study attributed to a shop in a named place",
    ),
    (
        re.compile(r"\b(?:one|a)\s+(?:shop|customer|molder)\s+(?:told me|said)\b", re.I),
        "a quote attributed to an unnamed customer",
    ),
    (
        re.compile(r"\bshops?\s+in\s+your\s+(?:region|area|state)\b", re.I),
        "a claim about specific nearby shops",
    ),
    (
        re.compile(r"\bcheck ring (?:at|that was)\s+\d+%\s+worn?\b", re.I),
        "an invented finding with fabricated precision",
    ),
    (
        re.compile(r"\bour customers?\b.{0,40}\btold me\b", re.I),
        "a customer testimonial",
    ),
]


@pytest.mark.parametrize("source, text", _shipped_copy())
def test_no_fabricated_metric_survives_in_shipped_copy(source, text):
    for banned in BANNED_FABRICATIONS:
        assert banned not in text, (
            f"{source} contains {banned!r} — a fabricated result removed in "
            f"migration 074. Numbers must be estimates the reader runs on their "
            f"own inputs, never a result we claim someone got."
        )


@pytest.mark.parametrize("source, text", _shipped_copy())
def test_no_invented_customer_story_in_shipped_copy(source, text):
    for pattern, what in FABRICATION_SHAPES:
        match = pattern.search(text)
        assert match is None, (
            f"{source} reads like {what}: {match.group(0)!r}. Social proof is the "
            f"pattern across the industry, not a story about a customer we do not "
            f"have."
        )


def test_the_scan_actually_catches_the_copy_it_was_written_for():
    """Guard the guard.

    Every assertion above passes trivially against an empty scan or a broken
    regex, and a copy-honesty test that silently checks nothing is worse than
    none — it reads like coverage. Pin it against the real removed sentences.
    """
    original_v3_step_1 = (
        "Not SPC after the fact — during the shot. One shop cut their scrap on a "
        "problem mold from 8.2% to 0.9% in the first week."
    )
    original_v6_step_2 = (
        "A 12-press shop in Michigan cut their scrap cost from $340,000 to "
        "$87,000 in six months after installing our system."
    )

    assert any(b in original_v3_step_1 for b in BANNED_FABRICATIONS)
    assert any(b in original_v6_step_2 for b in BANNED_FABRICATIONS)
    assert any(p.search(original_v6_step_2) for p, _ in FABRICATION_SHAPES)


def test_the_scan_reaches_every_body_it_claims_to():
    """A silent import failure would empty the parametrize list and pass."""
    sources = {source for source, _ in _shipped_copy()}

    assert any(s.startswith("074 ") for s in sources), "migration copy not scanned"
    assert any(s.startswith("vertical ") for s in sources), "vertical copy not scanned"
    assert "outreach_templates" in sources
    # 6 sequences were rewritten; every one must be under the scan.
    rewritten = {s for s in sources if s.startswith("074 ")}
    assert len(rewritten) >= 17, f"only {len(rewritten)} rewritten steps scanned"
