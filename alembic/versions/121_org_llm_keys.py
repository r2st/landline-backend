"""Per-org LLM API keys — bring-your-own Gemini key for AI features.

Revision ID: 121_org_llm_keys
Revises: 120_app_credentials
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "121_org_llm_keys"
down_revision = "120_app_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_llm_keys",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False, server_default="gemini"),
        sa.Column("key_encrypted", sa.Text(), nullable=False),
        sa.Column("key_hint", sa.String(20), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", postgresql.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_org_llm_keys_org_provider", "org_llm_keys",
        ["org_id", "provider"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_org_llm_keys_org_provider", table_name="org_llm_keys")
    op.drop_table("org_llm_keys")
