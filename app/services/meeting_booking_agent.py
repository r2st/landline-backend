"""Meeting Booking Agent — autonomous scheduling once a prospect shows interest.

When Dialtone classifies a cold-email reply as ``interested``, this agent takes
over the scheduling: it re-reads the reply for *meeting* intent, finds open slots
on the rep's Google Calendar inside the configured working-hours window, emails a
short proposal with 3–4 options in the prospect's own thread, parses whichever
slot they pick from the back-and-forth, creates the calendar invite, and sends
the confirmation + a pre-meeting reminder.

Design notes
------------
* **Pure core, injected edges.** The scheduling logic that matters —
  :func:`generate_slots`, :func:`select_slot_from_reply`, the copy builders — is
  pure and unit-tested offline. Everything with a side effect (Google Calendar,
  Gmail send, the AI call) is passed in as a parameter with a real default, so
  tests drive it deterministically and prod wires the real transport.
* **Best-effort, never blocks the pipeline.** Calendar/free-busy failures fall
  back to treating the window as free; a send failure is logged and the meeting
  row still records what was attempted. The reply-classification task calls
  :func:`on_interested_reply` inside its own try/except so a booking hiccup can
  never abort reply routing.
* **One agent config per org**, lazily created with defaults by the router.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.meeting_booking import BookedMeeting, MeetingBookingAgent
from app.services import google_calendar
from app.services.ai_service import get_ai_service

logger = logging.getLogger(__name__)

# The meeting-intent categories this agent reasons about. Distinct from Dialtone's
# reply-classification categories (interested/not_interested/…): here we only care
# whether the interested prospect wants to *meet*, wants *more info first*, or is
# actually declining/opting out.
INTENT_POSITIVE_MEETING = "positive_meeting"
INTENT_POSITIVE_INFO = "positive_info"
INTENT_NEUTRAL = "neutral"
INTENT_NEGATIVE = "negative"
INTENT_UNSUBSCRIBE = "unsubscribe"
MEETING_INTENTS = (
    INTENT_POSITIVE_MEETING,
    INTENT_POSITIVE_INFO,
    INTENT_NEUTRAL,
    INTENT_NEGATIVE,
    INTENT_UNSUBSCRIBE,
)

# How many slots we offer in a proposal, and the daily offer cap.
MAX_OFFERED_SLOTS = 4
OFFERED_PER_DAY = 2
# Don't offer a slot starting sooner than this from "now" — nobody books a call
# for 10 minutes out, and it avoids a race with the prospect's own calendar.
MIN_LEAD_MINUTES = 90

# --- Cheap keyword pre-filter (no LLM) ----------------------------------------
_UNSUB_RE = re.compile(r"\b(unsubscribe|opt[\s-]?out|remove me|take me off|stop emailing)\b", re.I)
_NEGATIVE_RE = re.compile(
    r"\b(not interested|no thanks|no thank you|not a fit|not right now|already have|"
    r"we('| a)re good|please stop|don'?t contact)\b",
    re.I,
)
_MEETING_RE = re.compile(
    r"\b(call|demo|meeting|meet|schedule|scheduling|calendar|book|booking|chat|"
    r"talk|connect|zoom|google meet|teams|catch up|hop on|jump on|walk me through|"
    r"available|availability|what times?|when (are|works|can))\b",
    re.I,
)
_INFO_RE = re.compile(
    r"\b(more info|more information|send (me )?(more|some)|tell me more|learn more|"
    r"pricing|price|quote|case stud|deck|one[\s-]?pager|details?)\b",
    re.I,
)


def keyword_intent(text: Optional[str]) -> Optional[str]:
    """High-precision keyword pass; returns an intent or ``None`` (defer to AI).

    Ordered by consequence: an explicit opt-out or decline wins over meeting
    language ("no thanks, please remove me — not looking for a call"), and an
    explicit meeting ask wins over a generic info ask.
    """
    if not text:
        return None
    if _UNSUB_RE.search(text):
        return INTENT_UNSUBSCRIBE
    if _NEGATIVE_RE.search(text):
        return INTENT_NEGATIVE
    if _MEETING_RE.search(text):
        return INTENT_POSITIVE_MEETING
    if _INFO_RE.search(text):
        return INTENT_POSITIVE_INFO
    return None


async def classify_reply_intent(reply_text: Optional[str], *, ai_service=None) -> dict:
    """Classify a reply's *meeting* intent.

    Returns ``{"intent", "confidence", "source", "reason"}`` where ``intent`` is
    one of :data:`MEETING_INTENTS`. A confident keyword hit short-circuits the
    LLM; otherwise the AI is asked for a structured verdict. On an AI outage the
    keyword result (or ``neutral``) is returned — the function never raises.
    """
    text = (reply_text or "").strip()
    kw = keyword_intent(text)
    # Opt-out / decline are compliance/consequence-critical and high-precision by
    # construction — trust the keyword outright, don't spend an LLM call on them.
    if kw in (INTENT_UNSUBSCRIBE, INTENT_NEGATIVE):
        return {"intent": kw, "confidence": 0.95, "source": "keyword", "reason": "keyword match"}

    ai_service = ai_service or get_ai_service()
    if not text or not getattr(ai_service, "is_configured", False):
        intent = kw or INTENT_NEUTRAL
        return {"intent": intent, "confidence": 0.5 if kw else 0.0, "source": "keyword", "reason": "no AI"}

    prompt = (
        "You classify a prospect's reply to a cold sales email by scheduling intent.\n"
        "Return STRICT JSON: {\"intent\": <one of positive_meeting|positive_info|"
        "neutral|negative|unsubscribe>, \"confidence\": <0..1>, \"reason\": <short>}\n\n"
        "Definitions:\n"
        "- positive_meeting: wants a call/demo/meeting or asks about availability.\n"
        "- positive_info: interested but wants more info/pricing/materials first, "
        "not yet a meeting.\n"
        "- neutral: ambiguous, out-of-office, or a question that isn't scheduling.\n"
        "- negative: not interested / declining.\n"
        "- unsubscribe: explicit opt-out.\n\n"
        f"Reply:\n\"\"\"\n{text[:2000]}\n\"\"\""
    )
    try:
        result = await ai_service.complete_json(prompt, max_tokens=200)
    except Exception as exc:  # noqa: BLE001 — degrade to keyword
        logger.info("meeting intent AI failed: %s", exc)
        result = {}

    intent = str(result.get("intent", "") or "").lower().strip()
    if intent not in MEETING_INTENTS:
        intent = kw or INTENT_NEUTRAL
        return {
            "intent": intent,
            "confidence": float(result.get("confidence", 0.0) or 0.0),
            "source": "keyword" if kw else "ai",
            "reason": result.get("reason") or "unrecognized AI intent",
        }
    return {
        "intent": intent,
        "confidence": float(result.get("confidence", 0.7) or 0.7),
        "source": "ai",
        "reason": result.get("reason") or "",
    }


# ---------------------------------------------------------------------------
# Slot generation (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotConfig:
    """The scheduling knobs :func:`generate_slots` needs, decoupled from the ORM.

    Built from a :class:`MeetingBookingAgent` via :meth:`from_agent` in prod;
    constructed directly in tests so slot logic runs without a database.
    """

    duration_minutes: int = 30
    buffer_minutes: int = 15
    working_start: str = "09:00"
    working_end: str = "17:00"
    timezone: str = "Asia/Kolkata"
    window_days: int = 7
    max_daily_meetings: int = 5

    @classmethod
    def from_agent(cls, agent: MeetingBookingAgent) -> "SlotConfig":
        return cls(
            duration_minutes=agent.default_meeting_duration_minutes,
            buffer_minutes=agent.buffer_between_meetings_minutes,
            working_start=agent.working_hours_start,
            working_end=agent.working_hours_end,
            timezone=agent.timezone,
            window_days=agent.booking_window_days,
            max_daily_meetings=agent.max_daily_meetings,
        )


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def _parse_hhmm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = value.split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return default


def _overlaps_busy(
    start: datetime, end: datetime, busy: list[tuple[datetime, datetime]], buffer: timedelta
) -> bool:
    """True if [start, end] collides with any busy interval, padded by ``buffer``.

    Busy intervals are widened by the buffer on both sides so a proposed slot is
    never offered flush against an existing meeting.
    """
    for bs, be in busy:
        if start < be + buffer and bs - buffer < end:
            return True
    return False


def generate_slots(
    config: SlotConfig,
    *,
    now: datetime,
    busy: Optional[list[tuple[datetime, datetime]]] = None,
    daily_counts: Optional[dict[date, int]] = None,
    max_slots: int = MAX_OFFERED_SLOTS,
    offered_per_day: int = OFFERED_PER_DAY,
) -> list[dict]:
    """Compute open meeting slots inside the working-hours window (pure).

    Walks each weekday in the booking window, stepping through the working-hours
    window in ``duration + buffer`` increments, and keeps slots that don't overlap
    a busy interval (padded by the buffer) and don't fall on a day already at the
    daily cap. Returns up to ``max_slots`` dicts ``{start, end, label}`` where
    ``start``/``end`` are tz-aware ISO strings and ``label`` is human copy in the
    agent's timezone (e.g. ``"Mon, Jul 21 · 2:30 PM IST"``).

    ``now`` must be timezone-aware. ``busy`` intervals and ``now`` may be in any
    tz; comparisons are absolute. A slot never starts sooner than
    :data:`MIN_LEAD_MINUTES` from ``now``.
    """
    busy = busy or []
    daily_counts = daily_counts or {}
    tz = _tz(config.timezone)
    duration = timedelta(minutes=max(5, config.duration_minutes))
    buffer = timedelta(minutes=max(0, config.buffer_minutes))
    step = timedelta(minutes=max(5, config.duration_minutes) + max(0, config.buffer_minutes))
    earliest = now.astimezone(timezone.utc) + timedelta(minutes=MIN_LEAD_MINUTES)

    sh, sm = _parse_hhmm(config.working_start, (9, 0))
    eh, em = _parse_hhmm(config.working_end, (17, 0))

    now_local = now.astimezone(tz)
    slots: list[dict] = []

    for day_offset in range(max(1, config.window_days)):
        day = (now_local + timedelta(days=day_offset)).date()
        # Weekends are not offered — a scheduling agent proposing a Sunday 9am
        # call reads as a bot, and the rep's working hours are weekdays.
        if day.weekday() >= 5:
            continue
        if daily_counts.get(day, 0) >= config.max_daily_meetings:
            continue

        window_start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
        window_end = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz)
        if window_end <= window_start:
            continue

        cursor = window_start
        day_count = 0
        while cursor + duration <= window_end and day_count < offered_per_day:
            slot_end = cursor + duration
            if cursor.astimezone(timezone.utc) >= earliest and not _overlaps_busy(
                cursor, slot_end, busy, buffer
            ):
                slots.append(
                    {
                        "start": cursor.isoformat(),
                        "end": slot_end.isoformat(),
                        "label": _slot_label(cursor),
                    }
                )
                day_count += 1
                if len(slots) >= max_slots:
                    return slots
            cursor += step

    return slots


def _slot_label(dt: datetime) -> str:
    """Human slot label in the slot's own timezone: 'Mon, Jul 21 · 2:30 PM IST'."""
    tzname = dt.strftime("%Z") or ""
    # %-I isn't portable (Windows); strip a leading zero manually.
    hour = dt.strftime("%I").lstrip("0") or "12"
    stamp = dt.strftime(f"%a, %b %d · {hour}:%M %p")
    return f"{stamp} {tzname}".strip()


