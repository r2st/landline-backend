"""Per-contact engagement profile (Feature 6 — Smart Send-Time Optimization).

One row per contact (unique on ``contact_id``), holding the recipient's resolved
timezone and a lightweight learned engagement pattern used to pick the best
per-recipient send time.

* ``tz`` / ``tz_source`` — the resolved IANA timezone and how it was resolved
  (``explicit`` → ``contact_geo`` → ``company_geo`` → ``domain`` → ``default``).
  ``contacts.timezone`` remains the cached authoritative tz honoured by the send
  window; this profile stores the richer bucket data and the resolution source
  for debuggability.
* ``buckets`` — counts per (day-of-week, hour) bucket in the recipient's LOCAL
  time, e.g. ``{"2_10": {"sends": 3, "opens": 2, "replies": 1}, ...}`` where the
  key is ``"<dow>_<hour>"`` and ``dow`` is 0=Sunday..6=Saturday (matching
  ``send_timing``'s day indexing).
* ``best_dow`` / ``best_hour`` — the recipient's peak local engagement bucket,
  chosen by a reply-weighted score (a reply outweighs an open).
* ``confidence`` — 0..1, how much personal signal backs the window (rises with
  ``opens_total``); a thin profile blends toward the cohort/best-practice default.

The profile is (re)computed nightly by ``refresh_engagement_profiles`` and read
lazily by the send queue when scheduling a step.
"""

import uuid
from datetime import datetime

from sqlalchemy import Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin


class ContactEngagementProfile(Base, OrgScopedMixin):
    __tablename__ = "contact_engagement_profiles"
    __table_args__ = (
        UniqueConstraint("contact_id", name="uq_engagement_profile_contact"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Resolved IANA timezone + the resolution source (see module docstring).
    tz: Mapped[str | None] = mapped_column(String(64))
    tz_source: Mapped[str | None] = mapped_column(String(20))
    # {"<dow>_<hour>": {"sends": int, "opens": int}} in the recipient's local time.
    buckets: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    opens_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    best_dow: Mapped[int | None] = mapped_column(Integer)   # 0=Sunday .. 6=Saturday
    best_hour: Mapped[int | None] = mapped_column(Integer)  # 0..23 local
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    last_computed: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<ContactEngagementProfile contact={self.contact_id} tz={self.tz} "
            f"best={self.best_dow}/{self.best_hour} conf={self.confidence}>"
        )
