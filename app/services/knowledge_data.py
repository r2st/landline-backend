"""Loaders + idempotent seeding for the knowledge base.

This module is the single source of truth for turning the version-controlled
seed files (``data/industry_contacts.csv`` and the markdown docs under
``docs/``) into database rows. It is consumed by:

- the seed migration (``027_knowledge_base``), and
- the test suite (which unit-tests the pure loaders).

Seeding is idempotent: contacts are keyed by (company_name, contact_email) and
documents by their slug ``key``, so re-running a migration never duplicates rows.
"""

from __future__ import annotations

import csv
from pathlib import Path

# repo root = .../landline-backend (parents: [0]=services, [1]=app, [2]=root)
REPO_ROOT = Path(__file__).resolve().parents[2]
CONTACTS_CSV = REPO_ROOT / "data" / "industry_contacts.csv"
DOCS_DIR = REPO_ROOT / "docs"

# Markdown reference docs surfaced in the website knowledge section.
# (key, source filename, title, category)
DOCUMENT_SOURCES = [
    (
        "sales-psychology-guidelines",
        "sales-psychology-guidelines.md",
        "Sales Psychology & Cold Email Guidelines",
        "guidelines",
    ),
    (
        "contact-approach-strategy",
        "contact-approach-strategy.md",
        "Manufacturing Lead Contact & Approach Strategy",
        "strategy",
    ),
    (
        "cold-calling-strategy",
        "cold-calling-strategy.md",
        "Cold Calling Strategy",
        "strategy",
    ),
    (
        "digital-sales-strategy",
        "digital-sales-strategy.md",
        "Digital Sales Strategy",
        "strategy",
    ),
    (
        "cold-call-scripts-by-industry",
        "cold-call-scripts-by-industry.md",
        "Cold Call Scripts by Industry",
        "scripts",
    ),
    (
        "ai-cold-calling-guidelines",
        "ai-cold-calling-guidelines.md",
        "AI Cold Calling Guidelines",
        "guidelines",
    ),
]


def load_industry_contacts() -> list[dict]:
    """Parse the seed CSV into a list of contact dicts.

    Empty CSV cells become ``None`` so they map cleanly onto nullable columns.
    """
    rows: list[dict] = []
    with CONTACTS_CSV.open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            rows.append(
                {
                    "industry": (raw.get("industry") or "").strip(),
                    "company_name": (raw.get("company_name") or "").strip(),
                    "website": _clean(raw.get("website")),
                    "location": _clean(raw.get("location")),
                    "employee_count": _clean(raw.get("employee_count")),
                    "contact_name": _clean(raw.get("contact_name")),
                    "contact_title": _clean(raw.get("contact_title")),
                    "contact_email": _clean(raw.get("contact_email")),
                    "notes": _clean(raw.get("notes")),
                }
            )
    return rows


def load_documents() -> list[dict]:
    """Read the markdown reference docs into document dicts."""
    docs: list[dict] = []
    for key, filename, title, category in DOCUMENT_SOURCES:
        path = DOCS_DIR / filename
        docs.append(
            {
                "key": key,
                "title": title,
                "category": category,
                "format": "markdown",
                "content": path.read_text(encoding="utf-8"),
            }
        )
    return docs


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def seed_knowledge(conn) -> dict[str, int]:
    """Idempotently seed industry contacts and documents from the source files.

    Engine-agnostic (uses the raw connection, like ``seed_all_verticals``), so it
    runs inside Alembic migrations on Postgres. Returns counts of rows inserted.
    """
    import uuid as _uuid

    import sqlalchemy as sa

    inserted = {"contacts": 0, "documents": 0}

    for row in load_industry_contacts():
        if not row["company_name"]:
            continue
        existing = conn.execute(
            sa.text(
                "SELECT id FROM industry_contacts "
                "WHERE company_name = :company AND "
                "COALESCE(contact_email, '') = COALESCE(:email, '')"
            ),
            {"company": row["company_name"], "email": row["contact_email"]},
        ).first()
        if existing:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO industry_contacts "
                "(id, industry, company_name, website, location, employee_count, "
                " contact_name, contact_title, contact_email, notes) "
                "VALUES (:id, :industry, :company_name, :website, :location, "
                " :employee_count, :contact_name, :contact_title, :contact_email, :notes)"
            ),
            {"id": str(_uuid.uuid4()), **row},
        )
        inserted["contacts"] += 1

    for doc in load_documents():
        existing = conn.execute(
            sa.text("SELECT id FROM knowledge_documents WHERE key = :key"),
            {"key": doc["key"]},
        ).first()
        if existing:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO knowledge_documents "
                "(id, key, title, category, format, content) "
                "VALUES (:id, :key, :title, :category, :format, :content)"
            ),
            {"id": str(_uuid.uuid4()), **doc},
        )
        inserted["documents"] += 1

    return inserted
