"""Add scanned-card detail fields and image storage to contacts

Adds the columns needed so a scanned business card persists ALL of its details
(department, multiple phone numbers, full address, website, notes) plus the card
image itself, instead of only name/title/email/phone/linkedin.

Revision ID: 011_add_contact_card_fields
Revises: 010_email_indexes_unsubscribe
Create Date: 2026-06-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "011_add_contact_card_fields"
down_revision = "010_email_indexes_unsubscribe"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("contacts", sa.Column("department", sa.String(150), nullable=True))
    op.add_column("contacts", sa.Column("mobile", sa.String(30), nullable=True))
    op.add_column("contacts", sa.Column("fax", sa.String(30), nullable=True))
    op.add_column("contacts", sa.Column("additional_phones", JSONB, nullable=True))
    op.add_column("contacts", sa.Column("website", sa.Text, nullable=True))
    op.add_column("contacts", sa.Column("address", sa.Text, nullable=True))
    op.add_column("contacts", sa.Column("city", sa.String(100), nullable=True))
    op.add_column("contacts", sa.Column("state", sa.String(100), nullable=True))
    op.add_column("contacts", sa.Column("postal_code", sa.String(20), nullable=True))
    op.add_column("contacts", sa.Column("country", sa.String(100), nullable=True))
    op.add_column("contacts", sa.Column("notes", sa.Text, nullable=True))
    op.add_column("contacts", sa.Column("card_image", sa.Text, nullable=True))


def downgrade():
    op.drop_column("contacts", "card_image")
    op.drop_column("contacts", "notes")
    op.drop_column("contacts", "country")
    op.drop_column("contacts", "postal_code")
    op.drop_column("contacts", "state")
    op.drop_column("contacts", "city")
    op.drop_column("contacts", "address")
    op.drop_column("contacts", "website")
    op.drop_column("contacts", "additional_phones")
    op.drop_column("contacts", "fax")
    op.drop_column("contacts", "mobile")
    op.drop_column("contacts", "department")
