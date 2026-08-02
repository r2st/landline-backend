"""Every path that puts mail on the wire clears the same two floors.

``send_outreach_email`` calls itself "the single choke point every outreach path
funnels through", and inside it are the kill switch and the do-not-contact list.
The docstring was true of the *outreach* paths and false of the system: several
modules hold a ``GmailSender``/``SmtpSender`` — or the raw Gmail client — and
send without going through it. Those sends are indistinguishable from any other
once they land, so they answer to the same floors, and
``email_dispatch.check_send_allowed`` exists to be callable from outside the
choke point.

This is the check that they call it. The failure it guards against is silent in
the way that matters: a send that *should not have happened* leaves no error
anywhere — the mail goes out, the transport returns success, and the only party
who learns about it is the recipient who asked to never hear from us again.

**Resolution is function-local.** The gate has to be visible in the function
that sends, not somewhere up the call graph, because "some caller checks" is
exactly the assumption that was wrong to begin with. A function that genuinely
cannot hold the gate — a transport wrapper, an internal ops notification —
carries a reasoned entry in :data:`REVIEWED` instead.
"""

import ast
import pathlib

import pytest

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"

# Classes whose ``.send_email`` reaches a real mail transport.
TRANSPORTS = ("GmailSender", "SmtpSender", "EmailSender")

# Calling any of these in the same function counts as clearing the floors:
# ``check_send_allowed`` is the gate itself, ``send_outreach_email`` contains
# it, and ``_guarded_send`` is the meeting agent's local wrapper around it.
GATES = ("check_send_allowed", "send_outreach_email", "_guarded_send")

# Send sites that hold no gate, reviewed individually. The reason is part of the
# entry — one without a reason is an unreviewed exception, so don't add one.
REVIEWED: dict[str, str] = {
    "app.services.gmail_sender.send_email": (
        "the transport itself. It is what a gated caller calls, and putting a "
        "DB-backed compliance check inside a synchronous Gmail client would "
        "invert the dependency — the gate needs a session, this has none."
    ),
    "app.services.welcome_email.send_welcome_email": (
        "account-lifecycle mail, not outreach: it carries a newly provisioned "
        "operator their own temporary password, and the recipient is a "
        "``User`` row rather than a contact. The kill switch pauses campaigns, "
        "not the ability to onboard a colleague while campaigns are paused; "
        "and a suppression entry on someone's address — theirs was scraped and "
        "unsubscribed once — must not silently swallow the only copy of their "
        "credentials."
    ),
    "app.services.email_sender.send_sequence_email": (
        "a template-rendering method on the SES transport class, reached only "
        "through EmailSender.send_email. Not an entry point: nothing in the "
        "application calls it."
    ),
    "app.services.meeting_booking_agent._default_gmail_send": (
        "the agent's injected transport default — the seam its tests replace "
        "with a spy. Every call site goes through _guarded_send, which holds "
        "the gate; putting it here too would only mean the tests' fake sender "
        "is the unchecked one."
    ),
    "app.services.smart_reply._send_reply": (
        "the auto-send decision is made ~150 lines earlier in "
        "handle_reply/_process, where check_send_allowed sits alongside the "
        "kill switch, the org opt-in, the confidence floor and the "
        "automated-message check — all of which have to agree before this is "
        "reached, and all of which are logged as one decision with a reason. "
        "Splitting one of the five down here would make the ops trail lie "
        "about why a reply was suppressed."
    ),
    "app.services.system_settings._notify_admin_killswitch": (
        "an internal ops notification to NOTIFICATION_EMAIL telling an operator "
        "the kill switch flipped. Applying the kill switch to the mail that "
        "announces the kill switch is circular, and the recipient is the "
        "operator's own configured address, not a contact — there is no "
        "suppression list for it to be on."
    ),
}


