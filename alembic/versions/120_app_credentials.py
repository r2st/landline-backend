"""Platform credentials, encrypted, editable from the dashboard.

Every API credential the app holds arrived only through the environment, so
rotating one was an SSH session, an edit to ``.env.production`` and a restart —
a deploy for a value change. The Google OAuth client is the case that forces the
issue: when the Cloud project behind it closes, Gmail connectivity is down until
someone with shell access is available.

A row here overrides the environment for its key; **no row means the environment
value stands**. So this table is a set of deltas and an empty one is a no-op:
nothing about an existing install changes when this migration runs.

Deliberately **not** org-scoped, which is the one thing to understand before
extending it. These credentials identify the *deployment* to a third party — one
Google Cloud project, one registered redirect URI, one set of provider accounts
— and are resolved on paths where no org is bound at all: the OAuth callback
runs before any user context exists, as do the beat sweeps. Making it per-tenant
would also hand a tenant admin the ability to write the platform's Google client
credentials and point the consent flow at a client they control. Only
``platform_admin`` may read or write the table; the exemption is recorded in
``tests/test_org_scoping_coverage.py`` with that reason.

``value_encrypted`` uses the same Fernet key as the stored OAuth refresh tokens
(``TOKEN_ENCRYPTION_KEY``). The column holds live client secrets and provider
keys, so a database dump must not be a credential dump.

Revision ID: 120_app_credentials
Revises: 119_contacts_email_lower_index
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "120_app_credentials"
down_revision = "119_contacts_email_lower_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_credentials",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", postgresql.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
    )
    # Unique, not merely indexed: "the override for this key" is one value, and
    # two rows would make which credential is live depend on row order.
    op.create_index(
        "ix_app_credentials_key", "app_credentials", ["key"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_app_credentials_key", table_name="app_credentials")
    op.drop_table("app_credentials")