def select_slot_from_reply(reply_text: Optional[str], proposed_slots: list[dict]) -> Optional[int]:
    """Best-effort keyword pick of an offered slot from a reply (pure).

    Recognises "option 2" / "the 2nd one" / "#3" ordinals and a day-name match
    ("Monday works"). Returns the 0-based index into ``proposed_slots`` or
    ``None`` when nothing matches (the AI parser is the primary path;
    :func:`handle_slot_selection` falls back here on an AI outage).
    """
    if not reply_text or not proposed_slots:
        return None
    text = reply_text.lower()

    # Ordinal words / digits: "option 2", "second", "#3", "number 1".
    words = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
    for w, n in words.items():
        if w in text and n <= len(proposed_slots):
            return n - 1
    m = re.search(r"(?:option|slot|number|#|no\.?)\s*(\d)", text)
    if m:
        n = int(m.group(1))
        if 1 <= n <= len(proposed_slots):
            return n - 1

    # Day-name match against the offered slot labels (only if unambiguous).
    days = ["monday", "tuesday", "wednesday", "thursday", "friday"]
    mentioned = [d for d in days if d in text]
    if len(mentioned) == 1:
        abbr = mentioned[0][:3].capitalize()
        matches = [i for i, s in enumerate(proposed_slots) if s.get("label", "").startswith(abbr)]
        if len(matches) == 1:
            return matches[0]
    return None


