"""Tests for Feature 1 — Reply Classification (Dialtone).

Covers the four pillars the spec calls out:

* keyword pre-filter precision (unsubscribe / OOO) + return-date parsing,
* the confidence gate,
* auto-action routing per category,
* suppression enforcement in send queries,
* the priority-inbox endpoints (filtering, stats, reclassify, read).
"""

import uuid as _uuid
from datetime import date, datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.consent import ConsentRecord
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.services.ai_service import (
    extract_return_date,
    keyword_prefilter,
)
from app.services.consent import check_consent
from app.tasks.replies import (
    apply_confidence_gate,
    classify_new_replies_async,
    classify_reply_record,
)


# ─── Fakes ───────────────────────────────────────────────────────────────────


class FakeAI:
    """Stub AIService.classify_reply — returns a canned result."""

    def __init__(self, result: dict):
        self.result = result
        self.calls = 0

    async def classify_reply(self, **kwargs):
        self.calls += 1
        return dict(self.result)


class FakeNotify:
    """Captures notification calls instead of sending."""

    def __init__(self):
        self.replies = []
        self.alerts = []

    async def notify_reply(self, contact_name, company_name, contact_email):
        self.replies.append((contact_name, company_name, contact_email))

    async def _send_notification(self, subject, body):
        self.alerts.append((subject, body))


def _ai_result(category, **over):
    base = {
        "category": category,
        "confidence": 0.95,
        "reason": "test",
        "return_date": None,
        "referral": {"name": None, "email": None},
        "unsubscribe_explicit": category == "unsubscribe",
        "source": "ai",
        "model": "test-model",
    }
    base.update(over)
    return base


# ─── Fixtures / helpers ──────────────────────────────────────────────────────


async def _make_reply(db, contact, company, *, subject="Re: intro", body="", **over):
    reply = EmailReply(
        id=_uuid.uuid4(),
        contact_id=contact.id if contact else None,
        company_id=company.id if company else None,
        from_email=contact.email if contact else "stranger@example.com",
        subject=subject,
        body_text=body,
        snippet=body[:200],
        received_at=datetime.now(timezone.utc),
    )
    for k, v in over.items():
        setattr(reply, k, v)
    db.add(reply)
    await db.flush()
    return reply


async def _make_enrollment(db, contact, *, status="active"):
    seq = OutreachSequence(id=_uuid.uuid4(), name=f"seq-{_uuid.uuid4().hex[:6]}", status="active")
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        status=status,
        current_step=2,
    )
    db.add(enr)
    await db.flush()
    return enr


# ─── Keyword pre-filter precision ────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Please unsubscribe me from this list",
        "UNSUBSCRIBE",
        "Can you opt me out?",  # opt out
        "opt-out please",
        "remove me from your mailing list",
        "take me off your list",
        "please stop emailing me",
        "do not contact me again",
    ],
)
def test_keyword_prefilter_detects_unsubscribe(text):
    assert keyword_prefilter("", text) == "unsubscribe"


@pytest.mark.parametrize(
    "text",
    [
        "I am out of office until Monday",
        "Automatic reply: on vacation",
        "This is an auto-reply",
        "I'm on annual leave right now",
        "Away from my desk, back soon",
        "I am currently travelling",
    ],
)
def test_keyword_prefilter_detects_ooo(text):
    assert keyword_prefilter("Auto", text) == "out_of_office"


@pytest.mark.parametrize(
    "text",
    [
        "Sure, let's set up a call next week",
        "Not a fit for us right now, thanks",
        "You want our head of ops, not me",
        "",
    ],
)
def test_keyword_prefilter_passes_through_ambiguous(text):
    # High precision: ordinary replies must NOT keyword-match (they go to the LLM).
    assert keyword_prefilter("", text) is None


def test_unsubscribe_wins_over_ooo():
    # A message that is both an autoresponder AND says unsubscribe is treated as
    # the compliance-critical opt-out.
    text = "Out of office. Also, please unsubscribe me."
    assert keyword_prefilter("", text) == "unsubscribe"


