"""Open/click tracking injection for outreach sequence sends.

The sequence path (``app.tasks.outreach.send_sequence_step``) historically sent
mail with no instrumentation at all: no pixel, no wrapped links. Everything
downstream that reads engagement — the ``send_condition`` branches, the sequence
optimizer, per-variant A/B stats — was therefore reading counts that were
structurally zero. This module is what makes those signals real.

Two injections, both keyed to ``(enrollment_id, step_number)`` because that pair
is the sequence path's send identity (there is no ``EmailSend`` row):

* ``inject_click_tracking`` — rewrites outbound ``http(s)`` links to redirect
  through ``/track/click``.
* ``inject_open_pixel``     — appends a 1x1 pixel hitting ``/track/open``.

Order matters: wrap links *first*, then append the pixel, so the pixel's own URL
is never itself rewritten into a click link.

**On the reliability of opens.** Apple Mail Privacy Protection pre-fetches
images, which manufactures opens that no human caused; many other clients block
images entirely, which hides opens that did. So an open is *weak* evidence and
is deliberately not used as a primary optimization signal — see
``sequence_optimizer`` and the A/B engine's ``metric="replied"`` default. Clicks
and replies are the signals worth trusting. We still record opens because they
are useful in aggregate and cost nothing.
"""

import logging
import re
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# href="..." / href='...' with an http(s) target.
_HREF_RE = re.compile(r"""(<a\b[^>]*?\bhref\s*=\s*)(["'])(https?://[^"']+)\2""", re.IGNORECASE)

# Never rewrite these — wrapping them either breaks the link or defeats its
# purpose. Unsubscribe in particular MUST stay a direct link: routing a
# one-click unsubscribe through a redirect risks breaking CAN-SPAM compliance.
_SKIP_URL_MARKERS = ("/track/open", "/track/click", "/track/unsubscribe", "unsubscribe")


def _should_skip(url: str) -> bool:
    """Whether a URL must be left exactly as authored."""
    lowered = url.lower()
    if any(marker in lowered for marker in _SKIP_URL_MARKERS):
        return True
    try:
        scheme = urlparse(url).scheme.lower()
    except Exception:
        return True
    return scheme not in ("http", "https")


def inject_click_tracking(body_html: str, enrollment_id: str, step_number: int) -> str:
    """Rewrite http(s) anchors in ``body_html`` to click-tracking redirects.

    Unsubscribe links, already-tracked links, and non-http schemes (mailto:,
    tel:) are left untouched. Never raises: on any failure the original body is
    returned, because a tracking problem must never stop a send.
    """
    if not body_html:
        return body_html
    try:
        from app.routers.tracking import generate_sequence_click_url

        def _replace(match: re.Match) -> str:
            prefix, quote, url = match.group(1), match.group(2), match.group(3)
            if _should_skip(url):
                return match.group(0)
            wrapped = generate_sequence_click_url(enrollment_id, step_number, url)
            return f"{prefix}{quote}{wrapped}{quote}"

        return _HREF_RE.sub(_replace, body_html)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Click-tracking injection failed for %s: %s", enrollment_id, exc)
        return body_html


def inject_open_pixel(body_html: str, enrollment_id: str, step_number: int) -> str:
    """Append a tracking pixel to ``body_html``.

    Placed before ``</body>`` when present, else appended. Never raises.
    """
    if not body_html:
        return body_html
    try:
        from app.routers.tracking import generate_sequence_pixel_url

        pixel_url = generate_sequence_pixel_url(enrollment_id, step_number)
        pixel = (
            f'<img src="{pixel_url}" width="1" height="1" alt="" '
            f'style="display:none;width:1px;height:1px;border:0" />'
        )
        if "</body>" in body_html.lower():
            idx = body_html.lower().rindex("</body>")
            return body_html[:idx] + pixel + body_html[idx:]
        return body_html + pixel
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Open-pixel injection failed for %s: %s", enrollment_id, exc)
        return body_html


def apply_tracking(
    body_html: str,
    enrollment_id: str,
    step_number: int,
    *,
    enabled: Optional[bool] = None,
) -> str:
    """Apply click + open tracking to a sequence step body.

    Gated on ``settings.sequence_tracking_enabled`` unless ``enabled`` is passed
    explicitly. Links are wrapped before the pixel is appended so the pixel URL
    is never itself rewritten.
    """
    if enabled is None:
        from app.config import get_settings

        enabled = get_settings().sequence_tracking_enabled
    if not enabled:
        return body_html

    tracked = inject_click_tracking(body_html, enrollment_id, step_number)
    return inject_open_pixel(tracked, enrollment_id, step_number)
