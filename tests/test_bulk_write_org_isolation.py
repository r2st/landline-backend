"""A bulk UPDATE is not covered by the org filter, and looks exactly like one that is.

``OrgScopedMixin`` isolates reads through a ``do_orm_execute`` hook that injects
``org_id == <caller>`` into every SELECT. It is load-bearing and it is invisible:
a router never writes the predicate, so a query with no visible scoping is the
*normal*, correct shape in this codebase.

That hook checks ``execute_state.is_select``. A Core ``update()`` or ``delete()``
is not a select, so it gets nothing — and it is written in the same file, in the
same style, with the same absence of a visible org predicate. There is no way to
tell the safe shape from the unsafe one by reading it; what separates them is
which SQLAlchemy construct is on the first line.

Keying on a primary key read back through a scoped SELECT is safe, and most of
the ~28 bulk writes in ``app/`` do exactly that. These tests cover the four that
did not: a broad predicate, or client-supplied ids, applied platform-wide.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_signature import EmailSignature
from app.models.email_template import EmailTemplate
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.models.tenant import Organization
from app.org_context import APPREND_ORG_ID, reset_current_org_id, set_current_org_id

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org_b(db_session: AsyncSession) -> Organization:
    org = Organization(
        id=uuid.uuid4(), name="Bulk Beta", slug="bulk-beta",
        is_active=True, billing_plan="free",
    )
    db_session.add(org)
    await db_session.commit()
    return org


async def _as_org(db: AsyncSession, org_id, *rows):
    """Commit rows under ``org_id``'s context, then hand back the first."""
    token = set_current_org_id(org_id)
    try:
        for row in rows:
            db.add(row)
        await db.commit()
    finally:
        reset_current_org_id(token)
    return rows[0] if rows else None


async def _read_unscoped(db: AsyncSession, model, row_id):
    """Read one row ignoring the org filter — the test is the referee here.

    Asserting through the API would only ever show the caller's own org, which
    is precisely the half that already worked.

    The rollback and ``expire_all`` are load-bearing, not hygiene. The app runs
    on its own session (``client`` overrides ``get_db``), so this session holds
    both an open read snapshot and an identity map still carrying the instance
    the test seeded. Without them a ``select`` by primary key returns the cached
    object with its *original* attribute values — so an assertion that a foreign
    row was left alone passes whether or not it was actually written. Every
    cross-tenant test in this file fails open that way.
    """
    await db.rollback()
    db.expire_all()
    return (
        await db.execute(
            select(model)
            .where(model.id == row_id)
            .execution_options(skip_org_filter=True)
        )
    ).scalar_one()


# --- templates and signatures: "there can be only one default" ---------------