async def handle_slot_selection(
    reply_text: Optional[str], proposed_slots: list[dict], *, ai_service=None
) -> dict:
    """Parse which offered slot a follow-up reply selects.

    Returns ``{"selected_index", "selected_slot", "wants_reschedule", "reason"}``.
    ``selected_index`` is the 0-based index into ``proposed_slots`` (or ``None``);
    ``wants_reschedule`` is True when the prospect wants a *different* time than
    any offered. Uses AI when configured, falling back to
    :func:`select_slot_from_reply`. Never raises.
    """
    text = (reply_text or "").strip()
    none_result = {"selected_index": None, "selected_slot": None, "wants_reschedule": False, "reason": ""}
    if not text or not proposed_slots:
        return none_result

    ai_service = ai_service or get_ai_service()
    if getattr(ai_service, "is_configured", False):
        options = "\n".join(f"{i + 1}. {s.get('label', '')}" for i, s in enumerate(proposed_slots))
        prompt = (
            "A prospect was offered these meeting time options:\n"
            f"{options}\n\n"
            "Read their reply and decide which option (if any) they chose.\n"
            "Return STRICT JSON: {\"choice\": <1-based option number, or 0 if none>, "
            "\"wants_different_time\": <true|false>, \"reason\": <short>}\n"
            "Pick 0 and wants_different_time=true if they want a time not offered.\n\n"
            f"Reply:\n\"\"\"\n{text[:2000]}\n\"\"\""
        )
        try:
            result = await ai_service.complete_json(prompt, max_tokens=200)
            choice = int(result.get("choice", 0) or 0)
            wants = bool(result.get("wants_different_time", False))
            if 1 <= choice <= len(proposed_slots):
                idx = choice - 1
                return {
                    "selected_index": idx,
                    "selected_slot": proposed_slots[idx],
                    "wants_reschedule": False,
                    "reason": result.get("reason") or "",
                }
            return {
                "selected_index": None,
                "selected_slot": None,
                "wants_reschedule": wants,
                "reason": result.get("reason") or "",
            }
        except Exception as exc:  # noqa: BLE001 — fall back to keyword
            logger.info("slot-selection AI failed: %s", exc)

    idx = select_slot_from_reply(text, proposed_slots)
    if idx is not None:
        return {
            "selected_index": idx,
            "selected_slot": proposed_slots[idx],
            "wants_reschedule": False,
            "reason": "keyword match",
        }
    return none_result


# ---------------------------------------------------------------------------
# Copy builders (pure)
# ---------------------------------------------------------------------------


def _contact_first_name(contact: Optional[Contact]) -> str:
    if contact is None:
        return "there"
    return (contact.first_name or "").strip() or "there"


