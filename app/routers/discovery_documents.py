"""Discovery from documents: upload → AI-extracted candidates → import.

Mounted at ``/discovery/documents`` (authenticated, org-scoped). The AI runs on
the org's own Gemini key (``services.org_gemini``); with none saved, upload is
refused with ``409 {"code": "gemini_key_required"}`` before anything is stored,
so the UI can send the user to Settings → API Keys.

See docs/features/discovery-document-outreach.md.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory, get_db
from app.dependencies import OrgScope, get_org_scope
from app.models.discovery_document import STATUS_PARSING, DiscoveryDocument
from app.services import discovery_documents as svc
from app.services import document_extract, org_gemini
from app.services.template_attachments import sanitize_filename

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discovery/documents", tags=["discovery"])

# Background jobs open their own sessions; tests point this at the test engine.
session_factory = async_session_factory

GEMINI_KEY_REQUIRED = {
    "code": "gemini_key_required",
    "message": "Add your Gemini API key in Settings → API Keys to use document discovery.",
}


class DocumentSummary(BaseModel):
    id: uuid.UUID
    filename: str
    file_type: str
    size_bytes: int
    status: str
    error: Optional[str] = None
    candidate_count: int = 0
    sequence_id: Optional[uuid.UUID] = None
    file_available: bool = True
    created_at: datetime


class DocumentDetail(DocumentSummary):
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)


class RejectedFile(BaseModel):
    filename: str
    error: str


class BatchUploadResult(BaseModel):
    documents: list[DocumentSummary]
    rejected: list[RejectedFile] = Field(default_factory=list)


# One request can carry this many files; each is still capped at
# ``document_extract.MAX_FILE_BYTES``.
MAX_BATCH_FILES = 10


class ImportRequest(BaseModel):
    candidate_ids: list[str] = Field(..., min_length=1, max_length=5000)


def _require_org(scope: OrgScope) -> uuid.UUID:
    if scope.org_id is None:
        raise HTTPException(status_code=400, detail="No organization context")
    return scope.org_id


def _summary(doc: DiscoveryDocument) -> dict:
    extracted = doc.extracted or {}
    return {
        "id": doc.id,
        "filename": doc.filename,
        "file_type": doc.file_type,
        "size_bytes": doc.size_bytes,
        "status": doc.status,
        "error": doc.error,
        "candidate_count": len(extracted.get("candidates", [])),
        "sequence_id": doc.sequence_id,
        "file_available": doc.content_purged_at is None,
        "created_at": doc.created_at,
    }


async def get_owned_document(
    db: AsyncSession, scope: OrgScope, document_id: uuid.UUID
) -> DiscoveryDocument:
    org_id = _require_org(scope)
    doc = (
        await db.execute(
            select(DiscoveryDocument).where(
                DiscoveryDocument.id == document_id,
                DiscoveryDocument.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.get("/ai-status")
async def ai_status(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Whether this org has a Gemini key — so the UI can show the banner up front."""
    org_id = _require_org(scope)
    return {"gemini_key_configured": await org_gemini.has_org_gemini_key(db, org_id)}


async def _store_upload(
    db: AsyncSession, scope: OrgScope, org_id: uuid.UUID, file: UploadFile
) -> DiscoveryDocument:
    """Validate and stage one file as a parsing document (raises DocumentRejected)."""
    content = await file.read(document_extract.MAX_FILE_BYTES + 1)
    filename = sanitize_filename(file.filename)
    file_type = document_extract.classify(filename, content)
    doc = DiscoveryDocument(
        org_id=org_id,
        filename=filename,
        file_type=file_type,
        size_bytes=len(content),
        content=content,
        status=STATUS_PARSING,
        created_by=scope.user.id,
    )
    db.add(doc)
    await db.flush()
    await db.refresh(doc)
    logger.info("Discovery document %s (%s, %d bytes) uploaded", doc.id, file_type, len(content))
    return doc


