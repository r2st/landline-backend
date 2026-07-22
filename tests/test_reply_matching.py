"""F7 — broadened reply detection.

Two behaviours:
* ``find_stoppable_enrollment`` matches enrollments beyond ``active`` (a reply
  during review / after completion / while paused still counts), preferring the
  still-running one.
* ``match_enrollment_by_headers`` recovers threaded replies the From: address
  match misses, by matching In-Reply-To / References ids to the enrollment's
  Gmail thread/message ids.
"""

import uuid

import pytest

from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.services.reply_matching import (
    REPLY_STOPPABLE_STATUSES,
    find_stoppable_enrollment,
    match_enrollment_by_headers,
)
from app.services.reply_matching import _message_id_tokens


async def _seq(db):
    seq = OutreachSequence(id=uuid.uuid4(), name=f"seq-{uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    return seq


async def _enr(db, contact, seq, *, status="active", thread=None, message=None):
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status=status,
        gmail_thread_id=thread, gmail_message_id=message,
    )
    db.add(enr)
    await db.flush()
    return enr


# ── find_stoppable_enrollment: broadened statuses ──────────────────────────────

@pytest.mark.parametrize("status", ["active", "pending_review", "completed", "paused"])
@pytest.mark.asyncio
async def test_broadened_statuses_are_matched(db_session, sample_contact, status):
    seq = await _seq(db_session)
    enr = await _enr(db_session, sample_contact, seq, status=status)
    found = await find_stoppable_enrollment(db_session, sample_contact.id)
    assert found is not None and found.id == enr.id


@pytest.mark.parametrize("status", ["bounced", "unsubscribed", "replied"])
@pytest.mark.asyncio
async def test_terminal_irrelevant_statuses_are_ignored(db_session, sample_contact, status):
    seq = await _seq(db_session)
    await _enr(db_session, sample_contact, seq, status=status)
    assert await find_stoppable_enrollment(db_session, sample_contact.id) is None


@pytest.mark.asyncio
async def test_active_is_preferred_over_completed(db_session, sample_contact):
    seq = await _seq(db_session)
    completed = await _enr(db_session, sample_contact, seq, status="completed")
    active = await _enr(db_session, sample_contact, seq, status="active")
    found = await find_stoppable_enrollment(db_session, sample_contact.id)
    assert found.id == active.id
    assert found.id != completed.id


def test_stoppable_statuses_exclude_terminal():
    assert "active" in REPLY_STOPPABLE_STATUSES
    assert "bounced" not in REPLY_STOPPABLE_STATUSES
    assert "replied" not in REPLY_STOPPABLE_STATUSES


# ── header token parsing ───────────────────────────────────────────────────────

def test_message_id_tokens_parses_full_and_local():
    toks = _message_id_tokens("<abc123@mail.gmail.com>", None)
    assert "abc123@mail.gmail.com" in toks
    assert "abc123" in toks


def test_message_id_tokens_handles_multiple_refs():
    toks = _message_id_tokens("<m1@x>", "<m1@x> <m2@y>")
    assert "m1" in toks and "m2" in toks
    # de-duplicated
    assert toks.count("m1") == 1


def test_message_id_tokens_empty():
    assert _message_id_tokens(None, "") == []


# ── match_enrollment_by_headers ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_match_by_message_id(db_session, sample_contact):
    seq = await _seq(db_session)
    enr = await _enr(db_session, sample_contact, seq, message="gmsg-42", thread="thr-9")
    found = await match_enrollment_by_headers(db_session, "<gmsg-42@mail.gmail.com>", None)
    assert found is not None and found.id == enr.id


@pytest.mark.asyncio
async def test_match_by_thread_id_in_references(db_session, sample_contact):
    seq = await _seq(db_session)
    enr = await _enr(db_session, sample_contact, seq, message="gmsg-1", thread="thr-9")
    found = await match_enrollment_by_headers(
        db_session, None, "<other@x> <thr-9@mail.gmail.com>"
    )
    assert found is not None and found.id == enr.id


