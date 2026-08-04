"""Rank prospects by what they actually did with our email.

Landline already scores companies on *fit* — industry, size, location, data
completeness (``scoring.py``) — and on *propensity*, which is driven by intent
signals (``lead_router.py``). Neither answers the question a rep opens the tool
to ask: **who, out of everyone we have emailed, is worth calling this morning?**

Fit is a property of the company and barely moves. Intent signals come from
outside (job postings, funding news). Behaviour — this person opened three of
our emails, clicked the demo link on Tuesday, and hasn't replied — is the
freshest and most direct evidence of interest we own, and nothing ranked on it.

The score is deliberately simple and explainable, because a rep who cannot see
why someone is at the top of the list will not trust the list:

    score = Σ (weight(event) × recency(event)) − penalties

* **Weights** are ordered by how much deliberate effort the act took. A reply is
  a person typing; a click is a person choosing to look; an open may be Apple's
  Mail Privacy Protection pre-fetching an image with nobody present. That last
  point is why an open is worth 5 and a click is worth 25 — on modern mail, open
  rate is a noisy proxy and click rate is close to ground truth.
* **Recency** halves the weight every ``HALF_LIFE_DAYS``. Interest decays; a
  click from March is not a lead today. Continuous rather than bracketed so the
  ranking doesn't jump when a lead crosses a boundary overnight.
* **Penalties** subtract for a bounce (the address is questionable) and zero the
  contact outright on unsubscribe/suppression — someone who asked to be left
  alone must never surface in a "call these people" list, whatever they clicked
  beforehand.

Reads **both send tables** (CLAUDE.md, "Sent mail lives in two tables"): the
sequence engine writes ``outreach_events`` and no ``EmailSend`` row, so a
ranking built on ``EmailSend`` alone would be blind to every cold send Dialtone
has ever made.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Points per engagement, before recency decay. See the module docstring for why
# a click outweighs an open by 5x rather than the ~2x a naive funnel would give.
EVENT_WEIGHTS: dict[str, float] = {
    "replied": 60.0,
    "clicked": 25.0,
    "opened": 5.0,
}

# Days for an event's contribution to halve. Two weeks: long enough that a lead
# who clicked last Friday is still near the top on Monday week, short enough
# that a quarter-old open doesn't outrank today's click.
HALF_LIFE_DAYS = 14.0

# How far back to look at all. Beyond this an event contributes < 1% of its
# weight, so the query cost buys nothing.
LOOKBACK_DAYS = 120

# A bounce says the address may be bad. Not fatal on its own (soft bounces
# happen), so it subtracts rather than disqualifies.
BOUNCE_PENALTY = 15.0

# Scores are reported on a 0-100 scale. The raw sum is unbounded (a contact who
# replied twice and clicked five times can exceed 200), so it is squashed rather
# than clipped: clipping makes every engaged lead a 100 and destroys the
# ordering exactly where ordering matters most.
_SQUASH_MIDPOINT = 60.0

# Bands for the label a UI shows next to the number.
_TIERS: tuple[tuple[float, str], ...] = (
    (70.0, "hot"),
    (40.0, "warm"),
    (15.0, "cool"),
)
_TIER_FLOOR = "cold"


def recency_factor(occurred_at: datetime, now: datetime) -> float:
    """Exponential decay: 1.0 at t=0, 0.5 after one half-life, →0 after that.

    A future timestamp (clock skew between the app and the tracker) clamps to
    1.0 rather than amplifying, so a bad clock can't mint a top-ranked lead.
    """
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    age_days = (now - occurred_at).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def normalize_score(raw: float) -> float:
    """Squash an unbounded positive score onto 0-100.

    ``100 × raw / (raw + midpoint)``: monotonic, so ordering is preserved
    exactly, and asymptotic, so no amount of engagement saturates the top of the
    scale and flattens the leaders into a tie.
    """
    if raw <= 0:
        return 0.0
    return round(100.0 * raw / (raw + _SQUASH_MIDPOINT), 1)


def tier_for(score: float) -> str:
    """The band label for a 0-100 engagement score."""
    for threshold, label in _TIERS:
        if score >= threshold:
            return label
    return _TIER_FLOOR


@dataclass
class ContactEngagement:
    """One ranked prospect."""

    contact_id: object
    contact_name: str
    email: str
    title: Optional[str]
    company_id: object
    company_name: str
    sent: int = 0
    opened: int = 0
    clicked: int = 0
    replied: int = 0
    bounced: int = 0
    raw_score: float = 0.0
    last_engaged_at: Optional[datetime] = None
    suppressed: bool = False
    active_enrollments: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """0-100 engagement score. Zero for anyone who opted out."""
        if self.suppressed:
            return 0.0
        return normalize_score(self.raw_score)

    @property
    def tier(self) -> str:
        return tier_for(self.score)

    def as_dict(self) -> dict:
        return {
            "contact_id": self.contact_id,
            "contact_name": self.contact_name,
            "email": self.email,
            "title": self.title,
            "company_id": self.company_id,
            "company_name": self.company_name,
            "sent": self.sent,
            "opened": self.opened,
            "clicked": self.clicked,
            "replied": self.replied,
            "bounced": self.bounced,
            "score": self.score,
            "tier": self.tier,
            "last_engaged_at": self.last_engaged_at,
            "active_enrollments": self.active_enrollments,
            "suppressed": self.suppressed,
            "reasons": self.reasons,
        }


def _describe(record: ContactEngagement) -> list[str]:
    """Plain-language reasons this contact ranks where it does.

    The ranking is only useful if a rep can act on it, and "score 78" is not
    something anyone can act on. These are the lines a UI puts under the name.
    """
    reasons: list[str] = []
    if record.suppressed:
        reasons.append("Unsubscribed or suppressed — do not contact")
        return reasons
    if record.replied:
        reasons.append(f"Replied {record.replied}×")
    if record.clicked:
        reasons.append(f"Clicked a link {record.clicked}×")
    if record.opened and not record.clicked:
        reasons.append(f"Opened {record.opened}× but never clicked")
    if record.bounced:
        reasons.append(f"{record.bounced} bounce(s) — verify the address")
    if not reasons:
        reasons.append(f"No engagement across {record.sent} send(s)")
    return reasons


async def rank_contacts(
    db: AsyncSession,
    *,
    limit: int = 50,
    min_score: float = 0.0,
    include_suppressed: bool = False,
    now: Optional[datetime] = None,
) -> list[dict]:
    """The top ``limit`` ranked contacts. See :func:`rank_contacts_with_totals`."""
    page, _counts, _total = await rank_contacts_with_totals(
        db,
        limit=limit,
        min_score=min_score,
        include_suppressed=include_suppressed,
        now=now,
    )
    return page


async def rank_contacts_with_totals(
    db: AsyncSession,
    *,
    limit: int = 50,
    min_score: float = 0.0,
    include_suppressed: bool = False,
    now: Optional[datetime] = None,
) -> tuple[list[dict], dict, int]:
    """Rank every emailed contact by recency-weighted engagement, best first.

    Returns ``(page, tier_counts, total_ranked)``. The counts and the total
    describe **everyone who qualified**, not the page — the caller asked for the
    top 50 and was told there were 50, so an org with five hundred engaged
    prospects reported the same "50 ranked, 12 hot" as one with fifty-one, and
    the histogram above the table was a histogram of the table. The scoring pass
    has already visited every contact by the time the list is cut, so the honest
    numbers are free; only the page is truncated.

    ``min_score`` filters on the normalised 0-100 score. ``include_suppressed``
    is off by default — an unsubscribed contact scores 0 and has no business in
    a prioritisation list, but the flag exists so a compliance view can see them.
    """
    from app.models.contact import Contact
    from app.models.company import Company
    from app.models.email_template import NON_WIRE_STATUSES, EmailSend
    from app.models.outreach import OutreachEnrollment, OutreachEvent

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    records: dict[object, ContactEngagement] = {}

    def _record_for(contact, company_name: str) -> ContactEngagement:
        existing = records.get(contact.id)
        if existing is not None:
            return existing
        name = " ".join(
            p for p in ((contact.first_name or "").strip(), (contact.last_name or "").strip()) if p
        )
        record = ContactEngagement(
            contact_id=contact.id,
            contact_name=name or (contact.email or "(unnamed)"),
            email=contact.email or "",
            title=contact.title,
            company_id=contact.company_id,
            company_name=company_name or "",
            suppressed=bool(getattr(contact, "suppressed", False))
            or bool(getattr(contact, "unsubscribed", False)),
        )
        records[contact.id] = record
        return record

    def _apply(record: ContactEngagement, event_type: str, occurred_at: Optional[datetime]) -> None:
        if event_type == "sent":
            record.sent += 1
            return
        if event_type == "bounced":
            record.bounced += 1
            record.raw_score -= BOUNCE_PENALTY
            return
        weight = EVENT_WEIGHTS.get(event_type)
        if weight is None or occurred_at is None:
            return
        setattr(record, event_type, getattr(record, event_type) + 1)
        record.raw_score += weight * recency_factor(occurred_at, now)
        stamp = occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=timezone.utc)
        if record.last_engaged_at is None or stamp > record.last_engaged_at:
            record.last_engaged_at = stamp

    # --- Sequence engine (outreach_events) ---
    seq_rows = (
        await db.execute(
            select(OutreachEvent.event_type, OutreachEvent.occurred_at, Contact, Company.name)
            .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
            .join(Contact, OutreachEnrollment.contact_id == Contact.id)
            .join(Company, Contact.company_id == Company.id, isouter=True)
            .where(OutreachEvent.occurred_at >= cutoff)
        )
    ).all()
    for event_type, occurred_at, contact, company_name in seq_rows:
        _apply(_record_for(contact, company_name or ""), event_type, occurred_at)

    # --- Manual / template sends (email_sends) ---
    send_rows = (
        await db.execute(
            select(EmailSend, Contact, Company.name)
            .join(Contact, EmailSend.contact_id == Contact.id)
            .join(Company, Contact.company_id == Company.id, isouter=True)
            .where(EmailSend.sent_at >= cutoff)
        )
    ).all()
    for send, contact, company_name in send_rows:
        record = _record_for(contact, company_name or "")
        # ``status`` is the send's outcome; the *_at stamps are what the
        # recipient did afterwards. Both matter and they are not the same thing.
        if send.status == "bounced":
            _apply(record, "bounced", send.sent_at)
        elif send.status not in NON_WIRE_STATUSES:
            # ``NON_WIRE_STATUSES`` rather than a literal. The literal that stood
            # here listed six of the seven and left out ``pending`` — the status
            # a row is *born* in — so a message composed and not yet on the wire
            # scored its recipient as engaged-with, and moved them up a list
            # whose whole purpose is "who should a rep call next".
            _apply(record, "sent", send.sent_at)
        for kind, stamp in (
            ("opened", send.opened_at),
            ("clicked", send.clicked_at),
            ("replied", send.replied_at),
        ):
            # SQLite hands these back naive; compare on one tz basis rather than
            # letting a naive/aware comparison raise mid-ranking.
            if stamp is None:
                continue
            aware = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
            if aware >= cutoff:
                _apply(record, kind, aware)

    if not records:
        return [], _blank_tiers(), 0

    # Active enrollments, so the list can say "already in a sequence" rather than
    # sending a rep to duplicate an automated touch.
    active_rows = (
        await db.execute(
            select(OutreachEnrollment.contact_id).where(OutreachEnrollment.status == "active")
        )
    ).all()
    for (contact_id,) in active_rows:
        record = records.get(contact_id)
        if record is not None:
            record.active_enrollments += 1

    ranked = list(records.values())
    if not include_suppressed:
        ranked = [r for r in ranked if not r.suppressed]
    ranked = [r for r in ranked if r.score >= min_score]
    ranked.sort(
        key=lambda r: (r.score, r.last_engaged_at or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )

    # Counted before the cut: these describe the population, and the page is
    # only the part of it that fits on screen.
    counts = _blank_tiers()
    for record in ranked:
        if record.tier in counts:
            counts[record.tier] += 1

    out = []
    for record in ranked[: max(1, limit)]:
        record.reasons = _describe(record)
        out.append(record.as_dict())
    return out, counts, len(ranked)


def _blank_tiers() -> dict:
    return {"hot": 0, "warm": 0, "cool": 0, "cold": 0}


