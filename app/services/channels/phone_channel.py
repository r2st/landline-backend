"""Phone / SMS channel handler — Twilio-style provider STUB.

Handles two actions on the phone channel:

* ``sms``  — send a text message (Twilio Messages API, once wired).
* ``call`` — place/queue a call, e.g. to drop a voicemail or connect a rep
  (Twilio Voice API, once wired).

Today it *simulates*: with no Twilio credentials configured it validates the
payload, synthesises a Twilio-shaped SID, and records ``status="simulated"``. The
:attr:`configured` flag flips to True once ``TWILIO_ACCOUNT_SID`` /
``TWILIO_AUTH_TOKEN`` / ``TWILIO_FROM_NUMBER`` are set; the real API call then
replaces ``_simulate`` and nothing else has to change.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.channel_execution import ChannelStepExecution
from app.services.channels.base import ChannelHandler, ChannelResult, utcnow

PHONE_ACTIONS = ("sms", "call")


class PhoneChannelHandler(ChannelHandler):
    """Serves both the ``phone`` and ``sms`` channels via Twilio (stubbed)."""

    channel = "phone"
    provider = "twilio_stub"

    @property
    def configured(self) -> bool:
        s = get_settings()
        return bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_from_number)

    def _resolve_action(self, execution: ChannelStepExecution) -> str:
        # The `sms` channel implies the sms action; otherwise use the explicit
        # action, defaulting a bare `phone` touch to a call.
        if execution.channel == "sms":
            return "sms"
        action = execution.action or "call"
        return action

    async def send(self, db: AsyncSession, execution: ChannelStepExecution) -> ChannelResult:
        action = self._resolve_action(execution)
        if action not in PHONE_ACTIONS:
            return self._apply(
                execution,
                ChannelResult(
                    status="failed",
                    provider=self.provider,
                    error=f"unsupported phone action '{action}'",
                ),
            )
        payload = execution.payload or {}
        to_number = payload.get("to_number") or payload.get("phone")
        if not to_number:
            return self._apply(
                execution,
                ChannelResult(
                    status="failed",
                    provider=self.provider,
                    error="no destination phone number on payload",
                ),
            )
        if action == "sms" and not (payload.get("message") or payload.get("body")):
            return self._apply(
                execution,
                ChannelResult(
                    status="failed", provider=self.provider, error="sms has no message body"
                ),
            )
        return self._simulate(execution, action, to_number)

    def _simulate(
        self, execution: ChannelStepExecution, action: str, to_number: str
    ) -> ChannelResult:
        # Twilio SIDs are 34 chars: 'SM'/'CA' prefix + 32 hex. Mimic the shape.
        prefix = "SM" if action == "sms" else "CA"
        sid = f"{prefix}{uuid.uuid4().hex}"
        detail = (
            f"[stub] Twilio {action} simulated to {to_number}; "
            "set TWILIO_* to send for real."
        )
        return self._apply(
            execution,
            ChannelResult(
                status="simulated",
                provider=self.provider,
                external_id=sid,
                detail=detail,
                raw={"simulated": True, "action": action, "to": to_number},
            ),
        )

    async def check_status(
        self, db: AsyncSession, execution: ChannelStepExecution
    ) -> ChannelResult:
        execution.status_checked_at = utcnow()
        return ChannelResult(
            status=execution.status,
            provider=self.provider,
            external_id=execution.external_id,
            detail="[stub] no live Twilio status; reflects last recorded state",
        )

    async def handle_reply(
        self, db: AsyncSession, execution: ChannelStepExecution, reply: dict
    ) -> ChannelResult:
        execution.reply = reply
        return self._apply(
            execution,
            ChannelResult(
                status="replied",
                provider=self.provider,
                external_id=execution.external_id,
                detail="[stub] inbound SMS/call reply recorded",
            ),
        )
