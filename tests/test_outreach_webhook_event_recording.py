"""What a signed ESP webhook actually writes.

``tests/test_outreach_webhook_auth.py`` covers who may reach
``POST /api/v1/outreach/events``. This file covers what happens once they do,
which had two defects that only the webhook caller could hit.

**The row landed in the wrong tenant.** A JWT caller arrives with ``org_id``
bound by ``get_current_user``; a signed webhook arrives with nothing bound,
because the route is in ``PUBLIC_ROUTES`` precisely so an ESP can reach it
without a token. With no org in context ``org_id_for_insert()`` falls back to the
first-party org, so *every tenant's* delivery events were filed under Apprend.
That fails in both directions at once:

* the tenant that sent the mail never saw its own bounces — the read filter
  scopes them away, so the funnel, the danger monitor and the bounce rate all
  read zero on real bounces;
* ``SendLimiter._db_sent_today`` and ``DangerMonitor`` counted a stranger's
  ``sent`` and ``bounced`` rows against *our* daily cap and *our* bounce rate.

Same failure the tracking pixel had (fixed with ``org_scope(enrollment.org_id)``)
and the one ``opt_out.record`` documents at its own insert: an unauthenticated
writer has to carry the org on the row, because there is no context to infer it
from.

**A redelivery was a 500.** Webhook delivery is at-least-once by design and
migration 117 made ``opened``/``clicked`` unique per ``(enrollment, step)``. So
the second delivery of the same open hit the index and came back a 500 — which
every ESP reads as "not accepted" and retries, indefinitely, on a body that can
never be accepted. Worse, an ESP that batches or backs off on 5xx will delay the
*other* events behind it. The duplicate is answered with the row that already
exists.
"""

import json
import uuid as _uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.models.tenant import Organization
from app.org_context import APPREND_ORG_ID
from app.services.webhook_dispatch import SIGNATURE_HEADER, sign_payload

WEBHOOK_SECRET = "test-esp-shared-secret"


@pytest.fixture
def webhook_secret(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "outreach_webhook_secret", WEBHOOK_SECRET)
    return WEBHOOK_SECRET


@pytest_asyncio.fixture
async def org_b(db_session: AsyncSession) -> Organization:
    org = Organization(
        id=_uuid.uuid4(), name="Beta Fabrication", slug="beta-fabrication",
        is_active=True, billing_plan="free",
    )
    db_session.add(org)
    await db_session.commit()
    return org


async def _enrollment_in(db: AsyncSession, org_id) -> OutreachEnrollment:
    """A sequence + contact + enrollment owned by ``org_id``.

    Every ``org_id`` is explicit. The insert stamp is exactly the mechanism under
    test, so letting it supply these would build the fixture out of the bug.
    """
    sequence = OutreachSequence(
        id=_uuid.uuid4(), name="ESP webhook seq", org_id=org_id, is_active=True
    )
    company = Company(
        id=_uuid.uuid4(), name=f"Beta Fab {_uuid.uuid4().hex[:6]}",
        domain=f"{_uuid.uuid4().hex[:10]}.test", source="test", org_id=org_id,
    )
    db.add_all([sequence, company])
    await db.flush()
    contact = Contact(
        id=_uuid.uuid4(), company_id=company.id, first_name="Dana",
        email=f"dana-{_uuid.uuid4().hex[:8]}@x.test", source="test", org_id=org_id,
    )
    db.add(contact)
    await db.flush()
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=sequence.id,
        status="active", current_step=1, org_id=org_id,
    )
    db.add(enrollment)
    await db.flush()
    await db.commit()
    return enrollment


def _signed(payload: dict) -> tuple[bytes, dict]:
    """Serialize once, sign those bytes, post those same bytes."""
    body = json.dumps(payload).encode()
    return body, {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign_payload(WEBHOOK_SECRET, body),
    }


async def _events(db: AsyncSession, enrollment_id):
    return (
        await db.execute(
            select(OutreachEvent)
            .where(OutreachEvent.enrollment_id == enrollment_id)
            .execution_options(skip_org_filter=True)
        )
    ).scalars().all()


# ─────────────────────────── the org the row lands in ────────────────────────


@pytest.mark.asyncio
async def test_a_signed_webhook_files_the_event_under_the_enrollments_org(
    unauth_client: AsyncClient, db_session: AsyncSession, webhook_secret, org_b,
):
    """The regression: this landed under APPREND_ORG_ID whatever the tenant."""
    enrollment = await _enrollment_in(db_session, org_b.id)

    body, headers = _signed({
        "enrollment_id": str(enrollment.id),
        "step_number": 1,
        "event_type": "bounced",
        "event_metadata": {"dsn": "550 5.1.1"},
    })
    resp = await unauth_client.post(
        "/api/v1/outreach/events", content=body, headers=headers
    )
    assert resp.status_code == 201, resp.text

    rows = await _events(db_session, enrollment.id)
    assert len(rows) == 1
    assert rows[0].org_id == org_b.id
    assert rows[0].org_id != APPREND_ORG_ID


