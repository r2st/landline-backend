"""Bounce handling: classify hard/soft bounces and suppress dead addresses.

When outreach bounces we must stop emailing that address — repeatedly hitting
an invalid mailbox tanks sender reputation and gets the domain blocklisted.

* A **hard** bounce (permanent: unknown user, mailbox not found, 5.x.x) → the
  address is suppressed immediately and the contact's active enrollments are set
  to ``bounced``.
* A **soft** bounce (transient: mailbox full, greylisted, 4.x.x) → tracked; the
  address is only suppressed after ``soft_bounce_suppress_threshold`` of them,
  since transient failures often clear on their own.
* A **blocked** rejection (5.7.x policy: spam verdict, DMARC/SPF failure, IP on
  a blocklist) → recorded and surfaced, and it suppresses **nothing**. See
  below; this is the class that used to be counted as hard.

Hard and soft increment ``Contact.bounce_count`` and stamp ``last_bounce_type``
/ ``last_bounced_at`` for the deliverability report. ``blocked`` stamps the type
and the timestamp but deliberately does not touch the counter — see below.

A policy block says something about *us*, and suppressing on it burns the list
---------------------------------------------------------------------------
``550 5.7.1 Our system has determined this message is likely unsolicited mail``
is a 5.x.x code, so the code-class rule read it as a permanent failure and
``record_bounce`` did the full hard-bounce treatment: contact ``suppressed`` and
``unsubscribed``, live enrollments closed as ``bounced``, and a
``reason="bounce"`` entry on the org do-not-contact list.

Not one word of that rejection was about the recipient. The mailbox is alive and
reading mail; what failed is our DMARC alignment, or our sending IP's
reputation, or a content verdict. The consequences compound in the worst
possible order:

* **Every recipient behind the same filter bounces at once.** A broken DMARC
  record does not reject one address, it rejects the whole send. One bad
  afternoon suppresses everyone at every Microsoft 365 and Google Workspace
  tenant we mailed — which, for B2B outreach, is most of the list.
* **The domain write-off then generalises it.** ``_maybe_write_off_domain``
  fires only on ``hard``, and ``MAILBOX_PROVIDER_DOMAINS`` shields
  ``gmail.com``-style consumer domains — but ``acme.com`` hosted on M365 is not
  in that set. Past ``dead_domain_bounce_threshold`` distinct addresses, every
  remaining contact at the company is suppressed too, on the strength of a
  rejection that never mentioned any of them.
* **The damage outlives the cause.** Fixing the DMARC record un-breaks sending
  and un-suppresses nothing. ``bounce`` is not in
  ``suppression.REVERSIBLE_REASONS`` (``manual`` alone is), so the entries can
  only be lifted one at a time, by hand, by an operator who first has to work
  out that they were never real bounces.

So the classifier answers a third way, and ``blocked`` is inert on the
suppression path. It is *not* silent: the send still records as bounced, so
``DangerMonitor``'s ``bounce_rate`` brake sees it in full and can pause the
mailbox — which is the correct response to a reputation problem, and the exact
opposite of deleting the audience. ``hard_bounce_count``, which asks "how many
dead addresses", correctly does not.

``bounce_count`` is left alone for the same reason: it is the counter
``soft_bounce_suppress_threshold`` reads, so incrementing it would reintroduce
the whole failure on a delay — the fifth blocked send to a live contact would
suppress them anyway.

Suppression is by **address**, not only by contact row
-----------------------------------------------------

A bounce suppresses three things, and it needs all three:

1. ``Contact.suppressed`` — the per-contact flag the send paths actually read
   (``tasks/outreach.py`` and the template send both gate on ``suppressed``, and
   on the suppression list; **neither reads ``unsubscribed``**). This module used
   to set ``unsubscribed`` alone, which stopped the enrollment that bounced and
   nothing else: the autonomous prospector de-duplicates enrollments per
   *(contact, sequence)* pair and filters on nothing else, so the next sequence
   picked up the same dead address and mailed it again. Every such send is
   another hard bounce against our own reputation, which is the one number the
   whole deliverability stack exists to protect.
2. ``Contact.unsubscribed`` — kept for the enrollment gate in
   ``routers/sequences.py`` and the contact filters, as before.
3. A ``SuppressionEntry`` on the org do-not-contact list, keyed by the **email
   address**. This is what survives the contact row. Discovery re-imports create
   a *new* ``Contact`` for an address we already know is dead — fresh row, fresh
   ``suppressed = False`` — and the address-level list is the only thing that
   catches it. ``reason="bounce"`` has been in the suppression enum since the
   list shipped and nothing but a manual CSV upload had ever written one.

A bounce for an address with **no** contact row suppresses too. We only receive a
DSN for mail we sent, so a hard bounce is proof the address is dead whether or
not we can still find the row that mailed it.

Classification is heuristic on the SMTP status code + diagnostic text, which is
what bounce webhooks (SES, Postmark) and mailer-daemon DSNs give us.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.services.outreach_events import last_sent_step

logger = logging.getLogger(__name__)

#: Every value :func:`classify_bounce` can return, and every value the
#: ``ck_bounce_kind`` CHECK constraint accepts (migration 113). Suppression
#: behaviour differs per kind — see the module docstring.
BOUNCE_KINDS = ("hard", "soft", "blocked")

# Permanent-failure phrases seen in DSN diagnostics.
_HARD_PHRASES = (
    "user unknown", "no such user", "unknown user", "recipient not found",
    "mailbox not found", "does not exist", "no such mailbox", "invalid recipient",
    "address rejected", "recipient rejected", "no mailbox", "account disabled",
    "account has been disabled", "user not found", "domain not found",
    "does not accept mail", "relay access denied", "no such address",
)
# Transient-failure phrases.
_SOFT_PHRASES = (
    "mailbox full", "over quota", "quota exceeded", "insufficient storage",
    "temporarily", "try again", "greylist", "grey-list", "deferred",
    "timed out", "connection timed out", "rate limit", "too many", "throttl",
    "service unavailable", "temporary failure", "temporary local problem",
)
# Rejections that are about the *sender* — our content verdict, our
# authentication, our IP's reputation. The recipient's mailbox is fine.
#
# Matched against the diagnostic *and* the status code string, because a DSN
# often carries the enhanced code only inside the diagnostic line
# ("550 5.7.1 …") while ``status_code`` holds the bare "550".
_POLICY_PHRASES = (
    "spam", "unsolicited", "bulk mail", "junk",
    "blocked", "block list", "blocklist", "blacklist", "denylist",
    "spamhaus", "barracuda", "spamcop", "uceprotect", "listed in",
    "reputation", "poor reputation", "sender score",
    "dmarc", "spf", "dkim", "not authenticated", "unauthenticated",
    "policy", "policy violation", "content rejected", "message rejected due to",
    "abuse", "phish",
)

# Enhanced status subclass ``x.7.y`` is "Security or Policy Status" (RFC 3463
# §3.8). A ``5.7.y`` is the permanent form, and it is the whole class this
# distinction exists for: Microsoft's ``5.7.509`` (DMARC), ``5.7.606`` (banned
# sending IP) and Google's ``5.7.1`` (unsolicited) all land here.
_ENHANCED_POLICY_RE = re.compile(r"\b5\.7\.\d+\b")


def classify_bounce(status_code: Optional[str] = None, diagnostic: str = "") -> str:
    """Return ``"hard"``, ``"soft"`` or ``"blocked"`` for a bounce.

    The order below is the whole of the logic, and each step is placed where it
    is because of what the step after it would otherwise decide.

    1. **A ``4.x`` is transient, whatever its reason.** First, so that a
       greylisting DSN mentioning "policy" is not escalated into a reputation
       alarm. ``4.7.x`` — a *temporary* policy refusal — belongs here: it
       suppresses nothing today and clears on its own.
    2. **An enhanced ``5.7.x`` is a policy block.** RFC 3463 §3.8 reserves the
       ``.7.`` subclass for security and policy, and this is the step the
       revision exists for: every rejection in the class carries a 5, so a
       code-class-first rule called it permanent and the caller suppressed a
       live recipient for something *we* did. See the module docstring.
    3. **An explicit dead-recipient phrase is hard.** Ahead of the looser
       policy-phrase match, because "user unknown" is direct evidence about the
       mailbox and a bare word like "blocked" in the same diagnostic is not.
       Getting this backwards would leave genuinely dead addresses in rotation,
       which is the failure the whole module was built to stop.
    4. **A looser policy phrase is a block** — a spam verdict, DMARC/SPF/DKIM,
       a named blocklist — for DSNs that carry no enhanced code.
    5. The SMTP status class (5.x.x → hard, 2.x.x → soft).
    6. Known transient phrases.

    Ambiguous input defaults to ``"soft"`` — we would rather retry a live
    address than permanently suppress a contact on a vague signal.
    """
    code = (status_code or "").strip()
    text = (diagnostic or "").lower()
    haystack = f"{code} {text}".lower()

    if re.match(r"\s*4", code):
        return "soft"

    if _ENHANCED_POLICY_RE.search(haystack):
        return "blocked"

    if any(p in text for p in _HARD_PHRASES):
        return "hard"

    if any(p in haystack for p in _POLICY_PHRASES):
        return "blocked"

    # Enhanced status code like "5.1.1" or classic "550".
    m = re.match(r"\s*([245])", code)
    if m:
        cls = m.group(1)
        if cls == "5":
            return "hard"
        # 2.x.x isn't a bounce, but if labelled as one treat as soft/transient.
        return "soft"

    if any(p in text for p in _SOFT_PHRASES):
        return "soft"
    return "soft"


async def _contacts_for_email(db: AsyncSession, email: str) -> list[Contact]:
    """All contacts with this email (case-insensitive)."""
    email = (email or "").strip().lower()
    if not email:
        return []
    rows = await db.execute(
        select(Contact).where(func.lower(Contact.email) == email)
    )
    return list(rows.scalars().all())


async def _suppress_address(db: AsyncSession, email: str, kind: str) -> bool:
    """Put ``email`` on the org do-not-contact list. Returns True if newly added.

    Idempotent by way of ``suppression.add_entry``, which is a no-op for an
    address already on the list — so a second DSN for the same mailbox, or a
    bounce for an address a compliance upload already covered, adds nothing.
    """
    from app.services import suppression as _suppression

    _, status = await _suppression.add_entry(
        db, email, reason="bounce", source="auto"
    )
    if status == "added":
        logger.info("Suppression list: added %s after a %s bounce", email, kind)
    return status == "added"


async def _suppress_contact(db: AsyncSession, contact: Contact) -> int:
    """Mark a contact do-not-email and stop its active enrollments.

    Returns the number of enrollments paused. Idempotent.
    """
    # ``suppressed`` is the flag the send paths gate on; ``unsubscribed`` is the
    # one the enrollment gate and the contact filters read. A bounce means both.
    contact.suppressed = True
    contact.unsubscribed = True
    rows = await db.execute(
        select(OutreachEnrollment).where(
            OutreachEnrollment.contact_id == contact.id,
            OutreachEnrollment.status == "active",
        )
    )
    paused = 0
    now = datetime.now(timezone.utc)
    for enr in rows.scalars().all():
        enr.status = "bounced"
        enr.completed_at = now
        paused += 1
        # Book the bounce on the funnel too. ``funnel_analytics`` derives both
        # ``bounce_rate`` and ``delivered`` (= sent − bounced) from a "bounced"
        # OutreachEvent, and nothing in the codebase wrote one — so the funnel
        # reported 0 bounces and 100% delivered however many addresses were
        # actually dead. Measured in production 2026-07-24: one real hard bounce
        # on the enrollment, ``bounce_rate: 0.0`` on the funnel.
        #
        # Recorded against the step that was last sent, which is the step whose
        # send bounced — and ``current_step`` is **not** that step.
        # ``send_sequence_step`` stamps its ``sent`` event with the step it sent
        # and *then* increments the counter, so ``current_step`` names the step
        # queued **next**. Booking the bounce there put it one step to the right
        # of the send it belongs to, and the funnel buckets per
        # ``(sequence_id, step_number)``: the step that actually bounced read
        # ``bounced: 0, delivered_rate: 100%``, while the step after it —
        # frequently one that has never sent, and past the end of the sequence
        # entirely when the bounce arrives after the last step — collected
        # bounces against a ``sent`` of 0, where ``bounce_rate`` divides to 0.0
        # and ``delivered`` clamps at 0. So both halves of the pair reported no
        # bounce, which is the exact reading this write was added to fix.
        #
        # ``last_sent_step`` is the shared resolver for "which step is this
        # outcome about" (``services/outreach_events``), already used for
        # replies after the same off-by-one zeroed every A/B variant's reply
        # count. An outcome event is attributed with it, never with
        # ``current_step``.
        #
        # Deliberately not deduplicated here: ``record_bounce`` only reaches
        # this for an *active* enrollment and flips it to "bounced" in the same
        # breath, so a second DSN for the same address finds nothing active and
        # writes nothing.
        db.add(
            OutreachEvent(
                enrollment_id=enr.id,
                step_number=await last_sent_step(db, enr),
                event_type="bounced",
                occurred_at=now,
                event_metadata={"email": contact.email, "source": "bounce_handler"},
            )
        )
    return paused


async def suppress_dead_domain(db: AsyncSession, domain: str) -> dict:
    """Take every remaining mailable contact at ``domain`` out of play.

    Called only once :func:`app.services.domain_reputation.is_domain_dead` says
    the domain is written off — see that module for the three clauses and why
    each is there. Here we only *act*, using exactly the machinery a single
    address bounce uses: the contact flags, the enrollment stop with its funnel
    event, and an address-level suppression entry so a re-import of the same
    mailbox is caught after the contact row is gone.

    Suppressing eagerly is the point. The send-time gate alone would leave the
    colleagues enrolled and due, so every one of them would still be picked up,
    still cost a query, and still sit in the queue looking like live outreach.
    ``reason="bounce"`` is honest and needs no schema change: these addresses are
    suppressed because their domain hard-bounced, and the suppression list is
    where an operator goes to find out why an address stopped being mailed.
    """
    from app.services import domain_reputation

    contacts = await domain_reputation.contacts_at_domain(db, domain)
    suppressed = 0
    enrollments_paused = 0
    listed = 0
    for contact in contacts:
        suppressed += 1
        enrollments_paused += await _suppress_contact(db, contact)
        if await _suppress_address(db, contact.email, "hard"):
            listed += 1
    await db.flush()
    if suppressed:
        logger.warning(
            "Domain %s written off: suppressed %d further contact(s), "
            "paused %d enrollment(s)",
            domain, suppressed, enrollments_paused,
        )
    return {
        "domain": domain,
        "contacts_suppressed": suppressed,
        "enrollments_paused": enrollments_paused,
        "list_entries_added": listed,
    }


async def _maybe_write_off_domain(
    db: AsyncSession, email: str, bounce_kind: str
) -> Optional[dict]:
    """Write off ``email``'s domain if this bounce was the one that proved it dead.

    Returns the suppression summary, or ``None`` when the domain still stands —
    which is the overwhelmingly common answer, so the cheap clauses in
    :func:`app.services.domain_reputation.is_domain_dead` (soft bounce, mailbox
    provider, threshold not met) run before anything expensive.
    """
    if bounce_kind != "hard":
        return None

    from app.services import domain_reputation

    domain = domain_reputation.domain_of(email)
    if not domain:
        return None

    dead, reason = await domain_reputation.is_domain_dead(db, domain)
    if not dead:
        return None

    logger.warning("Recipient domain written off — %s", reason)
    return await suppress_dead_domain(db, domain)


async def _record_policy_block(db: AsyncSession, email: str, now: datetime) -> dict:
    """Stamp a policy rejection on the matching contacts and change nothing else.

    Deliberately not a variant of the hard/soft path with flags turned off — the
    whole point is that none of that machinery runs. No suppression, no
    enrollment closed, no list entry, no domain write-off, and **no
    ``bounce_count``**: that counter is what ``soft_bounce_suppress_threshold``
    reads, so touching it would suppress the contact after N blocks and put the
    bug back on a timer.

    What is written is the audit trail an operator needs to see that the mailbox
    is fine and the sending domain is not: ``last_bounce_type='blocked'`` and
    ``last_bounced_at``. The bounced send itself is already recorded by the
    caller, so ``DangerMonitor``'s ``bounce_rate`` still counts it and can still
    pause the mailbox.
    """
    contacts = await _contacts_for_email(db, email)
    for contact in contacts:
        contact.last_bounce_type = "blocked"
        contact.last_bounced_at = now
    await db.flush()
    logger.warning(
        "Policy block for %s — %d contact(s) stamped, nothing suppressed. "
        "This rejection is about our sending reputation, not the recipient.",
        email, len(contacts),
    )
    return {
        "email": email,
        "kind": "blocked",
        "matched_contacts": len(contacts),
        "suppressed": 0,
        "enrollments_paused": 0,
        "list_entry_added": False,
        "domain_written_off": None,
    }


async def record_bounce(
    db: AsyncSession,
    email: str,
    *,
    status_code: Optional[str] = None,
    diagnostic: str = "",
    kind: Optional[str] = None,
) -> dict:
    """Record a bounce for ``email`` and suppress the contact when warranted.

    ``kind`` may be passed to force ``"hard"``/``"soft"``/``"blocked"``;
    otherwise it's classified from ``status_code`` + ``diagnostic``. Returns a
    summary dict.

    ``blocked`` — a spam verdict, a DMARC failure, our IP on a blocklist — is
    recorded and suppresses nothing, because none of it was about the recipient.
    See the module docstring for why treating it as a hard bounce burned the
    list rather than protecting it.
    """
    settings = get_settings()
    bounce_kind = (
        kind if kind in BOUNCE_KINDS else classify_bounce(status_code, diagnostic)
    )
    now = datetime.now(timezone.utc)

    if bounce_kind == "blocked":
        return await _record_policy_block(db, email, now)

    contacts = await _contacts_for_email(db, email)
    if not contacts:
        # No contact row, but we only get a DSN for mail we sent. A hard bounce
        # is proof the address is dead, so it goes on the address-level list
        # regardless — that is precisely the case the list exists for. A lone
        # soft bounce is not proof of anything and is left alone.
        listed = False
        if bounce_kind == "hard":
            listed = await _suppress_address(db, email, bounce_kind)
            await db.flush()
        logger.info(
            "Bounce for %s matched no contact (kind=%s, listed=%s)",
            email, bounce_kind, listed,
        )
        # The domain still gets checked. "No contact row for the address that
        # bounced" says nothing about the colleagues we *do* hold rows for, and
        # a re-import that replaced the contact is precisely how an address ends
        # up here — a case where the domain evidence is the only thing left.
        return {"email": email, "kind": bounce_kind, "matched_contacts": 0,
                "suppressed": 0, "enrollments_paused": 0,
                "list_entry_added": listed,
                "domain_written_off": await _maybe_write_off_domain(
                    db, email, bounce_kind
                )}

    suppressed = 0
    enrollments_paused = 0
    any_suppression = False
    for contact in contacts:
        contact.bounce_count = (contact.bounce_count or 0) + 1
        contact.last_bounce_type = bounce_kind
        contact.last_bounced_at = now

        should_suppress = bounce_kind == "hard" or (
            contact.bounce_count >= settings.soft_bounce_suppress_threshold
        )
        if not should_suppress:
            continue
        any_suppression = True
        # ``suppressed`` counts contacts this bounce newly took out of play.
        # Re-running suppression on an already-suppressed contact is deliberate
        # and cheap: it re-checks for enrollments created since (the prospector
        # can enroll a suppressed contact into a *different* sequence), so a
        # repeat DSN closes them rather than leaving them due forever.
        if not contact.suppressed:
            suppressed += 1
        enrollments_paused += await _suppress_contact(db, contact)

    # One list entry per address, not per matched contact row.
    list_entry_added = False
    if any_suppression:
        list_entry_added = await _suppress_address(db, email, bounce_kind)

    await db.flush()

    # Did this bounce just prove the whole *domain* is unreachable? Checked
    # after the flush so the stamp this call wrote is visible to the count.
    # Only on a hard bounce: a soft one is transient by definition and is the
    # wrong evidence to write off a company on, however many of them arrive.
    domain_result = await _maybe_write_off_domain(db, email, bounce_kind)
    logger.info(
        "Recorded %s bounce for %s — %d contact(s), suppressed=%d, "
        "enrollments_paused=%d, listed=%s",
        bounce_kind, email, len(contacts), suppressed, enrollments_paused,
        list_entry_added,
    )
    return {
        "email": email,
        "kind": bounce_kind,
        "matched_contacts": len(contacts),
        "suppressed": suppressed,
        "enrollments_paused": enrollments_paused,
        "list_entry_added": list_entry_added,
        "domain_written_off": domain_result,
    }
