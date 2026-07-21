"""Per-channel step execution handlers + dispatcher.

Each channel (email, linkedin, phone, sms) has a handler implementing the same
interface — :meth:`send`, :meth:`check_status`, :meth:`handle_reply` — so the
dispatcher can route a step to the right one without knowing the transport.
Email is real; LinkedIn and phone/SMS are provider stubs (see the module
docstrings) that keep the framework runnable until an API is wired in.
"""

from app.services.channels.base import ChannelHandler, ChannelResult
from app.services.channels.dispatcher import (
    dispatch_channel_step,
    get_handler,
    handler_for_channel,
)

__all__ = [
    "ChannelHandler",
    "ChannelResult",
    "dispatch_channel_step",
    "get_handler",
    "handler_for_channel",
]