def _send_sites(tree: ast.AST) -> list[tuple[int, str]]:
    """``(lineno, description)`` for every raw-transport send in *tree*.

    Three shapes, all of them real in this codebase:

    * ``GmailSender(account).send_email(...)`` — constructed and called inline.
    * ``sender.send_email`` where ``sender`` is a local — including the
      ``asyncio.to_thread(sender.send_email, ...)`` form, where the send is an
      *argument* rather than a call, so matching only ``ast.Call`` misses it.
    * ``service.users().messages().send(...)`` — the raw Gmail API, which
      bypasses the sender classes entirely.
    """
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "send_email":
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                if value.func.id in TRANSPORTS:
                    sites.append((node.lineno, f"{value.func.id}(...).send_email"))
            elif isinstance(value, ast.Name):
                sites.append((node.lineno, f"{value.id}.send_email"))
            elif isinstance(value, ast.Attribute) and value.attr == "self":
                sites.append((node.lineno, "self.send_email"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "send" and "messages()" in ast.unparse(node.func):
                sites.append((node.lineno, "gmail messages().send"))
    return sites


def _enclosing_function(tree: ast.AST, lineno: int) -> ast.AST | None:
    """The innermost def containing *lineno* (None at module level)."""
    best = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.lineno <= lineno <= (node.end_lineno or node.lineno):
            if best is None or node.lineno > best.lineno:
                best = node
    return best


def _module_name(path: pathlib.Path, root: pathlib.Path = APP_ROOT) -> str:
    """Dotted name for *path*, relative to *root*'s parent (so ``app.`` leads)."""
    rel = path.relative_to(root.parent).with_suffix("")
    return ".".join(rel.parts)


def _ungated_sends(root: pathlib.Path) -> list[str]:
    """Every send site whose function neither gates nor is REVIEWED."""
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        sites = _send_sites(tree)
        if not sites:
            continue
        module = _module_name(path, root)
        for lineno, description in sites:
            func = _enclosing_function(tree, lineno)
            name = func.name if func is not None else "<module>"
            key = f"{module}.{name}"
            if key in REVIEWED:
                continue
            body = ast.unparse(func) if func is not None else source
            if any(gate in body for gate in GATES):
                continue
            problems.append(f"{key} (line {lineno}): {description}")
    return problems


def test_every_send_site_clears_the_compliance_floors():
    ungated = _ungated_sends(APP_ROOT)
    assert not ungated, (
        "These functions put mail on the wire without calling "
        "email_dispatch.check_send_allowed (or send_outreach_email, which "
        "contains it). Either call the gate, or add an entry to REVIEWED in "
        "this file explaining why this send does not answer to the kill switch "
        "and the do-not-contact list:\n  " + "\n  ".join(ungated)
    )


def test_reviewed_entries_all_still_exist():
    """A REVIEWED entry for a function that is gone is a stale exemption.

    The list is the part of this test that can rot: a send site gets a waiver,
    the function is later renamed or deleted, and the waiver stays behind ready
    to silently cover the next function that takes the name.
    """
    live = set()
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        module = _module_name(path)
        for lineno, _ in _send_sites(tree):
            func = _enclosing_function(tree, lineno)
            live.add(f"{module}.{func.name if func else '<module>'}")
    stale = sorted(set(REVIEWED) - live)
    assert not stale, f"REVIEWED entries with no matching send site: {stale}"


def test_reviewed_entries_carry_a_reason():
    thin = sorted(k for k, v in REVIEWED.items() if len(v.strip()) < 40)
    assert not thin, f"REVIEWED entries without a real reason: {thin}"


# ─── self-tests: the checker has to fail on a planted violation ──────────────
#
# A checker that silently matches nothing passes forever. These plant each of
# the three send shapes in a temp tree and assert it is caught, and plant a
# gated one and assert it is not.

_UNGATED = """
from app.services.gmail_sender import GmailSender

async def blast(db, account, to_email):
    return GmailSender(account).send_email(to_email=to_email, subject="hi")
"""

_UNGATED_TO_THREAD = """
import asyncio

async def blast(db, account, to_email):
    sender = GmailSender(account)
    return await asyncio.to_thread(sender.send_email, to_email=to_email)
"""

_UNGATED_RAW_GMAIL = """
async def blast(self, gmail_service, body):
    return gmail_service.users().messages().send(userId="me", body=body).execute()
"""

_GATED = """
from app.services import email_dispatch
from app.services.gmail_sender import GmailSender

async def blast(db, account, to_email):
    blocked = await email_dispatch.check_send_allowed(db, to_email)
    if blocked is not None:
        return blocked
    return GmailSender(account).send_email(to_email=to_email, subject="hi")
"""


@pytest.mark.parametrize(
    "source", [_UNGATED, _UNGATED_TO_THREAD, _UNGATED_RAW_GMAIL],
    ids=["inline", "to_thread", "raw_gmail_api"],
)
def test_checker_catches_a_planted_ungated_send(tmp_path, source):
    (tmp_path / "planted.py").write_text(source)
    assert _ungated_sends(tmp_path), "checker missed an ungated send"


def test_checker_accepts_a_gated_send(tmp_path):
    (tmp_path / "planted.py").write_text(_GATED)
    assert _ungated_sends(tmp_path) == []
