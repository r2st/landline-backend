"""Channel handler interface.

A handler owns one channel's execution mechanics. The dispatcher creates a
:class:`~app.models.channel_execution.ChannelStepExecution` row and hands it to
the matching handler, which mutates the row in place (status, provider,
external_id, result, sent_at, error) and returns a :class:`ChannelResult`
summary. Keeping the row-mutation in the handler means every channel records its
outcome the same way, and the dispatcher stays channel-agnostic.

The three-method interface — send / check_status / handle_reply — is the whole
contract a new channel must satisfy to plug in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel_execution import ChannelStepExecution


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ChannelResult:
    """Outcome of a handler operation, mirrored onto the execution row."""

    status: str
    provider: Optional[str] = None
    external_id: Optional[str] = None
    detail: Optional[str] = None
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "provider": self.provider,
            "external_id": self.external_id,
            "detail": self.detail,
            "error": self.error,
        }


class ChannelHandler(ABC):
    """Common interface every channel implements."""

    #: One of CHANNEL_EXECUTION_CHANNELS.
    channel: str = ""
    #: Transport/provider label recorded on executions.
    provider: str = ""

    @property
    def configured(self) -> bool:
        """Whether a real integration is wired up (vs. running as a stub)."""
        return False

    @abstractmethod
    async def send(self, db: AsyncSession, execution: ChannelStepExecution) -> ChannelResult:
        """Execute the touch described by ``execution.payload`` and record it."""

    @abstractmethod
    async def check_status(
        self, db: AsyncSession, execution: ChannelStepExecution
    ) -> ChannelResult:
        """Refresh delivery/engagement status from the provider."""

    @abstractmethod
    async def handle_reply(
        self, db: AsyncSession, execution: ChannelStepExecution, reply: dict
    ) -> ChannelResult:
        """Attach an inbound reply to this touch and mark it replied."""

    # -- shared helpers ----------------------------------------------------

    def _apply(self, execution: ChannelStepExecution, result: ChannelResult) -> ChannelResult:
        """Mirror a result onto the execution row (single place that writes it)."""
        execution.status = result.status
        if result.provider:
            execution.provider = result.provider
        if result.external_id:
            execution.external_id = result.external_id
        if result.error:
            execution.error = result.error
        execution.result = result.as_dict()
        if result.status in ("sent", "simulated", "delivered"):
            if execution.sent_at is None:
                execution.sent_at = utcnow()
        return result
