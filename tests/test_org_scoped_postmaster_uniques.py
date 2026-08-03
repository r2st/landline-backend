"""Postmaster rows belong to a tenant, and so do the constraints on them.

Migration 093 created both Postmaster tables with platform-wide uniqueness —
``postmaster_accounts (email)``, ``postmaster_accounts (google_sub)`` and
``postmaster_metrics (domain, metric_date)`` — despite landing after migration
078 gave every table an ``org_id``.

Two consequences, and the first is the expensive one:

* A tenant's daily pull died on another tenant's row. ``_upsert_metric`` reads
  through the org filter, so org B correctly fails to find org A's row for the
  same domain and date, inserts, and collides with a constraint it can't see.
  The transaction rolls back — so org B loses *every* domain in that pull, not
  just the shared one — and Postmaster is the only window this product has into
  Gmail's spam signal. What they see is an empty reputation dashboard, which
  reads as "no problems".
* A Google identity could be connected by exactly one tenant on the deployment.
  The OAuth callback's ``google_sub`` lookup had to be global to find its own
  row, and a global lookup finds other tenants' rows: org B reconnecting an
  account org A already held overwrote org A's OAuth tokens with org B's and
  reassigned ``user_id`` across the boundary, while ``org_id`` stayed put.

Every test here writes as two orgs, because that is the only vantage point a
platform-wide constraint and a per-tenant one differ from. The single-org rules
still have to hold — widening is only correct if it stayed narrow where it was
right to be — so those are asserted too.
"""

import uuid
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.postmaster import PostmasterAccount, PostmasterMetric
from app.models.tenant import Organization
from app.org_context import APPREND_ORG_ID, org_scope
from app.services import postmaster_service

ORG_A = APPREND_ORG_ID
ORG_B = uuid.UUID("b0b00000-0000-4000-8000-000000000002")

SHARED_DOMAIN = "shared-sender.test"
DAY = date(2026, 7, 15)


@pytest.fixture
async def two_orgs(db_session: AsyncSession):
    db_session.add(
        Organization(id=ORG_B, name="Beta Corp", slug="beta-corp", is_active=True)
    )
    await db_session.commit()
    return ORG_A, ORG_B


def _account(org_id, email="pm@shared-sender.test", sub="sub-shared") -> PostmasterAccount:
    return PostmasterAccount(
        org_id=org_id,
        email=email,
        google_sub=sub,
        refresh_token_encrypted="unused-in-test",
        status="connected",
    )


def _metric(org_id, domain=SHARED_DOMAIN, day=DAY, **kw) -> PostmasterMetric:
    return PostmasterMetric(
        org_id=org_id, domain=domain, metric_date=day, domain_reputation="HIGH", **kw
    )


# ---------------------------------------------------------------------------
# The metric rows — the half that killed a tenant's whole daily pull
# ---------------------------------------------------------------------------


async def test_two_orgs_can_hold_the_same_domain_and_date(
    db_session: AsyncSession, two_orgs
):
    """The bug, stated directly: this insert used to violate a unique index.

    Not because Beta Corp already had a row for that day — they have nothing —
    but because Apprend does, in a row Beta Corp is not permitted to read.
    """
    db_session.add(_metric(ORG_A, spam_rate=0.001))
    await db_session.commit()

    db_session.add(_metric(ORG_B, spam_rate=0.04))
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(PostmasterMetric.org_id, PostmasterMetric.spam_rate)
            .where(PostmasterMetric.domain == SHARED_DOMAIN)
            .execution_options(skip_org_filter=True)
        )
    ).all()
    assert sorted((str(o), s) for o, s in rows) == sorted(
        [(str(ORG_A), 0.001), (str(ORG_B), 0.04)]
    )


