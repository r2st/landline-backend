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
