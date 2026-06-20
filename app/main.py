"""Landline Backend — B2B Lead Generation API for Apprend Technologies.

Main FastAPI application entry point. Wires up routers, middleware, and lifecycle events.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import companies, contacts, leads, outreach, dashboard, import_export, email_templates, follow_ups

settings = get_settings()

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    logger.info("Starting Landline API (%s)", settings.app_env)
    yield
    logger.info("Shutting down Landline API")
    # Close the database engine
    from app.database import engine
    await engine.dispose()


app = FastAPI(
    title="Landline — Lead Generation API",
    description=(
        "B2B lead generation backend for Apprend Technologies. "
        "Discovers, scores, and engages manufacturing companies in the US and Canada."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers under the API prefix
app.include_router(companies.router, prefix=settings.api_prefix)
app.include_router(contacts.router, prefix=settings.api_prefix)
app.include_router(leads.router, prefix=settings.api_prefix)
app.include_router(outreach.router, prefix=settings.api_prefix)
app.include_router(dashboard.router, prefix=settings.api_prefix)
app.include_router(import_export.router, prefix=settings.api_prefix)
app.include_router(email_templates.router, prefix=settings.api_prefix)
app.include_router(follow_ups.router, prefix=settings.api_prefix)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
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
