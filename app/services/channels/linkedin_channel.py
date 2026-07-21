"""LinkedIn channel handler — provider STUB.

LinkedIn has no official send-a-connection-request / send-a-message API for this
use case, so real execution will come through a partner automation provider
later. Today this handler *simulates* the touch: it validates the payload,
synthesises a deterministic external id, and records ``status="simulated"`` so a
multi-channel cadence runs end-to-end and its history is inspectable — without
pretending a message was actually delivered.

Swap-in point: when a real integration lands, set :attr:`configured` from the
credential and replace the ``_simulate`` body with the API call. The dispatcher,
model, router and tracking all stay as-is.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.channel_execution import ChannelStepExecution
from app.services.channels.base import ChannelHandler, ChannelResult, utcnow

# The actions this channel understands.
LINKEDIN_ACTIONS = ("connection_request", "message", "inmail")


class LinkedInChannelHandler(ChannelHandler):
    channel = "linkedin"
    provider = "linkedin_stub"

    @property
    def configured(self) -> bool:
        return bool(get_settings().linkedin_api_token)

    async def send(self, db: AsyncSession, execution: ChannelStepExecution) -> ChannelResult:
        payload = execution.payload or {}
        action = execution.action or "message"
        if action not in LINKEDIN_ACTIONS:
            return self._apply(
                execution,
                ChannelResult(
                    status="failed",
                    provider=self.provider,
                    error=f"unsupported linkedin action '{action}'",
                ),
            )
        # A LinkedIn touch needs a profile to target. Missing it is a real
        # failure (nothing to enrich here), not a soft skip.
        target = payload.get("linkedin_url") or payload.get("profile_url")
        if not target and action != "connection_request":
            # A message presupposes a connection/target.
            if not payload.get("recipient"):
                return self._apply(
                    execution,
                    ChannelResult(
                        status="failed",
                        provider=self.provider,
                        error="no linkedin profile/recipient on payload",
                    ),
                )
        return self._simulate(execution, action, target)

    def _simulate(
        self, execution: ChannelStepExecution, action: str, target: str | None
    ) -> ChannelResult:
        external_id = f"linkedin-sim-{action}-{uuid.uuid4().hex[:12]}"
        detail = (
            f"[stub] LinkedIn {action} simulated"
            + (f" to {target}" if target else "")
            + "; wire a real provider to send for real."
        )
        return self._apply(
            execution,
            ChannelResult(
                status="simulated",
                provider=self.provider,
                external_id=external_id,
                detail=detail,
                raw={"simulated": True, "action": action, "target": target},
            ),
        )

    async def check_status(
        self, db: AsyncSession, execution: ChannelStepExecution
    ) -> ChannelResult:
        execution.status_checked_at = utcnow()
        # A stub has no provider to poll — report the recorded state.
        return ChannelResult(
            status=execution.status,
            provider=self.provider,
            external_id=execution.external_id,
            detail="[stub] no live LinkedIn status; reflects last recorded state",
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
                detail="[stub] LinkedIn reply recorded",
            ),
        )
