"""Merge the two migration branches that both forked from 044.

Two concurrent Phase-1/2 workstreams each branched a head off
``044_conversation_analysis``:

* Phase 1.1/1.3/1.6 — ``045_deal_predictions`` → ``046_lead_prioritization`` →
  ``047_suggestion_outcomes``.
* Phase 2.1/2.4/2.6 — ``045_phase2_intent_coaching_competitive``.

This no-op merge reunites them into a single head so ``alembic upgrade head``
applies both branches cleanly.

Revision ID: 048_merge_phase1_phase2
Revises: 047_suggestion_outcomes, 045_phase2_intent_coaching_competitive
Create Date: 2026-07-13
"""

revision = "048_merge_phase1_phase2"
down_revision = ("047_suggestion_outcomes", "045_phase2_intent_coaching_competitive")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
