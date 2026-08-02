"""The campaign dashboard: ranking that survives a small sample, and alerts.

The campaign table already reports every number here. What this endpoint adds is
a *reading* of them, and a reading can be wrong in ways a table cannot:

* **Naming a winner that was lucky.** Ranking on the observed reply rate puts
  1-reply-in-12 above 16-in-400. The leaderboard ranks on the defensible rate.
* **Killing a campaign that has barely sent.** 0 replies in 20 sends looks like a
  failure and could still be a 16% campaign. A laggard is named only when its
  whole interval sits below the portfolio.
* **Averaging rates across campaigns.** A 10-send campaign then counts as much
  as a 990-send one, and the portfolio reports a rate no mail achieved.
* **Missing the campaign that is dying quietly.** A stalled campaign sends
  nothing, so every rate it has stays exactly where it was.

Each of those is pinned below.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services import campaign_dashboard

DASHBOARD = "/api/v1/dialtone/campaigns/dashboard"


async def _campaign(db, name, is_active=True):
    seq = OutreachSequence(
        id=uuid.uuid4(), name=name, is_active=is_active, status="active"
    )
    db.add(seq)
    await db.flush()
    return seq


async def _contact(db, company, email):
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, email=email,
        first_name="Dana", source="test",
    )
    db.add(contact)
    await db.flush()
    return contact


async def _enroll(db, sequence, contact, status="active", next_send_at=None):
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=sequence.id,
        current_step=1, status=status, next_send_at=next_send_at,
    )
    db.add(enr)
    await db.flush()
    return enr


async def _events(db, enrollment, event_type, n=1, when=None, metadata=None):
    for _ in range(n):
        db.add(OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
            event_type=event_type,
            occurred_at=when or datetime.now(timezone.utc),
            event_metadata=metadata or {},
        ))
    await db.flush()


async def _traffic(db, company, sequence, *, sent, replied=0, bounced=0,
                   when=None, metadata=None, label="t"):
    """One enrollment carrying a campaign's whole window of events.

    Events hang off the enrollment and the dashboard aggregates by sequence, so
    one enrollment per campaign is enough and keeps the fixtures readable.
    """
    contact = await _contact(db, company, f"{label}-{uuid.uuid4().hex[:8]}@acme.com")
    enr = await _enroll(db, sequence, contact)
    await _events(db, enr, "sent", n=sent, when=when, metadata=metadata)
    if replied:
        await _events(db, enr, "replied", n=replied, when=when)
    if bounced:
        await _events(db, enr, "bounced", n=bounced, when=when)
    return enr


def _kinds(payload):
    return [a["kind"] for a in payload["alerts"]]


def _alert(payload, kind, campaign=None):
    for a in payload["alerts"]:
        if a["kind"] == kind and (campaign is None or a["campaign"] == campaign):
            return a
    return None


def _named(rows):
    return [r["name"] for r in rows]


# --- The empty case ----------------------------------------------------------

@pytest.mark.asyncio
async def test_an_empty_system_reports_zeros_not_an_error(client):
    """No campaigns is a state, not a failure — and the window must still be
    reported, or the caller cannot tell "nothing happened" from "nothing was
    asked for"."""
    r = await client.get(DASHBOARD, params={"days": 30})
    assert r.status_code == 200
    body = r.json()

    assert body["campaign_count"] == 0
    assert body["portfolio"]["sent"] == 0
    assert body["portfolio"]["reply_rate"] == 0.0
    assert body["portfolio"]["direction"] == "insufficient_data"
    assert body["leaderboard"] == body["laggards"] == body["needs_volume"] == []
    assert body["alerts"] == []
    assert body["window"]["days"] == 30


@pytest.mark.asyncio
async def test_the_comparison_window_abuts_the_reported_one(client):
    """`previous_end` must equal `since` exactly. A gap between the windows
    silently drops a day of history out of every comparison on the screen."""
    r = await client.get(DASHBOARD, params={"days": 14})
    window = r.json()["window"]
    assert window["previous_end"] == window["since"]

    since = datetime.fromisoformat(window["since"])
    previous_start = datetime.fromisoformat(window["previous_start"])
    assert (since - previous_start).days == 14


