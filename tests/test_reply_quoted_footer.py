"""A quoted copy of our own footer is not the prospect asking to opt out.

The outbound CAN-SPAM footer (``email_sender._build_unsubscribe_footer``) reads:

    Don't want these emails? Reply UNSUBSCRIBE or click: https://…

Mail clients quote the message being replied to, so those words arrive back
inside **every genuine reply**. ``keyword_prefilter`` scanned the whole body for
``\\bunsubscribe\\b``, matched our own sentence, and returned "unsubscribe" with
confidence 1.0 — which is compliance-critical and therefore never second-guessed
by the LLM. ``route_auto_action`` then suppressed the contact, stopped every
enrollment, and emailed an unsubscribe audit line.

Observed in production 2026-07-24 while auditing why Dialtone had no replies: a
real answer to "Suman, quick question about Apprend Tech…" was recorded as
``reply_classification=unsubscribe``, ``handled_action=unsubscribed``, and the
contact was suppressed. The single most valuable event in the system was being
converted into an opt-out.

The fix reads only what the sender typed. Both directions are pinned below: a
quoted footer must NOT opt anyone out, and a human who really does type
"unsubscribe" still must.
"""

import pytest

from app.services.ai_service import keyword_prefilter, strip_quoted_reply

FOOTER = (
    "\n\n—\n"
    "Apprend Technologies\n"
    "Don't want these emails? Reply UNSUBSCRIBE or click: "
    "https://landline.apprendtechnologies.com/unsubscribe?token=abc123\n"
    "123 Main St, Toledo, OH"
)

GMAIL_QUOTE = (
    "On Tue, Jul 22, 2026 at 4:02 PM Suman Das <suman.das@apprend.tech> wrote:\n"
    "> Hi Dave,\n"
    ">\n"
    "> who notices first when a cavity starts drifting?\n"
    f"> {FOOTER}"
)


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------
def test_genuine_reply_quoting_our_footer_is_not_an_unsubscribe():
    body = f"Sounds interesting — can you send more detail?\n\n{GMAIL_QUOTE}"
    assert keyword_prefilter("Re: quick question", body) is None


def test_interested_reply_quoting_the_footer_reaches_the_llm_not_suppression():
    """The exact production shape, end to end through the prefilter."""
    body = (
        "Yes, we've had cavity drift on two tools this quarter. "
        "What does the pilot involve?\n\n" + GMAIL_QUOTE
    )
    assert keyword_prefilter("Re: cavity 4", body) != "unsubscribe"


@pytest.mark.parametrize(
    "quote_header",
    [
        "On Tue, Jul 22, 2026 at 4:02 PM Suman <s@apprend.tech> wrote:",
        "-----Original Message-----",
        "_" * 32,
        "From: Suman Das <suman.das@apprend.tech>",
        "Don't want these emails? Reply UNSUBSCRIBE or click: https://x/y",
    ],
)
def test_every_client_quote_marker_ends_the_authored_text(quote_header):
    body = f"Happy to talk next week.\n{quote_header}\nunsubscribe\n"
    assert keyword_prefilter("Re: hello", body) is None


# ---------------------------------------------------------------------------
# True positives must survive
# ---------------------------------------------------------------------------
def test_a_human_typed_unsubscribe_still_opts_them_out():
    body = f"Please unsubscribe me from this list.\n\n{GMAIL_QUOTE}"
    assert keyword_prefilter("Re: quick question", body) == "unsubscribe"


@pytest.mark.parametrize(
    "phrase",
    [
        "unsubscribe",
        "take me off your list",
        "remove me from your mailing list",
        "stop emailing me",
        "please opt me out",
        "do not contact me again",
    ],
)
def test_opt_out_language_above_the_quote_still_wins(phrase):
    assert keyword_prefilter("Re: hi", f"{phrase}\n\n{GMAIL_QUOTE}") == "unsubscribe"


def test_unsubscribe_in_the_subject_still_wins():
    """Subject lines are never quoted, so they are always the sender's words."""
    assert keyword_prefilter("UNSUBSCRIBE", f"thanks\n{GMAIL_QUOTE}") == "unsubscribe"


# ---------------------------------------------------------------------------
# Ordering and neighbouring categories are unchanged
# ---------------------------------------------------------------------------
def test_a_bounce_is_still_a_bounce_not_an_unsubscribe():
    """Unchanged precedence: a DSN echoes the footer too."""
    assert (
        keyword_prefilter(
            "Delivery Status Notification (Failure)",
            f"550 5.1.1 user unknown\n{FOOTER}",
            "mailer-daemon@googlemail.com",
        )
        == "bounce"
    )


def test_out_of_office_still_detected_below_a_quote():
    """OOO deliberately keeps scanning the full body — see keyword_prefilter."""
    body = f"I am out of the office until August 3.\n\n{GMAIL_QUOTE}"
    assert keyword_prefilter("Automatic reply: hi", body) == "out_of_office"


# ---------------------------------------------------------------------------
# strip_quoted_reply itself
# ---------------------------------------------------------------------------
def test_strip_keeps_only_what_the_sender_typed():
    assert strip_quoted_reply(f"Two lines\nof mine\n{GMAIL_QUOTE}").strip() == (
        "Two lines\nof mine"
    )


def test_strip_is_total_on_empty_input():
    assert strip_quoted_reply(None) == ""
    assert strip_quoted_reply("") == ""


def test_an_unquoted_reply_is_returned_whole():
    body = "No quote markers here at all, just a short answer."
    assert strip_quoted_reply(body) == body


def test_an_unrecognised_marker_degrades_to_the_old_behaviour():
    """Conservative by design: never drop text the sender actually wrote."""
    body = "My answer.\n~~~~ some client's odd quote rule ~~~~\ntheir quoted text"
    assert "their quoted text" in strip_quoted_reply(body)
