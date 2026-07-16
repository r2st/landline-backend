"""Tests for Feature 4 — Calendar Booking Injection (Dialtone).

Covers the four pillars the spec calls out:

* the interest gate (interested reply / link click / repeated opens),
* the ``{{booking_link}}`` empty-variable render + scaffolding-line strip,
* the AI single-link collapse (model emits more than one link),
* the URL host allowlist validation on save,

plus the per-sender booking config endpoints.
"""

import uuid as _uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services.ai_service import get_ai_service
from app.services.booking import (
    apply_booking_placeholder,
    collapse_booking_links,
    resolve_booking_url,
    should_inject_booking_link,
    validate_booking_url,
)
from app.services.email_compose import render

BOOKING_URL = "https://calendly.com/apprend/intro"


# ─── Fixtures / helpers ──────────────────────────────────────────────────────


async def _make_enrollment(db, contact, *, sender_account_id=None):
    seq = OutreachSequence(id=_uuid.uuid4(), name=f"seq-{_uuid.uuid4().hex[:6]}", status="active")
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        status="active",
        current_step=2,
        sender_account_id=sender_account_id,
    )
    db.add(enr)
    await db.flush()
    return enr


async def _add_event(db, enrollment, event_type, *, step_number=1):
    ev = OutreachEvent(
        id=_uuid.uuid4(),
        enrollment_id=enrollment.id,
        step_number=step_number,
        event_type=event_type,
    )
    db.add(ev)
    await db.flush()
    return ev


async def _make_gmail_account(db, **over):
    acct = GmailAccount(
        id=_uuid.uuid4(),
        email=over.pop("email", f"rep-{_uuid.uuid4().hex[:6]}@apprend.tech"),
        google_sub=_uuid.uuid4().hex,
        refresh_token_encrypted="enc",
    )
    for k, v in over.items():
        setattr(acct, k, v)
    db.add(acct)
    await db.flush()
    return acct


# ─── URL host allowlist validation ───────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://calendly.com/apprend/intro",
        "https://cal.com/apprend",
        "https://meetings.hubspot.com/apprend",   # subdomain of an allowlisted host
        "https://apprend.acuityscheduling.com/schedule.php",
    ],
)
def test_validate_booking_url_accepts_known_schedulers(url):
    ok, error = validate_booking_url(url)
    assert ok, error


@pytest.mark.parametrize(
    "url",
    [
        "http://calendly.com/apprend/intro",   # not https
        "https://evil.example.com/phish",      # host not on allowlist
        "https://calendly.com.evil.com/x",      # look-alike, not a real subdomain
        "ftp://calendly.com/x",
        "not-a-url",
    ],
)
def test_validate_booking_url_rejects_bad(url):
    ok, error = validate_booking_url(url)
    assert not ok
    assert error


def test_validate_booking_url_empty_is_valid_clear():
    # Empty means "clear the field" — a valid state.
    assert validate_booking_url("") == (True, "")
    assert validate_booking_url(None) == (True, "")
    assert validate_booking_url("   ") == (True, "")


def test_validate_booking_url_honors_custom_allowlist():
    ok, _ = validate_booking_url("https://sched.acme.dev/x", allowed_hosts=["acme.dev"])
    assert ok
    ok, _ = validate_booking_url("https://calendly.com/x", allowed_hosts=["acme.dev"])
    assert not ok


# ─── Interest gate ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_false_with_no_signal(db_session: AsyncSession, sample_contact):
    enr = await _make_enrollment(db_session, sample_contact)
    assert await should_inject_booking_link(sample_contact, enr, db_session) is False


@pytest.mark.asyncio
async def test_gate_true_on_interested_reply(
    db_session: AsyncSession, sample_company, sample_contact
):
    enr = await _make_enrollment(db_session, sample_contact)
    db_session.add(
        EmailReply(
            id=_uuid.uuid4(),
            contact_id=sample_contact.id,
            company_id=sample_company.id,
            from_email=sample_contact.email,
            received_at=datetime.now(timezone.utc),
            reply_classification="interested",
        )
    )
    await db_session.flush()
    assert await should_inject_booking_link(sample_contact, enr, db_session) is True


@pytest.mark.asyncio
async def test_gate_true_on_click_event(db_session: AsyncSession, sample_contact):
    enr = await _make_enrollment(db_session, sample_contact)
    await _add_event(db_session, enr, "clicked")
    assert await should_inject_booking_link(sample_contact, enr, db_session) is True


@pytest.mark.asyncio
async def test_gate_true_on_email_send_click(db_session: AsyncSession, sample_contact):
    enr = await _make_enrollment(db_session, sample_contact)
    db_session.add(
        EmailSend(
            id=_uuid.uuid4(),
            contact_id=sample_contact.id,
            clicked_at=datetime.now(timezone.utc),
        )
    )
    await db_session.flush()
    assert await should_inject_booking_link(sample_contact, enr, db_session) is True


@pytest.mark.asyncio
async def test_gate_true_on_two_opens_but_false_on_one(
    db_session: AsyncSession, sample_contact
):
    enr = await _make_enrollment(db_session, sample_contact)
    await _add_event(db_session, enr, "opened", step_number=1)
    # One open is not enough (default threshold is 2).
    assert await should_inject_booking_link(sample_contact, enr, db_session) is False
    await _add_event(db_session, enr, "opened", step_number=2)
    assert await should_inject_booking_link(sample_contact, enr, db_session) is True


# ─── {{booking_link}} render + scaffolding strip ─────────────────────────────


