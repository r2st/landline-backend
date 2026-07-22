"""Public-API key credentials (``api_keys``).

Chains off ``096_complaint_records``.

Adds ``api_keys`` — one row per public-API credential. Org-scoped from birth
(``org_id`` FK, created well after the 078 multi-tenancy retrofit). The raw key
is never stored: only its SHA-256 hex digest (``key_hash``, uniquely indexed so
the auth layer resolves a presented key in one equality match). ``scopes`` is a
JSON array of permission strings; ``rate_limit`` is the per-key requests/minute
budget; ``revoked_at`` / ``expires_at`` gate usability.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "097_api_keys"
down_revision = "096_complaint_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        # SHA-256 hex digest of the raw key — 64 chars, uniquely indexed.
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column(
            "scopes", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("rate_limit", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True,
    )
    op.create_index("ix_api_keys_org_id", "api_keys", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_org_id", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
