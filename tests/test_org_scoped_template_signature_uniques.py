"""One default template and one default signature — per tenant, not per deployment.

``email_templates`` (migration 002) and ``email_signatures`` (migration 019)
were written before this product had tenants, and each carried two constraints
with no org in them::

    UNIQUE (name)
    UNIQUE (is_default) WHERE is_default

Migration 078 added ``org_id`` to both tables and widened neither. The partial
index therefore said "**one** row in this table may be the default", across
every organization on the deployment — and 002 and 019 each seed that row into
the first-party org.

So the second organization to exist could not have a default template or a
default signature, and the failure was mislabelled at every step:

* ``create_email_signature`` auto-defaults a tenant's *first* signature. Its
  "does a default already exist?" read is org-scoped and correctly answers no.
* The insert then collides with a row in a tenant that read cannot see.
* Both routers catch it as ``except Exception`` and answer 409 **"a
  signature/template with that name already exists"** — the wrong cause, and no
  amount of renaming clears it.
* A tenant who works around the 409 by leaving the flag off gets the quiet
  version instead: ``email_dispatch.send_outreach_email`` appends the
  ``is_default`` signature to outbound mail, finds none for that org, and sends
  unsigned mail with nothing logged.

Why no test caught it, and what these tests do differently
----------------------------------------------------------
Every existing test runs as one tenant. A platform-wide unique constraint and a
per-tenant one are **indistinguishable from inside a single org** — they accept
and reject exactly the same writes. Every test below therefore writes as two
orgs, which is the only vantage point the difference is visible from.

The single-org rules still have to hold, so they are asserted too: widening a
constraint is only correct if it stayed narrow where it was right to be.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_signature import EmailSignature
from app.models.email_template import EmailTemplate
from app.models.tenant import Organization
from app.org_context import APPREND_ORG_ID, org_scope

# The first-party org (seeded by the fixture, as migration 078 seeds it) and a
# second tenant — the one this whole file exists to represent.
ORG_A = APPREND_ORG_ID
ORG_B = uuid.UUID("b0b00000-0000-4000-8000-000000000002")


@pytest.fixture
async def two_orgs(db_session: AsyncSession):
    """Add a second organization alongside the seeded first-party one."""
    db_session.add(
        Organization(id=ORG_B, name="Beta Corp", slug="beta-corp", is_active=True)
    )
    await db_session.commit()
    return ORG_A, ORG_B


def _signature(org_id, name="Default", *, is_default=True) -> EmailSignature:
    return EmailSignature(
        org_id=org_id, name=name, content_html="<p>—<br>Sam</p>", is_default=is_default
    )


def _template(org_id, name="Cold open", *, is_default=True) -> EmailTemplate:
    return EmailTemplate(
        org_id=org_id, name=name, subject="Quick question",
        body="<p>Hi {{first_name}}</p>", is_default=is_default,
    )


# ---------------------------------------------------------------------------
# The default flag — the half that locked a tenant out entirely
# ---------------------------------------------------------------------------


async def test_two_orgs_can_each_have_a_default_signature(
    db_session: AsyncSession, two_orgs
):
    """The bug, stated directly: this insert used to violate a unique index.

    Not because Beta Corp already had a default — they have nothing — but
    because Apprend does, in a row Beta Corp is not permitted to read.
    """
    db_session.add(_signature(ORG_A, "Apprend sign-off"))
    await db_session.commit()

    db_session.add(_signature(ORG_B, "Beta sign-off"))
    await db_session.commit()

    defaults = (
        await db_session.execute(
            select(EmailSignature.org_id).where(EmailSignature.is_default.is_(True))
        )
    ).scalars().all()
    assert sorted(map(str, defaults)) == sorted([str(ORG_A), str(ORG_B)])


async def test_two_orgs_can_each_have_a_default_template(
    db_session: AsyncSession, two_orgs
):
    db_session.add(_template(ORG_A, "Apprend cold open"))
    await db_session.commit()

    db_session.add(_template(ORG_B, "Beta cold open"))
    await db_session.commit()

    defaults = (
        await db_session.execute(
            select(EmailTemplate.org_id).where(EmailTemplate.is_default.is_(True))
        )
    ).scalars().all()
    assert sorted(map(str, defaults)) == sorted([str(ORG_A), str(ORG_B)])


@pytest.mark.parametrize("factory", [_signature, _template])
async def test_one_org_still_cannot_hold_two_defaults(
    db_session: AsyncSession, two_orgs, factory
):
    """The rule that was right, kept.

    Widening ``(is_default)`` to ``(org_id, is_default)`` is only correct if a
    single tenant is still held to one default — the routers' ``_clear_default``
    dance assumes it, and ``send_outreach_email`` resolves the default with a
    ``scalar_one_or_none()`` that raises the moment two exist.
    """
    db_session.add(factory(ORG_A, "First"))
    await db_session.commit()

    db_session.add(factory(ORG_A, "Second"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.parametrize("factory", [_signature, _template])
async def test_a_tenant_may_hold_many_non_defaults(
    db_session: AsyncSession, two_orgs, factory
):
    """The index is partial, so only the ``true`` rows are constrained.

    Worth pinning: a unique index on ``(org_id, is_default)`` *without* the
    ``WHERE is_default`` clause would let a tenant own exactly one non-default
    template as well, which is not a rule anybody wants.
    """
    for i in range(3):
        db_session.add(factory(ORG_A, f"Draft {i}", is_default=False))
    await db_session.commit()


# ---------------------------------------------------------------------------
# The name — the half that leaked across the boundary
# ---------------------------------------------------------------------------


async def test_two_orgs_can_use_the_same_signature_name(
    db_session: AsyncSession, two_orgs
):
    """"Default" is the name everyone picks, which is the point.

    The 409 this used to raise also answered a question the caller had no right
    to ask: it confirmed that some other tenant held that name.
    """
    db_session.add(_signature(ORG_A, "Default", is_default=False))
    db_session.add(_signature(ORG_B, "Default", is_default=False))
    await db_session.commit()


async def test_two_orgs_can_use_the_same_template_name(
    db_session: AsyncSession, two_orgs
):
    db_session.add(_template(ORG_A, "Follow-up 1", is_default=False))
    db_session.add(_template(ORG_B, "Follow-up 1", is_default=False))
    await db_session.commit()


@pytest.mark.parametrize("factory", [_signature, _template])
async def test_one_org_still_cannot_reuse_a_name(
    db_session: AsyncSession, two_orgs, factory
):
    db_session.add(factory(ORG_A, "Follow-up 1", is_default=False))
    await db_session.commit()

    db_session.add(factory(ORG_A, "Follow-up 1", is_default=False))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# What the send path does with it
# ---------------------------------------------------------------------------


async def test_the_send_path_resolves_this_orgs_default_signature(
    db_session: AsyncSession, two_orgs
):
    """The consequence that produced no error at all.

    ``send_outreach_email`` appends ``is_default`` signature to every outbound
    message via exactly this query. Under the platform-wide index a second
    tenant could not own a default, so this returned nothing for them and their
    mail went out unsigned — no exception, no log line, no test failure.

    Asserted through ``org_scope`` rather than an explicit ``org_id`` filter
    because that is how the send path runs: the query carries no org predicate
    of its own and relies entirely on the global read filter.
    """
    db_session.add(_signature(ORG_A, "Apprend sign-off"))
    db_session.add(_signature(ORG_B, "Beta sign-off"))
    await db_session.commit()

    for org_id, expected in ((ORG_A, "Apprend sign-off"), (ORG_B, "Beta sign-off")):
        async with org_scope(org_id):
            found = (
                await db_session.execute(
                    select(EmailSignature).where(EmailSignature.is_default.is_(True))
                )
            ).scalar_one_or_none()
        assert found is not None, f"org {org_id} had no default signature"
        assert found.name == expected


async def test_a_tenant_without_a_default_does_not_borrow_another_ones(
    db_session: AsyncSession, two_orgs
):
    """The failure mode the fix must not trade the old one for.

    Scoping the *constraint* is only half of it — if the send path's lookup were
    unscoped it would now find several defaults and sign Beta Corp's mail with
    Apprend's name and postal address. ``scalar_one_or_none`` would raise on the
    second one, so the assertion here is that it neither raises nor returns
    someone else's row.
    """
    db_session.add(_signature(ORG_A, "Apprend sign-off"))
    await db_session.commit()

    async with org_scope(ORG_B):
        found = (
            await db_session.execute(
                select(EmailSignature).where(EmailSignature.is_default.is_(True))
            )
        ).scalar_one_or_none()
    assert found is None
