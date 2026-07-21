"""Email channel handler — real sends through the existing dispatch pipeline.

Unlike the LinkedIn/phone stubs, this one actually puts mail on the wire: it
delegates to :func:`app.services.email_dispatch.send_outreach_email`, which
already enforces the kill switch, suppression, warm-up caps and sender rotation.
The handler's job is just to translate the payload into that call and map the
dispatcher's status vocabulary onto a channel-execution status.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel_execution import ChannelStepExecution
from app.services.channels.base import ChannelHandler, ChannelResult


class EmailChannelHandler(ChannelHandler):
    channel = "email"
    provider = "email"

    @property
    def configured(self) -> bool:
        # Email always has a transport (Gmail/SMTP rotation, else SES fallback).
        return True

    async def send(self, db: AsyncSession, execution: ChannelStepExecution) -> ChannelResult:
        payload = execution.payload or {}
        to_email = payload.get("to_email")
        subject = payload.get("subject") or ""
        body_html = payload.get("body_html") or payload.get("body") or ""
        if not to_email:
            return self._apply(
                execution,
                ChannelResult(status="failed", provider="email", error="no recipient email"),
            )

        # Local import keeps the channels package importable without pulling the
        # whole dispatch stack at model-registration time.
        from app.services.email_dispatch import send_outreach_email

        send_result = await send_outreach_email(
            db,
            to_email=to_email,
            subject=subject,
            body_html=body_html,
            sender_account_id=payload.get("sender_account_id"),
            thread_id=payload.get("thread_id"),
            in_reply_to=payload.get("in_reply_to"),
            references=payload.get("references"),
        )
        raw_status = send_result.get("status")
        # Soft-declines from the dispatcher aren't a channel failure — they mean
        # a guard held the send (kill switch, suppression, warm-up).
        if raw_status in ("halted", "suppressed", "skipped"):
            status = "skipped"
        elif raw_status == "sent":
            status = "sent"
        else:
            status = "failed"
        result = ChannelResult(
            status=status,
            provider=send_result.get("transport") or "email",
            external_id=send_result.get("gmail_message_id") or send_result.get("message_id"),
            detail=send_result.get("reason"),
            error=send_result.get("error") if status == "failed" else None,
            raw=send_result,
        )
        applied = self._apply(execution, result)
        # Preserve the transport-specific fields the send returned for readback.
        execution.result = {**result.as_dict(), "send_result": send_result}
        return applied

    async def check_status(
        self, db: AsyncSession, execution: ChannelStepExecution
    ) -> ChannelResult:
        # Email delivery/open signals arrive asynchronously via the tracking
        # pixel + reply sync, not a synchronous provider poll, so a status check
        # simply reports the last recorded state.
        from app.services.channels.base import utcnow

        execution.status_checked_at = utcnow()
        return ChannelResult(
            status=execution.status,
            provider=execution.provider or "email",
            external_id=execution.external_id,
            detail="email status is tracked via open pixel + reply sync",
        )

    async def handle_reply(
        self, db: AsyncSession, execution: ChannelStepExecution, reply: dict
    ) -> ChannelResult:
        execution.reply = reply
        return self._apply(
            execution,
            ChannelResult(
                status="replied",
                provider=execution.provider or "email",
                external_id=execution.external_id,
                detail="reply recorded",
            ),
        )
