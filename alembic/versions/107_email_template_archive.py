"""Let a template be retired without destroying what it sent.

``DELETE /email-templates/{id}`` removed the row, and ``email_sends.template_id``
is ``ON DELETE SET NULL`` — so deleting a template silently detached every send
it had ever produced. The mail stayed in the log; the answer to "which template
got those replies" did not. That is exactly backwards: the templates worth
retiring are the ones that have been used, which are the only ones whose history
is worth anything.

``is_archived`` gives the actual intent — take it out of the pickers, keep the
attribution. Delete stays available for a template that never sent, where there
is nothing to preserve and a typo'd draft should not be immortal.

Defaults false server-side: every existing template is live, which is what it
was a moment before this ran.

Revision ID: 107_email_template_archive
Revises: 106_enrollment_send_failures
"""

import sqlalchemy as sa
from alembic import op

revision = "107_email_template_archive"
down_revision = "106_enrollment_send_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "email_templates",
        sa.Column(
            "is_archived", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "email_templates",
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # The list endpoint's default is "live templates only", which on a mature
    # org is most of a small table — but the picker is on a hot path and the
    # predicate is free to index.
    op.create_index(
        "ix_email_templates_live",
        "email_templates",
        ["is_archived"],
        postgresql_where=sa.text("is_archived = false"),
    )


def downgrade() -> None:
    op.drop_index("ix_email_templates_live", table_name="email_templates")
    op.drop_column("email_templates", "archived_at")
    op.drop_column("email_templates", "is_archived")