async def test_one_org_still_cannot_duplicate_a_domain_and_date(
    db_session: AsyncSession, two_orgs
):
    """The rule that was right, kept.

    ``_upsert_metric`` reads-then-inserts, so a second row for one org would be
    a silent split of one day's history across two rows.
    """
    db_session.add(_metric(ORG_A))
    await db_session.commit()

    db_session.add(_metric(ORG_A))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_a_tenants_pull_survives_another_tenants_row(
    db_session: AsyncSession, two_orgs, monkeypatch
):
    """End to end: the daily pull, with the other tenant's row already there.

    This is the failure operators actually met — not an IntegrityError in a log,
    but a reputation dashboard that stayed empty for one tenant while the other
    filled in normally.
    """
    db_session.add(_metric(ORG_A, spam_rate=0.001))
    await db_session.commit()

    async def fake_fresh(db, account):
        return "access-token"

    class FakeClient:
        def __init__(self, access_token):
            pass

        async def list_domains(self):
            return [SHARED_DOMAIN]

        async def get_traffic_stats(self, domain):
            return [
                {
                    "name": f"domains/{domain}/trafficStats/20260715",
                    "domainReputation": "BAD",
                    "userReportedSpamRatio": 0.04,
                    "ipReputations": [],
                    "deliveryErrors": [],
                }
            ]

    monkeypatch.setattr(postmaster_service, "_fresh_access_token", fake_fresh)
    monkeypatch.setattr(postmaster_service, "PostmasterClient", FakeClient)

    async with org_scope(ORG_B):
        account = _account(ORG_B)
        db_session.add(account)
        await db_session.flush()
        result = await postmaster_service.pull_for_account(db_session, account)
        await db_session.commit()

    assert result["status"] == "ok"
    assert result["rows"] == 1

    # Org B sees its own reputation — a BAD one, which is the whole point of
    # having the signal at all — and org A's row is untouched.
    async with org_scope(ORG_B):
        b_row = (
            await db_session.execute(
                select(PostmasterMetric).where(PostmasterMetric.domain == SHARED_DOMAIN)
            )
        ).scalars().one()
        assert b_row.domain_reputation == "BAD"
        assert b_row.spam_rate == 0.04

    async with org_scope(ORG_A):
        a_row = (
            await db_session.execute(
                select(PostmasterMetric).where(PostmasterMetric.domain == SHARED_DOMAIN)
            )
        ).scalars().one()
        assert a_row.spam_rate == 0.001
        assert a_row.domain_reputation == "HIGH"


# ---------------------------------------------------------------------------
# The accounts — the half that wrote across the tenant boundary
# ---------------------------------------------------------------------------


async def test_two_orgs_can_connect_the_same_google_account(
    db_session: AsyncSession, two_orgs
):
    """An agency running Dialtone for two tenants has one Google login."""
    db_session.add(_account(ORG_A))
    await db_session.commit()

    db_session.add(_account(ORG_B))
    await db_session.commit()

    orgs = (
        await db_session.execute(
            select(PostmasterAccount.org_id)
            .where(PostmasterAccount.google_sub == "sub-shared")
            .execution_options(skip_org_filter=True)
        )
    ).scalars().all()
    assert sorted(map(str, orgs)) == sorted([str(ORG_A), str(ORG_B)])


async def test_one_org_still_cannot_connect_the_same_account_twice(
    db_session: AsyncSession, two_orgs
):
    """Reconnecting must update the tenant's row, never add a second one."""
    db_session.add(_account(ORG_A))
    await db_session.commit()

    db_session.add(_account(ORG_A, email="other@shared-sender.test"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_one_org_still_cannot_reuse_an_email(
    db_session: AsyncSession, two_orgs
):
    db_session.add(_account(ORG_A))
    await db_session.commit()

    db_session.add(_account(ORG_A, sub="sub-different"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_the_callback_lookup_does_not_reach_another_tenants_connection(
    db_session: AsyncSession, two_orgs
):
    """The predicate the OAuth callback now carries.

    Global on ``google_sub`` alone, this returned org A's row to org B's
    callback, which then overwrote org A's tokens and user_id in place.
    """
    a = _account(ORG_A)
    db_session.add(a)
    await db_session.commit()

    found = (
        await db_session.execute(
            select(PostmasterAccount)
            .where(
                PostmasterAccount.google_sub == "sub-shared",
                PostmasterAccount.org_id == ORG_B,
            )
            .execution_options(skip_org_filter=True)
        )
    ).scalar_one_or_none()
    assert found is None

    still_a = (
        await db_session.execute(
            select(PostmasterAccount)
            .where(PostmasterAccount.id == a.id)
            .execution_options(skip_org_filter=True)
        )
    ).scalar_one()
    assert still_a.org_id == ORG_A
