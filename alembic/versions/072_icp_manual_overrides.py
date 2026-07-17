"""Let a human pin ICP fields so the weekly re-learn stops overwriting them.

Chains off ``071_ai_email_drafts``.

The ICP is refit from won deals every Monday at 03:30 UTC
(``relearn-icp`` on the beat schedule), and ``learn_icp`` deactivates the
active profile and inserts a fresh one. That is correct for a profile nobody
has touched — the whole point is that it tracks what actually closes.

It is wrong the moment a human edits it. Without somewhere to record intent,
an operator's hand-tuned weights would survive until the next Monday and then
silently revert, with no error and nothing in the UI to explain why the agent
went back to prospecting the wrong companies. Shipping an "edit your ICP"
screen on top of that would be shipping a lie.

So intent gets stored separately from the fit:

* ``manual_overrides`` — ``{field_name: value}`` for fields a human has set.
  Learning recomputes everything as before, then re-applies these on top and
  carries them to the new profile, so the parts nobody pinned keep improving
  while explicit choices survive. Field-level rather than key-level on purpose:
  "you own industry_weights" is a rule an operator can hold in their head,
  whereas per-key merging inside a dict silently produces blends nobody chose.
* ``edited_at`` / ``edited_by`` — who took ownership and when, so the UI can
  distinguish "learned from 14 won deals" from "Sam decided this in March".

Nullable + server-default so existing rows are untouched and a rollback loses
only the override bookkeeping, never the learned profile itself.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "072_icp_manual_overrides"
down_revision = "071_ai_email_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "icp_profiles",
        sa.Column(
            "manual_overrides",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "icp_profiles",
        sa.Column("edited_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "icp_profiles",
        sa.Column("edited_by", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("icp_profiles", "edited_by")
    op.drop_column("icp_profiles", "edited_at")
    op.drop_column("icp_profiles", "manual_overrides")
