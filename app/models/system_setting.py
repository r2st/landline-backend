"""System-wide settings stored as a small key/value table.

Runtime-mutable configuration that must survive a process restart and be
toggleable without a redeploy. The flagship use is the **global sending kill
switch** (``sending_enabled``): a single row the sending pipeline consults
before putting any autonomous email on the wire. Also holds the persisted
warm-up start date so the ramp survives restarts and can be reset from the UI.

Values are JSONB so a setting can carry structured metadata (who changed it,
when, and why) alongside its primary value.
"""

import uuid
from datetime import datetime

from sqlalchemy import String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin


class SystemSetting(Base, OrgScopedMixin):
    __tablename__ = "system_settings"
    # Settings are per-organization: each tenant has its own kill switch,
    # warm-up date, etc. Uniqueness is on (org_id, key), NOT key alone —
    # otherwise only one org could ever hold a given setting (see migration 080).
    __table_args__ = (
        UniqueConstraint("org_id", "key", name="uq_system_settings_org_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Stable machine key, e.g. "sending_enabled" or "warmup_start_date".
    key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # The setting payload. For the kill switch: {"enabled": bool, "reason": str,
    # "updated_by": str}. For warm-up: {"date": "2026-07-15"}.
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    updated_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<SystemSetting {self.key}={self.value}>"
