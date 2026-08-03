"""Public REST API v1 — a self-documented, API-key-authenticated surface.

This module builds a **mounted FastAPI sub-application** served at
``/api/v1/public``. It is a sub-app (not just a router) so it carries its own
OpenAPI schema and interactive docs at ``/api/v1/public/docs`` — an external
integrator's entry point, separate from the internal app's docs — with the
``X-API-Key`` security scheme advertised via :class:`APIKeyHeader`.

Authentication and tenant isolation:

* Every data endpoint depends on :func:`require_scope`, which authenticates the
  ``X-API-Key`` header, binds the key's org as the request context, enforces the
  per-key rate limit, and checks the endpoint's required scope.
* Because the org is bound, the global read filter and per-insert ``org_id``
  stamping isolate every query to the key's tenant automatically — a caller can
  only ever see or touch its own org's data. Parent-resource lookups (a
  contact's company, a deal's company, an enrollment's contact/sequence) are
  org-scoped reads, so referencing another org's row resolves to 404.

The parent app whitelists the ``/api/v1/public`` prefix in its auth middleware
(see :mod:`app.auth`) and shares its ``dependency_overrides`` with this sub-app
so tests can inject a test database.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.deal_activity import DealActivity
from app.models.deal_note import DealNote
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.schemas.company import CompanyCreate, CompanyRead, CompanyUpdate
from app.schemas.contact import ContactCreate, ContactRead, ContactUpdate
from app.schemas.deal import DealActivityRead, DealCreate, DealNoteCreate, DealRead, DealUpdate
from app.schemas.public_api import (
    DealNotePublicRead,
    EnrollmentCreate,
    EnrollmentRead,
    PaginatedMeta,
    SequenceRead,
)
from app.services.api_key_auth import ApiKeyContext, require_scope
from app.services.sql_search import LIKE_ESCAPE, contains_pattern

logger = logging.getLogger(__name__)


# --- Paginated response envelopes (concrete item types for a clean schema) ---


class ContactList(PaginatedMeta):
    items: list[ContactRead]


class CompanyList(PaginatedMeta):
    items: list[CompanyRead]


class DealList(PaginatedMeta):
    items: list[DealRead]


def _paginate(page: int, page_size: int) -> tuple[int, int]:
    return (page - 1) * page_size, page_size


def _newest_first(timestamp_column, id_column):
    """``ORDER BY <timestamp> DESC, <id> DESC`` — a *total* order.

    ``OFFSET``/``LIMIT`` over a non-total order is not a paging scheme. Every
    ``created_at`` in this schema is a ``server_default`` of ``now()``, which in
    Postgres is transaction start time, so a CSV import or a discovery run writes
    hundreds of rows sharing one reading to the microsecond. Below the timestamp
    the database is free to order the tie however it likes, and it is not obliged
    to answer page 1 and page 2 the same way — so a client walking the pages sees
    some rows twice and never sees others, with nothing on either page to suggest
    anything went wrong.

    This is the public API, where paging through the whole collection is the
    normal way to use it, and an org that bulk-imported its contacts is the case
    where *every* row ties.
    """
    return timestamp_column.desc(), id_column.desc()


# ============================================================================
# Contacts
# ============================================================================

contacts_router = APIRouter(prefix="/contacts", tags=["contacts"])


async def _company_in_scope(db: AsyncSession, company_id: uuid.UUID) -> Company:
    """Load a company in the caller's org, or 404 (never another org's)."""
    company = (
        await db.execute(select(Company).where(Company.id == company_id))
    ).scalar_one_or_none()
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@contacts_router.get("", response_model=ContactList)
async def list_contacts(
    company_id: Optional[uuid.UUID] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    ctx: ApiKeyContext = Depends(require_scope("contacts:read")),
    db: AsyncSession = Depends(get_db),
):
    """List contacts in your org, newest first. Filter by ``company_id``."""
    filters = []
    if company_id is not None:
        filters.append(Contact.company_id == company_id)

    total = (
        await db.execute(select(func.count(Contact.id)).where(*filters))
    ).scalar_one()
    offset, limit = _paginate(page, page_size)
    rows = (
        await db.execute(
            select(Contact).where(*filters)
            .order_by(*_newest_first(Contact.created_at, Contact.id))
            .offset(offset).limit(limit)
        )
    ).scalars().all()
    return ContactList(items=list(rows), total=total, page=page, page_size=page_size)


@contacts_router.post("", response_model=ContactRead, status_code=201)
async def create_contact(
    body: ContactCreate,
    ctx: ApiKeyContext = Depends(require_scope("contacts:write")),
    db: AsyncSession = Depends(get_db),
):
    """Create a contact under one of your companies."""
    await _company_in_scope(db, body.company_id)
    data = body.model_dump()
    additional = data.get("additional_phones")
    if additional is not None:
        data["additional_phones"] = [p if isinstance(p, dict) else p.model_dump() for p in additional]
    contact = Contact(**data)
    db.add(contact)
    await db.flush()
    await db.refresh(contact)
    logger.info("Public API created contact %s (org %s)", contact.id, ctx.org_id)
    return contact


async def _contact_or_404(db: AsyncSession, contact_id: uuid.UUID) -> Contact:
    contact = (
        await db.execute(select(Contact).where(Contact.id == contact_id))
    ).scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


@contacts_router.get("/{contact_id}", response_model=ContactRead)
async def get_contact(
    contact_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(require_scope("contacts:read")),
    db: AsyncSession = Depends(get_db),
):
    """Fetch a single contact by id."""
    return await _contact_or_404(db, contact_id)


@contacts_router.patch("/{contact_id}", response_model=ContactRead)
async def update_contact(
    contact_id: uuid.UUID,
    body: ContactUpdate,
    ctx: ApiKeyContext = Depends(require_scope("contacts:write")),
    db: AsyncSession = Depends(get_db),
):
    """Update mutable fields on a contact."""
    contact = await _contact_or_404(db, contact_id)
    changes = body.model_dump(exclude_unset=True)
    if "additional_phones" in changes and changes["additional_phones"] is not None:
        changes["additional_phones"] = [
            p if isinstance(p, dict) else p.model_dump() for p in changes["additional_phones"]
        ]
    for field, value in changes.items():
        setattr(contact, field, value)
    await db.flush()
    await db.refresh(contact)
    return contact


@contacts_router.delete("/{contact_id}", status_code=204)
async def delete_contact(
    contact_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(require_scope("contacts:write")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a contact."""
    contact = await _contact_or_404(db, contact_id)
    await db.delete(contact)
    await db.flush()