# --- Ranking -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_lucky_small_campaign_does_not_top_the_leaderboard(
    client, db_session, sample_company
):
    """1 reply in 12 sends observes 8.3% and beats 16-in-400's 4.0%. It has not
    earned the top of the list; it has been lucky once."""
    lucky = await _campaign(db_session, "Lucky")
    mature = await _campaign(db_session, "Mature")
    await _traffic(db_session, sample_company, lucky, sent=12, replied=1)
    await _traffic(db_session, sample_company, mature, sent=400, replied=16)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()

    assert _named(body["leaderboard"]) == ["Mature"]
    # Not dropped — a campaign on no list at all reads as switched off.
    assert _named(body["needs_volume"]) == ["Lucky"]


@pytest.mark.asyncio
async def test_a_thin_campaign_is_not_called_a_laggard(
    client, db_session, sample_company
):
    """0 replies in 20 sends looks like a failure and has an upper bound near
    16%. Telling someone to kill that campaign is the most expensive thing this
    screen could get wrong."""
    good = await _campaign(db_session, "Good")
    thin = await _campaign(db_session, "Thin")
    await _traffic(db_session, sample_company, good, sent=400, replied=16)
    await _traffic(db_session, sample_company, thin, sent=20, replied=0)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()

    assert "Thin" not in _named(body["laggards"])
    assert "Thin" in _named(body["needs_volume"])


@pytest.mark.asyncio
async def test_a_campaign_whose_range_straddles_the_portfolio_is_not_a_laggard(
    client, db_session, sample_company
):
    """The case the *upper* bound exists for, and the one a lower-bound test
    cannot reach.

    1 reply in 40 sends observes 2.5% against a 4.9% portfolio, so it looks
    behind — and its interval runs from 0.4% to 12.9%, straddling the portfolio.
    Ranking the laggard list on the lower bound (which is below the portfolio
    for almost every campaign, including good ones) would name it. It might be
    the best campaign here; nobody knows yet.
    """
    big = await _campaign(db_session, "Established")
    unclear = await _campaign(db_session, "Unclear")
    await _traffic(db_session, sample_company, big, sent=1000, replied=50)
    await _traffic(db_session, sample_company, unclear, sent=40, replied=1)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()

    portfolio = body["portfolio"]["reply_rate"]
    row = next(r for r in body["leaderboard"] if r["name"] == "Unclear")
    assert row["reply_rate_ci_low"] < portfolio < row["reply_rate_ci_high"]
    assert "Unclear" not in _named(body["laggards"])


@pytest.mark.asyncio
async def test_a_campaign_that_is_defensibly_worse_is_named(
    client, db_session, sample_company
):
    """Same zero replies, enough volume for the upper bound to fall under the
    portfolio rate. Now it is worse, not unlucky."""
    good = await _campaign(db_session, "Good")
    dud = await _campaign(db_session, "Dud")
    await _traffic(db_session, sample_company, good, sent=1000, replied=60)
    await _traffic(db_session, sample_company, dud, sent=400, replied=0)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()

    assert _named(body["laggards"]) == ["Dud"]
    row = body["laggards"][0]
    assert row["reply_rate_ci_high"] < body["portfolio"]["reply_rate"]
    assert "below the portfolio" in row["reason"]


@pytest.mark.asyncio
async def test_lists_respect_list_size(client, db_session, sample_company):
    for i in range(4):
        seq = await _campaign(db_session, f"C{i}")
        await _traffic(db_session, sample_company, seq, sent=200, replied=8)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30, "list_size": 2})).json()
    assert len(body["leaderboard"]) == 2
    assert body["campaign_count"] == 4


# --- The portfolio -----------------------------------------------------------

