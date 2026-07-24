"""Unit tests for the web scraper's email extraction and ranking.

These cover the pure helpers only — no network. The behaviour under test is the
2026-07 fix: generic inboxes (``info@``/``sales@``/``contact@``) are the most
common — and often the only — address on a small manufacturer's site, so they
are kept as a ranked fallback instead of being discarded, while genuinely
un-sendable technical inboxes are still dropped.
"""

from app.services.web_scraper import (
    extract_emails,
    usable_emails,
)


def test_generic_inbox_is_kept_not_discarded():
    # The old behaviour dropped info@ outright, stranding shops that list only
    # a generic inbox. It must now survive as a usable (if lower-ranked) address.
    assert usable_emails(["info@acme-molding.com"]) == ["info@acme-molding.com"]
    assert usable_emails(["sales@acme.com"]) == ["sales@acme.com"]
    assert usable_emails(["contact@acme.com"]) == ["contact@acme.com"]


def test_personal_address_ranks_ahead_of_generic():
    got = usable_emails(
        ["info@acme.com", "sara.lee@acme.com", "sales@acme.com"]
    )
    assert got[0] == "sara.lee@acme.com"
    # both generics still present, just ranked behind the person
    assert set(got[1:]) == {"info@acme.com", "sales@acme.com"}


def test_technical_inboxes_are_dropped():
    raw = [
        "noreply@acme.com",
        "no-reply@acme.com",
        "postmaster@acme.com",
        "webmaster@acme.com",
        "abuse@acme.com",
        "mailer-daemon@acme.com",
        "unsubscribe@acme.com",
        "sales@acme.com",
    ]
    assert usable_emails(raw) == ["sales@acme.com"]


def test_asset_false_positives_dropped():
    assert usable_emails(["logo@2x.png", "sprite@3x.jpg", "info@acme.com"]) == [
        "info@acme.com"
    ]


def test_dedupe_and_case_fold():
    assert usable_emails(["Info@Acme.com", "info@acme.com", "INFO@ACME.COM"]) == [
        "info@acme.com"
    ]


def test_extract_emails_from_plain_html():
    html = '<a href="mailto:jane@acme.com">email jane</a> or bob@acme.com'
    assert extract_emails(html) == {"jane@acme.com", "bob@acme.com"}


def test_extract_emails_deobfuscates_bracketed_forms():
    # "name [at] domain [dot] com" style obfuscation is common on contact pages.
    html = "reach us: owner [at] machineshop [dot] com"
    assert "owner@machineshop.com" in extract_emails(html)


def test_extract_emails_unescapes_html_entities():
    # &#64; is an HTML-entity-encoded @ sign.
    html = "sales&#64;acme.com"
    assert "sales@acme.com" in extract_emails(html)


def test_ordinary_prose_is_not_mangled_into_an_email():
    # "at"/"dot" only rewrite inside brackets, so prose stays prose.
    html = "Come see us at the shop. We are open."
    assert extract_emails(html) == set()


def test_limit_is_respected():
    raw = [f"person{i}@acme.com" for i in range(20)]
    assert len(usable_emails(raw, limit=5)) == 5