@router.post("", response_model=DocumentSummary, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Store the file and start extraction in the background (poll the detail)."""
    org_id = _require_org(scope)
    if not await org_gemini.has_org_gemini_key(db, org_id):
        raise HTTPException(status_code=409, detail=GEMINI_KEY_REQUIRED)

    try:
        doc = await _store_upload(db, scope, org_id, file)
    except document_extract.DocumentRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    summary = _summary(doc)
    # Commit before the background job runs so its own session can see the row.
    await db.commit()
    background.add_task(svc.process_document, session_factory, doc.id, org_id)
    return summary


@router.post("/batch", response_model=BatchUploadResult, status_code=status.HTTP_202_ACCEPTED)
async def upload_documents(
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Upload several files at once — each becomes its own document.

    Files are judged one by one: a bad file is listed in ``rejected`` and the
    rest still go through. Only when *no* file is usable is the request a 422.
    Extraction runs in the background, one document after another, so a batch
    doesn't fire every Gemini call at once.
    """
    org_id = _require_org(scope)
    if not await org_gemini.has_org_gemini_key(db, org_id):
        raise HTTPException(status_code=409, detail=GEMINI_KEY_REQUIRED)
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=422,
            detail=f"Upload at most {MAX_BATCH_FILES} files at a time.",
        )

    docs: list[DiscoveryDocument] = []
    rejected: list[dict] = []
    for file in files:
        try:
            docs.append(await _store_upload(db, scope, org_id, file))
        except document_extract.DocumentRejected as exc:
            rejected.append({"filename": sanitize_filename(file.filename), "error": str(exc)})

    if not docs:
        raise HTTPException(
            status_code=422,
            detail={"message": "None of these files could be used.", "rejected": rejected},
        )

    summaries = [_summary(d) for d in docs]
    # Commit before the background jobs run so their own sessions see the rows.
    await db.commit()
    for doc in docs:
        background.add_task(svc.process_document, session_factory, doc.id, org_id)
    return {"documents": summaries, "rejected": rejected}


