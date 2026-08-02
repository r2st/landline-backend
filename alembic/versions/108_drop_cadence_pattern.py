"""Drop a column that described how a sequence ran and never did.

``outreach_sequences.cadence_pattern`` arrived in migration 035 carrying the
Combo Prospecting taxonomy — "triple" (call + voicemail + email), "quad"
(+ LinkedIn), "quint" (+ social DM). An exhaustive scan of ``app/`` finds no
reader and no writer: no endpoint accepted it, no service branched on it, and
the two step-creation paths never set it. Every row therefore holds the column
default, which means the field carries no information at all while presenting
itself — in the schema, and in the public API's sequence payload — as
configuration that decides how a cadence is executed.

That is the same trap as the fabricated Settings badges: a surface that reports
a setting nobody honours is worse than no surface, because it is believed.

It also could not have been made true as written. The taxonomy names five
channels (call, voicemail, email, LinkedIn, social); ``sequence_steps.channel``
offers email, call, linkedin, video, direct_mail — no voicemail, no social —
and the vocabulary has no word for a two-channel cadence, which the step model
expresses perfectly well. Wiring the column would have meant either inventing
terms the book doesn't have or mislabelling real cadences.

The channel mix is a property of the steps. ``app.models.sequence.channel_mix``
reads it from them and the API now reports that instead, so the answer is
derived from the rows that decide it rather than from a second field somebody
has to remember to update.

Dropping is safe on data grounds — the column is a constant — and the downgrade
restores it with its original default, which reproduces the prior state exactly.

Revision ID: 108_drop_cadence_pattern
Revises: 107_email_template_archive
"""

import sqlalchemy as sa
from alembic import op

revision = "108_drop_cadence_pattern"
down_revision = "107_email_template_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("outreach_sequences") as batch:
        batch.drop_column("cadence_pattern")


def downgrade() -> None:
    with op.batch_alter_table("outreach_sequences") as batch:
        batch.add_column(
            sa.Column(
                "cadence_pattern",
                sa.String(length=20),
                nullable=True,
                server_default="single_channel",
            )
        )