def build_proposal_text(contact: Optional[Contact], slots: list[dict], *, rep_name: str = "") -> tuple[str, str]:
    """Default proposal copy → ``(text, html)``. Used when the AI is unavailable."""
    name = _contact_first_name(contact)
    options = "\n".join(f"  {i + 1}. {s['label']}" for i, s in enumerate(slots))
    signoff = f"\n\nBest,\n{rep_name}" if rep_name else "\n\nBest"
    text = (
        f"Hi {name},\n\n"
        "Great to hear from you! Happy to set up a quick call. Here are a few times "
        "that work on my end — just reply with the one that suits you and I'll send "
        "an invite:\n\n"
        f"{options}\n\n"
        "If none of these work, let me know a window that does and I'll make it happen."
        f"{signoff}"
    )
    html = "<p>" + text.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    return text, html


def build_confirmation_text(
    contact: Optional[Contact], meeting: BookedMeeting, *, template: Optional[str] = None, rep_name: str = ""
) -> tuple[str, str]:
    """Confirmation copy → ``(text, html)``, honouring an operator template."""
    name = _contact_first_name(contact)
    when = _meeting_when_label(meeting)
    if template:
        text = _fill_template(template, name=name, when=when, title=meeting.title, rep_name=rep_name)
    else:
        signoff = f"\n\nSee you then,\n{rep_name}" if rep_name else "\n\nSee you then"
        text = (
            f"Hi {name},\n\n"
            f"You're all set — I've booked our {meeting.title} for {when} and sent a "
            f"calendar invite. Looking forward to it!{signoff}"
        )
    html = "<p>" + text.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    return text, html


def build_reminder_text(
    contact: Optional[Contact], meeting: BookedMeeting, *, template: Optional[str] = None, rep_name: str = ""
) -> tuple[str, str]:
    """Reminder copy → ``(text, html)``, honouring an operator template."""
    name = _contact_first_name(contact)
    when = _meeting_when_label(meeting)
    if template:
        text = _fill_template(template, name=name, when=when, title=meeting.title, rep_name=rep_name)
    else:
        signoff = f"\n\nTalk soon,\n{rep_name}" if rep_name else "\n\nTalk soon"
        text = (
            f"Hi {name},\n\n"
            f"Quick reminder that our {meeting.title} is coming up at {when}. "
            f"Reply here if anything's changed on your end.{signoff}"
        )
    html = "<p>" + text.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    return text, html


def _fill_template(template: str, **kwargs) -> str:
    """Substitute ``{name}``/``{when}``/``{title}``/``{rep_name}`` placeholders."""
    out = template
    for key, value in kwargs.items():
        out = out.replace("{" + key + "}", str(value or ""))
    return out


def _meeting_when_label(meeting: BookedMeeting) -> str:
    if meeting.start_time is None:
        return "the scheduled time"
    tz = _tz(meeting.timezone or "UTC")
    return _slot_label(meeting.start_time.astimezone(tz))


# ---------------------------------------------------------------------------
# Orchestration (DB + injected side effects)
# ---------------------------------------------------------------------------


async def get_active_agent(db: AsyncSession) -> Optional[MeetingBookingAgent]:
    """The org's booking-agent config if it exists AND is active, else ``None``.

    Org-scoped globally, so this only ever returns the current org's row.
    """
    agent = (await db.execute(select(MeetingBookingAgent).limit(1))).scalar_one_or_none()
    if agent is None or not agent.is_active:
        return None
    return agent


async def _load_calendar_account(db: AsyncSession, agent: MeetingBookingAgent) -> Optional[GmailAccount]:
    if agent.calendar_account_id is None:
        return None
    return (
        await db.execute(select(GmailAccount).where(GmailAccount.id == agent.calendar_account_id))
    ).scalar_one_or_none()


async def _daily_confirmed_counts(db: AsyncSession, tz: ZoneInfo) -> dict[date, int]:
    """Count confirmed meetings per local day so full days aren't offered again."""
    rows = (
        await db.execute(
            select(BookedMeeting.start_time).where(
                BookedMeeting.status == "confirmed",
                BookedMeeting.start_time.isnot(None),
            )
        )
    ).scalars().all()
    counts: dict[date, int] = {}
    for start in rows:
        if start is None:
            continue
        local_day = start.astimezone(tz).date()
        counts[local_day] = counts.get(local_day, 0) + 1
    return counts


def _default_gmail_send(account: GmailAccount, **kwargs) -> dict:
    """Real Gmail transport for the agent (thin wrapper over GmailSender)."""
    from app.services.gmail_sender import GmailSender

    return GmailSender(account).send_email(**kwargs)


