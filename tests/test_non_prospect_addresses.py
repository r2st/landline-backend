"""Tests for the non-prospect (scraper-residue) address gate.

These addresses pass both existing gates — they are syntactically valid and
their domains genuinely publish MX — so they were enrolled and emailed. They
are machine mailboxes that no human reads, so every send is spent reputation
for a guaranteed zero reply.
"""

import pytest

from app.services.email_validation import is_non_prospect_address, validate_email


@pytest.mark.parametrize(
    "email",
    [
        # Sentry's error-reporting address, scraped out of a Wix site's JS.
        # This one really was enrolled and emailed in production.
        "460ff4620fa44cba8df530afde949785@sentry.wixpress.com",
        "2062d0a4929b45348643784b5cb39c36@sentry.wixpress.com",
        "anything@sentry.io",
        # Parent-domain match.
        "someone@wixpress.com",
    ],
)
def test_rejects_machine_only_domains(email):
    is_junk, reason = is_non_prospect_address(email)
    assert is_junk
    assert reason


@pytest.mark.parametrize(
    "email",
    ["logo@2x.png", "banner@hero.jpg", "sprite@icons.svg", "app@bundle.js"],
)
def test_rejects_asset_filenames(email):
    is_junk, reason = is_non_prospect_address(email)
    assert is_junk
    assert "asset filename" in reason


def test_rejects_hex_blob_local_part():
    is_junk, reason = is_non_prospect_address(
        "460ff4620fa44cba8df530afde949785@example.com"
    )
    assert is_junk
    assert "machine-generated" in reason


@pytest.mark.parametrize(
    "email",
    [
        "no-reply@example.com",
        "noreply@example.com",
        "do-not-reply@example.com",
        "donotreply@example.com",
        "mailer-daemon@example.com",
        "postmaster@example.com",
        "abuse@example.com",
    ],
)
def test_rejects_unattended_mailboxes(email):
    is_junk, _ = is_non_prospect_address(email)
    assert is_junk


@pytest.mark.parametrize(
    "email",
    [
        # Role addresses are weak prospects but real inboxes a person may read —
        # deliberately NOT rejected here.
        "info@chicagocncmachining.com",
        "sales@cncmfg.com",
        "quotes@zyci.com",
        "accounts@machinetechs.com",
        "contact@cncmetaltech.com",
        # Ordinary human addresses.
        "liem@ltmachininginc.com",
        "kent.coomer@demachine.com",
        "bao.nguyen@atmachining.us",
        # A short hex-ish local part is a plausible human handle, not a blob.
        "abc123@example.com",
    ],
)
def test_accepts_real_prospects(email):
    is_junk, reason = is_non_prospect_address(email)
    assert not is_junk, f"{email} wrongly rejected: {reason}"
    assert reason == ""


def test_empty_and_malformed_are_rejected():
    assert is_non_prospect_address(None)[0]
    assert is_non_prospect_address("")[0]
    assert is_non_prospect_address("not-an-address")[0]


def test_validate_email_rejects_junk_without_a_dns_lookup(monkeypatch):
    """The junk gate runs before MX, so no network call is made for junk."""
    from app.services import email_validation

    def _boom(domain):  # pragma: no cover - must never be reached
        raise AssertionError("MX lookup should not run for a junk address")

    monkeypatch.setattr(email_validation, "check_mx", _boom)
    assert validate_email("460ff4620fa44cba8df530afde949785@sentry.wixpress.com") == "invalid"


def test_validate_email_still_consults_mx_for_a_real_address(monkeypatch):
    from app.services import email_validation

    monkeypatch.setattr(email_validation, "check_mx", lambda domain: "valid")
    assert validate_email("liem@ltmachininginc.com") == "valid"
