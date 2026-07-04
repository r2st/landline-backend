#!/usr/bin/env python
"""Seed (or re-seed) the default admin user.

The 036 migration seeds the default admin automatically on ``alembic upgrade
head``. This script is a manual, idempotent alternative — handy for a database
that predates the migration seed, or to (re)create the admin without running
migrations.

It reads the default admin email / password / name from settings
(``DEFAULT_ADMIN_EMAIL``, ``DEFAULT_ADMIN_PASSWORD``, ``DEFAULT_ADMIN_NAME``),
falling back to ``admin@landline.app`` / ``admin123``.

Usage:
    python -m scripts.seed_admin
    DEFAULT_ADMIN_PASSWORD=s3cret python -m scripts.seed_admin
"""

import asyncio
import logging

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session_factory
from app.models.user import User, UserRole
from app.security import hash_password

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("seed_admin")


async def seed_admin() -> None:
    settings = get_settings()
    email = settings.default_admin_email.lower()

    async with async_session_factory() as session:
        existing = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()

        if existing is not None:
            logger.info("Admin %s already exists (id=%s) — nothing to do.", email, existing.id)
            return

        admin = User(
            email=email,
            name=settings.default_admin_name,
            role=UserRole.ADMIN.value,
            password_hash=hash_password(settings.default_admin_password),
            must_change_password=True,
        )
        session.add(admin)
        await session.commit()
        logger.info(
            "Created default admin %s (must_change_password=True). "
            "Log in and change the password immediately.",
            email,
        )


if __name__ == "__main__":
    asyncio.run(seed_admin())