async def test_promoting_a_template_leaves_another_orgs_default_alone(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    """``_clear_default`` unset ``is_default`` wherever it was true, org-wide.

    The flag is what every picker and every unattended send path resolves when
    no template is named, so clearing it does not merely tidy a checkbox in
    someone else's account — it decides which copy that tenant's next mail goes
    out as, or that there is no default left to resolve at all.
    """
    b_default = EmailTemplate(
        id=uuid.uuid4(), name="Beta House Style", subject="Hello from Beta",
        body="<p>Hi {{first_name}}</p>", is_default=True, org_id=org_b.id,
    )
    await _as_org(db_session, org_b.id, b_default)

    user_a = await make_user("bulk-a@alpha-corp.com", org_id=APPREND_ORG_ID)
    resp = await client.post(
        "/api/v1/email-templates",
        headers=auth_headers(user_a),
        json={
            "name": "Alpha House Style",
            "subject": "Hello from Alpha",
            "body": "<p>Hi {{first_name}}</p>",
            "is_default": True,
        },
    )
    assert resp.status_code == 201, resp.text

    refreshed = await _read_unscoped(db_session, EmailTemplate, b_default.id)
    assert refreshed.is_default is True, (
        "another tenant's default template was cleared by this org's promotion"
    )


async def test_promoting_a_signature_leaves_another_orgs_default_alone(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    """Same bug, same shape, in the neighbouring router."""
    b_default = EmailSignature(
        id=uuid.uuid4(), name="Beta Sign-off",
        content_html="<p>-- Beta</p>", is_default=True, org_id=org_b.id,
    )
    await _as_org(db_session, org_b.id, b_default)

    user_a = await make_user("bulk-sig-a@alpha-corp.com", org_id=APPREND_ORG_ID)
    resp = await client.post(
        "/api/v1/email-signatures",
        headers=auth_headers(user_a),
        json={
            "name": "Alpha Sign-off",
            "content_html": "<p>-- Alpha</p>",
            "is_default": True,
        },
    )
    assert resp.status_code == 201, resp.text

    refreshed = await _read_unscoped(db_session, EmailSignature, b_default.id)
    assert refreshed.is_default is True, (
        "another tenant's default signature was cleared by this org's promotion"
    )


async def test_a_default_still_becomes_the_only_default_within_one_org(
    client: AsyncClient, db_session, make_user, auth_headers
):
    """Control. The scoping must not cost the invariant it scopes.

    Without this, a fix that simply stopped clearing anything would pass every
    test above.
    """
    old = EmailTemplate(
        id=uuid.uuid4(), name="Alpha Old", subject="Old",
        body="<p>Old</p>", is_default=True, org_id=APPREND_ORG_ID,
    )
    await _as_org(db_session, APPREND_ORG_ID, old)

    user_a = await make_user("bulk-ctl@alpha-corp.com", org_id=APPREND_ORG_ID)
    resp = await client.post(
        "/api/v1/email-templates",
        headers=auth_headers(user_a),
        json={
            "name": "Alpha New", "subject": "New",
            "body": "<p>New</p>", "is_default": True,
        },
    )
    assert resp.status_code == 201, resp.text

    refreshed = await _read_unscoped(db_session, EmailTemplate, old.id)
    assert refreshed.is_default is False, (
        "the single-default invariant broke inside the org that owns both rows"
    )


# --- bulk stage move: client-supplied ids, no scope ---------------------------

async def test_bulk_stage_move_cannot_touch_another_orgs_companies(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    """The ids come straight off the request body and were never scoped.

    An id is not a secret — it comes back in exports, webhooks and error text —
    so this is a write IDOR reachable by any authenticated tenant against any
    company on the platform. The count reported in the response *was* scoped
    (the ``select`` below it goes through the filter), so the caller sees
    "0 updated" while the row moves: silent from both ends.
    """
    victim = Company(
        id=uuid.uuid4(), name="BetaCo", domain="betaco.example",
        source="test", pipeline_stage="client", org_id=org_b.id,
    )
    await _as_org(db_session, org_b.id, victim)

    user_a = await make_user("bulk-stage-a@alpha-corp.com", org_id=APPREND_ORG_ID)
    resp = await client.post(
        "/api/v1/companies/bulk-update-stage",
        headers=auth_headers(user_a),
        json={"company_ids": [str(victim.id)], "pipeline_stage": "churned"},
    )
    assert resp.status_code in (200, 404), resp.text

    refreshed = await _read_unscoped(db_session, Company, victim.id)
    assert refreshed.pipeline_stage == "client", (
        "another tenant's company was moved to a different pipeline stage"
    )


async def test_bulk_stage_move_still_moves_the_callers_own_companies(
    client: AsyncClient, db_session, make_user, auth_headers
):
    """Control, for the same reason as above."""
    own = Company(
        id=uuid.uuid4(), name="AlphaCo", domain="alphaco.example",
        source="test", pipeline_stage="new_lead", org_id=APPREND_ORG_ID,
    )
    await _as_org(db_session, APPREND_ORG_ID, own)

    user_a = await make_user("bulk-stage-own@alpha-corp.com", org_id=APPREND_ORG_ID)
    resp = await client.post(
        "/api/v1/companies/bulk-update-stage",
        headers=auth_headers(user_a),
        json={"company_ids": [str(own.id)], "pipeline_stage": "engaged"},
    )
    assert resp.status_code == 200, resp.text

    refreshed = await _read_unscoped(db_session, Company, own.id)
    assert refreshed.pipeline_stage == "engaged"


# --- the kill switch ----------------------------------------------------------

async def _enrollment(db: AsyncSession, org_id, *, status: str, kill_switch_paused: bool = False):
    """An enrollment in ``org_id``, with the company/contact its FKs require."""
    suffix = uuid.uuid4().hex[:8]
    company = Company(
        id=uuid.uuid4(), name=f"Co {suffix}", domain=f"{suffix}.example",
        source="test", org_id=org_id,
    )
    await _as_org(db, org_id, company)
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="K", last_name="S",
        email=f"{suffix}@{suffix}.example", org_id=org_id,
    )
    await _as_org(db, org_id, contact)
    sequence = OutreachSequence(
        id=uuid.uuid4(), name=f"Seq {suffix}", is_active=True, org_id=org_id
    )
    await _as_org(db, org_id, sequence)
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=sequence.id,
        current_step=1,
        status=status,
        kill_switch_paused=kill_switch_paused,
        org_id=org_id,
    )
    await _as_org(db, org_id, enrollment)
    return enrollment


async def test_engaging_one_orgs_kill_switch_does_not_halt_another_org(
    db_session: AsyncSession, org_b
):
    """``system_settings`` is per-tenant; the UPDATE behind it was not.

    The model says so in as many words — "each tenant has its own kill switch"
    — and migration 080 made ``(org_id, key)`` unique precisely so two orgs
    could hold the setting independently. The setting was stored per-org and
    then acted on globally, which is the worst of the two: the switch reads as
    scoped in the UI and in the database, and halts the platform.
    """
    from app.services import system_settings

    b_active = (await _enrollment(db_session, org_b.id, status="active")).id
    a_active = (await _enrollment(db_session, APPREND_ORG_ID, status="active")).id

    token = set_current_org_id(APPREND_ORG_ID)
    try:
        await system_settings.set_sending_enabled(
            db_session, False, reason="alpha pausing itself", updated_by="alpha"
        )
        await db_session.commit()
    finally:
        reset_current_org_id(token)

    assert (await _read_unscoped(db_session, OutreachEnrollment, a_active)).status == "paused"
    victim = await _read_unscoped(db_session, OutreachEnrollment, b_active)
    assert victim.status == "active", (
        "one tenant's kill switch halted another tenant's live outreach"
    )
    assert victim.kill_switch_paused is False


async def test_releasing_one_orgs_kill_switch_does_not_resume_another_org(
    db_session: AsyncSession, org_b
):
    """The resume side is the worse half: it *defeats* the other org's switch.

    Org B engages its kill switch because its domain is on fire. Org A, who has
    never heard of org B, releases its own — and B's outreach resumes, with B's
    UI still reporting the switch as engaged. A safety control that another
    tenant can silently release is not a safety control.
    """
    from app.services import system_settings

    b_halted = (await _enrollment(
        db_session, org_b.id, status="paused", kill_switch_paused=True
    )).id
    a_halted = (await _enrollment(
        db_session, APPREND_ORG_ID, status="paused", kill_switch_paused=True
    )).id

    token = set_current_org_id(APPREND_ORG_ID)
    try:
        await system_settings.set_sending_enabled(
            db_session, True, reason="alpha resuming itself", updated_by="alpha"
        )
        await db_session.commit()
    finally:
        reset_current_org_id(token)

    assert (await _read_unscoped(db_session, OutreachEnrollment, a_halted)).status == "active"
    victim = await _read_unscoped(db_session, OutreachEnrollment, b_halted)
    assert victim.status == "paused", (
        "one tenant released another tenant's kill switch"
    )
    assert victim.kill_switch_paused is True
