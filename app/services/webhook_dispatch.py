"""Outbound webhook framework (feature 4.2).

Internal events (``lead.replied``, ``deal.stage_changed``, …) are fanned out to
tenant-registered :class:`WebhookSubscription` targets. Each delivery is
recorded as a :class:`WebhookEvent` (status, attempts, response code) for audit
and retry. Payloads are HMAC-signed with the subscription secret so receivers
can verify authenticity (``X-Landline-Signature: sha256=<hex>``).

Delivery is best-effort and never raises into the caller — an emit that can't
reach a subscriber marks the event failed and moves on.

The target URL is a tenant's, so it goes through the SSRF guard
--------------------------------------------------------------
``target_url`` is whatever an org user typed into a form, and this module POSTs
to it from inside the production network. Registration checked that the string
started with ``http://`` or ``https://`` and nothing else, so
``http://169.254.169.254/latest/meta-data/`` and ``http://127.0.0.1:8001/…`` were
both accepted.

Two things made that more than theoretical. ``POST
/integrations/webhooks/emit`` lets the same user fire a delivery on demand with a
payload of their choosing, and the response hands back each delivery's status and
``response_code`` — so the pair is not a blind SSRF but an interactive one with a
result channel. And the request carries a valid ``X-Landline-Signature`` for a
body the caller controls, which is exactly the shape a receiver is told to trust.

``web_scraper.validate_public_url`` already existed for this and is already
applied to org-supplied RSS feed URLs in the social-listening agent. It is
applied here at both ends: at registration, so a bad target is a 400 the user
sees, and again at delivery, because DNS is not a property of the string — a
hostname that resolved publicly when it was registered can be re-pointed at
169.254.169.254 afterwards, and a subscription outlives the check that admitted
it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import WebhookEvent, WebhookSubscription
from app.services.crypto import TokenCryptoError, decrypt
from app.services.web_scraper import UnsafeURLError, validate_public_url_async

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(10.0)

# The header carrying the HMAC. Named here rather than inlined because it is now
# read on the way *in* as well as written on the way out — an inbound webhook we
# accept has to be verified with the same scheme we ask our own subscribers to
# verify with (see ``app.routers.outreach.require_signed_webhook_or_user``).
SIGNATURE_HEADER = "X-Landline-Signature"


def sign_payload(secret: str, body: bytes) -> str:
    """HMAC-SHA256 signature header value for a raw JSON body."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_payload(secret: str, body: bytes, header_value: Optional[str]) -> bool:
    """Whether ``header_value`` is the signature this ``secret`` produces for ``body``.

    The inverse of :func:`sign_payload`, and deliberately the *only* way to check
    one: the comparison is constant-time (``hmac.compare_digest``), so a caller
    cannot recover a signature byte-by-byte from response timing.

    Returns ``False`` rather than raising for every failure mode — no secret
    configured, no header, a malformed header, a wrong digest. A verifier that
    raises on some rejections and returns on others invites a caller to treat one
    of those paths as "not really a rejection".
    """
    if not secret or not header_value:
        return False
    return hmac.compare_digest(sign_payload(secret, body), header_value.strip())


class WebhookDispatcher:
    """Fan internal events out to matching webhook subscriptions."""

    async def emit(
        self,
        db: AsyncSession,
        event_type: str,
        payload: dict,
        org_id: Optional[uuid.UUID] = None,
    ) -> list[WebhookEvent]:
        """Deliver ``event_type`` to every matching active subscription.

        Scopes to ``org_id`` when given (a tenant only receives its own events);
        ``None`` matches subscriptions with no org (the default tenant).
        """
        query = select(WebhookSubscription).where(WebhookSubscription.is_active.is_(True))
        query = query.where(WebhookSubscription.org_id == org_id)
        subs = (await db.execute(query)).scalars().all()

        events: list[WebhookEvent] = []
        for sub in subs:
            if not sub.matches(event_type):
                continue
            event = WebhookEvent(
                subscription_id=sub.id,
                event_type=event_type,
                payload=payload,
                status="pending",
            )
            db.add(event)
            await db.flush()
            await self._deliver(sub, event)
            events.append(event)
        return events

    async def _deliver(self, sub: WebhookSubscription, event: WebhookEvent) -> None:
        """Attempt one HTTP delivery, updating the event's status in place."""
        # Re-checked per delivery, not just at registration: DNS is not a
        # property of the stored string. A host that resolved publicly when the
        # subscription was created can be re-pointed at a private address the
        # next day, and the subscription outlives the check that admitted it.
        try:
            await validate_public_url_async(sub.target_url)
        except UnsafeURLError as exc:
            event.attempts += 1
            event.status = "failed"
            event.last_error = f"Refused unsafe target URL: {exc}"
            logger.warning(
                "Webhook subscription %s targets a disallowed address: %s", sub.id, exc
            )
            return

        body = json.dumps(
            {"event": event.event_type, "data": event.payload, "id": str(event.id)},
            default=str,
        ).encode()
        headers = {"Content-Type": "application/json", "X-Landline-Event": event.event_type}
        if sub.secret:
            try:
                secret = decrypt(sub.secret)
                headers["X-Landline-Signature"] = sign_payload(secret, body)
            except TokenCryptoError:
                logger.warning("Webhook subscription %s has an undecryptable secret", sub.id)

        event.attempts += 1
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(sub.target_url, content=body, headers=headers)
            event.response_code = resp.status_code
            if 200 <= resp.status_code < 300:
                event.status = "delivered"
                event.delivered_at = datetime.now(timezone.utc)
            else:
                event.status = "failed"
                event.last_error = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 — delivery never breaks the emitter
            event.status = "failed"
            event.last_error = str(exc)
            logger.warning("Webhook delivery to %s failed: %s", sub.target_url, exc)


_dispatcher: Optional[WebhookDispatcher] = None


def get_webhook_dispatcher() -> WebhookDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = WebhookDispatcher()
    return _dispatcher