@pytest.mark.asyncio
async def test_the_portfolio_pools_sends_it_does_not_average_rates(
    client, db_session, sample_company
):
    """10 sends / 1 reply and 990 sends / 9 replies is a 1.0% portfolio. The
    mean of the two campaign rates is 5.5%, which no mail achieved."""
    small = await _campaign(db_session, "Small")
    big = await _campaign(db_session, "Big")
    await _traffic(db_session, sample_company, small, sent=10, replied=1)
    await _traffic(db_session, sample_company, big, sent=990, replied=9)
    await db_session.commit()

    portfolio = (await client.get(DASHBOARD, params={"days": 30})).json()["portfolio"]

    assert portfolio["sent"] == 1000
    assert portfolio["replied"] == 10
    assert portfolio["reply_rate"] == 0.01
    assert portfolio["sample_sufficient"] is True


@pytest.mark.asyncio
async def test_a_campaign_that_stopped_still_counts_in_the_comparison(
    client, db_session, sample_company
):
    """A campaign that sent last period and nothing this one must keep its
    previous volume in the denominator. Drop it and the portfolio 'improves' by
    having a campaign switch off."""
    now = datetime.now(timezone.utc)
    stopped = await _campaign(db_session, "Stopped")
    running = await _campaign(db_session, "Running")
    await _traffic(db_session, sample_company, stopped, sent=200, replied=2,
                   when=now - timedelta(days=45))
    await _traffic(db_session, sample_company, running, sent=200, replied=10)
    await db_session.commit()

    portfolio = (await client.get(DASHBOARD, params={"days": 30})).json()["portfolio"]

    assert portfolio["previous_sent"] == 200
    assert portfolio["previous_reply_rate"] == 0.01
    assert portfolio["reply_rate"] == 0.05
    assert portfolio["direction"] == "improving"


@pytest.mark.asyncio
async def test_two_tiny_periods_are_not_a_direction(
    client, db_session, sample_company
):
    now = datetime.now(timezone.utc)
    seq = await _campaign(db_session, "Trickle")
    await _traffic(db_session, sample_company, seq, sent=4, replied=0,
                   when=now - timedelta(days=45))
    await _traffic(db_session, sample_company, seq, sent=6, replied=1)
    await db_session.commit()

    portfolio = (await client.get(DASHBOARD, params={"days": 30})).json()["portfolio"]
    assert portfolio["direction"] == "insufficient_data"


# --- Alerts ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_campaigns_bounce_rate_breaches_while_the_blend_looks_fine(
    client, db_session, sample_company
):
    """The danger rail auto-pauses on a *blended* rate. One campaign past the
    same line is what drags the blend up, and the healthy campaigns hold it
    below every threshold in the system until it is far too late."""
    healthy = await _campaign(db_session, "Healthy")
    burning = await _campaign(db_session, "Burning")
    await _traffic(db_session, sample_company, healthy, sent=1000, bounced=5)
    await _traffic(db_session, sample_company, burning, sent=100, bounced=12)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()

    alert = _alert(body, "bounce_rate", "Burning")
    assert alert is not None
    assert alert["severity"] == "critical"
    assert alert["value"] == 0.12

    # The blended rate the fleet-wide rail sees is under its own threshold.
    assert body["portfolio"]["bounce_rate"] < alert["threshold"]
    assert _alert(body, "bounce_rate", "Healthy") is None


@pytest.mark.asyncio
async def test_a_bounce_rate_between_the_lines_warns_rather_than_criticals(
    client, db_session, sample_company
):
    seq = await _campaign(db_session, "Drifting")
    await _traffic(db_session, sample_company, seq, sent=100, bounced=4)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()
    alert = _alert(body, "bounce_rate", "Drifting")
    assert alert["severity"] == "warning"


@pytest.mark.asyncio
async def test_a_bounce_rate_on_too_few_sends_is_not_an_alert(
    client, db_session, sample_company
):
    """2 bounces of 10 is 20% and is not evidence of anything. This is the same
    minimum sample the danger monitor applies before it will act."""
    seq = await _campaign(db_session, "Tiny")
    await _traffic(db_session, sample_company, seq, sent=10, bounced=2)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()
    assert _alert(body, "bounce_rate", "Tiny") is None


