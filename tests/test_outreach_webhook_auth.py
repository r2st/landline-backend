"""The outreach event webhook takes a credential.

``POST /api/v1/outreach/events`` was anonymous, justified by "an SES webhook
must reach it without a JWT" and "the ``enrollment_id`` foreign key guards it".

What the FK guards is that the enrollment exists — not that the caller may write
to it, which matters because ``outreach_events`` is the send ledger and not an
annotation on one:

* ``SendLimiter._db_sent_today`` counts ``event_type='sent'`` rows against the
  daily cap, and the Redis reservation counter seeds itself from that count. So
  anonymous ``sent`` rows spend the day's outreach budget without sending mail.
* ``DangerMonitor`` reads ``bounced`` rows for the bounce rate behind the
  system-wide kill switch.
* ``replied`` / ``bounced`` / ``unsubscribed`` are written onto
  ``enrollment.status``, ending that cadence.

Two of those three are global, so one enrollment id was enough to stop every
campaign, not just the one it named.

The tests below fix the boundary in both directions — an unsigned anonymous post
is refused, and the two legitimate callers (a signed ESP, a logged-in operator)
still get through. The refusal cases matter more than the admits: a mistake that
re-opens this endpoint looks exactly like a passing test suite otherwise.
"""

import uuid as _uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services.webhook_dispatch import (
    SIGNATURE_HEADER,
    sign_payload,
    verify_payload,
)

WEBHOOK_SECRET = "test-esp-shared-secret"


@pytest.fixture
def webhook_secret(monkeypatch):
    """Configure a shared secret, as a deployment with an ESP wired up would.

    ``get_settings`` is cached, so the setting is patched on the live instance
    and the cache is left alone — clearing it would drop every *other* test
    setting on the floor for the rest of the session.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "outreach_webhook_secret", WEBHOOK_SECRET)
    return WEBHOOK_SECRET


async def _make_enrollment(db: AsyncSession, contact_id) -> OutreachEnrollment:
    sequence = OutreachSequence(id=_uuid.uuid4(), name="Webhook auth seq")
    db.add(sequence)
    await db.flush()
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact_id,
        sequence_id=sequence.id,
        status="active",
    )
    db.add(enrollment)
    await db.flush()
    await db.commit()
    return enrollment


def _body_and_signature(secret: str, payload: dict) -> tuple[bytes, str]:
    """Serialize exactly once, sign those bytes, and post those same bytes.

    The signature covers the raw body, so a test that signs ``json.dumps(...)``
    and then hands the *dict* to httpx signs different bytes than it sends —
    which fails for a reason that has nothing to do with the code under test.
    """
    import json

    body = json.dumps(payload).encode()
    return body, sign_payload(secret, body)


# ───────────────────────────── the signing primitive ─────────────────────────

def test_verify_payload_accepts_its_own_signature():
    body = b'{"event_type":"bounced"}'
    assert verify_payload(WEBHOOK_SECRET, body, sign_payload(WEBHOOK_SECRET, body))


def test_verify_payload_rejects_a_different_body():
    """The signature is over the body, so swapping the body must invalidate it —
    otherwise a captured header could be replayed onto any payload."""
    signature = sign_payload(WEBHOOK_SECRET, b'{"event_type":"opened"}')
    assert not verify_payload(WEBHOOK_SECRET, b'{"event_type":"bounced"}', signature)


def test_verify_payload_rejects_a_different_secret():
    body = b'{"event_type":"bounced"}'
    assert not verify_payload("some-other-secret", body, sign_payload(WEBHOOK_SECRET, body))


@pytest.mark.parametrize(
    "secret, header",
    [
        ("", sign_payload("", b"{}")),  # no secret configured, but a valid HMAC of ""
        (WEBHOOK_SECRET, None),         # no header offered
        (WEBHOOK_SECRET, ""),           # empty header
        (WEBHOOK_SECRET, "sha256="),    # header shape, no digest
        (WEBHOOK_SECRET, "garbage"),    # not the header shape at all
    ],
)
def test_verify_payload_returns_false_never_raises(secret, header):
    """Every rejection path returns ``False``.

    The first case is the one worth stating: an unconfigured secret must not be
    signable. ``sign_payload("", body)`` is a perfectly well-formed HMAC keyed on
    the empty string, so a verifier that only compared digests would accept a
    caller who guessed that the secret was unset — turning "no ESP configured"
    back into "anyone may post".
    """
    assert verify_payload(secret, b"{}", header) is False


# ─────────────────────────────── the endpoint ────────────────────────────────

@pytest.mark.asyncio
async def test_anonymous_post_is_rejected(unauth_client: AsyncClient, db_session, sample_contact):
    """No signature, no token → 401, and no row is written.

    The enrollment here is real, so this is not the missing-enrollment 404 path:
    the caller has everything the old FK-based guard asked for and is still
    refused.
    """
    enrollment = await _make_enrollment(db_session, sample_contact.id)

    resp = await unauth_client.post(
        "/api/v1/outreach/events",
        json={
            "enrollment_id": str(enrollment.id),
            "step_number": 1,
            "event_type": "sent",
        },
    )

    assert resp.status_code == 401
    count = (
        await db_session.execute(
            select(func.count(OutreachEvent.id)).where(
                OutreachEvent.enrollment_id == enrollment.id
            )
        )
    ).scalar()
    assert count == 0, "a rejected request must not reach the send ledger"


@pytest.mark.asyncio
async def test_anonymous_post_cannot_stop_a_cadence(
    unauth_client: AsyncClient, db_session, sample_contact
):
    """The status side-effect is the sharper half: ``unsubscribed`` on someone
    else's enrollment ends that campaign permanently."""
    enrollment = await _make_enrollment(db_session, sample_contact.id)

    resp = await unauth_client.post(
        "/api/v1/outreach/events",
        json={
            "enrollment_id": str(enrollment.id),
            "step_number": 1,
            "event_type": "unsubscribed",
        },
    )

    assert resp.status_code == 401
    await db_session.refresh(enrollment)
    assert enrollment.status == "active"