@router.get("", response_model=list[DocumentSummary])
async def list_documents(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    org_id = _require_org(scope)
    docs = (
        await db.execute(
            select(DiscoveryDocument)
            .where(DiscoveryDocument.org_id == org_id)
            .order_by(DiscoveryDocument.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    for doc in docs:
        await svc.mark_stale(db, doc)
    return [_summary(d) for d in docs]


@router.get("/{document_id}", response_model=DocumentDetail)
async def get_document(
    document_id: uuid.UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    doc = await get_owned_document(db, scope, document_id)
    await svc.mark_stale(db, doc)
    extracted = doc.extracted or {}
    return {
        **_summary(doc),
        "candidates": extracted.get("candidates", []),
        "stats": extracted.get("stats", {}),
    }


@router.post("/{document_id}/import")
async def import_document_candidates(
    document_id: uuid.UUID,
    body: ImportRequest,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Create (or merge into existing) companies and contacts for the selection."""
    doc = await get_owned_document(db, scope, document_id)
    if doc.status != "ready":
        raise HTTPException(status_code=409, detail="This document hasn't finished processing.")
    return await svc.import_candidates(db, doc, body.candidate_ids)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Delete the upload and its extraction. Imported companies/contacts stay."""
    doc = await get_owned_document(db, scope, document_id)
    await db.delete(doc)
    await db.flush()


# ── Draft + send ─────────────────────────────────────────────────────────────

class DraftRequest(BaseModel):
    brief: str = Field(..., min_length=10, max_length=2000)
    tone: str = Field("professional")
    follow_ups: int = Field(2, ge=0, le=3)


class StepIn(BaseModel):
    subject: str = Field(..., min_length=1, max_length=300)
    body: str = Field(..., min_length=1, max_length=10000)
    delay_days: int = Field(0, ge=0, le=60)


class PreviewRequest(BaseModel):
    steps: list[StepIn] = Field(..., min_length=1, max_length=svc.MAX_STEPS)
    contact_id: Optional[uuid.UUID] = None


class SendRequest(BaseModel):
    contact_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=1000)
    steps: list[StepIn] = Field(..., min_length=1, max_length=svc.MAX_STEPS)
    name: Optional[str] = Field(None, max_length=255)
    tone: str = Field("professional")


def _require_ready(doc: DiscoveryDocument) -> None:
    if doc.status != "ready":
        raise HTTPException(status_code=409, detail="This document hasn't finished processing.")


@router.post("/{document_id}/draft")
async def draft_emails(
    document_id: uuid.UUID,
    body: DraftRequest,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Gemini-drafted first email + follow-ups (editable before sending)."""
    doc = await get_owned_document(db, scope, document_id)
    _require_ready(doc)
    if body.tone not in svc.DRAFT_TONES:
        raise HTTPException(status_code=422, detail=f"tone must be one of {', '.join(svc.DRAFT_TONES)}")
    try:
        steps = await svc.draft_steps(
            db, scope.org_id, doc,
            brief=body.brief, tone=body.tone, follow_ups=body.follow_ups,
        )
    except org_gemini.GeminiKeyMissing:
        raise HTTPException(status_code=409, detail=GEMINI_KEY_REQUIRED)
    except org_gemini.GeminiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"steps": steps, "merge_fields": list(svc.MERGE_FIELDS)}


@router.post("/{document_id}/preview")
async def preview_emails(
    document_id: uuid.UUID,
    body: PreviewRequest,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Render the steps for one of this document's contacts, as they'll be sent."""
    from app.models.company import Company
    from app.models.contact import Contact

    doc = await get_owned_document(db, scope, document_id)
    allowed = svc.document_contact_ids(doc)
    contact = company = None
    contact_id = str(body.contact_id) if body.contact_id else next(iter(sorted(allowed)), None)
    if contact_id:
        if contact_id not in allowed:
            raise HTTPException(status_code=404, detail="Contact not found in this document")
        contact = (
            await db.execute(select(Contact).where(Contact.id == uuid.UUID(contact_id)))
        ).scalar_one_or_none()
        if contact is not None and contact.company_id:
            company = (
                await db.execute(select(Company).where(Company.id == contact.company_id))
            ).scalar_one_or_none()
    return {
        "contact_id": contact_id,
        "to_email": contact.email if contact else None,
        "steps": svc.render_preview([s.model_dump() for s in body.steps], contact, company),
    }


@router.post("/{document_id}/send")
async def send_emails(
    document_id: uuid.UUID,
    body: SendRequest,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Create a sequence from the approved steps and enroll the chosen contacts.

    The first email goes out through the normal outreach queue (daily cap,
    warm-up, pacing, suppression, unsubscribe footer); follow-ups follow on
    business days and stop when the contact replies.
    """
    doc = await get_owned_document(db, scope, document_id)
    _require_ready(doc)
    allowed = svc.document_contact_ids(doc)
    stray = [str(c) for c in body.contact_ids if str(c) not in allowed]
    if stray:
        raise HTTPException(
            status_code=422,
            detail="Some contacts weren't imported from this document. Import them first.",
        )
    result = await svc.send_document_sequence(
        db, doc,
        contact_ids=body.contact_ids,
        steps=[s.model_dump() for s in body.steps],
        name=body.name,
        tone=body.tone,
    )
    logger.info(
        "Discovery document %s: sequence %s, %d enrolled, %d skipped",
        doc.id, result.sequence_id, result.enrolled_count, result.skipped_count,
    )
    return result


@router.get("/{document_id}/outreach")
async def document_outreach_status(
    document_id: uuid.UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Follow-up status for everyone this document's sequence emailed.

    Per contact: active (more follow-ups due, with ``next_send_at``), replied
    (follow-ups stopped), completed (all steps sent) or stopped (paused,
    unsubscribed, bounced).
    """
    doc = await get_owned_document(db, scope, document_id)
    return await svc.outreach_status(db, doc)