@pytest.mark.asyncio
async def test_a_campaign_whose_queue_stopped_moving_is_flagged(
    client, db_session, sample_company
):
    """The most common way a campaign dies is quietly: it stops sending, so
    every rate it has stays exactly where it was and nothing on a table moves."""
    now = datetime.now(timezone.utc)
    seq = await _campaign(db_session, "Stuck")
    contact = await _contact(db_session, sample_company, "stuck@acme.com")
    await _enroll(db_session, seq, contact, next_send_at=now - timedelta(days=3))
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()

    alert = _alert(body, "stalled", "Stuck")
    assert alert is not None
    assert alert["value"] == 1.0


@pytest.mark.asyncio
async def test_a_paused_enrollment_with_a_stale_due_date_is_not_stalled(
    client, db_session, sample_company
):
    """Out-of-office holds, send quarantines and operator pauses all rest with a
    due date in the past. Counting those would make the alert permanent."""
    now = datetime.now(timezone.utc)
    seq = await _campaign(db_session, "Paused")
    contact = await _contact(db_session, sample_company, "ooo@acme.com")
    await _enroll(db_session, seq, contact, status="paused",
                  next_send_at=now - timedelta(days=5))
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()
    assert _alert(body, "stalled", "Paused") is None


@pytest.mark.asyncio
async def test_an_enrollment_due_an_hour_ago_is_not_stalled(
    client, db_session, sample_company
):
    """A due date that just passed is a queue about to run, not a dead campaign."""
    now = datetime.now(timezone.utc)
    seq = await _campaign(db_session, "Fine")
    contact = await _contact(db_session, sample_company, "soon@acme.com")
    await _enroll(db_session, seq, contact, next_send_at=now - timedelta(hours=1))
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()
    assert _alert(body, "stalled", "Fine") is None


@pytest.mark.asyncio
async def test_a_stalled_campaign_is_flagged_even_with_no_sends_in_the_window(
    client, db_session, sample_company
):
    """The campaigns the alert list exists for are exactly the ones with nothing
    in the window — they have no sends *because* they are broken. Filtering
    alerts to campaigns that sent would hide every one of them."""
    now = datetime.now(timezone.utc)
    seq = await _campaign(db_session, "Silent")
    contact = await _contact(db_session, sample_company, "silent@acme.com")
    await _enroll(db_session, seq, contact, next_send_at=now - timedelta(days=10))
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()

    assert body["campaign_count"] == 0
    assert _alert(body, "stalled", "Silent") is not None


@pytest.mark.asyncio
async def test_enough_sends_with_no_replies_at_all_is_flagged(
    client, db_session, sample_company
):
    seq = await _campaign(db_session, "Silent copy")
    await _traffic(db_session, sample_company, seq, sent=120, replied=0)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()
    assert _alert(body, "no_replies", "Silent copy") is not None


@pytest.mark.asyncio
async def test_sixty_sends_with_no_replies_is_not_yet_evidence(
    client, db_session, sample_company
):
    """At a normal cold reply rate, 60 sends produce nothing often enough that
    alerting there would mostly cry wolf."""
    seq = await _campaign(db_session, "Early")
    await _traffic(db_session, sample_company, seq, sent=60, replied=0)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()
    assert _alert(body, "no_replies", "Early") is None


@pytest.mark.asyncio
async def test_mail_merge_going_out_under_an_ai_campaign_is_flagged(
    client, db_session, sample_company
):
    """The failure this codebase has hit twice: composition switched on, the
    provider chain down, static copy on the wire, and a reply rate everyone is
    still reading as a verdict on personalization."""
    seq = await _campaign(db_session, "Personalized (allegedly)")
    await _traffic(db_session, sample_company, seq, sent=200, replied=4)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()

    alert = _alert(body, "ai_compose_dark", "Personalized (allegedly)")
    assert alert is not None
    assert alert["value"] == 0.0


