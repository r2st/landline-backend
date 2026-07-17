# Landline SaaS Multi-Tenancy Design Doc

## Status: DRAFT — Awaiting Approval

## Goal

Convert Landline from a single-tenant application into a multi-tenant SaaS product where all data is isolated by organization. Migrate existing data into the organization "Apprend Technologies" and introduce a role hierarchy with platform-level and org-level admins.

---

## Current State

- **Tenant model exists** (`tenants` table) with billing, branding, config, usage metering
- **User.org_id FK exists** (nullable, points to `tenants.id`)
- **Only 6 of 74 tables have org_id**: User, Integration, WebhookSubscription, Workflow, WorkflowRun, TenantUsage
- **68 tables have NO org isolation** — data is either user-scoped or globally shared
- **UserScope** (in `dependencies.py`) filters by `user_id`, not org — so a user sees only their own data, but there's no org boundary
- **3 roles**: `admin`, `manager`, `agent` — flat enum, no platform-level distinction
- **JWT claims**: `user_id` + `role` only — no org context

---

## Design

### 1. Role Hierarchy

```
platform_admin          ← Manages ALL organizations, billing, platform settings
  └── org_admin         ← Manages their org: users, settings, integrations
       └── org_manager  ← Manages team: assigns leads, reviews deals, views reports
            └── org_agent  ← Individual contributor: calls, emails, prospecting
```

| Role | Scope | Can Do |
|------|-------|--------|
| `platform_admin` | Global | CRUD orgs, impersonate users, platform settings, billing, view all data |
| `org_admin` | Own org | Invite/remove users, org settings, integrations, Dialtone config, view all org data |
| `org_manager` | Own org | Assign leads, manage deals/pipelines, view team reports, approve AI drafts |
| `org_agent` | Own org | Own contacts/deals/calls/emails, prospecting, Dialtone inbox |

**Implementation:**
- Replace `UserRole` enum: `platform_admin`, `org_admin`, `org_manager`, `org_agent`
- `platform_admin` users have `org_id = NULL` (not bound to any org, can access all)
- All other roles require a non-null `org_id`
- Migration maps: `admin` → `org_admin`, `manager` → `org_manager`, `agent` → `org_agent`

### 2. Organization (Tenant) Model Changes

The existing `Tenant` model becomes the `Organization`. Rename for clarity:

```python
class Organization(Base):
    __tablename__ = "organizations"  # rename from "tenants"
    
    id: int (PK)
    name: str                    # "Apprend Technologies"
    slug: str (unique)           # "apprend-technologies"
    is_active: bool
    industry: str
    config: JSONB                # org-level settings (Dialtone, AI, etc.)
    branding: JSONB              # logo, colors, custom domain
    billing_plan: str            # free, starter, pro, enterprise
    billing_status: str          # active, trial, suspended
    trial_ends_at: datetime
    ai_monthly_call_limit: int
    max_users: int               # plan-based user limit
    created_at, updated_at
```

### 3. Adding org_id to All Tables

**68 tables** need `org_id` (FK to `organizations.id`, NOT NULL after backfill).

Migration strategy (single Alembic migration 078):
1. Add `org_id` column as **nullable** to all 68 tables
2. Create the "Apprend Technologies" organization record
3. Backfill: `UPDATE <table> SET org_id = <apprend_org_id>` for all existing rows
4. Set `org_id` to **NOT NULL**
5. Add composite indexes: `(org_id, <existing_key>)` on frequently queried tables
6. Rename `tenants` → `organizations` (with Alembic `op.rename_table`)

**Tables grouped by priority:**

Core CRM (17): companies, contacts, lead_scores, consent_records, deals, deal_activities, deal_notes, interactions, follow_ups, tasks, client_health, stakeholder_maps, org_relationships, meeting_briefs, investors, investor_interactions, investor_documents

Email/Outreach (18): outreach_sequences, sequence_steps, outreach_enrollments, outreach_events, email_templates, email_sends, email_replies, email_signatures, email_sentiment_scores, gmail_accounts, smtp_accounts, bounce_records, ai_email_drafts, contact_engagement_profiles, sender_health, sender_health_snapshot, domain_health, danger_signal_events

Prospecting/Discovery (7): icp_profiles, prospecting_runs, prospect_candidates, discovery_searches, discovered_businesses, company_news, company_research

AI/Analytics (8): ab_tests, ab_test_variants, ab_test_assignments, ab_test_experiments, email_test_exchanges, intent_signals, conversation_analyses, deal_predictions

Calling (4): call_tasks, call_scripts, call_notifications, call_recordings

Performance (4): rep_performance, rep_profiles, suggestion_outcomes, agent_daily_summaries

Infrastructure (6): orchestration_policies, content_embeddings, api_credit_ledger, system_settings, competitors, competitor_mentions

Already have org_id (update FK target): integrations, webhook_subscriptions, workflows, workflow_runs

### 4. Data Isolation Rewrite

Replace `UserScope` with `OrgScope`:

```python
class OrgScope:
    """Injected into every route. Provides org_id for all queries."""
    
    def __init__(self, user: User, db: Session):
        self.user = user
        self.org_id = user.org_id
        self.role = user.role
        self.db = db
    
    def query(self, model):
        """Always filter by org_id. Platform admins can override."""
        q = self.db.query(model)
        if self.role != "platform_admin":
            q = q.filter(model.org_id == self.org_id)
        return q
    
    def check_permission(self, required_role: str):
        """Role hierarchy check."""
        hierarchy = ["org_agent", "org_manager", "org_admin", "platform_admin"]
        if hierarchy.index(self.role) < hierarchy.index(required_role):
            raise HTTPException(403, "Insufficient permissions")
```

Every repository/service call must use `scope.query(Model)` instead of raw `db.query(Model)`.

### 5. JWT Changes

```python
# Current
{"user_id": 1, "role": "admin"}

# New
{"user_id": 1, "org_id": 5, "role": "org_admin"}
```

Platform admins get `org_id: null` in JWT. API supports `X-Org-Id` header for platform admins to impersonate an org.

### 6. API Changes

**New routes:**

```
# Platform admin
POST   /api/platform/organizations          # Create org
GET    /api/platform/organizations          # List all orgs
GET    /api/platform/organizations/:id      # Org detail
PUT    /api/platform/organizations/:id      # Update org
DELETE /api/platform/organizations/:id      # Deactivate org
GET    /api/platform/stats                  # Platform-wide stats
POST   /api/platform/impersonate/:user_id   # Get scoped token

# Org admin  
GET    /api/org/settings                    # Org settings
PUT    /api/org/settings                    # Update org settings
GET    /api/org/members                     # List org members
POST   /api/org/members/invite              # Invite user
PUT    /api/org/members/:id/role            # Change role
DELETE /api/org/members/:id                 # Remove user
GET    /api/org/usage                       # Org usage/billing
```

**Existing routes:** All stay the same but silently filter by `org_id` via `OrgScope`.

### 7. Frontend Changes

**New components:**
- `OrgContext` — stores current org in React context (from JWT)
- `OrgSwitcher` — platform admin dropdown to switch between orgs
- Platform Admin Dashboard (`/platform`) — org list, create org, platform stats
- Org Settings page (`/settings/organization`) — org details, branding, billing
- Team Management page (`/settings/team`) — invite, remove, change roles
- Role-based route guards — hide nav items and pages based on role
- Login flow — after auth, if platform_admin, show org picker; otherwise route to their org

**Existing pages:** No changes needed — they'll just see org-scoped data automatically.

### 8. Data Migration Plan

Single migration 078:

```sql
-- 1. Rename tenants → organizations
ALTER TABLE tenants RENAME TO organizations;

-- 2. Create "Apprend Technologies" org (upsert if tenant already exists)
INSERT INTO organizations (name, slug, is_active, billing_plan)
VALUES ('Apprend Technologies', 'apprend-technologies', true, 'pro')
ON CONFLICT (slug) DO NOTHING;

-- 3. Add org_id to all 68 tables (nullable first)
ALTER TABLE companies ADD COLUMN org_id INTEGER REFERENCES organizations(id);
-- ... repeat for all tables ...

-- 4. Backfill all existing data to Apprend Technologies
UPDATE companies SET org_id = (SELECT id FROM organizations WHERE slug = 'apprend-technologies');
-- ... repeat for all tables ...

-- 5. Set NOT NULL
ALTER TABLE companies ALTER COLUMN org_id SET NOT NULL;
-- ... repeat ...

-- 6. Update role enum
-- admin → org_admin, manager → org_manager, agent → org_agent

-- 7. Add composite indexes
CREATE INDEX ix_companies_org_id ON companies(org_id);
-- ... repeat for key tables ...
```

### 9. Implementation Phases

**Phase 1 — Foundation** (migration 078 + backend)
- Rename tenants → organizations
- Add org_id to all 68 tables + backfill
- New role enum
- OrgScope replaces UserScope
- JWT includes org_id
- All existing queries filter by org_id

**Phase 2 — Platform Admin** (backend + frontend)
- Platform admin routes (org CRUD, impersonation)
- Platform admin dashboard
- Org switcher

**Phase 3 — Org Management** (backend + frontend)
- Org settings, branding
- Team management (invite, roles)
- Usage/billing dashboard

**Phase 4 — Polish**
- Role-based UI guards (hide features by role)
- Org onboarding flow (new org signup)
- Billing integration (if needed)

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Migration takes down prod | Run backfill in batches; test on staging first |
| Missed org_id filter → data leak | Add DB-level RLS as backstop; audit all queries |
| Breaking existing API consumers | org_id is injected via scope, not required in request body |
| Role migration breaks current user's access | Map admin→org_admin preserves all existing permissions |

---

## Open Questions

1. Should `system_settings` remain global or become per-org? (Recommendation: per-org, with platform-level defaults)
2. Do we need per-org Dialtone configuration or is the current `config` JSONB on Organization sufficient?
3. Billing integration — Stripe? Razorpay? Or manual for now?
4. Custom domains per org?