@pytest.mark.asyncio
async def test_a_tenants_own_bounce_is_visible_to_that_tenant(
    unauth_client: AsyncClient, db_session: AsyncSession, webhook_secret, org_b,
):
    """The consequence, stated as the reader sees it.

    A row stamped to the wrong org is not merely mislabelled — the global read
    filter makes it *invisible* to the tenant it belongs to. Their funnel, their
    bounce rate and the danger monitor that trips on it all read zero while the
    address is bouncing.
    """
    from app.org_context import org_scope

    enrollment = await _enrollment_in(db_session, org_b.id)
    body, headers = _signed({
        "enrollment_id": str(enrollment.id),
        "step_number": 1,
        "event_type": "bounced",
    })
    assert (
        await unauth_client.post(
            "/api/v1/outreach/events", content=body, headers=headers
        )
    ).status_code == 201

    async with org_scope(org_b.id):
        mine = (
            await db_session.execute(
                select(func.count(OutreachEvent.id)).where(
                    OutreachEvent.event_type == "bounced"
                )
            )
        ).scalar()
    async with org_scope(APPREND_ORG_ID):
        theirs = (
            await db_session.execute(
                select(func.count(OutreachEvent.id)).where(
                    OutreachEvent.event_type == "bounced"
                )
            )
        ).scalar()

    assert mine == 1, "the tenant that sent the mail must see its own bounce"
    assert theirs == 0, "and nobody else's daily cap or bounce rate absorbs it"


@pytest.mark.asyncio
async def test_the_enrollment_status_still_advances(
    unauth_client: AsyncClient, db_session: AsyncSession, webhook_secret, org_b,
):
    """Binding the org must not cost the endpoint its other job."""
    enrollment = await _enrollment_in(db_session, org_b.id)
    enrollment_id = enrollment.id

    body, headers = _signed({
        "enrollment_id": str(enrollment_id),
        "step_number": 1,
        "event_type": "replied",
    })
    assert (
        await unauth_client.post(
            "/api/v1/outreach/events", content=body, headers=headers
        )
    ).status_code == 201

    status = (
        await db_session.execute(
            select(OutreachEnrollment.status)
            .where(OutreachEnrollment.id == enrollment_id)
            .execution_options(skip_org_filter=True)
        )
    ).scalar()
    assert status == "replied"


# ──────────────────────────────── redelivery ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_redelivered_open_is_accepted_not_500ed(
    unauth_client: AsyncClient, db_session: AsyncSession, webhook_secret, org_b,
):
    """The regression. Migration 117 turned an ordinary retry into a hard error."""
    enrollment = await _enrollment_in(db_session, org_b.id)
    body, headers = _signed({
        "enrollment_id": str(enrollment.id),
        "step_number": 1,
        "event_type": "opened",
    })

    first = await unauth_client.post(
        "/api/v1/outreach/events", content=body, headers=headers
    )
    second = await unauth_client.post(
        "/api/v1/outreach/events", content=body, headers=headers
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    # Same row, not a second one: idempotent is what an at-least-once receiver
    # has to be, and one open is what the optimizer's counts assume.
    assert second.json()["id"] == first.json()["id"]
    assert len(await _events(db_session, enrollment.id)) == 1


@pytest.mark.asyncio
async def test_a_repeatable_event_type_is_still_written_twice(
    unauth_client: AsyncClient, db_session: AsyncSession, webhook_secret, org_b,
):
    """The control, and the reason the index is partial.

    ``sent`` is written per attempt — ``send_claim.reconcile`` writes a second
    for an attempt whose bookkeeping was lost — so collapsing repeats here would
    hide real volume from the daily cap that counts these rows.
    """
    enrollment = await _enrollment_in(db_session, org_b.id)
    body, headers = _signed({
        "enrollment_id": str(enrollment.id),
        "step_number": 1,
        "event_type": "sent",
    })

    for _ in range(2):
        resp = await unauth_client.post(
            "/api/v1/outreach/events", content=body, headers=headers
        )
        assert resp.status_code == 201, resp.text

    assert len(await _events(db_session, enrollment.id)) == 2


@pytest.mark.asyncio
async def test_a_click_is_not_a_duplicate_of_an_open(
    unauth_client: AsyncClient, db_session: AsyncSession, webhook_secret, org_b,
):
    """The index keys on ``event_type``; the idempotency lookup must too."""
    enrollment = await _enrollment_in(db_session, org_b.id)

    ids = []
    for kind in ("opened", "clicked"):
        body, headers = _signed({
            "enrollment_id": str(enrollment.id),
            "step_number": 1,
            "event_type": kind,
        })
        resp = await unauth_client.post(
            "/api/v1/outreach/events", content=body, headers=headers
        )
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])

    assert ids[0] != ids[1]
    assert len(await _events(db_session, enrollment.id)) == 2
