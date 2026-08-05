"""Decoding a Gmail message body, in one place.

The Gmail API returns a message part's ``body.data`` as base64url **with the
padding stripped**. ``base64.urlsafe_b64decode`` raises
``binascii.Error: Incorrect padding`` unless the encoded length is a multiple of
4 — and an unpadded length is a multiple of 4 for only one raw length in three.
So this raises on roughly **two thirds of every message body Gmail returns**,
and which third a given message falls in is decided by its byte length: nothing
about the message being ordinary or malformed, and nothing an operator can see.

Four readers of Gmail bodies existed and one of them padded:

- ``email_history._decode_base64url`` did, which is why the inbox worked and
  this looked like solved ground.
- ``bounce_ingestion._message_text`` and ``complaint_ingestion._message_text``
  did not, and swallow the error (``except Exception: pass``), falling back to
  the ~100-character ``snippet``. A DSN's ``Final-Recipient`` and an ARF
  report's ``Original-Rcpt-To`` are in the body, not the snippet, so the scan
  logged "matched the query but parsed no recipient" and moved on: the bounce
  was never recorded, the address never suppressed, and the sequence kept
  mailing a dead mailbox — the exact loop ``bounce_ingestion`` exists to close,
  failing two times in three while reporting a clean sweep.
- ``smart_reply._extract_body`` did not and does not catch, so it raised
  instead — the louder failure, and the less costly one.

The shape is worth remembering: an exception swallowed by a defensive
``except`` around a *parser* reads as "this message had nothing in it", which is
indistinguishable from the quiet, correct case. Nothing here was ever going to
show up as an error rate.
"""

import base64
from typing import Optional, Union


def decode_body(data: Optional[Union[str, bytes]]) -> str:
    """Decode one Gmail ``body.data`` value to text.

    Restores the padding Gmail strips, and never raises on content: an
    undecodable byte becomes U+FFFD rather than losing the whole message, since
    a bounce notification with one bad byte still names the address that
    bounced.
    """
    if not data:
        return ""
    if isinstance(data, bytes):
        data = data.decode("ascii", "replace")
    # ``-len % 4`` is 0 when the length is already a multiple of 4. The obvious
    # ``4 - len % 4`` appends a spurious four there; b64decode happens to
    # tolerate it, which is not a property to rely on.
    padding = -len(data) % 4
    return base64.urlsafe_b64decode(data + "=" * padding).decode("utf-8", "replace")
