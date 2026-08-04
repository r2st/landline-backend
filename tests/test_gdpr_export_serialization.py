"""The subject access request has to be answerable, and has to contain the data.

``GET /api/v1/gdpr/export`` serialises each row by walking its columns. A
column's *name* and the *attribute* that maps it are two different strings, and
one model in the schema is a case where they differ::

    event_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

``metadata`` is taken on a declarative class, hence the rename. Reading the
column name off the instance therefore evaluated ``getattr(event, "metadata")``
and got ``Base.metadata`` — the SQLAlchemy registry object.

That broke the endpoint in two directions at once:

* ``MetaData`` is not JSON serialisable, so the response failed to encode and
  the request 500'd — for any subject who had been through a sequence, i.e. for
  every real prospect, since the sequence engine is the main sending path.
* the value it displaced was the recipient, the subject line and the full
  rendered body of every message sent to that person. The largest holding of the
  subject's data in the system was missing from the disclosure, and the failure
  that hid it looked like an unrelated encoding error.

These tests assert both halves, and one guards the shape generically so a future
renamed column cannot reintroduce it.
"""

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect as sa_inspect

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services import gdpr

_EMAIL = "dmitri@fabrication.co"
_BODY = "<p>Hi Dmitri — saw the new press line at Fabrication Ltd.</p>"


async def _seed(db, email=_EMAIL):
    company = Company(id=uuid.uuid4(), name="Fabrication Ltd", country="US", source="t")
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Dmitri",
        last_name="Sarkisyan", email=email, source="t",
    )
    db.add(contact)
    seq = OutreachSequence(id=uuid.uuid4(), name="press-line", is_active=True)
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        status="active", current_step=2, enrolled_at=datetime.now(timezone.utc),
    )
    db.add(enr)
    await db.flush()
    db.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=1, event_type="sent",
        occurred_at=datetime.now(timezone.utc),
        event_metadata={
            "status": "sent", "to_email": email,
            "subject": "Dmitri — a question about your press line",
            "body_html": _BODY,
        },
    ))
    await db.commit()
    return contact


@pytest.mark.asyncio
async def test_export_is_json_serialisable(db_session):
    """The whole point of the endpoint is that it can be handed to someone."""
    await _seed(db_session)
    data = await gdpr.export_subject(db_session, _EMAIL)
    # Raises TypeError on a MetaData object — which is what FastAPI's encoder
    # was hitting, one layer further out, as a 500.
    json.dumps(data)


@pytest.mark.asyncio
async def test_export_discloses_the_sequence_message_body(db_session):
    await _seed(db_session)
    data = await gdpr.export_subject(db_session, _EMAIL)

    events = data["outreach_events"]
    assert len(events) == 1
    meta = events[0]["metadata"]
    assert isinstance(meta, dict), f"expected the JSONB blob, got {type(meta).__name__}"
    assert meta["to_email"] == _EMAIL
    assert meta["body_html"] == _BODY
    assert meta["subject"].startswith("Dmitri")


@pytest.mark.asyncio
async def test_export_endpoint_returns_200(client, db_session):
    """End to end, because the encoder is where the 500 actually surfaced.

    The service-level test above catches the value; this one catches the
    response, and they are not the same assertion — a serialiser that returned
    something merely *stringifiable* would satisfy one and still hand the subject
    the repr of an ORM internal instead of their data.
    """
    await _seed(db_session)
    resp = await client.get("/api/v1/gdpr/export", params={"email": _EMAIL})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outreach_events"][0]["metadata"]["body_html"] == _BODY


@pytest.mark.asyncio
async def test_serializer_uses_attributes_not_column_names(db_session):
    """A renamed column must not silently serialise a class attribute.

    Stated over the mapper rather than against ``OutreachEvent`` by name: this
    is the check that survives the next model that has to rename a column
    because the natural name collides with something on the declarative base.
    ``metadata``, ``registry`` and ``query`` are all such names.
    """
    await _seed(db_session)
    data = await gdpr.export_subject(db_session, _EMAIL)

    mapper = sa_inspect(OutreachEvent)
    renamed = [
        (p.key, p.columns[0].name)
        for p in mapper.column_attrs
        if p.columns[0].name != p.key
    ]
    # If this model ever stops being the renamed-column case, the test above it
    # is still meaningful but this one is vacuous — so say so rather than pass.
    assert renamed == [("event_metadata", "metadata")], (
        "OutreachEvent no longer renames a column; move this guard to whichever "
        "model does, or the serialiser regression has nothing pinning it"
    )
    exported = data["outreach_events"][0]
    for _attr, column in renamed:
        assert not isinstance(exported[column], type(OutreachEvent.metadata))