async def _guarded_send(
    db: AsyncSession,
    sender: Callable,
    account: GmailAccount,
    *,
    to_email: str,
    kind: str,
    refusable: bool = True,
    contact_id=None,
    **kwargs,
) -> tuple[bool, object]:
    """Clear the compliance floors, then send. ``(ok, transport_result)``.

    Every send in this module goes through here, and the reason is that this
    agent holds a ``GmailSender`` directly instead of calling
    ``send_outreach_email``. That was the bug: the kill switch and the
    do-not-contact list are enforced inside ``send_outreach_email``, so a path
    that doesn't call it doesn't get them — and this one is *autonomous*, firing
    on a beat with no human between the decision and the wire.

    What that cost: an operator engaging the kill switch because their domain
    was on fire stopped the queue, the sequences and every manual send, watched
    the UI report the platform paused — and this agent kept proposing meetings.
    A recipient who unsubscribed, hard-bounced or filed a spam complaint stayed
    on the suppression list that every *other* path honours, and got mail from
    this one anyway.

    Reminders and confirmations go through the same gate as proposals rather
    than claiming a transactional exemption. The list does not record which of
    them a recipient meant to escape, and a meeting-reminder to an address that
    filed a complaint is the one that lands hardest. If an operator wants that
    mail to go, the fix is to remove the entry — a decision with a record, not a
    branch in a module.

    The **volume** floors are claimed here for the same reason: this agent is
    autonomous volume out of a mailbox the sequence queue is also sending from,
    and it consumed none of that mailbox's warm-up allowance or the org's daily
    cap. A day-1 mailbox ramped to 5/day sent its 5 sequence steps and then a
    meeting proposal every 15 minutes, with every warm-up check reading 5/5 —
    the ramp enforced against a number this path never touched.

    ``refusable`` splits the four sends where the compliance floors deliberately
    do not, and the asymmetry is the point. Suppression is absolute because it
    is about consent, and the list does not record which mail a recipient meant
    to escape. The volume caps are *operational* — they exist to keep a cold
    domain from burning itself — so they bind the mail that is cold volume
    (a proposal, a re-proposal) and not the mail that is one half of a
    conversation already underway. Refusing a confirmation would leave a
    prospect who just picked a slot with an event on their calendar and no
    word from us; refusing a reminder would silence a meeting starting in an
    hour. Both still *count*, because the wire does not care what we called it.

    Failures are swallowed to a ``False`` exactly like the direct calls this
    replaced: the agent records the attempt in ``conversation_thread`` and a
    dead Gmail token must not take down the sweep for every other meeting.
    **Callers must act on that ``False``** — see :func:`propose_meeting`, which
    would otherwise persist a ``proposed`` meeting for a message nobody got and
    leave the 15-minute sweep polling forever for a reply to it.
    """
    from app.services import email_dispatch

    blocked = await email_dispatch.check_send_allowed(db, to_email)
    if blocked is not None:
        logger.warning(
            "meeting agent: %s to %s not sent (%s): %s",
            kind, to_email, blocked.get("status"), blocked.get("reason"),
        )
        return False, None

    slot = await email_dispatch.claim_send_volume(db, account, refusable=refusable)
    if slot.blocked is not None:
        logger.info(
            "meeting agent: %s to %s held (%s): %s",
            kind, to_email, slot.blocked.get("status"), slot.blocked.get("reason"),
        )
        return False, None
    try:
        result = sender(account, to_email=to_email, **kwargs)
    except Exception as exc:  # noqa: BLE001 — record the attempt, don't crash the sweep
        await email_dispatch.settle_send_volume(db, slot, sent=False)
        logger.warning("meeting agent: %s send failed to %s: %s", kind, to_email, exc)
        return False, None

    # ``GmailSender.send_email`` **returns** ``{"status": "error"}`` for an
    # expired token or a 4xx from the API — it does not raise, so the
    # ``except`` above never sees the most common way a send fails. Reporting
    # that as ``True`` would put the ok/not-ok answer back where it was: the
    # accounting would correctly release the slot and record nothing, and the
    # caller would still persist a ``proposed`` meeting for a message nobody
    # received. One outcome, one answer — ``landed`` is it.
    #
    # A test double returning something that isn't a dict, or a dict with no
    # ``status``, reads as landed; that is the contract every existing spy in
    # the suite was written against.
    landed = not (isinstance(result, dict) and result.get("status") not in (None, "sent"))
    await email_dispatch.settle_send_volume(db, slot, sent=landed)
    await email_dispatch.record_account_send(db, account, sent=landed)
    if landed:
        _record_sent_mail(
            db, account, to_email=to_email, contact_id=contact_id,
            result=result, **kwargs,
        )
    else:
        logger.warning(
            "meeting agent: %s to %s returned %s: %s",
            kind, to_email, result.get("status"), result.get("detail"),
        )
    return landed, result


def _record_sent_mail(
    db: AsyncSession,
    account: GmailAccount,
    *,
    to_email: str,
    contact_id,
    result: object,
    subject: str = "",
    body_html: str = "",
    thread_id: Optional[str] = None,
    **_ignored,
) -> None:
    """Write the ``EmailSend`` row for a message this agent put on the wire.

    This agent wrote to *neither* send table. Per "Sent mail lives in two
    tables", anything answering "what did we send?" reads both — and this was a
    third place, read by nothing:

    * **The unified inbox showed the reply and not the proposal.** A prospect
      answering "Tuesday works" appeared as a thread whose opening message does
      not exist — the exact symptom that split caused before.
    * **The reply might not have been ingested at all.**
      ``_is_outreach_reply`` decides whether an inbound message is outreach or
      inbox noise, and its three tests are a thread we started, a message id we
      recorded, and an address we have emailed. All three read ``EmailSend`` or
      an enrollment. A proposal on a *fresh* thread to a contact with no live
      enrollment matched none of them, so the reply was dropped as noise — and
      the booking sweep, which polls ``EmailReply`` by thread, waited for a
      reply that was never recorded. The agent's own loop depended on a row it
      wasn't writing.
    * **The daily cap's seed didn't see it.** ``_db_sent_today`` counts rows;
      a send with no row is one the counter forgets at the next reseed.

    Synchronous and unflushed on purpose: the caller is mid-transaction and owns
    the commit.
    """
    sent = result if isinstance(result, dict) else {}
    db.add(EmailSend(
        contact_id=contact_id,
        to_email=to_email,
        subject=(subject or "")[:500],
        body_html=body_html,
        status="sent",
        sent_at=datetime.now(timezone.utc),
        gmail_message_id=sent.get("gmail_message_id"),
        gmail_thread_id=sent.get("gmail_thread_id") or thread_id,
        from_email=account.email,
        sender_account_id=account.id,
        user_id=getattr(account, "user_id", None),
    ))


