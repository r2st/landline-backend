"""F6 — signal-based send timing.

``surface_prospects_on_signals`` reacts to fresh, relevant company news by
finding un-enrolled emailable contacts and either flagging them (default) or
auto-enrolling them. Covers freshness/relevance gating, the un-enrolled filter,
idempotency via ``actioned``, and the auto-enroll path.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models.company_news import CompanyNews
from app.models.contact import Contact
from app.models.follow_up import FollowUp
from app.models.intent_signal import IntentSignal
from app.models.outreach import OutreachEnrollment
from app.services.signal_enrollment import surface_prospects_on_signals


def _news(company_id, *, hours_ago=1, relevance=8, category="funding", actioned=False,
          is_actionable=True, title="Raised $10M Series A"):
    return CompanyNews(
        id=uuid.uuid4(), company_id=company_id, title=title,
        discovered_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        category=category, relevance_score=relevance,
        is_actionable=is_actionable, actioned=actioned,
    )


@pytest.mark.asyncio
async def test_fresh_signal_flags_unenrolled_prospect(db_session, sample_company, sample_contact):
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )

    assert result["companies"] == 1
    assert result["flagged"] == 1
    assert result["enrolled"] == 0
    assert result["signals_actioned"] == 1

    followups = (await db_session.execute(select(FollowUp))).scalars().all()
    assert len(followups) == 1
    assert "PRIORITY" in followups[0].reason
    assert followups[0].contact_id == sample_contact.id


@pytest.mark.asyncio
async def test_stale_signal_is_ignored(db_session, sample_company, sample_contact):
    db_session.add(_news(sample_company.id, hours_ago=200))
    await db_session.flush()
    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )
    assert result["flagged"] == 0


@pytest.mark.asyncio
async def test_low_relevance_non_actionable_signal_ignored(
    db_session, sample_company, sample_contact
):
    db_session.add(_news(sample_company.id, relevance=2, is_actionable=False))
    await db_session.flush()
    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )
    assert result["flagged"] == 0


@pytest.mark.asyncio
async def test_already_enrolled_contact_is_not_flagged(
    db_session, sample_company, sample_contact
):
    # Contact already in an active sequence → not an un-enrolled prospect.
    from app.models.sequence import OutreachSequence
    seq = OutreachSequence(id=uuid.uuid4(), name="s")
    db_session.add(seq)
    await db_session.flush()
    db_session.add(OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    ))
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )
    assert result["flagged"] == 0
    assert result["companies"] == 0


@pytest.mark.asyncio
async def test_signal_is_marked_actioned_so_it_runs_once(
    db_session, sample_company, sample_contact
):
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    first = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )
    assert first["flagged"] == 1

    # Second sweep: the signal is now actioned → nothing new.
    second = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )
    assert second["flagged"] == 0

    news = (await db_session.execute(select(CompanyNews))).scalars().all()
    assert all(n.actioned for n in news)


@pytest.mark.asyncio
async def test_unsubscribed_contact_is_skipped(db_session, sample_company):
    c = Contact(
        id=uuid.uuid4(), company_id=sample_company.id, first_name="No",
        email="no@acmestamp.com", source="test", unsubscribed=True,
    )
    db_session.add(c)
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )
    assert result["flagged"] == 0


@pytest.mark.asyncio
async def test_auto_enroll_path_creates_enrollment(db_session, sample_company, sample_contact):
    # Seed the vertical sequence the company resolves to so auto-enroll has a
    # target. sample_company is 'Metal Stamping' → resolve_vertical picks a
    # vertical; create a matching active sequence tagged with its key.
    from app.models.sequence import OutreachSequence
    from app.services.industry_verticals import resolve_vertical

    vertical = resolve_vertical(
        sample_company.industry_segment or "", sample_company.name or ""
    )
    if vertical is None:
        pytest.skip("no vertical resolved for the sample company")
    db_session.add(OutreachSequence(
        id=uuid.uuid4(), name=vertical.sequence_name, industry=vertical.key,
        is_active=True, status="active",
    ))
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=True
    )
    assert result["enrolled"] == 1
    assert result["flagged"] == 0
    n_enroll = (
        await db_session.execute(select(func.count()).select_from(OutreachEnrollment))
    ).scalar_one()
    assert n_enroll == 1


# --- Buyer-intent signals ----------------------------------------------------
#
# `IntentSignal` is the stronger of the two tables: `intent_monitor` scored it
# for relevance to our value prop *and* for urgency, where a news item was only
# scored for being about this company. It fed `fit_score` and the copywriter's
# prompt — both of which improve a message that was going to be sent anyway —
# and caused no outreach to exist. A company that just posted a Quality Engineer
# role waited for the daily sweep to reach it on the merits, like a cold name.


def _intent(company_id, *, hours_ago=1, relevance=90, urgency=80, actioned=False,
            expires_in_days=60, signal_type="hiring", title="Hiring a Quality Engineer"):
    now = datetime.now(timezone.utc)
    return IntentSignal(
        id=uuid.uuid4(), company_id=company_id, signal_type=signal_type, title=title,
        relevance_score=relevance, urgency_score=urgency,
        detected_at=now - timedelta(hours=hours_ago),
        expires_at=None if expires_in_days is None else now + timedelta(days=expires_in_days),
        actioned=actioned,
    )


@pytest.mark.asyncio
async def test_a_fresh_buying_signal_surfaces_the_prospect(
    db_session, sample_company, sample_contact
):
    db_session.add(_intent(sample_company.id))
    await db_session.flush()

    result = await surface_prospects_on_signals(db_session, fresh_hours=48)

    assert result["flagged"] == 1
    assert result["intent_actioned"] == 1
    assert result["news_actioned"] == 0
    followup = (await db_session.execute(select(FollowUp))).scalars().one()
    assert "Hiring a Quality Engineer" in followup.reason
    assert followup.contact_id == sample_contact.id


@pytest.mark.asyncio
async def test_a_weak_buying_signal_is_not_worth_interrupting_for(
    db_session, sample_company, sample_contact
):
    """The monitor keeps anything above 30 relevance. Recording it and acting on
    it are different bars — a mildly relevant, non-urgent mention is not a reason
    to jump a company to the front."""
    db_session.add(_intent(sample_company.id, relevance=40, urgency=20))  # 32.0
    await db_session.flush()

    result = await surface_prospects_on_signals(db_session, fresh_hours=48)
    assert result["flagged"] == 0
    assert result["intent_actioned"] == 0


@pytest.mark.asyncio
async def test_a_signal_whose_buying_window_has_closed_is_not_acted_on(
    db_session, sample_company, sample_contact
):
    """Freshly *detected* is not the same as still live. A signal we learned an
    hour ago whose window already closed describes a purchase made without us."""
    db_session.add(_intent(sample_company.id, hours_ago=1, expires_in_days=-1))
    await db_session.flush()

    result = await surface_prospects_on_signals(db_session, fresh_hours=48)
    assert result["flagged"] == 0
    assert result["intent_actioned"] == 0


@pytest.mark.asyncio
async def test_a_signal_with_no_window_is_treated_as_live(
    db_session, sample_company, sample_contact
):
    """``expires_at`` is nullable. NULL means the analyser inferred no window,
    not that the window closed — dropping those would silently ignore a whole
    class of signal."""
    db_session.add(_intent(sample_company.id, expires_in_days=None))
    await db_session.flush()

    result = await surface_prospects_on_signals(db_session, fresh_hours=48)
    assert result["flagged"] == 1


@pytest.mark.asyncio
async def test_an_actioned_signal_is_not_surfaced_twice(
    db_session, sample_company, sample_contact
):
    """The sweep runs repeatedly. Without the flag, one job posting flags the
    same contact every run until the freshness window closes."""
    signal = _intent(sample_company.id)
    db_session.add(signal)
    await db_session.flush()

    first = await surface_prospects_on_signals(db_session, fresh_hours=48)
    assert first["flagged"] == 1
    assert signal.actioned is True

    second = await surface_prospects_on_signals(db_session, fresh_hours=48)
    assert second["flagged"] == 0
    assert (await db_session.execute(
        select(func.count(FollowUp.id))
    )).scalar() == 1


@pytest.mark.asyncio
async def test_intent_can_be_switched_off_without_touching_news(
    db_session, sample_company, sample_contact
):
    db_session.add(_intent(sample_company.id))
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, include_intent=False
    )
    assert result["news_actioned"] == 1
    assert result["intent_actioned"] == 0


@pytest.mark.asyncio
async def test_intent_headlines_the_followup_over_press_at_the_same_company(
    db_session, sample_company, sample_contact
):
    """Both tables can carry the same week's event. The scales are not
    comparable (1-10 against 0-100), so the tie is broken on source rather than
    on the numbers: intent was scored against our value prop, press was not."""
    db_session.add(_news(sample_company.id, relevance=10, title="Opens new facility"))
    db_session.add(_intent(sample_company.id, title="Hiring a Quality Engineer"))
    await db_session.flush()

    result = await surface_prospects_on_signals(db_session, fresh_hours=48)

    # One prospect, one follow-up — both signals actioned, neither duplicated.
    assert result["flagged"] == 1
    assert result["news_actioned"] == 1 and result["intent_actioned"] == 1
    followup = (await db_session.execute(select(FollowUp))).scalars().one()
    assert "Hiring a Quality Engineer" in followup.reason
    assert "Opens new facility" not in followup.reason


@pytest.mark.asyncio
async def test_a_buying_signal_does_not_bypass_the_human_by_default(
    db_session, sample_company, sample_contact
):
    """The safety question this answers: a hot signal is evidence about *timing*.
    The case for skipping human review would have to be about the copy, and no
    signal speaks to that. Default stays flag-for-a-human."""
    db_session.add(_intent(sample_company.id, relevance=100, urgency=100))
    await db_session.flush()

    result = await surface_prospects_on_signals(db_session, fresh_hours=48)
    assert result["flagged"] == 1
    assert result["enrolled"] == 0
    assert (await db_session.execute(
        select(func.count(OutreachEnrollment.id))
    )).scalar() == 0


# --------------------------------------------------------------------------
# Fan-out cap — one signal must not blast every mailbox at a company
# --------------------------------------------------------------------------


async def _company_with_contact_mix(db_session, company):
    """The contact mix round 9's scraper fix now produces at an SMB shop.

    Generic inboxes are deliberately kept (they are all a small shop often
    exposes), so ``info@`` + ``sales@`` alongside named people is the normal
    case rather than the exception.
    """
    made = {}
    for addr, primary in [
        ("info@acme.test", False),
        ("sales@acme.test", False),
        ("jane@acme.test", False),
        ("bob@acme.test", False),
    ]:
        c = Contact(
            id=uuid.uuid4(), company_id=company.id,
            first_name=addr.split("@")[0], email=addr, source="test",
            is_primary=primary,
        )
        db_session.add(c)
        made[addr] = c
    await db_session.flush()
    return made


@pytest.mark.asyncio
async def test_one_signal_does_not_flag_every_contact_at_a_company(
    db_session, sample_company, sample_contact
):
    """The burst: four addresses, one buyer.

    Before the cap this surfaced every emailable contact, and on the auto-enroll
    path dispatched a send for each immediately — a simultaneous
    multi-recipient blast at the exact company a buying signal says is worth
    winning.
    """
    await _company_with_contact_mix(db_session, sample_company)
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, max_contacts_per_company=2
    )

    assert result["flagged"] == 2
    followups = (await db_session.execute(select(FollowUp))).scalars().all()
    assert len(followups) == 2


@pytest.mark.asyncio
async def test_the_cap_keeps_named_people_over_shared_inboxes(
    db_session, sample_company
):
    """``info@`` is a real mailbox but a queue somebody triages, and cold mail is
    the first thing that queue deletes. When only two of four can be touched,
    the named people are the two."""
    await _company_with_contact_mix(db_session, sample_company)
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, max_contacts_per_company=2
    )

    followups = (await db_session.execute(select(FollowUp))).scalars().all()
    chosen = set()
    for f in followups:
        c = (await db_session.execute(
            select(Contact).where(Contact.id == f.contact_id)
        )).scalar_one()
        chosen.add(c.email)
    assert chosen == {"bob@acme.test", "jane@acme.test"}


@pytest.mark.asyncio
async def test_the_primary_contact_outranks_a_named_peer(
    db_session, sample_company
):
    made = await _company_with_contact_mix(db_session, sample_company)
    # The operator marked the shared inbox as primary — that is a human
    # judgement about who to talk to, and it outranks the address shape.
    made["info@acme.test"].is_primary = True
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, max_contacts_per_company=1
    )

    followup = (await db_session.execute(select(FollowUp))).scalars().one()
    contact = (await db_session.execute(
        select(Contact).where(Contact.id == followup.contact_id)
    )).scalar_one()
    assert contact.email == "info@acme.test"


@pytest.mark.asyncio
async def test_the_cap_applies_to_the_auto_enroll_path(
    db_session, sample_company, monkeypatch
):
    """The dangerous path: these rows dispatch a send immediately."""
    from app.models.sequence import OutreachSequence, SequenceStep

    sample_company.industry_segment = "CNC Machining"
    await _company_with_contact_mix(db_session, sample_company)
    seq = OutreachSequence(
        id=uuid.uuid4(), name="CNC", industry="cnc_machining", is_active=True
    )
    db_session.add(seq)
    await db_session.flush()
    db_session.add(SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
        subject_template="hi", body_template="hi",
    ))
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    dispatched = []
    from app.tasks import outreach as outreach_tasks
    monkeypatch.setattr(
        outreach_tasks.send_sequence_step, "delay", lambda eid: dispatched.append(eid)
    )

    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=True,
        max_contacts_per_company=2,
    )

    assert result["enrolled"] == 2
    assert len(dispatched) == 2, "one immediate send per enrollment, capped"
    assert (await db_session.execute(
        select(func.count(OutreachEnrollment.id))
    )).scalar() == 2


@pytest.mark.asyncio
async def test_a_cap_below_one_means_uncapped(
    db_session, sample_company, sample_contact
):
    """An escape hatch for an operator who wants the old behaviour, rather than
    a value that silently surfaces nobody."""
    await _company_with_contact_mix(db_session, sample_company)
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, max_contacts_per_company=0
    )

    # sample_contact plus the four above.
    assert result["flagged"] == 5


@pytest.mark.asyncio
async def test_the_cap_defaults_to_the_configured_value(
    db_session, sample_company, sample_contact
):
    """Callers that pass nothing get the setting, not "unlimited"."""
    from app.config import get_settings

    await _company_with_contact_mix(db_session, sample_company)
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5
    )

    assert result["flagged"] == get_settings().max_contacts_per_company_per_signal
