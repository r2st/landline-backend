"""Merge the Phase 3 and Phase 4 migration branches that both forked from 048.

Two concurrent workstreams each branched a head off ``048_merge_phase1_phase2``:

* Phase 3 (advanced AI) — ``049_add_prospecting`` → ``050_add_churn_expansion`` →
  ``051_add_sender_health`` → ``052_add_content_embeddings`` →
  ``053_add_call_recordings``.
* Phase 4 (platform) — ``049_add_tenants`` → ``050_add_integrations`` →
  ``051_add_workflows``.

This no-op merge reunites them into a single head so ``alembic upgrade head``
applies both branches cleanly.

Revision ID: 054_merge_phase3_phase4
Revises: 053_add_call_recordings, 051_add_workflows
Create Date: 2026-07-13
"""

revision = "054_merge_phase3_phase4"
down_revision = ("053_add_call_recordings", "051_add_workflows")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
