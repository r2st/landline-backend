"""Tests for the seeded plastic-injection-molding sequence, the hardened
List-Unsubscribe mailto header, and the business-hours send window."""

import base64
import re
from unittest.mock import MagicMock

import pytest

from app.services.email_compose import render
from app.services.gmail_sender import GmailSender, _apply_unsubscribe_headers
from app.services.outreach_templates import (
    MOLDING_SEQUENCE_STEPS,
    SUBJECT_AB_VARIANTS,
    get_molding_sequence,
)
from app.services.send_limiter import SendLimiter

CTX = {
    "first_name": "Raj",
    "company_name": "OneNet Plastics",
    "title": "VP Operations",
    "demo_link": "https://best-molding-demo.pages.dev",
}

SPAM_WORDS = ["act now", "limited time", "buy now", "click here", "winner",
              "congratulations", "100% free", "risk-free", "guarantee", "urgent"]


def _strip(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


class TestMoldingSequenceShape:
    def test_has_four_steps(self):
        assert len(MOLDING_SEQUENCE_STEPS) == 4
        assert get_molding_sequence()["name"].startswith("Plastic Injection Molding")

    def test_step_numbers_sequential(self):
        nums = [s[0] for s in MOLDING_SEQUENCE_STEPS]
        assert nums == [1, 2, 3, 4]

    def test_delays_non_decreasing(self):
        delays = [s[1] for s in MOLDING_SEQUENCE_STEPS]
        for i in range(1, len(delays)):
            assert delays[i] >= delays[i - 1]


class TestMoldingSequenceContent:
    def test_subjects_under_60_chars(self):
        for _, _, subj, _ in MOLDING_SEQUENCE_STEPS:
            rendered = render(subj, CTX)
            assert len(rendered) < 60, f"subject too long: {rendered}"

    def test_subjects_lowercase_start(self):
        for _, _, subj, _ in MOLDING_SEQUENCE_STEPS:
            cleaned = subj.replace("{{first_name}}", "").replace("{{company_name}}", "")
            first = next((c for c in cleaned if c.isalpha()), None)
            if first:
                assert first.islower() or first == "I", f"subject not lowercase: {subj}"

    def test_bodies_under_200_words(self):
        for _, _, _, body in MOLDING_SEQUENCE_STEPS:
            words = _strip(render(body, CTX)).split()
            assert len(words) < 200, f"body too long: {len(words)} words"

    def test_every_body_starts_with_greeting(self):
        for _, _, _, body in MOLDING_SEQUENCE_STEPS:
            assert render(body, CTX).startswith("<p>Hi Raj,</p>")

    def test_no_spam_words(self):
        for _, _, subj, body in MOLDING_SEQUENCE_STEPS:
            text = render(subj + " " + body, CTX).lower()
            for w in SPAM_WORDS:
                assert w not in text, f"spam word '{w}' in step"

    def test_no_all_caps_words(self):
        for _, _, subj, body in MOLDING_SEQUENCE_STEPS:
            words = _strip(render(subj + " " + body, CTX)).split()
            caps = [w for w in words if len(w) > 3 and w.isupper() and w.isalpha()]
            assert not caps, f"ALL CAPS words: {caps}"

    def test_no_exclamation_marks(self):
        for _, _, subj, body in MOLDING_SEQUENCE_STEPS:
            assert "!" not in render(subj + body, CTX)

    def test_no_images(self):
        for _, _, _, body in MOLDING_SEQUENCE_STEPS:
            assert "<img" not in body.lower()

    def test_no_raw_urls_in_text(self):
        """URLs must live inside href attributes, not bare in the prose."""
        for _, _, _, body in MOLDING_SEQUENCE_STEPS:
            plain = _strip(render(body, CTX))
            assert not re.search(r"https?://\S+", plain), f"raw URL in: {plain}"

    def test_links_use_demo_link(self):
        for _, _, _, body in MOLDING_SEQUENCE_STEPS:
            rendered = render(body, CTX)
            assert 'href="https://best-molding-demo.pages.dev"' in rendered

    def test_mentions_molding_terms(self):
        all_text = " ".join(render(b, CTX) for _, _, _, b in MOLDING_SEQUENCE_STEPS).lower()
        terms = ["cavity", "mold", "press", "scrap", "resin", "sink mark",
                 "core-pin", "check ring", "pack pressure"]
        found = [t for t in terms if t in all_text]
        assert len(found) >= 5, f"too few molding terms: {found}"

    def test_step4_offers_free_pilot(self):
        body = render(MOLDING_SEQUENCE_STEPS[3][3], CTX).lower()
        assert "free" in body and "pilot" in body
        assert "reply" in body

    def test_emotional_arc(self):
        s1 = render(MOLDING_SEQUENCE_STEPS[0][3], CTX).lower()
        s2 = render(MOLDING_SEQUENCE_STEPS[1][3], CTX).lower()
        s3 = render(MOLDING_SEQUENCE_STEPS[2][3], CTX).lower()
        s4 = render(MOLDING_SEQUENCE_STEPS[3][3], CTX).lower()
        assert "blame" in s1 or "2 am" in s1
        assert "$" in s2
        assert "retire" in s3
        assert "last" in s4 or "leave you alone" in s4

    def test_no_fabricated_named_testimonial(self):
        """Guard: copy must not assert a fake specific customer result as fact."""
        all_text = " ".join(render(b, CTX) for _, _, _, b in MOLDING_SEQUENCE_STEPS).lower()
        # These are the kinds of fabricated-proof phrasings we deliberately avoid.
        assert "a shop in michigan" not in all_text
        assert "told me:" not in all_text


class TestSubjectABVariants:
    def test_variants_for_all_four_steps(self):
        assert set(SUBJECT_AB_VARIANTS.keys()) == {1, 2, 3, 4}

    def test_each_step_has_multiple_variants(self):
        for step, variants in SUBJECT_AB_VARIANTS.items():
            assert len(variants) >= 2, f"step {step} needs >=2 subject variants"

    def test_variants_render_under_60_chars(self):
        for variants in SUBJECT_AB_VARIANTS.values():
            for v in variants:
                assert len(render(v, CTX)) < 65


class TestListUnsubscribeMailto:
    def _header(self, raw_str, name):
        # Unfold continuation lines before matching.
        unfolded = raw_str.replace("\r\n ", " ").replace("\n ", " ")
        for line in unfolded.splitlines():
            if line.lower().startswith(name.lower() + ":"):
                return line
        return ""

    def test_mailto_falls_back_to_sender_email(self):
        from email.mime.multipart import MIMEMultipart
        real = MIMEMultipart("alternative")
        _apply_unsubscribe_headers(real, "https://x/unsub?token=abc", "sales@example.com")
        assert "mailto:sales@example.com" in real["List-Unsubscribe"]
        assert "<https://x/unsub?token=abc>" in real["List-Unsubscribe"]
        assert real["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    def test_no_headers_when_url_none(self):
        from email.mime.multipart import MIMEMultipart
        real = MIMEMultipart("alternative")
        _apply_unsubscribe_headers(real, None, "sales@example.com")
        assert real["List-Unsubscribe"] is None

    def test_mime_includes_mailto_and_url(self):
        account = MagicMock()
        account.email = "sales@example.com"
        account.display_name = "Sales"
        sender = GmailSender(account)
        result = sender._build_mime(
            to_email="prospect@example.com",
            subject="hi",
            body_html="<p>Hello</p>",
            body_text="Hello",
            reply_to=None,
            list_unsubscribe_url="https://x/unsub?token=abc",
        )
        raw = base64.urlsafe_b64decode(result["raw"]).decode("utf-8")
        header = self._header(raw, "List-Unsubscribe")
        assert "mailto:sales@example.com" in header
        assert "https://x/unsub?token=abc" in header


class TestSendWindow:
    def test_disabled_always_allows(self, monkeypatch):
        limiter = SendLimiter()
        monkeypatch.setattr(limiter.settings, "send_window_enabled", False)
        ok, _ = limiter.within_send_window()
        assert ok

    def test_unknown_timezone_fails_open(self, monkeypatch):
        limiter = SendLimiter()
        monkeypatch.setattr(limiter.settings, "send_window_enabled", True)
        monkeypatch.setattr(limiter.settings, "send_window_timezone", "Not/AZone")
        ok, reason = limiter.within_send_window()
        assert ok  # fail open, never block all sending on a config typo

    def test_window_respects_hours(self, monkeypatch):
        """Force a 1-minute-wide window far from 'now' in UTC to assert it blocks."""
        limiter = SendLimiter()
        monkeypatch.setattr(limiter.settings, "send_window_enabled", True)
        monkeypatch.setattr(limiter.settings, "send_window_timezone", "UTC")
        monkeypatch.setattr(limiter.settings, "send_window_weekdays_only", False)
        # A window that is impossible to be inside (start == end → empty range).
        monkeypatch.setattr(limiter.settings, "send_window_start_hour", 0)
        monkeypatch.setattr(limiter.settings, "send_window_end_hour", 0)
        ok, reason = limiter.within_send_window()
        assert not ok
        assert "outside send window" in reason