def test_apply_booking_placeholder_strips_scaffolding_line_when_empty():
    template = (
        "Hi Jordan,\n"
        "Wanted to bump this.\n"
        "Grab a time here: {{booking_link}}\n"
        "- Suman\n"
    )
    stripped = apply_booking_placeholder(template, "")
    # The whole scaffolding line is gone — no dangling "Grab a time here:".
    assert "Grab a time here" not in stripped
    assert "booking_link" not in stripped
    rendered = render(stripped, {"booking_link": ""})
    assert "Grab a time here" not in rendered
    assert rendered.strip().endswith("- Suman")
    # Non-scaffolding lines survive.
    assert "Wanted to bump this." in rendered


def test_apply_booking_placeholder_keeps_line_when_url_present():
    template = "Grab a time here: {{booking_link}}"
    kept = apply_booking_placeholder(template, BOOKING_URL)
    assert kept == template  # placeholder left in place for Jinja
    rendered = render(kept, {"booking_link": BOOKING_URL})
    assert rendered == f"Grab a time here: {BOOKING_URL}"


def test_apply_booking_placeholder_handles_whitespace_variant():
    template = "Book: {{ booking_link }}"
    assert apply_booking_placeholder(template, "") == ""


# ─── Single-link collapse ────────────────────────────────────────────────────


def test_collapse_booking_links_reduces_duplicates_to_one():
    body = (
        f"Happy to chat — here's my calendar: {BOOKING_URL}. "
        f"Again, book anytime at {BOOKING_URL} — thanks!"
    )
    collapsed = collapse_booking_links(body, BOOKING_URL)
    assert collapsed.count(BOOKING_URL) == 1
    # First occurrence is the one kept.
    assert collapsed.startswith("Happy to chat")


def test_collapse_booking_links_noop_on_single_or_absent():
    single = f"here: {BOOKING_URL}"
    assert collapse_booking_links(single, BOOKING_URL) == single
    assert collapse_booking_links("no link here", BOOKING_URL) == "no link here"
    assert collapse_booking_links("", BOOKING_URL) == ""


@pytest.mark.asyncio
async def test_generate_followup_collapses_multiple_links(monkeypatch):
    """When the model emits the booking link twice, only one survives."""
    ai = get_ai_service()

    async def fake_chat(prompt, max_tokens=512, temperature=None):
        return (
            '{"subject": "Re: intro", '
            f'"body": "Happy to grab 15 min: {BOOKING_URL}  or here {BOOKING_URL}", '
            '"is_final": false, "should_stop_sequence": false}'
        )

    monkeypatch.setattr(ai, "_chat", fake_chat)
    result = await ai.generate_followup(
        original_subject="intro",
        original_body="hello",
        step_number=1,
        contact_name="Jordan Avery",
        days_since_last=3,
        booking_url=BOOKING_URL,
    )
    assert result["body"].count(BOOKING_URL) == 1


# ─── resolve_booking_url fallback chain ──────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_prefers_sender_url_over_global(db_session: AsyncSession, monkeypatch):
    acct = await _make_gmail_account(db_session, booking_url=BOOKING_URL)

    class _S:
        scheduling_link = "https://cal.com/global"

    url = await resolve_booking_url(db_session, acct.id, settings=_S())
    assert url == BOOKING_URL


@pytest.mark.asyncio
async def test_resolve_falls_back_to_global_then_empty(db_session: AsyncSession):
    acct = await _make_gmail_account(db_session)  # no booking_url

    class _S:
        scheduling_link = "https://cal.com/global"

    assert await resolve_booking_url(db_session, acct.id, settings=_S()) == "https://cal.com/global"

    class _Empty:
        scheduling_link = ""

    assert await resolve_booking_url(db_session, None, settings=_Empty()) == ""


# ─── Per-sender booking config endpoints ─────────────────────────────────────


@pytest.mark.asyncio
async def test_get_booking_404_for_unknown_sender(client: AsyncClient):
    resp = await client.get(f"/api/v1/dialtone/senders/{_uuid.uuid4()}/booking")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_put_then_get_booking_config(
    client: AsyncClient, db_session: AsyncSession
):
    acct = await _make_gmail_account(db_session)
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/dialtone/senders/{acct.id}/booking",
        json={
            "booking_url": BOOKING_URL,
            "booking_provider": "calendly",
            "booking_auto_inject": False,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["booking_url"] == BOOKING_URL
    assert body["booking_provider"] == "calendly"
    assert body["booking_auto_inject"] is False
    assert body["account_type"] == "gmail"

    got = await client.get(f"/api/v1/dialtone/senders/{acct.id}/booking")
    assert got.status_code == 200
    assert got.json()["booking_url"] == BOOKING_URL


@pytest.mark.asyncio
async def test_put_booking_rejects_bad_url(client: AsyncClient, db_session: AsyncSession):
    acct = await _make_gmail_account(db_session)
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/dialtone/senders/{acct.id}/booking",
        json={"booking_url": "https://evil.example.com/phish"},
    )
    assert resp.status_code == 400
    # Nothing was persisted.
    got = await client.get(f"/api/v1/dialtone/senders/{acct.id}/booking")
    assert got.json()["booking_url"] is None


@pytest.mark.asyncio
async def test_put_booking_empty_clears_url(client: AsyncClient, db_session: AsyncSession):
    acct = await _make_gmail_account(db_session, booking_url=BOOKING_URL)
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/dialtone/senders/{acct.id}/booking",
        json={"booking_url": ""},
    )
    assert resp.status_code == 200
    assert resp.json()["booking_url"] is None
