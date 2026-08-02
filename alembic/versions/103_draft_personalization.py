"""Record which prospect data points wrote each AI draft.

``ai_email_drafts.personalization`` — ``{"points": [...], "depth": n}`` from
``app.services.personalization``, the facts the copywriter was actually handed
(company news, buying signals, prior touches, seniority, size band, …).

Two things need it. A reviewer judging "did the model invent this?" has to see
what it was given, and until now the draft row showed only the output. And
nothing could ask whether personalization *works*: ``depth`` is the column that
lets the campaign analytics correlate reply rate against how much we knew, which
is the difference between believing personalization helps and knowing it.

Defaulted and non-null, so existing drafts read as ``{}`` (asked before the
question existed) rather than NULL. Safe online add — no rewrite, no index.

Revision ID: 103_draft_personalization
Revises: 102_ooo_pause_and_contact_tags
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "103_draft_personalization"
down_revision = "102_ooo_pause_and_contact_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_email_drafts",
        sa.Column(
            "personalization",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_email_drafts", "personalization")
