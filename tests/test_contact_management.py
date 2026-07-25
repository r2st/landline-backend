"""Contact management: tags, filters, bulk suppression, engagement.

The list view could already search and filter on the fields it had. What it
couldn't do is the work an operator actually does with a list: label a batch of
contacts they met somewhere, find them again later, take a bad slice out of
circulation in one action, and see what has already been sent to someone before
deciding what to send next.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services import contact_tags


async def _company(db) -> Company:
    c = Company(id=uuid.uuid4(), name="Acme Stamping", domain="acme.com",
                country="US", source="test")
    db.add(c)
    await db.flush()
    return c


async def _contact(db, company, *, email=None, tags=None, **kw) -> Contact:
    c = Contact(
        id=uuid.uuid4(), company_id=company.id,
        first_name="Dana", last_name="Reyes",
        email=email or f"c{uuid.uuid4().hex[:8]}@acme.com",
        tags=tags or [], **kw,
    )
    db.add(c)
    await db.flush()
    return c


# --- normalisation ------------------------------------------------------------


def test_tag_normalisation_collapses_near_duplicates():
    """Without this the filter menu shows one label three times."""
    assert contact_tags.normalize_tag("  IMTS 2026 ") == "imts 2026"
    assert contact_tags.normalize_tag("trade  show") == "trade show"
    assert contact_tags.normalize_tag("   ") is None
    assert contact_tags.normalize_tag(None) is None


def test_normalize_tags_dedupes_and_keeps_order():
    """Order is preserved so a contact's chips don't reshuffle on every edit."""
    assert contact_tags.normalize_tags(
        ["Warm", "warm", " WARM ", "cold"]
    ) == ["warm", "cold"]


def test_tag_length_and_count_are_capped():
    assert len(contact_tags.normalize_tag("x" * 500)) == contact_tags.MAX_TAG_LENGTH
    many = contact_tags.normalize_tags([f"t{i}" for i in range(200)])
    assert len(many) == contact_tags.MAX_TAGS_PER_CONTACT


def test_delta_is_idempotent_in_both_directions():
    """A bulk-select UI generates both of these constantly."""
    assert contact_tags.apply_tag_delta(["a"], add=["a"]) == ["a"]
    assert contact_tags.apply_tag_delta(["a"], remove=["nope"]) == ["a"]


def test_contradictory_delta_resolves_to_removed():
    """Same tag added and removed — one answer, and the same one either order."""
    assert contact_tags.apply_tag_delta(["a"], add=["b"], remove=["b"]) == ["a"]


# --- tag endpoints ------------------------------------------------------------


async def test_tag_a_contact_and_read_it_back(client, db_session):
    company = await _company(db_session)
    contact = await _contact(db_session, company)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/contacts/{contact.id}/tags",
        json={"add": ["IMTS 2026", "warm intro"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tags"] == ["imts 2026", "warm intro"]

    resp = await client.post(
        f"/api/v1/contacts/{contact.id}/tags", json={"remove": ["warm intro"]}
    )
    assert resp.json()["tags"] == ["imts 2026"]


async def test_tag_list_reports_usage_counts(client, db_session):
    """A tag on two contacts and a tag on two thousand must not look the same."""
    company = await _company(db_session)
    await _contact(db_session, company, tags=["warm", "imts 2026"])
    await _contact(db_session, company, tags=["warm"])
    await _contact(db_session, company, tags=[])
    await db_session.commit()

    resp = await client.get("/api/v1/contacts/tags")
    assert resp.status_code == 200
    tags = {t["tag"]: t["count"] for t in resp.json()["tags"]}
    assert tags == {"warm": 2, "imts 2026": 1}
    # Most-used first, so the filter menu leads with what's useful.
    assert resp.json()["tags"][0]["tag"] == "warm"


async def test_filter_by_tag(client, db_session):
    company = await _company(db_session)
    tagged = await _contact(db_session, company, tags=["imts 2026"])
    await _contact(db_session, company, tags=["cold"])
    await db_session.commit()

    resp = await client.get("/api/v1/contacts/?tags=imts 2026")
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.json()["items"]]
    assert ids == [str(tagged.id)]


async def test_tag_filter_does_not_match_a_longer_tag(client, db_session):
    """"imts" must not match "imts 2026" — the substring fallback is exact."""
    company = await _company(db_session)
    await _contact(db_session, company, tags=["imts 2026"])
    await db_session.commit()

    resp = await client.get("/api/v1/contacts/?tags=imts")
    assert resp.json()["items"] == []


async def test_tag_filter_all_vs_any(client, db_session):
    company = await _company(db_session)
    both = await _contact(db_session, company, tags=["warm", "imts 2026"])
    one = await _contact(db_session, company, tags=["warm"])
    await db_session.commit()

    all_resp = await client.get("/api/v1/contacts/?tags=warm,imts 2026&tag_match=all")
    assert [c["id"] for c in all_resp.json()["items"]] == [str(both.id)]

    any_resp = await client.get("/api/v1/contacts/?tags=warm,imts 2026&tag_match=any")
    assert {c["id"] for c in any_resp.json()["items"]} == {str(both.id), str(one.id)}


async def test_bulk_tag_applies_deltas_not_replacements(client, db_session):
    """A list-view bulk action doesn't know the contacts' other tags and must
    not wipe them."""
    company = await _company(db_session)
    a = await _contact(db_session, company, tags=["existing"])
    b = await _contact(db_session, company, tags=[])
    ghost = uuid.uuid4()
    await db_session.commit()

    resp = await client.post(
        "/api/v1/contacts/bulk-tag",
        json={"contact_ids": [str(a.id), str(b.id), str(ghost)], "add": ["Q3 push"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 2
    # A bad paste is reported, not silently swallowed as success.
    assert body["not_found"] == [str(ghost)]

    await db_session.refresh(a)
    await db_session.refresh(b)
    assert a.tags == ["existing", "q3 push"]
    assert b.tags == ["q3 push"]


async def test_rename_tag_merges_without_duplicating(client, db_session):
    company = await _company(db_session)
    both = await _contact(db_session, company, tags=["tradeshow", "trade show"])
    only_old = await _contact(db_session, company, tags=["tradeshow"])
    await db_session.commit()

    resp = await client.post(
        "/api/v1/contacts/tags/rename",
        json={"old": "tradeshow", "new": "trade show"},
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] == 2

    await db_session.refresh(both)
    await db_session.refresh(only_old)
    assert both.tags == ["trade show"]
    assert only_old.tags == ["trade show"]


async def test_rename_to_itself_is_a_no_op(client, db_session):
    resp = await client.post(
        "/api/v1/contacts/tags/rename", json={"old": "warm", "new": " WARM "}
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] == 0


# --- filters ------------------------------------------------------------------


async def test_filter_by_suppression_and_bounces(client, db_session):
    company = await _company(db_session)
    clean = await _contact(db_session, company)
    blocked = await _contact(db_session, company, suppressed=True)
    bounced = await _contact(db_session, company, bounce_count=3)
    await db_session.commit()

    resp = await client.get("/api/v1/contacts/?suppressed=true")
    assert [c["id"] for c in resp.json()["items"]] == [str(blocked.id)]

    resp = await client.get("/api/v1/contacts/?has_bounced=true")
    assert [c["id"] for c in resp.json()["items"]] == [str(bounced.id)]

    resp = await client.get("/api/v1/contacts/?has_bounced=false")
    ids = {c["id"] for c in resp.json()["items"]}
    assert str(clean.id) in ids and str(bounced.id) not in ids


async def test_list_exposes_compliance_state(client, db_session):
    """The management view shows who's off-limits without a call per row."""
    company = await _company(db_session)
    await _contact(db_session, company, suppressed=True, bounce_count=2,
                   email_status="invalid")
    await db_session.commit()

    row = (await client.get("/api/v1/contacts/")).json()["items"][0]
    assert row["suppressed"] is True
    assert row["bounce_count"] == 2
    assert row["email_status"] == "invalid"


