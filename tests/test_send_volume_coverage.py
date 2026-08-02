"""Every path that puts mail on the wire spends the volume it costs.

The companion to ``test_send_gate_coverage``. That one asks whether a send was
*allowed*; this one asks whether it was *counted*.

Two counters govern how much a mailbox may send: the per-account warm-up ramp
(``sender_warmup_plans.daily_limit``, enforced against
``GmailAccount.daily_send_count``) and the org's daily cap (a Redis reservation
seeded from the day's rows). Both were claimed in exactly one place each —
``send_outreach_email`` for the ramp, ``app.tasks.outreach`` for the cap — so
every path holding a transport directly sent from the same mailbox for free.

**The failure is the one warm-up exists to prevent, and it is silent.** A
brand-new mailbox on day 1 of a 5/day ramp sends five sequence steps and stops,
exactly as designed; then the meeting agent proposes, the smart-reply answers, a
human replies from the inbox, and an operator fires two "tests" — and every
warm-up check still reads ``5/5``. Nothing errors. The ramp reports itself
enforced, the graph of ``daily_send_count`` shows a textbook curve, and the
mailbox is putting four times that volume on the wire from a cold IP. What
notices is the receiving side, weeks later, and by then the domain is the thing
that is broken.

**Refusability is a product decision, not a technical one**, so it is recorded
per call site rather than inferred. Autonomous cold volume is refused when the
day is spent. A human hitting Reply, an operator testing a mailbox and a new
colleague's credentials are never refused — and are still counted, because the
volume left the same mailbox and the next cold send has to see it.
"""

import ast
import pathlib


from tests.test_send_gate_coverage import (
    APP_ROOT,
    _enclosing_function,
    _module_name,
    _send_sites,
)

# Calling any of these in the same function counts as spending the volume:
# ``record_account_send`` books it against the mailbox, ``claim_send_volume``
# takes the slot that ``settle_send_volume`` then closes out, and
# ``send_outreach_email`` does all three internally.
CLAIMS = (
    "record_account_send",
    "claim_send_volume",
    "send_outreach_email",
)

# Send sites that spend no volume, reviewed individually. As in the gate test,
# the reason is part of the entry — an entry without one is an unreviewed
# exception, so don't add one.
REVIEWED: dict[str, str] = {
    "app.services.gmail_sender.send_email": (
        "the transport itself — what a counted caller calls. It has no session "
        "to book against and no idea which of the accounting paths reached it."
    ),
    "app.services.email_sender.send_sequence_email": (
        "a template-rendering method on the SES transport class, reached only "
        "through EmailSender.send_email. Not an entry point: nothing in the "
        "application calls it."
    ),
    "app.services.meeting_booking_agent._default_gmail_send": (
        "the agent's injected transport default — the seam its tests replace "
        "with a spy. Every call site goes through _guarded_send, which claims "
        "and settles the slot; counting here as well would double-count every "
        "proposal and leave the test double as the uncounted one."
    ),
    "app.services.smart_reply._send_reply": (
        "the raw Gmail call. The slot is claimed immediately above it in "
        "_process, where it has to be: it is a reservation, so it is taken "
        "last of the six auto-send floors and handed back on the failure path "
        "this function raises into."
    ),
    "app.services.system_settings._notify_admin_killswitch": (
        "goes out over SES, not a connected mailbox, so there is no warm-up "
        "ramp or reputation to spend. It also has no session — it is called "
        "from the kill-switch write path after the transaction is done, and "
        "the mail it sends is the announcement that sending stopped."
    ),
}


def _uncounted_sends(root: pathlib.Path) -> list[str]:
    """Every send site whose function neither claims volume nor is REVIEWED."""
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
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
            body = ast.unparse(func) if func is not None else path.read_text()
            if any(claim in body for claim in CLAIMS):
                continue
            problems.append(f"{key} (line {lineno}): {description}")
    return problems


def test_every_send_site_spends_the_volume_it_costs():
    uncounted = _uncounted_sends(APP_ROOT)
    assert not uncounted, (
        "These functions put mail on the wire without claiming volume for it "
        "(email_dispatch.claim_send_volume / record_account_send, or "
        "send_outreach_email, which does both). A send the warm-up ramp cannot "
        "see is a send the ramp is not ramping. Either claim, or add an entry "
        "to REVIEWED in this file explaining why this send costs a mailbox "
        "nothing:\n  " + "\n  ".join(uncounted)
    )


def test_reviewed_entries_all_still_exist():
    """A REVIEWED entry for a function that is gone is a stale exemption."""
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

_UNCOUNTED = """
from app.services import email_dispatch
from app.services.gmail_sender import GmailSender

async def blast(db, account, to_email):
    blocked = await email_dispatch.check_send_allowed(db, to_email)
    if blocked is not None:
        return blocked
    return GmailSender(account).send_email(to_email=to_email, subject="hi")
"""

_COUNTED = """
from app.services import email_dispatch
from app.services.gmail_sender import GmailSender

async def blast(db, account, to_email):
    slot = await email_dispatch.claim_send_volume(db, account)
    if slot.blocked is not None:
        return slot.blocked
    result = GmailSender(account).send_email(to_email=to_email, subject="hi")
    await email_dispatch.settle_send_volume(db, slot, sent=True)
    await email_dispatch.record_account_send(db, account, sent=True)
    return result
"""


def test_checker_catches_a_send_that_clears_the_gate_but_pays_nothing(tmp_path):
    """The planted send is *compliant* — it just doesn't cost the mailbox anything.

    This is the exact shape the five paths had after the compliance fix landed,
    and the reason this file exists next to that one rather than inside it.
    """
    (tmp_path / "planted.py").write_text(_UNCOUNTED)
    assert _uncounted_sends(tmp_path), "checker missed an uncounted send"


def test_checker_accepts_a_counted_send(tmp_path):
    (tmp_path / "planted.py").write_text(_COUNTED)
    assert _uncounted_sends(tmp_path) == []
