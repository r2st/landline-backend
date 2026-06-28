"""Seed the 'Plastic Injection Molding — Deal Cracker' outreach sequence.

Idempotent: inserts the sequence and its steps only if a sequence with the same
name does not already exist. Safe to run repeatedly. Copy is the single source
of truth in app.services.outreach_templates.

Revision ID: 025_seed_molding_sequence
Revises: 024_enrollment_threading
Create Date: 2026-06-28
"""
import uuid

import sqlalchemy as sa
from alembic import op

from app.services.outreach_templates import get_molding_sequence

revision = "025_seed_molding_sequence"
down_revision = "024_enrollment_threading"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    seq = get_molding_sequence()

    existing = conn.execute(
        sa.text("SELECT id FROM outreach_sequences WHERE name = :name"),
        {"name": seq["name"]},
    ).first()
    if existing:
        # Already seeded — nothing to do.
        return

    sequence_id = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO outreach_sequences (id, name, description, is_active, status) "
            "VALUES (:id, :name, :description, true, 'active')"
        ),
        {
            "id": str(sequence_id),
            "name": seq["name"],
            "description": seq["description"],
        },
    )

    for step in seq["steps"]:
        conn.execute(
            sa.text(
                "INSERT INTO sequence_steps "
                "(id, sequence_id, step_number, delay_days, subject_template, body_template) "
                "VALUES (:id, :sequence_id, :step_number, :delay_days, :subject, :body)"
            ),
            {
                "id": str(uuid.uuid4()),
                "sequence_id": str(sequence_id),
                "step_number": step["step_number"],
                "delay_days": step["delay_days"],
                "subject": step["subject_template"],
                "body": step["body_template"],
            },
        )


def downgrade():
    conn = op.get_bind()
    seq = get_molding_sequence()
    # Steps are removed via ON DELETE CASCADE when the sequence is deleted.
    conn.execute(
        sa.text("DELETE FROM outreach_sequences WHERE name = :name"),
        {"name": seq["name"]},
    )