@pytest.mark.asyncio
async def test_a_genuinely_ai_composed_campaign_is_not_flagged(
    client, db_session, sample_company
):
    seq = await _campaign(db_session, "Really personalized")
    await _traffic(db_session, sample_company, seq, sent=200, replied=4,
                   metadata={"ai_generated": True, "personalization": {"depth": 4}})
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()

    assert _alert(body, "ai_compose_dark") is None
    assert body["portfolio"]["ai_composed_rate"] == 1.0
    assert body["portfolio"]["avg_personalization_depth"] == 4.0


@pytest.mark.asyncio
async def test_no_ai_alert_when_composition_is_switched_off(
    db_session, sample_company
):
    """With the composer off, static copy is the intended behaviour and saying
    so every day is how an alert list gets ignored."""
    seq = await _campaign(db_session, "Static by design")
    await _traffic(db_session, sample_company, seq, sent=200, replied=4)
    await db_session.commit()

    class _Off:
        ai_compose_enabled = False

    board = await campaign_dashboard.build(db_session, days=30, settings=_Off())
    assert [a for a in board.alerts if a.kind == "ai_compose_dark"] == []


@pytest.mark.asyncio
async def test_a_declining_campaign_is_reported_as_information_not_alarm(
    client, db_session, sample_company
):
    """A reply rate falling is worth knowing and is not a fire. Ranking it with
    a bounce breach is how a critical alert stops meaning anything."""
    now = datetime.now(timezone.utc)
    seq = await _campaign(db_session, "Fading")
    await _traffic(db_session, sample_company, seq, sent=200, replied=20,
                   when=now - timedelta(days=45))
    await _traffic(db_session, sample_company, seq, sent=200, replied=2)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()

    alert = _alert(body, "declining", "Fading")
    assert alert is not None
    assert alert["severity"] == "info"
    assert alert["value"] < 0


@pytest.mark.asyncio
async def test_alerts_arrive_worst_first(client, db_session, sample_company):
    """The list is read from the top and often only from the top."""
    now = datetime.now(timezone.utc)
    burning = await _campaign(db_session, "Burning")
    fading = await _campaign(db_session, "Fading")
    await _traffic(db_session, sample_company, burning, sent=200, bounced=30)
    await _traffic(db_session, sample_company, fading, sent=200, replied=20,
                   when=now - timedelta(days=45))
    await _traffic(db_session, sample_company, fading, sent=200, replied=2)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()
    severities = [a["severity"] for a in body["alerts"]]

    assert severities[0] == "critical"
    assert severities == sorted(severities, key=["critical", "warning", "info"].index)
    assert "bounce_rate" in _kinds(body)
    assert "declining" in _kinds(body)


# --- Agreement with the table it summarizes ----------------------------------

@pytest.mark.asyncio
async def test_the_dashboard_and_the_table_report_the_same_numbers(
    client, db_session, sample_company
):
    """Both surfaces run the same aggregation on purpose. A dashboard naming a
    best performer the table ranks fourth gives a reader no way to tell which
    screen is lying."""
    a = await _campaign(db_session, "A")
    b = await _campaign(db_session, "B")
    await _traffic(db_session, sample_company, a, sent=300, replied=15, bounced=2)
    await _traffic(db_session, sample_company, b, sent=150, replied=3)
    await db_session.commit()

    board = (await client.get(DASHBOARD, params={"days": 30})).json()
    table = (await client.get(
        "/api/v1/dialtone/campaigns/analytics",
        params={"days": 30, "sort_by": "reply_rate_ci_low"},
    )).json()

    by_name = {row["name"]: row for row in table["campaigns"]}
    assert board["portfolio"]["sent"] == table["totals"]["sent"]
    assert board["portfolio"]["reply_rate"] == table["totals"]["reply_rate"]
    for row in board["leaderboard"]:
        assert row["reply_rate_ci_low"] == by_name[row["name"]]["reply_rate_ci_low"]
    assert _named(board["leaderboard"])[0] == table["campaigns"][0]["name"]
