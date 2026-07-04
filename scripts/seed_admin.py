#!/usr/bin/env python3
"""Seed the default admin user.

Usage:
    python scripts/seed_admin.py

Creates an admin user (admin@landline.ai / admin123) if one doesn't exist.
"""

import asyncio
import sys
from pathlib import Path

# Ensure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory, engine, Base
from app.models.user import User
from app.security import hash_password

DEFAULT_EMAIL = "admin@landline.ai"
DEFAULT_PASSWORD = "admin123"
DEFAULT_NAME = "Admin"


async def seed():
    async with async_session_factory() as session:
        existing = (
            await session.execute(select(User).where(User.email == DEFAULT_EMAIL))
        ).scalar_one_or_none()

        if existing:
            print(f"Admin user already exists: {existing.email} (id={existing.id})")
            return

        admin = User(
            email=DEFAULT_EMAIL,
            name=DEFAULT_NAME,
            role="admin",
            password_hash=hash_password(DEFAULT_PASSWORD),
            is_active=True,
            must_change_password=True,
        )
        session.add(admin)
        await session.commit()
        print(f"Created admin user: {admin.email} (id={admin.id})")
        print(f"  Password: {DEFAULT_PASSWORD}")
        print("  ⚠  must_change_password is set — user will be prompted on first login.")


if __name__ == "__main__":
    asyncio.run(seed())
