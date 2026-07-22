"""Tests for the name-merge personalization fixes (email pipeline audit #1).

A missing/blank first name must never render "Hi ," — the greeting falls back to
"there", and a subject that opened with an emptied merge field must not start
with a dangling comma.
"""

import pytest

from app.services.ai_email_generator import _fallback_response
from app.services.email_compose import build_merge_context, clean_subject, render


class _Obj:
    """Minimal stand-in for a Contact/Company (build_merge_context uses getattr)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


# ─── build_merge_context: greeting fallback ──────────────────────────────────


def test_blank_first_name_falls_back_to_there():
    contact = _Obj(first_name="", last_name="Avery", title="VP")
    company = _Obj(name="Acme", industry_segment="CNC", city="Cleveland")
    ctx = build_merge_context(contact, company)
    assert ctx["first_name"] == "there"
    # "Hi {{first_name}}," now renders "Hi there," not "Hi ,".
    assert render("Hi {{first_name}},", ctx) == "Hi there,"


def test_whitespace_only_first_name_falls_back():
    ctx = build_merge_context(_Obj(first_name="   ", last_name=""), _Obj(name="Acme"))
    assert ctx["first_name"] == "there"


def test_none_first_name_falls_back():
    ctx = build_merge_context(_Obj(first_name=None, last_name="Avery"), _Obj(name="Acme"))
    assert ctx["first_name"] == "there"


def test_real_first_name_is_preserved():
    ctx = build_merge_context(_Obj(first_name="Dana", last_name="Lee"), _Obj(name="Acme"))
    assert ctx["first_name"] == "Dana"


def test_full_name_never_uses_the_there_fallback():
    # full_name must be built from real names only, never "there LastName".
    ctx = build_merge_context(_Obj(first_name="", last_name="Avery"), _Obj(name="Acme"))
    assert ctx["first_name"] == "there"
    assert ctx["full_name"] == "Avery"

    ctx2 = build_merge_context(_Obj(first_name="", last_name=""), _Obj(name="Acme"))
    assert ctx2["full_name"] == ""


def test_no_contact_still_falls_back():
    ctx = build_merge_context(None, _Obj(name="Acme"))
    assert ctx["first_name"] == "there"


# ─── clean_subject: strip dangling leading punctuation ───────────────────────


def test_clean_subject_strips_leading_comma():
    assert clean_subject(", who eats the scrap cost?") == "who eats the scrap cost?"


def test_clean_subject_strips_leading_comma_and_space_variants():
    assert clean_subject("  , quick question") == "quick question"
    assert clean_subject("- follow up") == "follow up"


def test_clean_subject_leaves_clean_subjects_alone():
    assert clean_subject("Quick question about Acme") == "Quick question about Acme"


def test_clean_subject_collapses_double_space_from_empty_merge():
    # "Quick question for {{company}} team" with empty company → double space.
    assert clean_subject("Quick question for  team") == "Quick question for team"


def test_clean_subject_handles_empty():
    assert clean_subject("") == ""
    assert clean_subject(None) == ""


def test_rendered_subject_with_blank_name_then_cleaned():
    ctx = build_merge_context(_Obj(first_name="", last_name=""), _Obj(name="Acme"))
    # A subject template keyed on first name — with the "there" fallback it no
    # longer produces a leading comma, and clean_subject is a safe no-op.
    rendered = render("{{first_name}}, who eats it?", ctx)
    assert clean_subject(rendered) == "there, who eats it?"


# ─── ai_email_generator fallback: empty-string first_name ────────────────────


def test_fallback_response_empty_first_name_uses_there():
    # ai_compose used to pass first_name="" which made this render "Hi ,".
    result = _fallback_response({"first_name": "", "company_name": "Acme"}, "first_touch")
    assert result["body"].startswith("Hi there,")
    assert result["subject"].startswith("there,")
    assert "Hi ," not in result["body"]


def test_fallback_response_real_first_name():
    result = _fallback_response({"first_name": "Dana", "company_name": "Acme"}, "first_touch")
    assert result["body"].startswith("Hi Dana,")