# --- bulk suppression ---------------------------------------------------------


async def test_bulk_suppress_stops_live_enrollments(client, db_session):
    """Leaving them active means the queue keeps picking a contact it will then
    refuse to mail, forever."""
    company = await _company(db_session)
    contact = await _contact(db_session, company)
    seq = OutreachSequence(id=uuid.uuid4(), name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(seq)
    await db_session.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status="active",
    )
    db_session.add(enr)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/contacts/bulk-suppress",
        json={"contact_ids": [str(contact.id)], "suppressed": True,
              "reason": "bought list"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1
    assert resp.json()["enrollments_stopped"] == 1

    await db_session.refresh(contact)
    await db_session.refresh(enr)
    assert contact.suppressed is True
    # 'unsubscribed', not 'bounced' — a compliance stop must not dent sender
    # deliverability metrics.
    assert enr.status == "unsubscribed"


async def test_bulk_suppress_writes_a_consent_record(client, db_session):
    from app.models.consent import ConsentRecord

    company = await _company(db_session)
    contact = await _contact(db_session, company)
    await db_session.commit()

    await client.post(
        "/api/v1/contacts/bulk-suppress",
        json={"contact_ids": [str(contact.id)], "reason": "gdpr request"},
    )

    records = (
        await db_session.execute(
            select(ConsentRecord).where(ConsentRecord.contact_id == contact.id)
        )
    ).scalars().all()
    assert len(records) == 1
    assert records[0].revoked_at is not None
    assert "gdpr request" in records[0].basis


async def test_unsuppressing_does_not_restart_sequences(client, db_session):
    """Un-ticking a checkbox must not silently resume cold outreach."""
    company = await _company(db_session)
    contact = await _contact(db_session, company, suppressed=True)
    seq = OutreachSequence(id=uuid.uuid4(), name=f"s-{uuid.uuid4().hex[:6]}")
    db_session.add(seq)
    await db_session.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        status="unsubscribed",
    )
    db_session.add(enr)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/contacts/bulk-suppress",
        json={"contact_ids": [str(contact.id)], "suppressed": False},
    )
    assert resp.status_code == 200
    assert resp.json()["enrollments_stopped"] == 0

    await db_session.refresh(contact)
    await db_session.refresh(enr)
    assert contact.suppressed is False
    assert enr.status == "unsubscribed"


