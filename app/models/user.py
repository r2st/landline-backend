"""User model — authentication and role-based access control.

Supports three roles:
* ``admin``   — full access, can manage users
* ``manager`` — can view all data, manage team members' work
* ``agent``   — scoped to own data (emails, tasks, calls, etc.)
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserRole(str, enum.Enum):
    """Roles a user can hold. Value is the string stored in the ``role`` column."""

    ADMIN = "admin"
    MANAGER = "manager"
    AGENT = "agent"

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(r.value for r in cls)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('admin','manager','agent')",
            name="ck_users_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=UserRole.AGENT.value)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Multi-tenant org membership (feature 4.1). NULL = the default first-party
    # tenant (Apprend), which keeps every pre-4.1 user working unchanged.
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Which user (admin) created this account, if any. Self-referential.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Self-referential relationship to the creating admin.
    creator: Mapped["User | None"] = relationship("User", remote_side=[id], foreign_keys=[created_by])
    # The tenant (organization) this user belongs to (feature 4.1).
    tenant = relationship("Tenant", back_populates="users", foreign_keys=[org_id])

    def __repr__(self) -> str:
        return f"<User {self.email} role={self.role} active={self.is_active}>"
