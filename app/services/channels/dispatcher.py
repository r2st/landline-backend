"""Channel dispatcher — routes a step to the handler for its channel.

One registry, keyed by channel string, mapping to a singleton handler. The
``sms`` channel shares the phone handler (both are Twilio). :func:`dispatch_channel_step`
is the front door the router and any future orchestrator-execution path call:
it creates the :class:`ChannelStepExecution` row, invokes the handler's
``send``, and returns the persisted row.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel_execution import (
    CHANNEL_EXECUTION_CHANNELS,
    ChannelStepExecution,
)
from app.services.channels.base import ChannelHandler
from app.services.channels.email_channel import EmailChannelHandler
from app.services.channels.linkedin_channel import LinkedInChannelHandler
from app.services.channels.phone_channel import PhoneChannelHandler

logger = logging.getLogger(__name__)


class UnknownChannelError(ValueError):
    """Raised when a channel has no registered handler."""


# Singleton handlers — they hold no per-request state.
_EMAIL = EmailChannelHandler()
_LINKEDIN = LinkedInChannelHandler()
_PHONE = PhoneChannelHandler()

_REGISTRY: dict[str, ChannelHandler] = {
    "email": _EMAIL,
    "linkedin": _LINKEDIN,
    "phone": _PHONE,
    # SMS is a phone-provider (Twilio) action; share the handler.
    "sms": _PHONE,
}


def handler_for_channel(channel: str) -> Optional[ChannelHandler]:
    """Return the handler for *channel*, or ``None`` if unregistered."""
    return _REGISTRY.get(channel)


def get_handler(channel: str) -> ChannelHandler:
    """Return the handler for *channel*, raising :class:`UnknownChannelError`."""
    handler = _REGISTRY.get(channel)
    if handler is None:
        raise UnknownChannelError(
            f"no handler for channel '{channel}'; known: {sorted(_REGISTRY)}"
        )
    return handler


async def dispatch_channel_step(
    db: AsyncSession,
    *,
    channel: str,
    action: str = "message",
    payload: Optional[dict] = None,
    enrollment_id: Optional[uuid.UUID] = None,
    contact_id: Optional[uuid.UUID] = None,
    step_number: Optional[int] = None,
    org_id: Optional[uuid.UUID] = None,
) -> ChannelStepExecution:
    """Create + execute a channel-step touch, returning the persisted row.

    The row is created first (``pending``) so a mid-flight crash still leaves an
    auditable record, then the handler runs and mutates it to its final status.
    """
    if channel not in CHANNEL_EXECUTION_CHANNELS:
        raise UnknownChannelError(
            f"unknown channel '{channel}'; allowed: {list(CHANNEL_EXECUTION_CHANNELS)}"
        )
    handler = get_handler(channel)

    execution = ChannelStepExecution(
        channel=channel,
        action=action,
        payload=payload or {},
        status="pending",
        enrollment_id=enrollment_id,
        contact_id=contact_id,
        step_number=step_number,
    )
    # Respect an explicit org_id (background/task callers with no request context);
    # otherwise the OrgScopedMixin before_flush hook stamps the current org.
    if org_id is not None:
        execution.org_id = org_id
    db.add(execution)
    await db.flush()

    try:
        result = await handler.send(db, execution)
        logger.info(
            "Channel %s/%s execution %s -> %s (provider=%s)",
            channel, action, execution.id, result.status, result.provider,
        )
    except Exception as exc:  # noqa: BLE001 — record the failure, don't crash the caller
        logger.exception("Channel %s dispatch failed", channel)
        execution.status = "failed"
        execution.error = str(exc)
        execution.result = {"status": "failed", "error": str(exc)}
    await db.flush()
    return execution
