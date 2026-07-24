"""Apollo running out of credits is a budget condition, not a request bug.

Apollo answers HTTP 422 both for a malformed request and for "you have
insufficient credits", so the status code alone cannot tell an operator which
one is happening. Production logged ten identical

    Apollo enrich error after 1 attempt(s): HTTP 422: {"error":"You have
    insufficient credits! …"}

lines every five minutes for days (observed 2026-07-24). Enrichment — the hop
that turns a discovered business into an emailable person — was completely dead,
and it read in the logs as a recurring bug in our own payload.

Mapping it onto the existing ``budget_exhausted`` vocabulary means it says what
it is, and a caller that already understands that status can stop walking the
batch instead of asking the same dead question once per contact.
"""

import httpx
import pytest

from app.services.enrichment import _is_provider_credit_exhaustion

APOLLO_NO_CREDITS = (
    '{"error":"You have insufficient credits! '
    "<a href='https://app.apollo.io/#/settings/plans/upgrade' "
    "aria-onclick='close_alert'>Upgrade your plan</a> to increase your number "
    'of lead credits."}'
)


def _response(status, body):
    return httpx.Response(
        status_code=status, text=body, request=httpx.Request("POST", "https://x/y")
    )


def test_the_exact_production_body_is_recognised():
    assert _is_provider_credit_exhaustion(_response(422, APOLLO_NO_CREDITS)) is True


@pytest.mark.parametrize(
    "body",
    [
        '{"error":"You are out of credits"}',
        '{"error":"No credits remaining on this plan"}',
        '{"error":"Credit limit reached"}',
        '{"error":"INSUFFICIENT CREDITS"}',
    ],
)
def test_other_phrasings_are_recognised(body):
    assert _is_provider_credit_exhaustion(_response(422, body)) is True


def test_a_real_request_fault_is_not_a_budget_condition():
    """The 422 this endpoint used to return for a misplaced key.

    Misreading this as "out of credits" would tell an operator to buy credits
    they already have, for a bug in our own request.
    """
    body = '{"error":"API key must be passed in the X-Api-Key header"}'
    assert _is_provider_credit_exhaustion(_response(422, body)) is False


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_only_422_is_considered(status):
    """A 429 is rate limiting and a 401 is a bad key; neither is a credit verdict."""
    assert _is_provider_credit_exhaustion(_response(status, APOLLO_NO_CREDITS)) is False


def test_an_empty_body_is_not_a_budget_condition():
    assert _is_provider_credit_exhaustion(_response(422, "")) is False