@pytest.mark.asyncio
async def test_unauthenticated_caller_cannot_probe_enrollment_ids(unauth_client: AsyncClient):
    """401 must come before the 404.

    If the missing-enrollment check ran first, the endpoint would answer "is this
    enrollment id real?" for anyone — a free oracle for the one input the write
    path needs.
    """
    resp = await unauth_client.post(
        "/api/v1/outreach/events",
        json={
            "enrollment_id": str(_uuid.uuid4()),
            "step_number": 1,
            "event_type": "opened",
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_signed_webhook_is_accepted(
    unauth_client: AsyncClient, db_session, sample_contact, webhook_secret
):
    """The ESP path still works: correct signature over the body → 201."""
    enrollment = await _make_enrollment(db_session, sample_contact.id)
    body, signature = _body_and_signature(
        webhook_secret,
        {"enrollment_id": str(enrollment.id), "step_number": 2, "event_type": "bounced"},
    )

    resp = await unauth_client.post(
        "/api/v1/outreach/events",
        content=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature},
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["event_type"] == "bounced"
    await db_session.refresh(enrollment)
    assert enrollment.status == "bounced"


@pytest.mark.asyncio
async def test_signature_from_the_wrong_secret_is_rejected(
    unauth_client: AsyncClient, db_session, sample_contact, webhook_secret
):
    enrollment = await _make_enrollment(db_session, sample_contact.id)
    body, signature = _body_and_signature(
        "not-the-configured-secret",
        {"enrollment_id": str(enrollment.id), "step_number": 1, "event_type": "bounced"},
    )

    resp = await unauth_client.post(
        "/api/v1/outreach/events",
        content=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature},
    )

    assert resp.status_code == 401
    await db_session.refresh(enrollment)
    assert enrollment.status == "active"


@pytest.mark.asyncio
async def test_signature_over_a_different_body_is_rejected(
    unauth_client: AsyncClient, db_session, sample_contact, webhook_secret
):
    """A signature captured from one delivery cannot be replayed onto another.

    Signed for step 1 / ``opened``, sent as step 1 / ``bounced`` — the digest is
    genuine, the body is not the one it covers.
    """
    enrollment = await _make_enrollment(db_session, sample_contact.id)
    _signed_body, signature = _body_and_signature(
        webhook_secret,
        {"enrollment_id": str(enrollment.id), "step_number": 1, "event_type": "opened"},
    )
    other_body, _ = _body_and_signature(
        webhook_secret,
        {"enrollment_id": str(enrollment.id), "step_number": 1, "event_type": "bounced"},
    )

    resp = await unauth_client.post(
        "/api/v1/outreach/events",
        content=other_body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature},
    )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_signature_is_rejected_when_no_secret_is_configured(
    unauth_client: AsyncClient, db_session, sample_contact, monkeypatch
):
    """With no ESP configured there is no signature that can be right.

    This is the deployment the fix actually lands on — nothing is wired up — so
    it is the case where "unconfigured" must mean *closed*. The caller here
    signs with the empty secret, which is the best guess available to someone who
    knows the setting exists and suspects it is unset.
    """
    monkeypatch.setattr(get_settings(), "outreach_webhook_secret", "")
    enrollment = await _make_enrollment(db_session, sample_contact.id)
    body, signature = _body_and_signature(
        "",
        {"enrollment_id": str(enrollment.id), "step_number": 1, "event_type": "sent"},
    )

    resp = await unauth_client.post(
        "/api/v1/outreach/events",
        content=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature},
    )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_authenticated_operator_may_still_record_an_event(
    client: AsyncClient, db_session, sample_contact
):
    """The manual-tracking half of the endpoint keeps working with a JWT.

    Worth a test of its own: the endpoint's docstring always claimed both
    callers, but only the anonymous one was reachable — the authenticated router
    has no POST /events — so "manual tracking" would have gone out with the fix
    if the JWT branch were not there.
    """
    enrollment = await _make_enrollment(db_session, sample_contact.id)

    resp = await client.post(
        "/api/v1/outreach/events",
        json={
            "enrollment_id": str(enrollment.id),
            "step_number": 1,
            "event_type": "opened",
        },
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["event_type"] == "opened"


@pytest.mark.asyncio
async def test_bad_signature_is_not_rescued_by_a_valid_token(
    client: AsyncClient, db_session, sample_contact, webhook_secret
):
    """A wrong signature is refused even when the request also carries a good JWT.

    This is the only case that separates "reject a bad signature" from "ignore
    it": with no token to fall back on, both behaviours end at the same 401. The
    distinction is worth holding because the fall-through version makes the
    header optional in practice — an ESP whose secret has been rotated out keeps
    working for as long as it also holds a token, and the signature stops meaning
    anything.
    """
    enrollment = await _make_enrollment(db_session, sample_contact.id)
    body, signature = _body_and_signature(
        "not-the-configured-secret",
        {"enrollment_id": str(enrollment.id), "step_number": 1, "event_type": "bounced"},
    )

    # ``client`` supplies a valid admin Bearer token on every request.
    resp = await client.post(
        "/api/v1/outreach/events",
        content=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature},
    )

    assert resp.status_code == 401
    await db_session.refresh(enrollment)
    assert enrollment.status == "active"


@pytest.mark.asyncio
async def test_garbage_bearer_token_is_rejected(
    unauth_client: AsyncClient, db_session, sample_contact
):
    """A token that is present but invalid fails closed rather than being
    treated as "no token, therefore anonymous"."""
    enrollment = await _make_enrollment(db_session, sample_contact.id)

    resp = await unauth_client.post(
        "/api/v1/outreach/events",
        json={
            "enrollment_id": str(enrollment.id),
            "step_number": 1,
            "event_type": "opened",
        },
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )

    assert resp.status_code == 401
