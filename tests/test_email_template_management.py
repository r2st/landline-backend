"""Template authoring that catches the mistake before the mail goes out.

Three failure modes, all previously silent:

* **An unknown merge field.** Jinja substitutes an empty string for an undefined
  name, so ``{{firstname}}`` (the field is ``first_name``) does not raise, does
  not warn, and does not log. It renders as nothing and the mail reads ``Hi ,``.
* **A Jinja syntax error.** Raised at *send* time, inside the dispatcher, on a
  live enrollment — days after the authoring mistake, against someone who is not
  looking.
* **Deleting a template that has sends.** ``email_sends.template_id`` is
  ``ON DELETE SET NULL``, so removing the row detached every send it produced and
  took the answer to "which copy earned those replies" with it.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.email_template import EmailSend, EmailTemplate
from app.services import template_validation

GOOD_BODY = "Hi {{first_name}}, a note about {{company}}. Reply STOP to unsubscribe."


async def _template(db, name="Base", subject="Hi {{first_name}}",
                    body=GOOD_BODY, is_default=False, is_archived=False):
    template = EmailTemplate(
        id=uuid.uuid4(), name=name, subject=subject, body=body,
        is_default=is_default, is_archived=is_archived,
    )
    db.add(template)
    await db.flush()
    return template


async def _send(db, template, status="sent", opened=False, clicked=False,
                replied=False, when=None):
    now = when or datetime.now(timezone.utc)
    db.add(EmailSend(
        id=uuid.uuid4(), template_id=template.id, to_email="lead@acme.com",
        subject="s", status=status, sent_at=now,
        opened_at=now if opened else None,
        clicked_at=now if clicked else None,
        replied_at=now if replied else None,
    ))
    await db.flush()


# --- validate_template -------------------------------------------------------

def test_a_good_template_validates():
    result = template_validation.validate_template("Hi {{first_name}}", GOOD_BODY)
    assert result["valid"] is True
    assert result["errors"] == []
    assert "first_name" in result["fields_used"]


def test_an_unknown_field_is_an_error_not_a_warning():
    """It is never a caller being clever — the render context is a closed set,
    so an unknown name always renders empty."""
    result = template_validation.validate_template(
        "Hi {{firstname}}", "Hello {{firstname}}, unsubscribe here"
    )
    assert result["valid"] is False
    assert any("firstname" in e for e in result["errors"])
    # The message has to name the alternatives, or the author cannot act on it.
    assert any("first_name" in e for e in result["errors"])


def test_a_syntax_error_is_caught_before_the_send_path():
    result = template_validation.validate_template("Hi {{first_name}", GOOD_BODY)
    assert result["valid"] is False
    assert any(e.startswith("subject:") for e in result["errors"])


def test_empty_subject_or_body_is_an_error():
    assert template_validation.validate_template("", GOOD_BODY)["valid"] is False
    assert template_validation.validate_template("Hi", "  ")["valid"] is False


def test_filters_and_attribute_access_are_left_to_jinja():
    result = template_validation.validate_template(
        "Hi {{ first_name|title }}", "About {{ company }}, unsubscribe"
    )
    assert result["valid"] is True


def test_a_field_inside_a_url_warns_but_does_not_block():
    """An empty render there breaks the link rather than reading as a missing
    word — worth saying, not worth refusing."""
    result = template_validation.validate_template(
        "Hi {{first_name}}",
        "See https://apprend.tech/{{city}}/demo — unsubscribe",
    )
    assert result["valid"] is True
    assert any("URL" in w for w in result["warnings"])


def test_copy_with_no_merge_fields_warns():
    result = template_validation.validate_template(
        "Quarterly update", "Same text for everyone. Unsubscribe."
    )
    assert result["valid"] is True
    assert any("broadcast" in w for w in result["warnings"])


def test_a_missing_unsubscribe_line_warns():
    result = template_validation.validate_template(
        "Hi {{first_name}}", "A note about {{company}}."
    )
    assert result["valid"] is True
    assert any("unsubscribe" in w.lower() for w in result["warnings"])


def test_extra_fields_can_be_declared_by_a_wider_caller():
    """A sequence step merges facts the template library does not know about.
    Declaring them beats having the validator reject what it cannot see."""
    result = template_validation.validate_template(
        "Hi {{first_name}}", "Book here: {{booking_link}} — unsubscribe",
        extra_fields=["booking_link"],
    )
    assert result["valid"] is True


def test_unknown_fields_are_not_reported_for_an_unparseable_template():
    """A template that fails to parse produces garbage field names, and a list
    of those on top of the real error is noise."""
    result = template_validation.validate_template("{{ oops", GOOD_BODY)
    assert result["valid"] is False
    assert len([e for e in result["errors"] if "Unknown merge field" in e]) == 0


# --- Write paths -------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_rejects_an_unknown_merge_field(client):
    resp = await client.post("/api/v1/email-templates", json={
        "name": "Typo", "subject": "Hi {{firstname}}", "body": GOOD_BODY,
    })
    assert resp.status_code == 400
    assert "firstname" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_accepts_a_valid_template(client):
    resp = await client.post("/api/v1/email-templates", json={
        "name": "Good", "subject": "Hi {{first_name}}", "body": GOOD_BODY,
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_archived"] is False


@pytest.mark.asyncio
async def test_update_validates_against_the_final_state_not_the_patch(
    client, db_session
):
    """Editing only the subject still has to render alongside the stored body."""
    template = await _template(db_session, name="Patchable")
    await db_session.commit()

    ok = await client.put(f"/api/v1/email-templates/{template.id}",
                          json={"subject": "Hi {{full_name}}"})
    assert ok.status_code == 200, ok.text

    bad = await client.put(f"/api/v1/email-templates/{template.id}",
                           json={"body": "Hello {{nope}}"})
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_update_of_an_unrelated_field_skips_content_validation(
    client, db_session
):
    template = await _template(db_session, name="Renameable")
    await db_session.commit()

    resp = await client.put(f"/api/v1/email-templates/{template.id}",
                            json={"name": "Renamed"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed"


# --- /validate ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_endpoint_returns_warnings_the_save_path_swallows(client):
    resp = await client.post("/api/v1/email-templates/validate", json={
        "subject": "Hi {{first_name}}", "body": "A note about {{company}}.",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["valid"] is True
    assert data["warnings"]
    assert "first_name" in data["valid_fields"]


@pytest.mark.asyncio
async def test_validate_endpoint_checks_an_edit_against_the_stored_body(
    client, db_session
):
    template = await _template(db_session, name="Stored")
    await db_session.commit()

    resp = await client.post("/api/v1/email-templates/validate", json={
        "template_id": str(template.id), "subject": "Hi {{nope}}",
    })
    assert resp.json()["valid"] is False


# --- Archive / delete --------------------------------------------------------

@pytest.mark.asyncio
async def test_deleting_a_used_template_is_refused(client, db_session):
    """It would detach the sends and lose the attribution."""
    await _template(db_session, name="Keeper", is_default=True)
    used = await _template(db_session, name="Used")
    await _send(db_session, used)
    await db_session.commit()

    resp = await client.delete(f"/api/v1/email-templates/{used.id}")
    assert resp.status_code == 409
    assert "archive" in resp.json()["detail"].lower()
    assert str(used.id) in resp.json()["detail"]


@pytest.mark.asyncio
async def test_an_unused_template_can_still_be_deleted(client, db_session):
    await _template(db_session, name="Keeper", is_default=True)
    draft = await _template(db_session, name="Draft")
    await db_session.commit()

    resp = await client.delete(f"/api/v1/email-templates/{draft.id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_archiving_hides_it_from_the_list_but_keeps_the_sends(
    client, db_session
):
    await _template(db_session, name="Keeper", is_default=True)
    retiring = await _template(db_session, name="Retiring")
    await _send(db_session, retiring)
    await db_session.commit()

    resp = await client.post(f"/api/v1/email-templates/{retiring.id}/archive")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_archived"] is True
    assert resp.json()["archived_at"] is not None

    live = await client.get("/api/v1/email-templates")
    assert [t["name"] for t in live.json()] == ["Keeper"]

    everything = await client.get("/api/v1/email-templates?include_archived=true")
    assert {t["name"] for t in everything.json()} == {"Keeper", "Retiring"}

    # The sends — and the attribution — survive.
    perf = await client.get(f"/api/v1/email-templates/{retiring.id}/performance")
    assert perf.json()["sent"] == 1


@pytest.mark.asyncio
async def test_the_default_template_cannot_be_archived(client, db_session):
    """Every un-templated send falls back to it; archiving would leave that path
    resolving to something nobody can see."""
    default = await _template(db_session, name="Default", is_default=True)
    await db_session.commit()

    resp = await client.post(f"/api/v1/email-templates/{default.id}/archive")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_archiving_is_idempotent_and_reversible(client, db_session):
    await _template(db_session, name="Keeper", is_default=True)
    template = await _template(db_session, name="Toggle")
    await db_session.commit()

    first = await client.post(f"/api/v1/email-templates/{template.id}/archive")
    stamp = first.json()["archived_at"]
    again = await client.post(f"/api/v1/email-templates/{template.id}/archive")
    assert again.json()["archived_at"] == stamp

    back = await client.post(f"/api/v1/email-templates/{template.id}/unarchive")
    assert back.json()["is_archived"] is False
    assert back.json()["archived_at"] is None


# --- Clone -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clone_copies_the_copy_but_never_the_default_flag(client, db_session):
    """Inheriting default status would silently repoint every un-templated send
    at untested copy."""
    source = await _template(db_session, name="Winner", is_default=True)
    await db_session.commit()

    resp = await client.post(f"/api/v1/email-templates/{source.id}/clone")
    assert resp.status_code == 201, resp.text
    clone = resp.json()
    assert clone["name"] == "Winner (copy)"
    assert clone["subject"] == source.subject
    assert clone["body"] == source.body
    assert clone["is_default"] is False
    assert clone["is_archived"] is False


@pytest.mark.asyncio
async def test_clone_accepts_a_name_and_rejects_a_collision(client, db_session):
    source = await _template(db_session, name="Source", is_default=True)
    await _template(db_session, name="Taken")
    await db_session.commit()

    named = await client.post(
        f"/api/v1/email-templates/{source.id}/clone?name=Variant B"
    )
    assert named.status_code == 201
    assert named.json()["name"] == "Variant B"

    clash = await client.post(
        f"/api/v1/email-templates/{source.id}/clone?name=Taken"
    )
    assert clash.status_code == 409


@pytest.mark.asyncio
async def test_clone_of_a_missing_template_is_404(client):
    resp = await client.post(f"/api/v1/email-templates/{uuid.uuid4()}/clone")
    assert resp.status_code == 404


# --- Performance -------------------------------------------------------------

@pytest.mark.asyncio
async def test_performance_rates_are_over_delivered_not_attempted(
    client, db_session
):
    """A template whose sends error looks identical to one being ignored if you
    only read the open rate — failed sends never open."""
    template = await _template(db_session, name="Mixed", is_default=True)
    for _ in range(8):
        await _send(db_session, template, opened=True)
    for _ in range(2):
        await _send(db_session, template, status="failed")
    await db_session.commit()

    resp = await client.get(f"/api/v1/email-templates/{template.id}/performance")
    data = resp.json()
    assert data["sent"] == 10
    assert data["failed"] == 2
    assert data["delivered"] == 8
    assert data["failure_rate"] == 0.2
    # 8/8, not 8/10 — dividing by attempts reports a copy problem for what is a
    # delivery problem.
    assert data["open_rate"] == 1.0


@pytest.mark.asyncio
async def test_a_bounce_counts_as_a_delivery_failure(client, db_session):
    template = await _template(db_session, name="Bouncy", is_default=True)
    await _send(db_session, template)
    await _send(db_session, template, status="bounced")
    await db_session.commit()

    resp = await client.get(f"/api/v1/email-templates/{template.id}/performance")
    assert resp.json()["failed"] == 1
    assert resp.json()["delivered"] == 1


@pytest.mark.asyncio
async def test_performance_counts_engagement_and_bounds_the_window(
    client, db_session
):
    template = await _template(db_session, name="Windowed", is_default=True)
    now = datetime.now(timezone.utc)
    await _send(db_session, template, opened=True, clicked=True, replied=True)
    await _send(db_session, template, opened=True, when=now - timedelta(days=60))
    await db_session.commit()

    all_time = await client.get(
        f"/api/v1/email-templates/{template.id}/performance"
    )
    assert all_time.json()["sent"] == 2
    assert all_time.json()["opened"] == 2
    assert all_time.json()["clicked"] == 1
    assert all_time.json()["replied"] == 1

    recent = await client.get(
        f"/api/v1/email-templates/{template.id}/performance?days=30"
    )
    assert recent.json()["sent"] == 1


@pytest.mark.asyncio
async def test_performance_of_an_unused_template_is_zeroes_not_a_crash(
    client, db_session
):
    template = await _template(db_session, name="Unused", is_default=True)
    await db_session.commit()

    resp = await client.get(f"/api/v1/email-templates/{template.id}/performance")
    data = resp.json()
    assert data["sent"] == 0
    assert data["open_rate"] == 0.0
    assert data["first_send_at"] is None


@pytest.mark.asyncio
async def test_performance_of_a_missing_template_is_404(client):
    resp = await client.get(f"/api/v1/email-templates/{uuid.uuid4()}/performance")
    assert resp.status_code == 404