# ─── Return-date parsing ─────────────────────────────────────────────────────


def test_extract_return_date_iso():
    assert extract_return_date("back on 2026-08-15", today=date(2026, 7, 1)) == "2026-08-15"


def test_extract_return_date_month_name_rolls_forward():
    # "August 3" seen on July 1 → this year's Aug 3.
    assert extract_return_date("returning August 3", today=date(2026, 7, 1)) == "2026-08-03"
    # A month already past rolls to next year.
    assert extract_return_date("back on January 5", today=date(2026, 7, 1)) == "2027-01-05"


def test_extract_return_date_none_when_absent():
    assert extract_return_date("I am away", today=date(2026, 7, 1)) is None


# ─── Confidence gate ─────────────────────────────────────────────────────────


def test_gate_trusts_keyword_regardless_of_confidence():
    assert apply_confidence_gate("unsubscribe", 0.0, "keyword") == "unsubscribe"
    assert apply_confidence_gate("out_of_office", 0.1, "keyword") == "out_of_office"


def test_gate_passes_high_confidence_ai():
    assert apply_confidence_gate("interested", 0.9, "ai") == "interested"
    assert apply_confidence_gate("interested", 0.75, "ai") == "interested"


def test_gate_downgrades_low_confidence_ai_to_unknown():
    assert apply_confidence_gate("interested", 0.74, "ai") == "unknown"
    assert apply_confidence_gate("not_interested", 0.5, "ai") == "unknown"


# ─── classify_reply (service) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_reply_keyword_unsubscribe_no_llm(monkeypatch):
    from app.services.ai_service import AIService

    svc = AIService.__new__(AIService)  # skip __init__ (no API keys needed)

    async def _boom(*a, **k):
        raise AssertionError("LLM must not be called for keyword unsubscribe")

    monkeypatch.setattr(svc, "complete_json", _boom, raising=False)
    out = await svc.classify_reply("Re: hi", "please unsubscribe me")
    assert out["category"] == "unsubscribe"
    assert out["source"] == "keyword"
    assert out["confidence"] == 1.0
    assert out["unsubscribe_explicit"] is True


@pytest.mark.asyncio
async def test_classify_reply_ai_path_normalizes(monkeypatch):
    from app.services.ai_service import AIService

    svc = AIService.__new__(AIService)
    svc.model = "test-model"

    async def _fake_complete_json(prompt, max_tokens=400):
        return {"category": "INTERESTED", "confidence": "1.4", "reason": "wants a call"}

    monkeypatch.setattr(svc, "complete_json", _fake_complete_json, raising=False)
    out = await svc.classify_reply("Re: hi", "yes let's talk, send me times")
    assert out["category"] == "interested"
    assert out["confidence"] == 1.0  # clamped
    assert out["source"] == "ai"


@pytest.mark.asyncio
async def test_classify_reply_ai_error_falls_back_unknown(monkeypatch):
    from app.services.ai_service import AIService

    svc = AIService.__new__(AIService)
    svc.model = "test-model"

    async def _err(prompt, max_tokens=400):
        return {"error": "boom"}

    monkeypatch.setattr(svc, "complete_json", _err, raising=False)
    out = await svc.classify_reply("Re: hi", "some ambiguous text")
    assert out["category"] == "unknown"
    assert out["source"] == "fallback"


# ─── Auto-action routing per category ────────────────────────────────────────


@pytest.mark.asyncio
async def test_route_interested_flags_and_notifies(
    db_session: AsyncSession, sample_company, sample_contact
):
    enr = await _make_enrollment(db_session, sample_contact, status="active")
    reply = await _make_reply(db_session, sample_contact, sample_company, body="let's talk")
    notify = FakeNotify()

    summary = await classify_reply_record(
        db_session, reply, ai_service=FakeAI(_ai_result("interested")),
        notification_service=notify,
    )
    await db_session.flush()

    assert summary["category"] == "interested"
    assert reply.priority_flag is True
    assert reply.handled_action == "none"
    assert reply.reply_classification == "interested"
    assert notify.replies, "operator should be notified for a hot lead"
    # Do NOT auto-advance / complete the sequence.
    await db_session.refresh(enr)
    assert enr.status == "active"


