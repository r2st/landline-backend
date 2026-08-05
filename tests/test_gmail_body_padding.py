"""Gmail strips the base64url padding, and three of four decoders didn't restore it.

``base64.urlsafe_b64decode`` raises ``binascii.Error: Incorrect padding`` unless
the encoded length is a multiple of 4, and an *unpadded* encoded length is a
multiple of 4 for only one raw byte-length in three. Gmail returns
``body.data`` unpadded. So decoding it inline fails on roughly **two message
bodies in three**, selected by nothing but the message's length.

What made it survive is where each of the four readers put the failure:

* ``email_history`` padded, so the inbox — the surface anyone looks at — was
  fine, and the ground looked covered.
* ``bounce_ingestion`` and ``complaint_ingestion`` wrapped the decode in
  ``except Exception: pass`` and fell back to Gmail's ~100-character
  ``snippet``. A DSN's ``Final-Recipient`` and an ARF report's
  ``Original-Rcpt-To`` live in the body, so the sweep logged "matched the query
  but parsed no recipient" and moved on: no ``BounceRecord``, no suppression,
  and the sequence kept mailing a dead mailbox — while reporting a clean sweep.
* ``smart_reply`` didn't catch, so it raised instead: an ordinary prospect reply
  took the auto-responder down rather than being answered.

The lengths in these tests are chosen, not incidental. A test body that happens
to encode to a multiple of 4 passes against the broken decoder, which is exactly
how this went unnoticed — ``tests/test_gmail_sync_offloaded.py``'s "Sounds
good." is one of those.
"""

import base64

import pytest

from app.services.bounce_ingestion import BounceIngestionService, parse_bounce
from app.services.complaint_ingestion import ComplaintIngestionService
from app.services.email_history import EmailHistoryService
from app.services.gmail_body import decode_body
from app.services.smart_reply import SmartReplyService


def _gmail_b64(text: str) -> str:
    """Encode the way Gmail does: base64url, padding stripped."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# Raw lengths 0/1/2 mod 3 → encoded lengths 0/2/3 mod 4. Only the first decodes
# without padding, so a one-length test proves almost nothing.
_LENGTHS = [30, 31, 32]


@pytest.mark.parametrize("n", _LENGTHS)
def test_decode_body_handles_every_length_class(n: int):
    body = "x" * n
    assert decode_body(_gmail_b64(body)) == body


@pytest.mark.parametrize("n", _LENGTHS)
def test_padded_input_still_decodes(n: int):
    """Some clients do pad. Restoring padding must not corrupt what has it."""
    body = "x" * n
    padded = base64.urlsafe_b64encode(body.encode()).decode()
    assert decode_body(padded) == body


def test_decode_body_on_nothing():
    assert decode_body(None) == ""
    assert decode_body("") == ""


def test_a_bad_byte_costs_a_character_not_the_message():
    """A bounce with one undecodable byte still names the address that bounced."""
    raw = "Final-Recipient: rfc822; dead@prospect.co\n".encode() + b"\xff\xfe"
    decoded = decode_body(base64.urlsafe_b64encode(raw).decode().rstrip("="))
    assert "dead@prospect.co" in decoded


# --------------------------------------------------------------------------- #
# The four readers
# --------------------------------------------------------------------------- #

_DSN = """\
Your message could not be delivered.

Final-Recipient: rfc822; {email}
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 User unknown
{pad}"""


def _dsn(email: str, pad: str) -> str:
    return _DSN.format(email=email, pad=pad)


@pytest.mark.parametrize("pad", ["", ".", ".."])
def test_the_bounce_sweep_reads_the_dsn_body_at_every_length(pad: str):
    """The recipient comes out of the body whatever the body's length.

    Without the fix this is the sweep's whole failure: two of these three cases
    decode to nothing, ``_message_text`` returns only the snippet, and
    ``parse_bounce`` finds no recipient — so the address is never suppressed.
    """
    text = _dsn("dead@prospect.co", pad)
    msg = {
        "snippet": "Delivery Status Notification (Failure)",
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _gmail_b64(text)},
        },
    }

    extracted = BounceIngestionService()._message_text(msg)
    assert "Final-Recipient" in extracted, (
        "the DSN body decoded to nothing — only the snippet survived, and the "
        "snippet does not carry the bounced address"
    )
    parsed = parse_bounce(extracted)
    assert parsed is not None and parsed["email"] == "dead@prospect.co", parsed


@pytest.mark.parametrize("pad", ["", ".", ".."])
def test_the_complaint_sweep_reads_the_report_body_at_every_length(pad: str):
    report = (
        "Feedback-Type: abuse\n"
        "User-Agent: SomeGenerator/1.0\n"
        "Version: 1\n"
        f"Original-Rcpt-To: angry@prospect.co\n{pad}"
    )
    msg = {
        "snippet": "This is an email abuse report",
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _gmail_b64(report)},
        },
    }

    extracted = ComplaintIngestionService()._message_text(msg)
    assert "Original-Rcpt-To" in extracted, extracted


@pytest.mark.parametrize("pad", ["", ".", ".."])
def test_the_inbox_reads_the_reply_body_at_every_length(pad: str):
    """The one reader that already padded — kept honest through the shared decoder."""
    body = f"Sounds good, let's talk Tuesday.{pad}"
    text, _ = EmailHistoryService()._extract_body({
        "mimeType": "text/plain",
        "body": {"data": _gmail_b64(body)},
    })
    assert text == body


@pytest.mark.parametrize("pad", ["", ".", ".."])
def test_the_auto_responder_reads_the_reply_body_at_every_length(pad: str):
    """``smart_reply`` didn't catch, so this was a raise, not a silent empty."""
    body = f"Sounds good, let's talk Tuesday.{pad}"
    assert SmartReplyService()._extract_body({
        "mimeType": "text/plain",
        "body": {"data": _gmail_b64(body)},
    }) == body


@pytest.mark.parametrize("pad", ["", ".", ".."])
def test_the_auto_responder_reads_a_multipart_reply(pad: str):
    """The multipart leg decodes separately and had the same bug."""
    body = f"Sounds good, let's talk Tuesday.{pad}"
    assert SmartReplyService()._extract_body({
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _gmail_b64(body)}},
            {"mimeType": "text/html", "body": {"data": _gmail_b64(f"<p>{body}</p>")}},
        ],
    }) == body


def test_every_gmail_body_reader_goes_through_the_one_decoder():
    """A fifth reader that hand-rolls the decode shows up here, not in the mail.

    The bug was four copies of one decode and one of them being right. The
    guard against a fifth is that ``base64`` decoding of a Gmail body appears
    in exactly one module.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "gmail_body.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "urlsafe_b64decode" in text:
            offenders.append(str(path.relative_to(root)))

    assert offenders == [], (
        "these modules decode base64url themselves instead of using "
        f"gmail_body.decode_body: {offenders}"
    )
