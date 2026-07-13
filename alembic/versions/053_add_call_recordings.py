"""Add Phase 3.5 call_recordings table (voice intelligence).

Revision ID: 053_add_call_recordings
Revises: 052_add_content_embeddings
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "053_add_call_recordings"
down_revision = "052_add_content_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_recordings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("call_task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("call_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recording_url", sa.String(1000), nullable=True),
        sa.Column("duration_seconds", sa.Integer, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="uploaded"),
        sa.Column("transcription", sa.Text, nullable=True),
        sa.Column("transcription_source", sa.String(30), nullable=True),
        sa.Column("analysis", postgresql.JSONB, nullable=True),
        sa.Column("talk_listen_ratio", sa.Float, nullable=True),
        sa.Column("model_used", sa.String(50), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_call_recordings_call_task_id", "call_recordings", ["call_task_id"])
    op.create_index("ix_call_recordings_status", "call_recordings", ["status"])


def downgrade() -> None:
    op.drop_table("call_recordings")