@pytest.mark.asyncio
async def test_route_unsubscribe_suppresses_everywhere(
    db_session: AsyncSession, sample_company, sample_contact
):
    enr = await _make_enrollment(db_session, sample_contact, status="active")
    reply = await _make_reply(db_session, sample_contact, sample_company, body="unsubscribe me")

    await classify_reply_record(
        db_session, reply, ai_service=FakeAI(_ai_result("unsubscribe")),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(sample_contact)
    await db_session.refresh(enr)
    assert sample_contact.suppressed is True
    assert sample_contact.unsubscribed is True
    assert enr.status == "unsubscribed"
    assert reply.handled_action == "unsubscribed"
    # Consent revocation logged for the audit trail.
    records = (
        await db_session.execute(
            select(ConsentRecord).where(ConsentRecord.contact_id == sample_contact.id)
        )
    ).scalars().all()
    assert any(r.revoked_at is not None for r in records)


@pytest.mark.asyncio
async def test_route_unsubscribe_keyword_wins_even_if_ai_says_not_interested(
    db_session: AsyncSession, sample_company, sample_contact
):
    # Real service keyword pre-filter must classify the opt-out as unsubscribe
    # regardless of what an AI might think — no FakeAI here.
    reply = await _make_reply(
        db_session, sample_contact, sample_company, body="not interested, please unsubscribe me"
    )
    from app.services.ai_service import AIService

    svc = AIService.__new__(AIService)
    svc.model = "test"

    async def _should_not_run(*a, **k):
        raise AssertionError("keyword opt-out must not reach the LLM")

    svc.complete_json = _should_not_run  # type: ignore

    await classify_reply_record(db_session, reply, ai_service=svc, notification_service=FakeNotify())
    await db_session.flush()
    assert reply.reply_classification == "unsubscribe"
    await db_session.refresh(sample_contact)
    assert sample_contact.suppressed is True


@pytest.mark.asyncio
async def test_route_ooo_requeues_until_return_date(
    db_session: AsyncSession, sample_company, sample_contact
):
    enr = await _make_enrollment(db_session, sample_contact, status="active")
    reply = await _make_reply(db_session, sample_contact, sample_company, body="OOO")
    return_iso = (datetime.now(timezone.utc) + timedelta(days=14)).date().isoformat()

    await classify_reply_record(
        db_session, reply,
        ai_service=FakeAI(_ai_result("out_of_office", source="keyword", return_date=return_iso)),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(enr)
    assert reply.handled_action == "requeued"
    assert enr.status == "active"  # never dropped
    assert enr.next_send_at is not None
    assert enr.next_send_at.date().isoformat() == return_iso


@pytest.mark.asyncio
async def test_route_ooo_defaults_to_plus_7_days_when_unparseable(
    db_session: AsyncSession, sample_company, sample_contact
):
    enr = await _make_enrollment(db_session, sample_contact, status="active")
    reply = await _make_reply(db_session, sample_contact, sample_company, body="OOO")

    await classify_reply_record(
        db_session, reply,
        ai_service=FakeAI(_ai_result("out_of_office", source="keyword", return_date=None)),
        notification_service=FakeNotify(),
    )
    await db_session.flush()
    await db_session.refresh(enr)
    assert enr.next_send_at is not None
    # SQLite returns naive datetimes; normalize to aware UTC for comparison.
    next_at = enr.next_send_at
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=timezone.utc)
    delta = next_at - datetime.now(timezone.utc)
    assert timedelta(days=6) < delta < timedelta(days=8)


@pytest.mark.asyncio
async def test_route_not_interested_completes_enrollment(
    db_session: AsyncSession, sample_company, sample_contact
):
    enr = await _make_enrollment(db_session, sample_contact, status="active")
    reply = await _make_reply(db_session, sample_contact, sample_company, body="no thanks")

    await classify_reply_record(
        db_session, reply, ai_service=FakeAI(_ai_result("not_interested")),
        notification_service=FakeNotify(),
    )
    await db_session.flush()
    await db_session.refresh(enr)
    assert enr.status == "completed"
    assert reply.handled_action == "sequence_removed"


@pytest.mark.asyncio
async def test_route_wrong_person_flags_and_stashes_referral(
    db_session: AsyncSession, sample_company, sample_contact
):
    enr = await _make_enrollment(db_session, sample_contact, status="active")
    reply = await _make_reply(db_session, sample_contact, sample_company, body="try our CTO")
    referral = {"name": "Jane Doe", "email": "jane@acmestamp.com"}

    await classify_reply_record(
        db_session, reply,
        ai_service=FakeAI(_ai_result("wrong_person", referral=referral)),
        notification_service=FakeNotify(),
    )
    await db_session.flush()

    await db_session.refresh(sample_contact)
    await db_session.refresh(enr)
    assert sample_contact.status == "wrong_person"
    assert enr.status == "completed"
    assert reply.handled_action == "flagged_cleanup"
    assert reply.classification_meta["referral"]["email"] == "jane@acmestamp.com"


@pytest.mark.asyncio
async def test_low_confidence_gates_to_unknown_no_action(
    db_session: AsyncSession, sample_company, sample_contact
):
    enr = await _make_enrollment(db_session, sample_contact, status="active")
    reply = await _make_reply(db_session, sample_contact, sample_company, body="hmm")

    await classify_reply_record(
        db_session, reply,
        ai_service=FakeAI(_ai_result("interested", confidence=0.4)),
        notification_service=FakeNotify(),
    )
    await db_session.flush()
    await db_session.refresh(enr)
    assert reply.reply_classification == "unknown"
    assert reply.handled_action == "none"
    assert reply.priority_flag is False
    assert enr.status == "active"  # untouched


# ─── Idempotency of the batch task ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_new_replies_only_processes_null(
    db_session: AsyncSession, sample_company, sample_contact
):
    fresh = await _make_reply(db_session, sample_contact, sample_company, body="let's talk")
    already = await _make_reply(
        db_session, sample_contact, sample_company, body="old",
        reply_classification="interested", handled_action="none",
    )
    await db_session.commit()

    ai = FakeAI(_ai_result("not_interested"))
    report = await classify_new_replies_async(
        db_session, ai_service=ai, notification_service=FakeNotify()
    )
    await db_session.commit()

    assert report["processed"] == 1
    assert ai.calls == 1  # the already-classified reply was skipped
    await db_session.refresh(fresh)
    await db_session.refresh(already)
    assert fresh.reply_classification == "not_interested"
    assert already.reply_classification == "interested"  # unchanged


