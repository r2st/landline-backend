"""Multi-tenancy foundation — org-based data isolation.

Chains off ``077_sender_health_snapshot`` (the current head).

Turns Landline into a multi-tenant SaaS by making the organization the isolation
boundary for every tenant-owned row. In one migration:

1. Rename ``tenants`` → ``organizations`` (all existing FKs follow the table
   automatically in Postgres, so ``users``/``integrations``/``workflows``/…
   ``org_id`` now reference ``organizations.id`` with no extra DDL).
2. Widen the billing-plan CHECK to include ``pro`` and create the first-party
   "Apprend Technologies" organization with a fixed, well-known id
   (:data:`APPREND_ORG_ID`) so the application-layer default and DB rows agree.
3. Migrate the ``users.role`` CHECK values: ``admin→org_admin``,
   ``manager→org_manager``, ``agent→org_agent`` and allow ``platform_admin``.
4. Add ``org_id`` (UUID FK → organizations, ``server_default`` = Apprend) to the
   67 tenant-owned tables that lacked it, set NOT NULL, and index it.
5. Backfill every pre-existing row (in the new tables and in the 4 tables that
   already had a nullable ``org_id``) to Apprend, then tighten NOT NULL.

``users.org_id`` is intentionally left NULLABLE — ``platform_admin`` accounts are
not bound to any org. ``organizations`` and ``tenant_usage`` (already scoped by
``tenant_id``) get no ``org_id``.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "078_multi_tenancy"
down_revision = "077_sender_health_snapshot"
branch_labels = None
depends_on = None

# Fixed id of the first-party org. MUST match app.org_context.APPREND_ORG_ID.
APPREND_ORG_ID = "a9910000-0000-4000-8000-000000000001"
APPREND_ORG_SLUG = "apprend-technologies"

BILLING_PLANS_SQL = "'free','starter','pro','growth','enterprise'"

# The 67 tenant-owned tables that had no org_id. Order is irrelevant.
NEW_ORG_TABLES = [
    "ab_test_assignments", "ab_test_experiments", "ab_test_variants", "ab_tests",
    "agent_daily_summaries", "ai_email_drafts", "api_credit_ledger", "bounce_records",
    "call_notifications", "call_recordings", "call_scripts", "call_tasks", "client_health",
    "companies", "company_news", "company_research", "competitor_mentions", "competitors",
    "consent_records", "contact_engagement_profiles", "contacts", "content_embeddings",
    "conversation_analyses", "danger_signal_events", "deal_activities", "deal_notes",
    "deal_predictions", "deals", "discovered_businesses", "discovery_searches",
    "domain_health", "email_replies", "email_sends", "email_sentiment_scores",
    "email_signatures", "email_templates", "email_test_exchanges", "follow_ups",
    "gmail_accounts", "icp_profiles", "industry_contacts", "intent_signals", "interactions",
    "investor_documents", "investor_interactions", "investors", "knowledge_documents",
    "lead_scores", "meeting_briefs", "orchestration_policies", "org_relationships",
    "outreach_enrollments", "outreach_events", "outreach_sequences", "prospect_candidates",
    "prospecting_runs", "rep_performance", "rep_profiles", "sender_health",
    "sender_health_snapshot", "sequence_steps", "smtp_accounts", "stakeholder_maps",
    "suggestion_outcomes", "system_settings", "tasks", "webhook_events",
]

# Tables that already had a NULLABLE org_id → organizations (via the rename).
# Backfill + tighten to NOT NULL. (users stays nullable — platform admins.)
RETARGET_NOT_NULL_TABLES = [
    "integrations", "webhook_subscriptions", "workflows", "workflow_runs",
]


def upgrade() -> None:
    conn = op.get_bind()

    # 1. tenants → organizations. Dependent FK constraints follow the table.
    op.rename_table("tenants", "organizations")
    # Keep constraint/index names tidy after the rename.
    op.execute("ALTER TABLE organizations RENAME CONSTRAINT ck_tenants_billing_plan TO ck_organizations_billing_plan")

    # 2. Widen the billing-plan CHECK to include 'pro', then seed Apprend.
    op.drop_constraint("ck_organizations_billing_plan", "organizations", type_="check")
    op.create_check_constraint(
        "ck_organizations_billing_plan", "organizations",
        f"billing_plan IN ({BILLING_PLANS_SQL})",
    )
    op.execute(
        sa.text(
            """
            INSERT INTO organizations (id, name, slug, is_active, billing_plan, config, branding)
            VALUES (CAST(:id AS uuid), 'Apprend Technologies', :slug, true, 'pro', '{}'::jsonb, '{}'::jsonb)
            ON CONFLICT (slug) DO NOTHING
            """
        ).bindparams(id=APPREND_ORG_ID, slug=APPREND_ORG_SLUG)
    )
    # Resolve the org id actually present (normally == APPREND_ORG_ID).
    org_id = conn.execute(
        sa.text("SELECT id FROM organizations WHERE slug = :slug").bindparams(slug=APPREND_ORG_SLUG)
    ).scalar_one()

    # 3. Role enum values: admin→org_admin, manager→org_manager, agent→org_agent,
    #    and permit platform_admin.
    # IF EXISTS: some environments' ``users`` table was created without a named
    # role CHECK (schema drift), so an unconditional DROP would fail there.
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_role")
    op.execute("UPDATE users SET role = 'org_admin'   WHERE role = 'admin'")
    op.execute("UPDATE users SET role = 'org_manager' WHERE role = 'manager'")
    op.execute("UPDATE users SET role = 'org_agent'   WHERE role = 'agent'")
    op.create_check_constraint(
        "ck_users_role", "users",
        "role IN ('org_agent','org_manager','org_admin','platform_admin')",
    )

    # 4. Existing users: any NULL org_id (the old "default first-party tenant")
    #    now belongs to Apprend. Column stays nullable for platform admins.
    op.execute(
        sa.text("UPDATE users SET org_id = :org WHERE org_id IS NULL").bindparams(org=org_id)
    )

    # 5. The 4 tables that already had a nullable org_id: backfill + NOT NULL.
    for table in RETARGET_NOT_NULL_TABLES:
        op.execute(
            sa.text(f"UPDATE {table} SET org_id = :org WHERE org_id IS NULL").bindparams(org=org_id)
        )
        op.alter_column(table, "org_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)

    # 6. The 67 new tables: add org_id (server_default = Apprend fills existing
    #    rows), tighten NOT NULL, index. FK CASCADE so deleting an org clears it.
    for table in NEW_ORG_TABLES:
        op.add_column(
            table,
            sa.Column(
                "org_id", postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=True, server_default=APPREND_ORG_ID,
            ),
        )
        # Backfill defensively (server_default already covers existing rows).
        op.execute(
            sa.text(f"UPDATE {table} SET org_id = :org WHERE org_id IS NULL").bindparams(org=org_id)
        )
        op.alter_column(table, "org_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
        op.create_index(f"ix_{table}_org_id", table, ["org_id"])


def downgrade() -> None:
    # 6. Drop org_id from the new tables.
    for table in NEW_ORG_TABLES:
        op.drop_index(f"ix_{table}_org_id", table_name=table)
        op.drop_column(table, "org_id")

    # 5. Loosen the retargeted tables back to nullable.
    for table in RETARGET_NOT_NULL_TABLES:
        op.alter_column(table, "org_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)

    # 3. Revert role values.
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_role")
    op.execute("UPDATE users SET role = 'admin'   WHERE role IN ('org_admin','platform_admin')")
    op.execute("UPDATE users SET role = 'manager' WHERE role = 'org_manager'")
    op.execute("UPDATE users SET role = 'agent'   WHERE role = 'org_agent'")
    op.create_check_constraint(
        "ck_users_role", "users", "role IN ('admin','manager','agent')",
    )

    # 2. Remove the Apprend org and restore the original billing-plan CHECK.
    op.execute(sa.text("DELETE FROM organizations WHERE slug = :slug").bindparams(slug=APPREND_ORG_SLUG))
    op.drop_constraint("ck_organizations_billing_plan", "organizations", type_="check")
    op.create_check_constraint(
        "ck_organizations_billing_plan", "organizations",
        "billing_plan IN ('free','starter','growth','enterprise')",
    )
    op.execute("ALTER TABLE organizations RENAME CONSTRAINT ck_organizations_billing_plan TO ck_tenants_billing_plan")

    # 1. organizations → tenants.
    op.rename_table("organizations", "tenants")
