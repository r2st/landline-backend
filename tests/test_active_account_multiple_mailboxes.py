"""A second connected mailbox must not break the fallback sender lookup.

``get_active_account`` asserted, via ``scalar_one_or_none``, that at most one
Gmail account is active-and-connected at a time. Nothing promises that:
``gmail_accounts`` is unique on ``email`` and ``google_sub`` and on nothing else,
and the function directly below it — ``get_sending_accounts`` — returns a *list*
of connected accounts, because multi-mailbox sending is the design. Rotation,
per-account warm-up ramps and per-account sender health all exist precisely to
spread volume across several.

So the second mailbox a tenant connects turns the lookup into
``MultipleResultsFound``. It is the third recorded instance of this exact bug
class in this codebase (duplicate contacts broke every unsubscribe link;
duplicate group names broke all three auto-group endpoints), which is why
CLAUDE.md asks for a constraint check before writing one.

Where it bites depends on the caller:

- ``welcome_email`` and ``notifications`` call it **directly**, with no rotation
  in front, so they fail the moment a second mailbox exists.
- ``resolve_sender_info`` and the outreach send path reach it only as a fallback
  when rotation finds nothing — which is what happens when the connected
  accounts have ``sending_enabled=False``. A tenant who connects two mailboxes
  and has not enabled sending on either gets a crash instead of the
  "no sender configured" answer the fallback exists to give.
"""

import uuid

import pytest

from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount
from app.services.email_dispatch import get_active_account


def make_gmail(email: str, **kwargs) -> GmailAccount:
    defaults = dict(
        id=uuid.uuid4(),
        email=email,
        google_sub=f"sub-{uuid.uuid4()}",
        is_active=True,
        status="connected",
        sending_enabled=False,
        refresh_token_encrypted="encrypted-refresh-token",
    )
    defaults.update(kwargs)
    return GmailAccount(**defaults)


def make_smtp(email: str, **kwargs) -> SmtpAccount:
    defaults = dict(
        id=uuid.uuid4(),
        email=email,
        host="smtp.example.com",
        port=587,
        username=email,
        password_encrypted="x",
        is_active=True,
        status="connected",
        sending_enabled=False,
    )
    defaults.update(kwargs)
    return SmtpAccount(**defaults)


async def test_two_connected_gmail_accounts_do_not_raise(db_session):
    """The regression. Two mailboxes is a supported setup, not a broken one."""
    db_session.add_all(
        [make_gmail("first@apprend.tech"), make_gmail("second@apprend.tech")]
    )
    await db_session.flush()

    account = await get_active_account(db_session)

    assert account is not None
    assert account.email in {"first@apprend.tech", "second@apprend.tech"}


async def test_the_choice_is_stable_across_calls(db_session):
    """Whichever it picks, it must pick the same one every time.

    An arbitrary winner is how the duplicate-group bug scattered work between
    two rows instead of accumulating in one. Here it would mean outreach
    alternating sender identity between mailboxes at random, which is precisely
    what the rotation and warm-up machinery exists to control deliberately.
    """
    db_session.add_all(
        [make_gmail(f"mailbox{i}@apprend.tech") for i in range(4)]
    )
    await db_session.flush()

    picks = {(await get_active_account(db_session)).email for _ in range(5)}

    assert len(picks) == 1, f"the fallback sender is not deterministic: {picks}"


async def test_gmail_still_wins_over_smtp(db_session):
    """The documented preference order survives the fix."""
    db_session.add_all(
        [
            make_smtp("smtp-a@apprend.tech"),
            make_smtp("smtp-b@apprend.tech"),
            make_gmail("gmail-a@apprend.tech"),
            make_gmail("gmail-b@apprend.tech"),
        ]
    )
    await db_session.flush()

    account = await get_active_account(db_session)

    assert isinstance(account, GmailAccount)


async def test_two_connected_smtp_accounts_do_not_raise(db_session):
    """The SMTP branch is the same query with the same missing constraint."""
    db_session.add_all(
        [make_smtp("smtp-one@apprend.tech"), make_smtp("smtp-two@apprend.tech")]
    )
    await db_session.flush()

    account = await get_active_account(db_session)

    assert isinstance(account, SmtpAccount)


async def test_no_connected_account_is_still_none(db_session):
    """The fallback's real job: report "nothing configured", not crash."""
    db_session.add_all(
        [
            make_gmail("disconnected@apprend.tech", status="disconnected"),
            make_smtp("inactive@apprend.tech", is_active=False),
        ]
    )
    await db_session.flush()

    assert await get_active_account(db_session) is None


async def test_a_single_account_is_unchanged(db_session):
    """The common case must behave exactly as before."""
    db_session.add(make_gmail("only@apprend.tech"))
    await db_session.flush()

    account = await get_active_account(db_session)

    assert account is not None and account.email == "only@apprend.tech"


@pytest.mark.parametrize(
    "caller",
    ["welcome_email", "notifications"],
    ids=["welcome_email", "notifications"],
)
async def test_the_direct_callers_reach_it_without_rotation(caller):
    """Why this is not merely a fallback-path edge case.

    These two resolve their sender by calling ``get_active_account`` outright,
    so for them a second connected mailbox is not an unusual configuration that
    degrades a fallback — it is an unconditional failure of the feature.
    """
    import inspect

    module = __import__(f"app.services.{caller}", fromlist=["*"])
    source = inspect.getsource(module)

    assert "get_active_account" in source
    assert "get_next_rotation_account" not in source, (
        f"{caller} grew a rotation step; this test's premise needs rechecking"
    )