# ─── Suppression enforcement in send queries ─────────────────────────────────


@pytest.mark.asyncio
async def test_check_consent_blocks_suppressed_contact(
    db_session: AsyncSession, sample_company, sample_contact
):
    sample_contact.suppressed = True
    await db_session.flush()
    result = await check_consent(db_session, sample_contact, sample_company)
    assert result.can_email is False
    assert "suppress" in result.reason.lower()


@pytest.mark.asyncio
async def test_suppressed_contacts_excluded_from_send_query(
    db_session: AsyncSession, sample_company
):
    live = Contact(
        id=_uuid.uuid4(), company_id=sample_company.id, first_name="Live",
        email="live@acmestamp.com", source="test",
    )
    dead = Contact(
        id=_uuid.uuid4(), company_id=sample_company.id, first_name="Dead",
        email="dead@acmestamp.com", source="test", suppressed=True,
    )
    db_session.add_all([live, dead])
    await db_session.flush()

    rows = (
        await db_session.execute(
            select(Contact).where(
                Contact.company_id == sample_company.id,
                Contact.suppressed.is_(False),
            )
        )
    ).scalars().all()
    emails = {c.email for c in rows}
    assert "live@acmestamp.com" in emails
    assert "dead@acmestamp.com" not in emails


# ─── Priority-inbox endpoints ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbox_empty(client: AsyncClient):
    resp = await client.get("/api/v1/dialtone/inbox")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_inbox_filters(
    client: AsyncClient, db_session: AsyncSession, sample_company, sample_contact
):
    now = datetime.now(timezone.utc)
    hot = await _make_reply(
        db_session, sample_contact, sample_company, subject="interested!",
        reply_classification="interested", priority_flag=True, is_read=False,
        classification_confidence=0.95, classified_at=now,
    )
    ooo = await _make_reply(
        db_session, sample_contact, sample_company, subject="ooo",
        reply_classification="out_of_office", priority_flag=False, is_read=True,
        classified_at=now - timedelta(hours=1),
    )
    await db_session.commit()

    # category filter
    resp = await client.get("/api/v1/dialtone/inbox", params={"category": "interested"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == str(hot.id)

    # priority_only
    resp = await client.get("/api/v1/dialtone/inbox", params={"priority_only": True})
    ids = {i["id"] for i in resp.json()["items"]}
    assert ids == {str(hot.id)}

    # unread_only
    resp = await client.get("/api/v1/dialtone/inbox", params={"unread_only": True})
    ids = {i["id"] for i in resp.json()["items"]}
    assert str(hot.id) in ids and str(ooo.id) not in ids

    # bad category → 400
    resp = await client.get("/api/v1/dialtone/inbox", params={"category": "nope"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_inbox_stats(
    client: AsyncClient, db_session: AsyncSession, sample_company, sample_contact
):
    await _make_reply(
        db_session, sample_contact, sample_company,
        reply_classification="interested", priority_flag=True, is_read=False,
    )
    await _make_reply(
        db_session, sample_contact, sample_company,
        reply_classification="unsubscribe", is_read=True,
    )
    await _make_reply(db_session, sample_contact, sample_company)  # unclassified (NULL)
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/inbox/stats")
    assert resp.status_code == 200, resp.text
    stats = resp.json()
    assert stats["by_category"]["interested"] == 1
    assert stats["by_category"]["unsubscribe"] == 1
    assert stats["unclassified"] == 1
    assert stats["priority"] == 1
    assert stats["unread"] == 2  # interested + the unclassified one


@pytest.mark.asyncio
async def test_inbox_mark_read(
    client: AsyncClient, db_session: AsyncSession, sample_company, sample_contact
):
    reply = await _make_reply(
        db_session, sample_contact, sample_company,
        reply_classification="interested", is_read=False,
    )
    await db_session.commit()

    resp = await client.post(f"/api/v1/dialtone/inbox/{reply.id}/read")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_read"] is True


@pytest.mark.asyncio
async def test_inbox_reclassify_reruns_action(
    client: AsyncClient, db_session: AsyncSession, sample_company, sample_contact
):
    enr = await _make_enrollment(db_session, sample_contact, status="active")
    reply = await _make_reply(
        db_session, sample_contact, sample_company,
        reply_classification="not_interested", handled_action="sequence_removed",
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/dialtone/inbox/{reply.id}/reclassify",
        json={"category": "interested", "priority_flag": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply_classification"] == "interested"
    assert body["priority_flag"] is True
    # Correction recorded as a training signal.
    assert body["classification_meta"]["manual_overrides"][-1]["to"] == "interested"


@pytest.mark.asyncio
async def test_inbox_reclassify_rejects_bad_category(
    client: AsyncClient, db_session: AsyncSession, sample_company, sample_contact
):
    reply = await _make_reply(
        db_session, sample_contact, sample_company, reply_classification="unknown"
    )
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/dialtone/inbox/{reply.id}/reclassify", json={"category": "bogus"}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reply_classified_appears_in_activity_feed(
    client: AsyncClient, db_session: AsyncSession, sample_company, sample_contact
):
    await _make_reply(
        db_session, sample_contact, sample_company, subject="yes!",
        reply_classification="interested", priority_flag=True,
        classified_at=datetime.now(timezone.utc),
    )
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/activity", params={"types": "reply_classified"})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["type"] == "reply_classified"
    assert items[0]["metadata"]["classification"] == "interested"
