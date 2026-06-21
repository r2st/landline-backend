"""Separate async engine & session factory for Celery tasks.

The main database.py creates an engine that is bound to the FastAPI event
loop at import time.  Celery tasks run in their own thread with a fresh
event loop created by _run_async(), so using the same engine causes:

    RuntimeError: ... got Future ... attached to a different loop

This module provides celery_session() which creates a fresh
engine + session on every call, avoiding the cross-loop issue.
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from contextlib import asynccontextmanager

from app.config import get_settings


@asynccontextmanager
async def celery_session():
    """Create a fresh async engine and session for use inside a Celery task.
    
    The engine is disposed after the session closes, so no connections linger
    across event loops.
    """
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
    )
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = async_session()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()
