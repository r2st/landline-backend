"""Give each sequence a voice, so one house tone stops serving every audience.

Chains off ``072_icp_manual_overrides``.

The AI copywriter has always written in exactly one register — the warm,
conversational voice hard-coded into ``ai_email_generator.SYSTEM_PROMPT``. That
voice is good, and it is the right default. It is also the *only* option, which
means the sequence seeded for a plant manager whose scrap bin is eating his
margin reads in the same key as the one aimed at an ops VP comparing vendors.
Tone is a property of who is reading, and sequences are already seeded per
industry vertical, so the sequence row is the seam where that choice belongs.

``tone`` is NOT NULL with a server default of ``conversational``: that is the
voice the system prompt already describes, so every existing row keeps writing
exactly the same email it wrote yesterday. This migration changes no copy —
it only creates somewhere to say otherwise.

NOT NULL rather than nullable-means-default because the generator reads this on
every compose. A NULL would have to mean "no voice", which isn't a state that
means anything, and would push a ``or DEFAULT`` fallback into every read site.

The CHECK constraint mirrors ``ck_sequence_status``: the vocabulary is small,
closed, and known at deploy time, and an unconstrained free-text tone would let
a typo ("empathic") reach the prompt builder, silently miss its guidance entry,
and degrade to the default with nothing to explain why the copy went flat.
Constraining it here makes that a write-time error instead of a quiet one.
"""

from alembic import op
import sqlalchemy as sa

revision = "073_sequence_tone"
down_revision = "072_icp_manual_overrides"
branch_labels = None
depends_on = None

TONES = ("friendly", "professional", "urgent", "empathetic", "conversational")


def upgrade() -> None:
    op.add_column(
        "outreach_sequences",
        sa.Column(
            "tone",
            sa.String(length=20),
            nullable=False,
            server_default="conversational",
        ),
    )
    op.create_check_constraint(
        "ck_sequence_tone",
        "outreach_sequences",
        sa.text("tone IN ({})".format(", ".join(f"'{t}'" for t in TONES))),
    )


def downgrade() -> None:
    op.drop_constraint("ck_sequence_tone", "outreach_sequences", type_="check")
    op.drop_column("outreach_sequences", "tone")