# --- engagement ---------------------------------------------------------------


async def test_engagement_counts_both_send_tables(client, db_session):
    """Sequence sends live only in outreach_events; manual sends only in
    email_sends. Reading one is how a surface reports "never emailed" about
    someone we've emailed repeatedly."""
    company = await _company(db_session)
    contact = await _contact(db_session, company)
    seq = OutreachSequence(id=uuid.uuid4(), name="Molding outreach")
    db_session.add(seq)
    await db_session.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status="active",
    )
    db_session.add(enr)
    await db_session.flush()

    base = datetime.now(timezone.utc) - timedelta(days=3)
    db_session.add(OutreachEvent(
        enrollment_id=enr.id, step_number=1, event_type="sent", occurred_at=base,
        event_metadata={"subject": "quick question about your presses"},
    ))
    db_session.add(OutreachEvent(
        enrollment_id=enr.id, step_number=1, event_type="opened",
        occurred_at=base + timedelta(hours=2),
    ))
    db_session.add(EmailSend(
        id=uuid.uuid4(), contact_id=contact.id, to_email=contact.email,
        subject="following up", status="sent",
        sent_at=base + timedelta(days=1),
        replied_at=base + timedelta(days=1, hours=3),
    ))
    await db_session.commit()

    resp = await client.get(f"/api/v1/contacts/{contact.id}/engagement")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["sent"] == 2       # one from each table
    assert body["opened"] == 1
    assert body["replied"] == 1
    assert body["open_rate"] == 0.5
    assert body["active_enrollments"] == 1

    # Newest first, and the sequence send's subject survives (it lives in the
    # event metadata — there is no EmailSend row carrying it).
    assert body["events"][0]["type"] == "replied"
    sequence_send = next(
        e for e in body["events"] if e["source"] == "outreach" and e["type"] == "sent"
    )
    assert sequence_send["subject"] == "quick question about your presses"
    assert sequence_send["sequence_name"] == "Molding outreach"


async def test_engagement_ignores_sends_that_never_left(client, db_session):
    """A send declined by the warm-up cap is not a send.

    Counting it inflates the denominator every rate divides by, so a contact
    we've never actually emailed reads as 0% open rate rather than "untouched".
    """
    company = await _company(db_session)
    contact = await _contact(db_session, company)
    now = datetime.now(timezone.utc)
    for status in ("skipped", "halted", "error"):
        db_session.add(EmailSend(
            id=uuid.uuid4(), contact_id=contact.id, to_email=contact.email,
            subject="never sent", status=status, sent_at=now,
        ))
    db_session.add(EmailSend(
        id=uuid.uuid4(), contact_id=contact.id, to_email=contact.email,
        subject="really sent", status="sent", sent_at=now, opened_at=now,
    ))
    await db_session.commit()

    body = (await client.get(f"/api/v1/contacts/{contact.id}/engagement")).json()
    assert body["sent"] == 1
    assert body["open_rate"] == 1.0
    assert [e["subject"] for e in body["events"]] == ["really sent", "really sent"]


async def test_engagement_of_an_untouched_contact_is_zeroed_not_undefined(
    client, db_session
):
    company = await _company(db_session)
    contact = await _contact(db_session, company)
    await db_session.commit()

    body = (await client.get(f"/api/v1/contacts/{contact.id}/engagement")).json()
    assert body["sent"] == 0
    assert body["open_rate"] == 0.0
    assert body["events"] == []
    assert body["last_touch_at"] is None


async def test_engagement_404s_for_an_unknown_contact(client):
    resp = await client.get(f"/api/v1/contacts/{uuid.uuid4()}/engagement")
    assert resp.status_code == 404
