"""Application user model — role-based access control.

Landline started with a single shared API key (see ``app/auth.py``). This model
introduces real per-user identities with hashed passwords, roles, and an
account lifecycle so the app can move to JWT-based login while keeping the API
key working alongside it during the transition.

Roles (least → most privilege for management purposes):
    - ``agent``   — a salesperson; day-to-day use of the app.
    - ``manager`` — an agent who can also see team-wide views.
    - ``admin``   — full access, including user management under ``/admin/users``.

The ``role`` column is stored as a plain ``String`` (validated against
``UserRole`` in the app layer) rather than a PostgreSQL ENUM — this keeps
Alembic migrations and the SQLite test harness simple, and lets us add roles
later without an ``ALTER TYPE``.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, String, func
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserRole(str, enum.Enum):
    """The roles a user can hold. Values are what's stored in the DB."""

    ADMIN = "admin"
    MANAGER = "manager"
    AGENT = "agent"

    @classmethod
    def values(cls) -> list[str]:
        return [r.value for r in cls]


class User(Base):
    """A person who can log in to Landline."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    # bcrypt hash (via passlib) — never the plaintext password.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # One of UserRole.values(); defaults to the lowest-privilege role.
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=UserRole.AGENT.value)

    # Deactivated users keep their history but can no longer log in (soft delete).
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Force a password change on next login (true for admin-seeded accounts).
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # The admin who created this account (null for the seeded default admin).
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<User {self.email} role={self.role} active={self.is_active}>"