@pytest.mark.asyncio
async def test_no_header_match_returns_none(db_session, sample_contact):
    seq = await _seq(db_session)
    await _enr(db_session, sample_contact, seq, message="gmsg-1", thread="thr-9")
    assert await match_enrollment_by_headers(db_session, "<nope@x>", "<nada@y>") is None


@pytest.mark.asyncio
async def test_header_match_skips_terminal_enrollment(db_session, sample_contact):
    seq = await _seq(db_session)
    await _enr(db_session, sample_contact, seq, status="bounced", message="gmsg-1")
    assert await match_enrollment_by_headers(db_session, "<gmsg-1@x>", None) is None


# ── tenant isolation: an org_id filter keeps matching within one tenant ─────────
#
# The smart-reply sweep runs per-account and passes the account's org so a reply
# can never match — and stop — another tenant's enrollment, even though threading
# ids / contact rows are not globally unique. These tests bypass the request-time
# global read filter (test context is unbound) to prove the explicit org_id
# predicate does the scoping on its own.


async def _org(db, slug):
    from app.models.tenant import Organization

    org = Organization(
        id=uuid.uuid4(), name=slug, slug=slug, is_active=True, billing_plan="free",
    )
    db.add(org)
    await db.flush()
    return org


async def _enr_org(db, contact_id, seq, org_id, *, status="active", thread=None, message=None):
    """Seed an enrollment under an explicit org (bypasses the auto-stamp)."""
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact_id, sequence_id=seq.id,
        current_step=1, status=status, org_id=org_id,
        gmail_thread_id=thread, gmail_message_id=message,
    )
    db.add(enr)
    await db.flush()
    return enr


@pytest.mark.asyncio
async def test_match_by_headers_scoped_to_org(db_session, sample_contact):
    """Two orgs share the same threading id; org_id picks the right enrollment."""
    from app.org_context import APPREND_ORG_ID

    seq = await _seq(db_session)
    other = await _org(db_session, "beta-co")
    mine = await _enr_org(
        db_session, sample_contact.id, seq, APPREND_ORG_ID, message="shared-msg"
    )
    theirs = await _enr_org(
        db_session, sample_contact.id, seq, other.id, message="shared-msg"
    )

    found_a = await match_enrollment_by_headers(
        db_session, "<shared-msg@x>", None, org_id=APPREND_ORG_ID
    )
    assert found_a is not None and found_a.id == mine.id

    found_b = await match_enrollment_by_headers(
        db_session, "<shared-msg@x>", None, org_id=other.id
    )
    assert found_b is not None and found_b.id == theirs.id


@pytest.mark.asyncio
async def test_match_by_headers_no_cross_org_leak(db_session, sample_contact):
    """A reply scoped to org A must not match an enrollment that lives in org B."""
    from app.org_context import APPREND_ORG_ID

    seq = await _seq(db_session)
    other = await _org(db_session, "gamma-co")
    await _enr_org(db_session, sample_contact.id, seq, other.id, message="only-in-b")

    # Org A asks for a thread that only exists in org B → no match.
    found = await match_enrollment_by_headers(
        db_session, "<only-in-b@x>", None, org_id=APPREND_ORG_ID
    )
    assert found is None


@pytest.mark.asyncio
async def test_find_stoppable_enrollment_scoped_to_org(db_session, sample_contact):
    """find_stoppable_enrollment filters by org when given one."""
    from app.org_context import APPREND_ORG_ID

    seq = await _seq(db_session)
    other = await _org(db_session, "delta-co")
    await _enr_org(db_session, sample_contact.id, seq, other.id, status="active")

    # No enrollment for this contact in org A → None, despite the org-B row.
    assert (
        await find_stoppable_enrollment(
            db_session, sample_contact.id, org_id=APPREND_ORG_ID
        )
        is None
    )
    # Scoped to org B, the row is found.
    found_b = await find_stoppable_enrollment(
        db_session, sample_contact.id, org_id=other.id
    )
    assert found_b is not None
