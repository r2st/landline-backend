"""Add users table for role-based auth and seed the default admin.

Introduces per-user identities (hashed passwords + roles) alongside the
existing shared API key. Also seeds a default admin account
(``admin@landline.app`` / ``admin123``) with ``must_change_password = true`` so
the first operator can log in and immediately rotate the password.

Revision ID: 036_add_user_auth
Revises: 034_sentiment_testing
Create Date: 2026-07-03
"""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# NOTE: down_revision points at 034_sentiment_testing — the head of origin/main
# when this was written. If another branch (e.g. a 035 sales-book migration)
# also descends from 034, resolve the resulting multiple-heads with a merge
# migration (`alembic merge`).
revision = "036_add_user_auth"
down_revision = "034_sentiment_testing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="agent"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "must_change_password",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    _seed_default_admin()


def _seed_default_admin() -> None:
    """Insert the default admin account (idempotent via ON CONFLICT)."""
    # Imported here so the module still imports if app deps are unavailable at
    # migration-graph load time.
    from app.config import get_settings
    from app.security import hash_password

    settings = get_settings()
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO users (
                id, email, password_hash, name, role,
                is_active, must_change_password, created_by, created_at, updated_at
            ) VALUES (
                :id, :email, :password_hash, :name, 'admin',
                true, true, NULL, now(), now()
            )
            ON CONFLICT (email) DO NOTHING
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "email": settings.default_admin_email.lower(),
            "password_hash": hash_password(settings.default_admin_password),
            "name": settings.default_admin_name,
        },
    )


def downgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
