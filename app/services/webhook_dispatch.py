"""Outbound webhook framework (feature 4.2).

Internal events (``lead.replied``, ``deal.stage_changed``, …) are fanned out to
tenant-registered :class:`WebhookSubscription` targets. Each delivery is
recorded as a :class:`WebhookEvent` (status, attempts, response code) for audit
and retry. Payloads are HMAC-signed with the subscription secret so receivers
can verify authenticity (``X-Landline-Signature: sha256=<hex>``).

Delivery is best-effort and never raises into the caller — an emit that can't
reach a subscriber marks the event failed and moves on.
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