# ============================================================================
# Companies
# ============================================================================

companies_router = APIRouter(prefix="/companies", tags=["companies"])


@companies_router.get("", response_model=CompanyList)
async def list_companies(
    q: Optional[str] = Query(None, description="Case-insensitive name substring"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    ctx: ApiKeyContext = Depends(require_scope("companies:read")),
    db: AsyncSession = Depends(get_db),
):
    """List companies in your org, newest first. Optional name search via ``q``."""
    filters = []
    if q:
        filters.append(Company.name.ilike(contains_pattern(q), escape=LIKE_ESCAPE))
    total = (
        await db.execute(select(func.count(Company.id)).where(*filters))
    ).scalar_one()
    offset, limit = _paginate(page, page_size)
    rows = (
        await db.execute(
            select(Company).where(*filters)
            .order_by(*_newest_first(Company.created_at, Company.id))
            .offset(offset).limit(limit)
        )
    ).scalars().all()
    return CompanyList(items=list(rows), total=total, page=page, page_size=page_size)


@companies_router.post("", response_model=CompanyRead, status_code=201)
async def create_company(
    body: CompanyCreate,
    ctx: ApiKeyContext = Depends(require_scope("companies:write")),
    db: AsyncSession = Depends(get_db),
):
    """Create a company."""
    company = Company(**body.model_dump())
    db.add(company)
    await db.flush()
    await db.refresh(company)
    logger.info("Public API created company %s (org %s)", company.id, ctx.org_id)
    return company


async def _company_or_404(db: AsyncSession, company_id: uuid.UUID) -> Company:
    return await _company_in_scope(db, company_id)


@companies_router.get("/{company_id}", response_model=CompanyRead)
async def get_company(
    company_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(require_scope("companies:read")),
    db: AsyncSession = Depends(get_db),
):
    """Fetch a single company by id."""
    return await _company_or_404(db, company_id)


@companies_router.patch("/{company_id}", response_model=CompanyRead)
async def update_company(
    company_id: uuid.UUID,
    body: CompanyUpdate,
    ctx: ApiKeyContext = Depends(require_scope("companies:write")),
    db: AsyncSession = Depends(get_db),
):
    """Update mutable fields on a company."""
    company = await _company_or_404(db, company_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(company, field, value)
    await db.flush()
    await db.refresh(company)
    return company


@companies_router.delete("/{company_id}", status_code=204)
async def delete_company(
    company_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(require_scope("companies:write")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a company (cascades to its contacts and deals)."""
    company = await _company_or_404(db, company_id)
    await db.delete(company)
    await db.flush()


# ============================================================================
# Deals (pipeline)
# ============================================================================

deals_router = APIRouter(prefix="/deals", tags=["deals"])


@deals_router.get("", response_model=DealList)
async def list_deals(
    company_id: Optional[uuid.UUID] = Query(None),
    status_: Optional[str] = Query(None, alias="status", pattern="^(won|lost|pending)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    ctx: ApiKeyContext = Depends(require_scope("deals:read")),
    db: AsyncSession = Depends(get_db),
):
    """List deals in your org, newest first. Filter by ``company_id``/``status``."""
    filters = []
    if company_id is not None:
        filters.append(Deal.company_id == company_id)
    if status_ is not None:
        filters.append(Deal.status == status_)
    total = (await db.execute(select(func.count(Deal.id)).where(*filters))).scalar_one()
    offset, limit = _paginate(page, page_size)
    rows = (
        await db.execute(
            select(Deal).where(*filters)
            .order_by(*_newest_first(Deal.created_at, Deal.id))
            .offset(offset).limit(limit)
        )
    ).scalars().all()
    return DealList(items=list(rows), total=total, page=page, page_size=page_size)


@deals_router.post("", response_model=DealRead, status_code=201)
async def create_deal(
    body: DealCreate,
    ctx: ApiKeyContext = Depends(require_scope("deals:write")),
    db: AsyncSession = Depends(get_db),
):
    """Create a deal for one of your companies."""
    await _company_in_scope(db, body.company_id)
    deal = Deal(**body.model_dump())
    db.add(deal)
    await db.flush()
    await db.refresh(deal)
    db.add(DealActivity(
        deal_id=deal.id,
        activity_type="created",
        description=f"Deal '{deal.title}' created via public API",
        new_value=deal.status,
    ))
    await db.flush()
    logger.info("Public API created deal %s (org %s)", deal.id, ctx.org_id)
    return deal


async def _deal_or_404(db: AsyncSession, deal_id: uuid.UUID) -> Deal:
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if deal is None:
        raise HTTPException(status_code=404, detail="Deal not found")
    return deal


@deals_router.get("/{deal_id}", response_model=DealRead)
async def get_deal(
    deal_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(require_scope("deals:read")),
    db: AsyncSession = Depends(get_db),
):
    """Fetch a single deal by id."""
    return await _deal_or_404(db, deal_id)


@deals_router.patch("/{deal_id}", response_model=DealRead)
async def update_deal(
    deal_id: uuid.UUID,
    body: DealUpdate,
    ctx: ApiKeyContext = Depends(require_scope("deals:write")),
    db: AsyncSession = Depends(get_db),
):
    """Update mutable fields on a deal (stage/status changes are logged)."""
    deal = await _deal_or_404(db, deal_id)
    changes = body.model_dump(exclude_unset=True)
    if "deal_stage" in changes and changes["deal_stage"] != deal.deal_stage:
        db.add(DealActivity(
            deal_id=deal.id, activity_type="stage_change",
            description=f"Stage {deal.deal_stage} → {changes['deal_stage']}",
            old_value=deal.deal_stage, new_value=changes["deal_stage"],
        ))
    if "status" in changes and changes["status"] != deal.status:
        db.add(DealActivity(
            deal_id=deal.id, activity_type="status_changed",
            description=f"Status {deal.status} → {changes['status']}",
            old_value=deal.status, new_value=changes["status"],
        ))
    for field, value in changes.items():
        setattr(deal, field, value)
    await db.flush()
    await db.refresh(deal)
    return deal


@deals_router.delete("/{deal_id}", status_code=204)
async def delete_deal(
    deal_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(require_scope("deals:write")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a deal."""
    deal = await _deal_or_404(db, deal_id)
    await db.delete(deal)
    await db.flush()


# --- Deal activity / notes ---------------------------------------------------


@deals_router.get("/{deal_id}/notes", response_model=list[DealNotePublicRead])
async def list_deal_notes(
    deal_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(require_scope("deals:read")),
    db: AsyncSession = Depends(get_db),
):
    """List a deal's notes, newest first."""
    await _deal_or_404(db, deal_id)
    rows = (
        await db.execute(
            select(DealNote).where(DealNote.deal_id == deal_id)
            .order_by(*_newest_first(DealNote.created_at, DealNote.id))
        )
    ).scalars().all()
    return list(rows)


@deals_router.post("/{deal_id}/notes", response_model=DealNotePublicRead, status_code=201)
async def add_deal_note(
    deal_id: uuid.UUID,
    body: DealNoteCreate,
    ctx: ApiKeyContext = Depends(require_scope("deals:write")),
    db: AsyncSession = Depends(get_db),
):
    """Add a note to a deal (attributed to the API key, no in-app user)."""
    await _deal_or_404(db, deal_id)
    note = DealNote(deal_id=deal_id, content=body.content, user_id=None)
    db.add(note)
    await db.flush()
    await db.refresh(note)
    db.add(DealActivity(
        deal_id=deal_id, activity_type="note_added",
        description="Note added via public API",
    ))
    await db.flush()
    return note


@deals_router.get("/{deal_id}/activity", response_model=list[DealActivityRead])
async def list_deal_activity(
    deal_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(require_scope("deals:read")),
    db: AsyncSession = Depends(get_db),
):
    """Get a deal's activity timeline, newest first."""
    await _deal_or_404(db, deal_id)
    rows = (
        await db.execute(
            select(DealActivity).where(DealActivity.deal_id == deal_id)
            .order_by(*_newest_first(DealActivity.created_at, DealActivity.id))
        )
    ).scalars().all()
    return list(rows)


# ============================================================================
# Sequences & enrollments
# ============================================================================

sequences_router = APIRouter(tags=["sequences"])


@sequences_router.get("/sequences", response_model=list[SequenceRead])
async def list_sequences(
    ctx: ApiKeyContext = Depends(require_scope("sequences:read")),
    db: AsyncSession = Depends(get_db),
):
    """List your org's outreach sequences."""
    rows = (
        await db.execute(
            select(OutreachSequence)
            .order_by(*_newest_first(OutreachSequence.created_at, OutreachSequence.id))
        )
    ).scalars().all()
    return list(rows)


@sequences_router.get("/enrollments", response_model=list[EnrollmentRead])
async def list_enrollments(
    contact_id: Optional[uuid.UUID] = Query(None),
    sequence_id: Optional[uuid.UUID] = Query(None),
    status_: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    ctx: ApiKeyContext = Depends(require_scope("sequences:read")),
    db: AsyncSession = Depends(get_db),
):
    """List sequence enrollments, filterable by contact, sequence, or status."""
    filters = []
    if contact_id is not None:
        filters.append(OutreachEnrollment.contact_id == contact_id)
    if sequence_id is not None:
        filters.append(OutreachEnrollment.sequence_id == sequence_id)
    if status_ is not None:
        filters.append(OutreachEnrollment.status == status_)
    offset, limit = _paginate(page, page_size)
    rows = (
        await db.execute(
            select(OutreachEnrollment).where(*filters)
            .order_by(*_newest_first(OutreachEnrollment.enrolled_at, OutreachEnrollment.id))
            .offset(offset).limit(limit)
        )
    ).scalars().all()
    return list(rows)


@sequences_router.post("/enrollments", response_model=EnrollmentRead, status_code=201)
async def create_enrollment(
    body: EnrollmentCreate,
    ctx: ApiKeyContext = Depends(require_scope("sequences:write")),
    db: AsyncSession = Depends(get_db),
):
    """Enroll a contact into a sequence.

    Both the contact and the sequence must belong to your org (they resolve via
    org-scoped reads, so referencing another org's row is a 404). Re-enrolling a
    contact already active in the same sequence is a 409.
    """
    contact = (
        await db.execute(select(Contact).where(Contact.id == body.contact_id))
    ).scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    sequence = (
        await db.execute(select(OutreachSequence).where(OutreachSequence.id == body.sequence_id))
    ).scalar_one_or_none()
    if sequence is None:
        raise HTTPException(status_code=404, detail="Sequence not found")

    # One row answers "already enrolled?". Nothing in the schema stops a second
    # active enrollment for the pair, so asserting a single one would answer a
    # duplicate with 500 instead of the 409 below — see the same check on
    # ``POST /outreach/enrollments``.
    existing = (
        await db.execute(
            select(OutreachEnrollment.id)
            .where(
                OutreachEnrollment.contact_id == body.contact_id,
                OutreachEnrollment.sequence_id == body.sequence_id,
                OutreachEnrollment.status == "active",
            )
            .limit(1)
        )
    ).scalars().first()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="Contact is already actively enrolled in this sequence"
        )

    enrollment = OutreachEnrollment(
        contact_id=body.contact_id,
        sequence_id=body.sequence_id,
        status="active",
        current_step=1,
    )
    db.add(enrollment)
    await db.flush()
    await db.refresh(enrollment)
    logger.info("Public API enrolled contact %s in sequence %s (org %s)",
                body.contact_id, body.sequence_id, ctx.org_id)
    return enrollment


@sequences_router.get("/enrollments/{enrollment_id}", response_model=EnrollmentRead)
async def get_enrollment(
    enrollment_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(require_scope("sequences:read")),
    db: AsyncSession = Depends(get_db),
):
    """Fetch a single enrollment by id."""
    enrollment = (
        await db.execute(select(OutreachEnrollment).where(OutreachEnrollment.id == enrollment_id))
    ).scalar_one_or_none()
    if enrollment is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    return enrollment


@sequences_router.delete("/enrollments/{enrollment_id}", response_model=EnrollmentRead)
async def cancel_enrollment(
    enrollment_id: uuid.UUID,
    ctx: ApiKeyContext = Depends(require_scope("sequences:write")),
    db: AsyncSession = Depends(get_db),
):
    """Cancel (pause) an enrollment so it stops sending. Idempotent."""
    enrollment = (
        await db.execute(select(OutreachEnrollment).where(OutreachEnrollment.id == enrollment_id))
    ).scalar_one_or_none()
    if enrollment is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    enrollment.status = "paused"
    enrollment.next_send_at = None
    await db.flush()
    await db.refresh(enrollment)
    return enrollment


# ============================================================================
# The mounted sub-application
# ============================================================================

PUBLIC_API_DESCRIPTION = (
    "Landline public REST API. Authenticate every request with an "
    "`X-API-Key` header (create keys under your org's API-key settings). "
    "All data is scoped to the organization that owns the key."
)


def build_public_app() -> FastAPI:
    """Construct the public-API sub-application.

    Isolated OpenAPI + docs at ``/api/v1/public/docs``. The ``X-API-Key`` scheme
    is advertised automatically via the :class:`APIKeyHeader` dependency each
    route carries.
    """
    public_app = FastAPI(
        title="Landline Public API",
        version="1.0.0",
        description=PUBLIC_API_DESCRIPTION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    public_app.include_router(contacts_router)
    public_app.include_router(companies_router)
    public_app.include_router(deals_router)
    public_app.include_router(sequences_router)

    @public_app.get("/health", tags=["meta"], include_in_schema=False)
    async def public_health():
        return {"status": "healthy", "api": "landline-public-v1"}

    return public_app


public_app = build_public_app()