async def propose_meeting(
    db: AsyncSession,
    *,
    agent: MeetingBookingAgent,
    contact: Optional[Contact],
    reply: Optional[EmailReply],
    company: Optional[Company] = None,
    now: Optional[datetime] = None,
    ai_service=None,
    busy_provider: Optional[Callable] = None,
    send_email: Optional[Callable] = None,
) -> Optional[BookedMeeting]:
    """Find slots, email a proposal, and create a ``proposed`` BookedMeeting.

    Returns the meeting on success, or ``None`` when there's nothing to do (no
    calendar account configured, no contact email, or no open slots). Side
    effects (free/busy fetch, email send) are injected with real defaults so the
    call is fully testable.
    """
    now = now or datetime.now(timezone.utc)
    account = await _load_calendar_account(db, agent)
    if account is None:
        logger.info("meeting agent: no calendar account configured — skipping proposal")
        return None
    to_email = (contact.email if contact else None) or (reply.from_email if reply else None)
    if not to_email:
        logger.info("meeting agent: no recipient email — skipping proposal")
        return None

    # Checked here as well as in _guarded_send, because this is the one call
    # that *creates* a meeting: a proposal whose mail is blocked would leave a
    # ``proposed`` row for the 15-minute sweep to keep polling for a reply to a
    # message nobody received. Nothing to propose, so nothing to record.
    from app.services import email_dispatch

    blocked = await email_dispatch.check_send_allowed(db, to_email)
    if blocked is not None:
        logger.info(
            "meeting agent: no proposal to %s (%s): %s",
            to_email, blocked.get("status"), blocked.get("reason"),
        )
        return None

    cfg = SlotConfig.from_agent(agent)
    tz = _tz(cfg.timezone)

    # Free/busy is best-effort: on error we treat the window as free rather than
    # refuse to schedule. Runs the blocking Google client off the event loop.
    busy: list[tuple[datetime, datetime]] = []
    provider = busy_provider or _default_busy_provider
    try:
        busy = await provider(
            account,
            start=now,
            end=now + timedelta(days=max(1, cfg.window_days) + 1),
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("meeting agent: free/busy fetch failed (%s) — assuming free", exc)
        busy = []

    daily_counts = await _daily_confirmed_counts(db, tz)
    slots = generate_slots(cfg, now=now, busy=busy, daily_counts=daily_counts)
    if not slots:
        logger.info("meeting agent: no open slots in the window — skipping proposal")
        return None

    rep_name = account.display_name or ""
    text, html = build_proposal_text(contact, slots, rep_name=rep_name)

    title = f"Intro call with {company.name}" if company else "Intro call"
    meeting = BookedMeeting(
        contact_id=contact.id if contact else None,
        sender_account_id=account.id,
        title=title[:255],
        timezone=cfg.timezone,
        status="proposed",
        proposed_slots=slots,
        conversation_thread=[
            {"role": "contact", "text": (reply.body_text or reply.snippet or "") if reply else "", "at": now.isoformat()},
            {"role": "agent", "text": text, "at": now.isoformat()},
        ],
    )

    subject = f"Re: {reply.subject}" if reply and reply.subject else "Let's find a time"
    sender = send_email or _default_gmail_send
    ok, sent = await _guarded_send(
        db, sender, account,
        to_email=to_email,
        kind="proposal",
        contact_id=contact.id if contact else None,
        subject=subject[:255],
        body_html=html,
        body_text=text,
        thread_id=reply.gmail_thread_id if reply else None,
        in_reply_to=reply.gmail_message_id if reply else None,
        references=reply.gmail_message_id if reply else None,
    )
    if not ok:
        # The row is added only now, and only if the mail went. This is the
        # same reasoning as the pre-check above — a ``proposed`` meeting whose
        # proposal never arrived is a row the 15-minute sweep polls forever for
        # a reply to a message nobody received — extended to the outcomes the
        # pre-check cannot see: a dead Gmail token, and the volume caps, which
        # decline *routinely* rather than exceptionally. Returning None lets the
        # caller propose again once there is allowance to send it with.
        logger.info(
            "meeting agent: proposal to %s did not go out — no meeting recorded",
            to_email,
        )
        return None

    db.add(meeting)
    await db.flush()

    if isinstance(sent, dict):
        meeting.gmail_thread_id = sent.get("gmail_thread_id") or (reply.gmail_thread_id if reply else None)
        meeting.gmail_message_id = sent.get("gmail_message_id")

    logger.info(
        "meeting agent: proposed %d slot(s) to %s (meeting %s)", len(slots), to_email, meeting.id
    )
    return meeting


async def _default_busy_provider(account: GmailAccount, *, start: datetime, end: datetime):
    """Fetch busy intervals via Google, off the event loop; [] on error."""
    import asyncio

    result = await asyncio.to_thread(
        google_calendar.get_busy_intervals, account, start=start, end=end
    )
    return result.get("busy", []) if isinstance(result, dict) else []


async def repropose_meeting(
    db: AsyncSession,
    *,
    meeting: BookedMeeting,
    agent: MeetingBookingAgent,
    contact: Optional[Contact] = None,
    now: Optional[datetime] = None,
    busy_provider: Optional[Callable] = None,
    send_email: Optional[Callable] = None,
) -> bool:
    """Offer a fresh set of slots when the prospect asked for a different time.

    Regenerates open slots (excluding the ones already offered), replaces
    ``proposed_slots``, appends the new proposal to the thread, and emails it in
    the same thread. Returns True if a new proposal was sent; False when no fresh
    slot could be found (the human is then left to take over). Keeps the meeting
    in ``proposed``.
    """
    now = now or datetime.now(timezone.utc)
    account = await _load_calendar_account(db, agent)
    if account is None:
        return False
    if contact is None and meeting.contact_id is not None:
        contact = (
            await db.execute(select(Contact).where(Contact.id == meeting.contact_id))
        ).scalar_one_or_none()
    to_email = contact.email if contact else None
    if not to_email:
        return False

    cfg = SlotConfig.from_agent(agent)
    tz = _tz(cfg.timezone)
    provider = busy_provider or _default_busy_provider
    try:
        busy = await provider(account, start=now, end=now + timedelta(days=max(1, cfg.window_days) + 1))
    except Exception:  # noqa: BLE001
        busy = []

    daily_counts = await _daily_confirmed_counts(db, tz)
    already = {s.get("start") for s in (meeting.proposed_slots or [])}
    fresh = [s for s in generate_slots(cfg, now=now, busy=busy, daily_counts=daily_counts, max_slots=MAX_OFFERED_SLOTS + len(already)) if s["start"] not in already]
    fresh = fresh[:MAX_OFFERED_SLOTS]
    if not fresh:
        logger.info("meeting agent: no fresh slots to re-propose for meeting %s", meeting.id)
        return False

    text, html = build_proposal_text(contact, fresh, rep_name=account.display_name or "")
    sender = send_email or _default_gmail_send
    ok, _ = await _guarded_send(
        db, sender, account,
        to_email=to_email,
        kind="re-proposal",
        contact_id=contact.id if contact else None,
        subject=f"Re: {meeting.title}"[:255],
        body_html=html,
        body_text=text,
        thread_id=meeting.gmail_thread_id,
        in_reply_to=meeting.gmail_message_id,
        references=meeting.gmail_message_id,
    )
    if not ok:
        # ``proposed_slots`` is what ``handle_slot_selection`` matches the next
        # reply against. Replacing it before knowing the mail went would leave
        # the meeting offering times the prospect was never shown — so their
        # reply picking one of the *old* ones no longer matches anything, and
        # the agent re-proposes at them instead of confirming. The result is
        # written after the send, not before it.
        logger.info(
            "meeting agent: re-proposal for meeting %s did not go out — slots unchanged",
            meeting.id,
        )
        return False

    meeting.proposed_slots = fresh
    meeting.conversation_thread = [
        *(meeting.conversation_thread or []),
        {"role": "agent", "text": text, "at": now.isoformat(), "kind": "reproposal"},
    ]
    return True


async def confirm_meeting(
    db: AsyncSession,
    *,
    meeting: BookedMeeting,
    agent: MeetingBookingAgent,
    slot: dict,
    contact: Optional[Contact] = None,
    now: Optional[datetime] = None,
    create_event: Optional[Callable] = None,
    send_email: Optional[Callable] = None,
) -> BookedMeeting:
    """Lock a chosen ``slot`` in: set times + status, create the invite, confirm.

    Idempotent enough for the polling task: only a ``proposed`` meeting is
    confirmed. The calendar-event creation is best-effort; a failure still
    confirms the meeting and sends the confirmation email (the times are agreed
    either way).
    """
    now = now or datetime.now(timezone.utc)
    start = datetime.fromisoformat(slot["start"])
    end = datetime.fromisoformat(slot.get("end") or "") if slot.get("end") else (
        start + timedelta(minutes=agent.default_meeting_duration_minutes)
    )
    meeting.start_time = start
    meeting.end_time = end
    meeting.status = "confirmed"

    account = await _load_calendar_account(db, agent)
    to_email = (contact.email if contact else None)
    if to_email is None and contact is None and meeting.contact_id is not None:
        contact = (
            await db.execute(select(Contact).where(Contact.id == meeting.contact_id))
        ).scalar_one_or_none()
        to_email = contact.email if contact else None

    if account is not None:
        creator = create_event or google_calendar.create_event
        try:
            import asyncio

            event = await asyncio.to_thread(
                creator,
                account,
                summary=meeting.title,
                start=start,
                end=end,
                attendee_emails=[to_email] if to_email else None,
                description=meeting.description or "",
            )
            if isinstance(event, dict) and event.get("event_id"):
                meeting.calendar_event_id = event["event_id"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("meeting agent: calendar event create failed: %s", exc)

    text, html = build_confirmation_text(
        contact, meeting, template=agent.confirmation_template,
        rep_name=(account.display_name if account else "") or "",
    )
    meeting.conversation_thread = [
        *(meeting.conversation_thread or []),
        {"role": "agent", "text": text, "at": now.isoformat(), "kind": "confirmation"},
    ]
    if account is not None and to_email:
        sender = send_email or _default_gmail_send
        await _guarded_send(
            db, sender, account,
            to_email=to_email,
            kind="confirmation",
            contact_id=contact.id if contact else None,
            # The prospect has picked a slot and the invite is on their
            # calendar. A cap that swallows the confirmation leaves them with a
            # meeting and no word from us — worse than the volume it saves.
            refusable=False,
            subject=f"Confirmed: {meeting.title}"[:255],
            body_html=html,
            body_text=text,
            thread_id=meeting.gmail_thread_id,
            in_reply_to=meeting.gmail_message_id,
            references=meeting.gmail_message_id,
        )

    logger.info("meeting agent: confirmed meeting %s for %s", meeting.id, meeting.start_time)
    return meeting


async def send_reminder(
    db: AsyncSession,
    *,
    meeting: BookedMeeting,
    agent: MeetingBookingAgent,
    contact: Optional[Contact] = None,
    now: Optional[datetime] = None,
    send_email: Optional[Callable] = None,
) -> bool:
    """Send the pre-meeting reminder once and stamp ``reminder_sent_at``.

    Returns True if a reminder was sent. Idempotent: a meeting that already has
    ``reminder_sent_at`` is skipped by the caller, and this stamps it before the
    send so a retry can't double-send.
    """
    now = now or datetime.now(timezone.utc)
    account = await _load_calendar_account(db, agent)
    if contact is None and meeting.contact_id is not None:
        contact = (
            await db.execute(select(Contact).where(Contact.id == meeting.contact_id))
        ).scalar_one_or_none()
    to_email = contact.email if contact else None

    meeting.reminder_sent_at = now
    text, html = build_reminder_text(
        contact, meeting, template=agent.reminder_template,
        rep_name=(account.display_name if account else "") or "",
    )
    meeting.conversation_thread = [
        *(meeting.conversation_thread or []),
        {"role": "agent", "text": text, "at": now.isoformat(), "kind": "reminder"},
    ]
    if account is None or not to_email:
        return False
    sender = send_email or _default_gmail_send
    ok, _ = await _guarded_send(
        db, sender, account,
        to_email=to_email,
        kind="reminder",
        contact_id=contact.id if contact else None,
        # ``reminder_sent_at`` is stamped above, before the send, so a retry
        # can't double-send — which means a refusal here is permanent silence
        # about a meeting starting within the hour. Counted, never refused.
        refusable=False,
        subject=f"Reminder: {meeting.title}"[:255],
        body_html=html,
        body_text=text,
        thread_id=meeting.gmail_thread_id,
        in_reply_to=meeting.gmail_message_id,
        references=meeting.gmail_message_id,
    )
    return ok


async def on_interested_reply(
    db: AsyncSession,
    *,
    reply: EmailReply,
    contact: Optional[Contact],
    company: Optional[Company] = None,
    ai_service=None,
    now: Optional[datetime] = None,
    busy_provider: Optional[Callable] = None,
    send_email: Optional[Callable] = None,
) -> Optional[BookedMeeting]:
    """Hook from reply classification: propose a meeting on a positive reply.

    No-op (returns ``None``) unless the org has an *active* booking agent AND the
    reply reads as wanting to meet (or is at least positively interested). The
    caller invokes this inside its own try/except, so any failure here is
    contained and never disrupts reply routing.
    """
    agent = await get_active_agent(db)
    if agent is None:
        return None

    # Don't re-propose if this contact already has a live meeting in flight.
    if contact is not None:
        existing = (
            await db.execute(
                select(BookedMeeting.id).where(
                    BookedMeeting.contact_id == contact.id,
                    BookedMeeting.status.in_(("proposed", "confirmed")),
                ).limit(1)
            )
        ).first()
        if existing is not None:
            logger.info("meeting agent: contact %s already has a live meeting — skipping", contact.id)
            return None

    reply_text = reply.body_text or reply.snippet or ""
    verdict = await classify_reply_intent(reply_text, ai_service=ai_service)
    # Only meeting-seeking (or clearly positive) replies get a proposal. An
    # info-first, neutral, negative, or opt-out reply is left for the human.
    if verdict["intent"] != INTENT_POSITIVE_MEETING:
        logger.info(
            "meeting agent: reply %s intent=%s — no proposal", reply.id, verdict["intent"]
        )
        return None

    return await propose_meeting(
        db,
        agent=agent,
        contact=contact,
        reply=reply,
        company=company,
        now=now,
        ai_service=ai_service,
        busy_provider=busy_provider,
        send_email=send_email,
    )
