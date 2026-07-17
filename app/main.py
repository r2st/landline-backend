"""Landline Backend — B2B Lead Generation API for Apprend Technologies.

Main FastAPI application entry point. Wires up routers, middleware, and lifecycle events.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.auth import ApiKeyMiddleware
from app.dependencies import get_current_user
from app.rate_limit import limiter
from app.security import get_configured_secret
from app.routers import auth, admin, companies, contacts, leads, outreach, dashboard, import_export, email_templates, email_signatures, follow_ups, gmail, smtp, discovery, tracking, ai, groups, news, tactics, pipeline, activity, sequences, suggestions, deals, tasks, interactions, health, clients, reports, emails, knowledge, calls, investors, sentiment, email_testing, intent, coaching, competitive, routing, tenants, integrations, workflows, prospecting, deliverability, orchestration, relationships, meetings, system, email_test, dialtone, replies, domain_health, danger_signals, ab_tests, send_timing, verification, ai_drafts, platform, org

settings = get_settings()


def require_secret_key() -> None:
    """Refuse to boot without a configured signing secret.

    Signing JWTs with a hardcoded default would let anyone mint valid tokens,
    so a missing secret is a fatal misconfiguration rather than something to
    paper over with a fallback.
    """
    if not get_configured_secret():
        raise RuntimeError(
            "Refusing to start: no signing secret configured. Set SECRET_KEY "
            "(or JWT_SECRET_KEY / TOKEN_ENCRYPTION_KEY) in the environment."
        )

# Configure logging — every line is tagged with the current org (multi-tenant
# observability), so a stray cross-tenant read/write is traceable to a request.
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | org=%(org_id)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
from app.org_context import OrgContextLogFilter  # noqa: E402

for _handler in logging.getLogger().handlers:
    _handler.addFilter(OrgContextLogFilter())
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    # Fail fast on a missing signing secret rather than booting insecurely.
    require_secret_key()
    logger.info("Starting Landline API (%s)", settings.app_env)
    yield
    logger.info("Shutting down Landline API")
    # Close the database engine
    from app.database import engine
    await engine.dispose()


# Hide interactive API docs in production — the schema is an information-
# disclosure surface (enumerates every endpoint, model, and auth scheme).
_is_prod = settings.app_env.lower() == "production"

app = FastAPI(
    title="Landline — Lead Generation API",
    description=(
        "B2B lead generation backend for Apprend Technologies. "
        "Discovers, scores, and engages manufacturing companies in the US and Canada."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

# Rate limiting (slowapi) — endpoints opt in via @limiter.limit(...).
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API key / JWT authentication — ALWAYS installed. The middleware fails closed:
# every non-public request must present a valid API key or Bearer token.
app.add_middleware(ApiKeyMiddleware)

# Dependency that enforces an authenticated, active user on a whole router.
_auth = [Depends(get_current_user)]

# Mount routers under the API prefix
app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(admin.router, prefix=settings.api_prefix)
app.include_router(companies.router, prefix=settings.api_prefix, dependencies=_auth)
# contacts: was mounted WITHOUT auth, so its endpoints (none of which take a
# user/scope dependency) ran with no org context bound → the global read filter
# was a no-op and they saw/wrote every tenant's contacts. Require auth so the
# caller's org is bound on every request.
app.include_router(contacts.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(leads.router, prefix=settings.api_prefix, dependencies=_auth)
# The public SES webhook lives on outreach.public_router (stays unauthenticated);
# the main outreach.router (sequences, enrollments, events, stats) is org-scoped
# data and none of its endpoints take a user dependency, so it MUST require auth
# to bind org context — otherwise it ran unscoped across all tenants.
app.include_router(outreach.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(outreach.public_router, prefix=settings.api_prefix)
app.include_router(dashboard.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(import_export.router, prefix=settings.api_prefix, dependencies=_auth)
# email_templates/deals/emails/follow_ups/calls were all mounted WITHOUT auth
# and have endpoints that take no user/scope dependency, so they ran unscoped
# across all tenants. Require auth so org context is bound (see contacts above).
app.include_router(email_templates.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(email_signatures.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(follow_ups.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(gmail.router, prefix=settings.api_prefix)
app.include_router(smtp.router, prefix=settings.api_prefix)
app.include_router(discovery.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(tracking.router, prefix=settings.api_prefix)
app.include_router(ai.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(groups.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(news.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(tactics.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(pipeline.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(activity.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(sequences.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(suggestions.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(deals.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(tasks.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(interactions.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(health.router, prefix=settings.api_prefix)
app.include_router(clients.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(reports.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(emails.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(knowledge.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(calls.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(investors.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(sentiment.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(email_testing.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(intent.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(coaching.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(competitive.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(routing.router, prefix=settings.api_prefix, dependencies=_auth)
# Phase 4 — Platform & Ecosystem
app.include_router(tenants.router, prefix=settings.api_prefix, dependencies=_auth)
# SaaS Phase 2 — Platform Admin (cross-org management; platform_admin only).
app.include_router(platform.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(integrations.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(workflows.router, prefix=settings.api_prefix, dependencies=_auth)
# Phase 3 — Advanced AI & Autonomous Operations
app.include_router(prospecting.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(deliverability.router, prefix=settings.api_prefix, dependencies=_auth)
# Phase 2 — Orchestration, relationship intelligence, meeting briefings
app.include_router(orchestration.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(relationships.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(meetings.router, prefix=settings.api_prefix, dependencies=_auth)
# System control — global sending kill switch + warm-up reset.
app.include_router(system.router, prefix=settings.api_prefix, dependencies=_auth)
# Test-send + deliverability verification.
app.include_router(email_test.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(dialtone.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(danger_signals.router, prefix=settings.api_prefix, dependencies=_auth)
app.include_router(replies.router, prefix=settings.api_prefix, dependencies=_auth)
# Feature 5 — Domain Health Dashboard (SPF/DKIM/DMARC + DNSBL, scored & persisted).
app.include_router(domain_health.router, prefix=settings.api_prefix, dependencies=_auth)
# Feature 3 — A/B Testing (variant persistence, assignment, results dashboard).
app.include_router(ab_tests.router, prefix=settings.api_prefix, dependencies=_auth)
# Human review queue for AI-composed first touches.
app.include_router(ai_drafts.router, prefix=settings.api_prefix, dependencies=_auth)
# Feature 6 — Smart Send-Time Optimization (per-recipient timezone/window + A/B).
app.include_router(send_timing.router, prefix=settings.api_prefix, dependencies=_auth)
# P3 — Hunter.io email verification (on-demand verify, bulk verify, stats).
app.include_router(verification.router, prefix=settings.api_prefix, dependencies=_auth)
# P4 — Google Places SMB discovery. A second router out of routers/discovery.py,
# on the /dialtone/discovery prefix; the /discovery one above is Apollo's.
app.include_router(discovery.places_router, prefix=settings.api_prefix, dependencies=_auth)
# Phase 3 — organization self-service management (settings, team, usage). Auth
# is required; per-route OrgScope enforces org_admin for writes.
app.include_router(org.router, prefix=settings.api_prefix, dependencies=_auth)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "app": settings.app_name,
        "env": settings.app_env,
        "version": "1.0.0",
    }


@app.get(f"{settings.api_prefix}/health")
async def api_health_check():
    """Health check under the API prefix."""
    return {
        "status": "healthy",
        "app": settings.app_name,
        "env": settings.app_env,
        "version": "1.0.0",
    }


@app.get("/")
async def root():
    """Root endpoint — API info."""
    return {
        "name": "Landline Lead Generation API",
        "by": "Apprend Technologies",
        "docs": "/docs",
        "health": "/health",
    }
