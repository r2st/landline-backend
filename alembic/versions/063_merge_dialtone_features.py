"""Merge the parallel Dialtone feature branches that all forked from 059.

Features 1, 2, and 5 were built concurrently, so each branched a head off
``059_launch_sender_defaults``:

* Feature 1 (reply classification) — ``060_reply_classification``.
* Feature 2 (danger-signal auto-pause) — ``060_danger_signals``.
* Feature 5 (domain health dashboard) — ``062_domain_health``.

This no-op merge reunites them into a single head so ``alembic upgrade head``
applies all three branches cleanly. It lands with Feature 1, the last of the
three to be committed (see the chaining note in ``062_domain_health``).

Revision ID: 063_merge_dialtone_features
Revises: 060_reply_classification, 060_danger_signals, 062_domain_health
Create Date: 2026-07-16
"""

revision = "063_merge_dialtone_features"
down_revision = ("060_reply_classification", "060_danger_signals", "062_domain_health")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
