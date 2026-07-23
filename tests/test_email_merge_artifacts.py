"""Tests for merge-artifact repair in rendered email bodies.

Every first-touch cold email to a contact with no first name on record went out
opening with a literal ``"Hi ,"`` — the most obvious "broken automated mail"
tell an email can carry. ``clean_subject`` only ever covered subject lines;
``clean_body`` is the body-side counterpart.
"""

import pytest

from app.services.email_compose import (
    build_merge_context,
    clean_body,
    clean_subject,
    render,
    subject_context,
)


class _Contact:
    def __init__(self, first_name=None, last_name=None, title=None):
        self.first_name = first_name
        self.last_name = last_name
        self.title = title


class _Company:
    def __init__(self, name="Acme CNC", industry_segment="CNC Machining", city="Cleveland"):
        self.name = name
        self.industry_segment = industry_segment
        self.city = city


# ─── clean_body ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body,expected",
    [
        ("<p>Hi ,</p>", "<p>Hi there,</p>"),
        ("<p>Hello ,</p>", "<p>Hello there,</p>"),
        ("<p>Hey ,</p>", "<p>Hey there,</p>"),
        ("<p>Dear ,</p>", "<p>Dear there,</p>"),
        # Extra whitespace between greeting and comma.
        ("<p>Hi   ,</p>", "<p>Hi there,</p>"),
        ("Hi\t,", "Hi there,"),
    ],
)
def test_repairs_empty_greeting(body, expected):
    assert clean_body(body) == expected


def test_preserves_a_real_greeting():
    body = "<p>Hi Dana,</p><p>Every shop has the job that makes the floor tense.</p>"
    assert clean_body(body) == body


def test_is_idempotent():
    once = clean_body("<p>Hi ,</p>")
    assert clean_body(once) == once


def test_closes_up_space_stranded_before_comma():
    # An emptied mid-sentence merge field leaves "the shop , you run".
    assert clean_body("the shop , you run") == "the shop, you run"


def test_empty_body_is_safe():
    assert clean_body("") == ""
    assert clean_body(None) == ""


def test_custom_fallback_name():
    assert clean_body("<p>Hi ,</p>", fallback_name="friend") == "<p>Hi friend,</p>"


def test_does_not_touch_prose_containing_the_greeting_word():
    body = "<p>We said hi, then left.</p>"
    assert clean_body(body) == body


# ─── The production regression, end to end ───────────────────────────────────


def test_nameless_contact_renders_a_clean_first_touch():
    """The exact shape that went out 15 times: no first name, real company.

    Subject must not open with a bare comma and the body must not open "Hi ,".
    """
    subject_tpl = "{{first_name}}, who eats it when a part runs out of tol?"
    body_tpl = "<p>Hi {{first_name}},</p><p>At {{company_name}}, you know the one.</p>"

    ctx = build_merge_context(_Contact(first_name=None), _Company(name="Chicago CNC"))

    subject = clean_subject(render(subject_tpl, subject_context(ctx)))
    body = clean_body(render(body_tpl, ctx))

    # Subject drops the name entirely — "there, who eats it..." reads worse than
    # simply leading with the question.
    assert not subject.startswith(",")
    assert subject == "who eats it when a part runs out of tol?"
    # Body keeps the friendly generic greeting.
    assert "Hi ," not in body
    assert "Hi there," in body
    assert "At Chicago CNC," in body


def test_subject_context_keeps_a_real_first_name():
    ctx = build_merge_context(_Contact(first_name="Dana"), _Company())
    subject = clean_subject(render("{{first_name}}, quick question", subject_context(ctx)))
    assert subject == "Dana, quick question"


def test_subject_context_leaves_other_fields_intact():
    ctx = build_merge_context(_Contact(first_name=None), _Company(name="Chicago CNC"))
    sctx = subject_context(ctx)
    assert sctx["first_name"] == ""
    assert sctx["company_name"] == "Chicago CNC"
    assert sctx["industry"] == "CNC Machining"


def test_named_contact_is_unaffected():
    body_tpl = "<p>Hi {{first_name}},</p>"
    ctx = build_merge_context(_Contact(first_name="Dana"), _Company())
    assert clean_body(render(body_tpl, ctx)) == "<p>Hi Dana,</p>"


def test_blank_string_first_name_is_treated_as_missing():
    ctx = build_merge_context(_Contact(first_name="   "), _Company())
    assert ctx["first_name"] == "there"


def test_sample_context_previews_as_a_named_contact():
    """The template-preview sample must not lose its name in subject rendering."""
    from app.services.email_compose import SAMPLE_CONTEXT

    subject = clean_subject(
        render("{{first_name}}, quick question", subject_context(dict(SAMPLE_CONTEXT)))
    )
    assert subject == "Jordan, quick question"
