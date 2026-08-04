"""Rule 1 of ``classify_bounce`` read one field, and it was usually the empty one.

The classifier's documented first rule is "a ``4.x`` is transient, whatever its
reason", and it exists so that a greylisting or a throttle mentioning a policy
word is not escalated into a permanent reputation verdict. It was implemented as
``re.match(r"\\s*4", code)`` — ``status_code`` only.

Two lines below it, the ``5.7.x`` rule searches ``code`` **and** the diagnostic,
with a comment saying why: "a DSN often carries the enhanced code only inside
the diagnostic line (``550 5.7.1 …``) while ``status_code`` holds the bare
``550``". That is just as true of the transient class, and more often true of
it, because the callers that pass a diagnostic and no status code at all are the
reply-classification path (which hands over a subject line and a message body)
and every hand-built call in the codebase.

So the exact DSN rule 1 names — a temporary policy refusal — fell through to
rule 4, the loose policy-phrase match, and came back ``blocked``. Gmail's
throttle is the canonical one:

    421-4.7.28 Gmail has detected an unusual rate of unsolicited mail
               originating from your IP address

Transient. Clears on its own. Retried by the sender. And the word "unsolicited"
in it turned a rate limit into a permanent policy block on the deliverability
dashboard, stamped ``last_bounce_type='blocked'`` on the contact, and reported a
sender-reputation incident to an operator who had one busy hour.

The guards matter as much as the rule: an explicit ``5`` status code still wins,
and a diagnostic carrying both classes defers to the permanent one. Calling a
dead address transient leaves it in rotation, which is the failure the whole
module was built to stop, so these pin both directions.
"""

import pytest

from app.services.bounce_handler import classify_bounce, record_bounce
from app.services.suppression import is_suppressed


# --- the rule, applied to a diagnostic-only code -----------------------------

def test_gmail_rate_limit_is_transient_not_a_policy_block():
    """The canonical case: 4.7.x throttle whose text contains 'unsolicited'."""
    assert classify_bounce(
        status_code=None,
        diagnostic=(
            "421-4.7.28 Gmail has detected an unusual rate of unsolicited mail "
            "originating from your IP address."
        ),
    ) == "soft"


def test_greylisting_that_names_a_blocklist_is_still_transient():
    """Most greylisting DSNs cite a blocklist. None of them are permanent."""
    assert classify_bounce(
        status_code=None,
        diagnostic="451 4.7.1 Service unavailable; client host blocked using Spamhaus",
    ) == "soft"


def test_a_bare_transient_smtp_code_opening_the_diagnostic_is_transient():
    """No enhanced code at all — the leading reply code is the only signal."""
    assert classify_bounce(
        status_code=None,
        diagnostic="450 Requested action aborted: policy restrictions apply",
    ) == "soft"


def test_a_transient_code_in_status_still_wins_as_before():
    """The original path is untouched."""
    assert classify_bounce(status_code="4.7.1", diagnostic="policy") == "soft"
    assert classify_bounce(status_code="421", diagnostic="spam") == "soft"


# --- the guards --------------------------------------------------------------

def test_an_explicit_permanent_status_code_beats_a_4_in_the_text():
    """A 5xx status code is authoritative; a stray 4.x.y in the body is not.

    Real DSNs quote the earlier attempt, the original headers, and sometimes a
    software version. None of that may downgrade a permanent rejection.
    """
    assert classify_bounce(
        status_code="550",
        diagnostic="user unknown (previous attempt deferred 4.2.1)",
    ) == "hard"
    assert classify_bounce(
        status_code="5.7.1",
        diagnostic="blocked; earlier attempt was 4.7.0",
    ) == "blocked"


def test_a_permanent_enhanced_code_in_the_text_beats_a_transient_one():
    """Both classes present and no status code → the permanent one decides."""
    assert classify_bounce(
        status_code=None,
        diagnostic="550 5.1.1 user unknown; the 4.2.0 retry also failed",
    ) == "hard"


def test_a_permanent_policy_block_is_still_a_block():
    """The 5.7.x rule is unchanged — this is the case it was added for."""
    assert classify_bounce(
        status_code=None,
        diagnostic="550 5.7.1 Our system has determined this message is likely "
                   "unsolicited mail",
    ) == "blocked"


def test_a_dead_address_is_still_hard():
    """The direction that costs the list if it regresses."""
    assert classify_bounce(
        status_code="550", diagnostic="5.1.1 <dana@acme.com>: user unknown"
    ) == "hard"
    assert classify_bounce(
        status_code=None, diagnostic="550 5.1.1 no such user here"
    ) == "hard"


# --- and what it means downstream --------------------------------------------

@pytest.mark.asyncio
async def test_a_throttle_does_not_stamp_a_contact_as_policy_blocked(db_session):
    """Written against the outcome, not the label.

    A misread throttle suppressed nothing — ``blocked`` is inert on that path —
    so the cost is entirely in what it *says*: the contact carries
    ``last_bounce_type='blocked'`` and the deliverability page reports a
    reputation incident. A soft bounce says what actually happened, and counts
    toward the soft threshold that would eventually retire a genuinely bad
    address.
    """
    summary = await record_bounce(
        db_session,
        "dana@acme.com",
        diagnostic="421-4.7.28 unusual rate of unsolicited mail from your IP",
    )

    assert summary["kind"] == "soft"
    # A lone soft bounce for an address with no contact row lists nothing —
    # transient failure is not evidence of anything.
    assert summary["list_entry_added"] is False
    assert await is_suppressed(db_session, "dana@acme.com") is False
