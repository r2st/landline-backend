"""Email templates + send log for outreach email composition.

Adds the email_templates and email_sends tables and seeds two starter
templates (a professional default and a shorter casual follow-up), both
referencing the Apprend demo link.

Revision ID: 002_email_templates
Revises: 001_initial
Create Date: 2026-06-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "002_email_templates"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEMO_LINK = "https://true-molding-demo.pages.dev"

PROFESSIONAL_BODY = f"""\
<p>Hi {{{{first_name}}}},</p>

<p>I came across {{{{company}}}} and your work in {{{{industry_segment}}}} here in
{{{{city}}}}, and wanted to reach out. As {{{{contact_title}}}}, you know how much
margin slips away when a press goes down unnoticed or scrap creeps up between checks.</p>

<p><strong>Apprend</strong> is a press monitoring platform built for manufacturers like
{{{{company}}}}. It watches every press in real time &mdash; cycle times, downtime,
scrap, and OEE &mdash; and flags problems the moment they start, so your team can act
before a shift's output is lost.</p>

<p>Teams using Apprend typically see:</p>
<ul>
  <li>Faster reaction to unplanned downtime</li>
  <li>Lower scrap rates through early drift detection</li>
  <li>Clear, real-time visibility across the floor</li>
</ul>

<p>I'd love to show you what it looks like on a live line. You can explore an
interactive demo here:</p>

<p><a href="{DEMO_LINK}">{DEMO_LINK}</a></p>

<p>Would you be open to a quick 15-minute call to see if it's a fit for {{{{company}}}}?</p>

<p>Best regards,<br>
The Apprend Team<br>
Apprend Technologies</p>
"""

CASUAL_BODY = f"""\
<p>Hi {{{{first_name}}}},</p>

<p>Quick follow-up &mdash; I know things move fast in {{{{industry_segment}}}}.</p>

<p>We built <strong>Apprend</strong> to take the guesswork out of press monitoring:
real-time downtime, scrap, and OEE for every press at {{{{company}}}}, without the
spreadsheets.</p>

<p>Easier to just show you. Here's a 2-minute interactive demo:</p>

<p><a href="{DEMO_LINK}">{DEMO_LINK}</a></p>

<p>Worth a quick look?</p>

<p>Cheers,<br>
The Apprend Team</p>
"""


def upgrade() -> None:
    op.create_table(
        "email_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_email_templates_name"),
    )
    # At most one template may be the default.
    op.create_index(
        "uq_email_templates_one_default",
        "email_templates",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.create_table(
        "email_sends",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("to_email", sa.String(255), nullable=True),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="sent"),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["template_id"], ["email_templates.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_email_sends_contact", "email_sends", ["contact_id"])
    op.create_index("ix_email_sends_template", "email_sends", ["template_id"])

    # --- Seed two starter templates ---
    templates = sa.table(
        "email_templates",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String),
        sa.column("subject", sa.String),
        sa.column("body", sa.Text),
        sa.column("is_default", sa.Boolean),
    )
    op.bulk_insert(
        templates,
        [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "name": "Professional Outreach — Press Monitoring",
                "subject": "Real-time press monitoring for {{company}}",
                "body": PROFESSIONAL_BODY,
                "is_default": True,
            },
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "name": "Casual Follow-Up — Quick Demo",
                "subject": "{{first_name}}, a 2-minute look at Apprend",
                "body": CASUAL_BODY,
                "is_default": False,
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_email_sends_template", table_name="email_sends")
    op.drop_index("ix_email_sends_contact", table_name="email_sends")
    op.drop_table("email_sends")
    op.drop_index("uq_email_templates_one_default", table_name="email_templates")
    op.drop_table("email_templates")
