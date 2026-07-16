"""Pydantic schemas for per-sender Calendar Booking config (Feature 4).

Backs ``GET/PUT /dialtone/senders/{account_id}/booking`` — the operator surface
for setting each sending identity's scheduling link, provider label, and the
auto-inject toggle that governs whether AI follow-ups weave the link on interest.
"""

from typing import Optional

from pydantic import BaseModel


class BookingConfig(BaseModel):
    """A sender's current booking configuration (read model)."""

    account_id: str
    account_type: str          # 'gmail' | 'smtp'
    email: Optional[str] = None
    booking_url: Optional[str] = None
    booking_provider: Optional[str] = None   # 'calendly' | 'cal.com' | 'other' (display only)
    booking_auto_inject: bool = True


class BookingConfigUpdate(BaseModel):
    """Fields settable on a sender's booking config (write model).

    All optional so a caller can patch just the toggle or just the URL; a field
    left unset is untouched. ``booking_url`` is validated (https + known-scheduler
    host allowlist) before it is persisted.
    """

    booking_url: Optional[str] = None
    booking_provider: Optional[str] = None
    booking_auto_inject: Optional[bool] = None
