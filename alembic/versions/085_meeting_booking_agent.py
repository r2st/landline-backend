"""Meeting Booking Agent — per-org config + booked meetings.

Chains off ``084_deal_agent_llm_config`` (the current head).

Adds two org-scoped tables backing the autonomous scheduling agent that runs
once Dialtone gets a positive reply:

* ``meeting_booking_agents`` — one config row per org (unique on ``org_id``):
  calendar account, meeting length, buffer, working-hours window + timezone,
  booking window, daily cap, and confirmation/reminder copy.
* ``booked_meetings`` — one row per meeting the agent is working, walking
  ``proposed → confirmed → completed`` (or ``cancelled`` / ``no_show``), with the
  offered slots, the scheduling conversation, and the Google Calendar event id.

Both carry ``org_id`` (UUID FK → organizations) with the Apprend server_default
so a raw insert lands in the first-party org, matching the rest of the schema.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "085_meeting_booking_agent"
down_revision = "084_deal_agent_llm_config"
branch_labels = None
depends_on = None

# Must match app.org_context.APPREND_ORG_ID — the DB-level default org for raw
# inserts (ORM inserts are stamped by the before_flush hook).
_APPREND_ORG_ID = "a9910000-0000-4000-8000-000000000001"


def upgrade() -> None:
    op.create_table(
        "meeting_booking_agents",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False, server_default=_APPREND_ORG_ID,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "calendar_account_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("gmail_accounts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("default_meeting_duration_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("buffer_between_meetings_minutes", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("working_hours_start", sa.String(5), nullable=False, server_default="09:00"),
        sa.Column("working_hours_end", sa.String(5), nullable=False, server_default="17:00"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("booking_window_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("max_daily_meetings", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("confirmation_template", sa.Text(), nullable=True),
        sa.Column("reminder_template", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("org_id", name="uq_meeting_booking_agent_org"),
    )
    op.create_index(
        "ix_meeting_booking_agents_org_id", "meeting_booking_agents", ["org_id"],
    )
    op.create_index(
        "ix_meeting_booking_agents_calendar_account_id",
        "meeting_booking_agents", ["calendar_account_id"],
    )

    op.create_table(
        "booked_meetings",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False, server_default=_APPREND_ORG_ID,
        ),
        sa.Column(
            "contact_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "enrollment_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("outreach_enrollments.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "sender_account_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("gmail_accounts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("title", sa.String(255), nullable=False, server_default="Intro call"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("start_time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("end_time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("calendar_event_id", sa.String(255), nullable=True),
        sa.Column("proposed_slots", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("conversation_thread", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("gmail_thread_id", sa.String(255), nullable=True),
        sa.Column("gmail_message_id", sa.String(255), nullable=True),
        sa.Column("reminder_sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_booked_meetings_org_id", "booked_meetings", ["org_id"])
    op.create_index("ix_booked_meetings_contact_id", "booked_meetings", ["contact_id"])
    op.create_index("ix_booked_meetings_enrollment_id", "booked_meetings", ["enrollment_id"])
    op.create_index("ix_booked_meetings_status", "booked_meetings", ["status"])
    op.create_index("ix_booked_meetings_start_time", "booked_meetings", ["start_time"])
    op.create_index("ix_booked_meetings_gmail_thread_id", "booked_meetings", ["gmail_thread_id"])


def downgrade() -> None:
    op.drop_index("ix_booked_meetings_gmail_thread_id", table_name="booked_meetings")
    op.drop_index("ix_booked_meetings_start_time", table_name="booked_meetings")
    op.drop_index("ix_booked_meetings_status", table_name="booked_meetings")
    op.drop_index("ix_booked_meetings_enrollment_id", table_name="booked_meetings")
    op.drop_index("ix_booked_meetings_contact_id", table_name="booked_meetings")
    op.drop_index("ix_booked_meetings_org_id", table_name="booked_meetings")
    op.drop_table("booked_meetings")

    op.drop_index("ix_meeting_booking_agents_calendar_account_id", table_name="meeting_booking_agents")
    op.drop_index("ix_meeting_booking_agents_org_id", table_name="meeting_booking_agents")
    op.drop_table("meeting_booking_agents")
