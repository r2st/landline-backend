"""Smart Send-Time Optimization (Feature 6).

Makes the existing global send-time analysis (``send_timing.get_optimal_times``)
*per-recipient*: infer each contact's timezone, learn their personal engagement
pattern, pick their optimal LOCAL window, fall back to a best-practice default
when data is thin, and hand the send queue a ``scheduled_send_at`` so a step is
held until the recipient's best moment — always *within* the business-hours
guardrails the send limiter already enforces, and never past a hard delay cap.

Everything here is fail-safe: any resolution/computation problem degrades to the
platform-default timezone and the best-practice window, and the delay cap
guarantees optimization can never stall a sequence (warm-up caps and the
danger-signal pause still take precedence in the queue).

Day-of-week convention throughout is **0=Sunday .. 6=Saturday**, matching
``send_timing``'s ``day_names`` indexing and the ``ContactEngagementProfile``
schema.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.contact_engagement import ContactEngagementProfile
from app.models.outreach import OutreachEnrollment, OutreachEvent

logger = logging.getLogger(__name__)

# Platform default (reporting) timezone — the last-resort fallback and the base
# for the existing send window.
DEFAULT_TZ = "America/Toronto"

# US state / CA province → primary IANA timezone. Multi-zone states map to their
# predominant zone (a heuristic — good enough to avoid a 3 AM send). Keyed by the
# 2-letter code AND the lowercased full name so either form on a contact/company
# resolves.
_US_STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix",
    "AR": "America/Chicago", "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York", "DE": "America/New_York", "FL": "America/New_York",
    "GA": "America/New_York", "HI": "Pacific/Honolulu", "ID": "America/Boise",
    "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis", "IA": "America/Chicago",
    "KS": "America/Chicago", "KY": "America/New_York", "LA": "America/Chicago",
    "ME": "America/New_York", "MD": "America/New_York", "MA": "America/New_York",
    "MI": "America/Detroit", "MN": "America/Chicago", "MS": "America/Chicago",
    "MO": "America/Chicago", "MT": "America/Denver", "NE": "America/Chicago",
    "NV": "America/Los_Angeles", "NH": "America/New_York", "NJ": "America/New_York",
    "NM": "America/Denver", "NY": "America/New_York", "NC": "America/New_York",
    "ND": "America/Chicago", "OH": "America/New_York", "OK": "America/Chicago",
    "OR": "America/Los_Angeles", "PA": "America/New_York", "RI": "America/New_York",
    "SC": "America/New_York", "SD": "America/Chicago", "TN": "America/Chicago",
    "TX": "America/Chicago", "UT": "America/Denver", "VT": "America/New_York",
    "VA": "America/New_York", "WA": "America/Los_Angeles", "WV": "America/New_York",
    "WI": "America/Chicago", "WY": "America/Denver", "DC": "America/New_York",
}
_US_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
_CA_PROV_TZ = {
    "AB": "America/Edmonton", "BC": "America/Vancouver", "MB": "America/Winnipeg",
    "NB": "America/Moncton", "NL": "America/St_Johns", "NS": "America/Halifax",
    "NT": "America/Yellowknife", "NU": "America/Iqaluit", "ON": "America/Toronto",
    "PE": "America/Halifax", "QC": "America/Toronto", "SK": "America/Regina",
    "YT": "America/Whitehorse",
}
_CA_PROV_NAMES = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB",
    "new brunswick": "NB", "newfoundland and labrador": "NL", "newfoundland": "NL",
    "nova scotia": "NS", "northwest territories": "NT", "nunavut": "NU",
    "ontario": "ON", "prince edward island": "PE", "quebec": "QC", "québec": "QC",
    "saskatchewan": "SK", "yukon": "YT",
}
# ccTLD (last domain label) → IANA timezone — a weak, last-resort signal for the
# rare non-North-American contact where no geography is known.
_CCTLD_TZ = {
    "uk": "Europe/London", "gb": "Europe/London", "ie": "Europe/Dublin",
    "de": "Europe/Berlin", "fr": "Europe/Paris", "es": "Europe/Madrid",
    "it": "Europe/Rome", "nl": "Europe/Amsterdam", "se": "Europe/Stockholm",
    "ch": "Europe/Zurich", "au": "Australia/Sydney", "nz": "Pacific/Auckland",
    "in": "Asia/Kolkata", "sg": "Asia/Singapore", "jp": "Asia/Tokyo",
    "br": "America/Sao_Paulo", "ca": "America/Toronto", "us": "America/New_York",
}

_WEEKDAYS = {1, 2, 3, 4, 5}  # Mon..Fri under the 0=Sunday convention.


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce a possibly-naive datetime to a UTC-aware one (SQLite loses tz)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_valid_tz(name: Optional[str]) -> bool:
    if not name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def dow_sunday0(dt: datetime) -> int:
    """Day of week with 0=Sunday..6=Saturday (Python's weekday() is 0=Monday)."""
    return (dt.weekday() + 1) % 7


def _state_tz(state: Optional[str], country: Optional[str]) -> Optional[str]:
    """Map a US/CA state/province (code or full name) to an IANA timezone."""
    if not state:
        return None
    raw = state.strip()
    code = raw.upper()
    name = raw.lower()
    country_norm = (country or "").strip().upper()

    # Prefer the country hint, but fall back to trying both tables so a bare
    # "CA" (California vs Canada ambiguity aside) still resolves.
    if country_norm in ("CA", "CANADA"):
        prov = _CA_PROV_TZ.get(code) or _CA_PROV_TZ.get(_CA_PROV_NAMES.get(name, ""))
        if prov:
            return prov
    if country_norm in ("US", "USA", "UNITED STATES"):
        st = _US_STATE_TZ.get(code) or _US_STATE_TZ.get(_US_STATE_NAMES.get(name, ""))
        if st:
            return st
    # No / unknown country → try US first (dominant), then CA provinces by name.
    if code in _US_STATE_TZ:
        return _US_STATE_TZ[code]
    if name in _US_STATE_NAMES:
        return _US_STATE_TZ[_US_STATE_NAMES[name]]
    if code in _CA_PROV_TZ:
        return _CA_PROV_TZ[code]
    if name in _CA_PROV_NAMES:
        return _CA_PROV_TZ[_CA_PROV_NAMES[name]]
    return None


def _cctld_tz(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    if "." not in domain:
        return None
    tld = domain.rsplit(".", 1)[1]
    return _CCTLD_TZ.get(tld)


class SendTimeOptimizer:
    """Per-recipient timezone resolution, engagement profiling, and windowing."""

    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # Timezone resolution chain
    # ------------------------------------------------------------------
    def resolve_timezone(
        self, contact: Contact, company: Optional[Company] = None
    ) -> tuple[str, str]:
        """Resolve a contact's IANA timezone. Returns ``(tz, source)``.

        Chain (first hit wins): explicit ``contact.timezone`` → contact geo →
        company geo → email-domain ccTLD → platform default.
        """
        # 1. Explicit (already set / previously cached).
        if is_valid_tz(getattr(contact, "timezone", None)):
            return contact.timezone, "explicit"

        # 2. Contact geography (US/CA state → zone).
        tz = _state_tz(getattr(contact, "state", None), getattr(contact, "country", None))
        if tz:
            return tz, "contact_geo"

        # 3. Company geography.
        if company is not None:
            tz = _state_tz(
                getattr(company, "state_province", None), getattr(company, "country", None)
            )
            if tz:
                return tz, "company_geo"

        # 4. Email/domain ccTLD heuristic (weak, non-NA last resort).
        tz = _cctld_tz(getattr(contact, "email", None))
        if tz:
            return tz, "domain"

        # 5. Platform default.
        return DEFAULT_TZ, "default"

    # ------------------------------------------------------------------
    # Engagement pattern → profile fields
    # ------------------------------------------------------------------
    def build_profile_fields(
        self, events: Iterable, tz: str
    ) -> dict:
        """Bucket a contact's send/open/reply events into a local (dow, hour) grid.

        ``events`` is any iterable of objects with ``event_type`` and
        ``occurred_at``. ``sent`` events are bucketed by send time; ``opened`` /
        ``clicked`` / ``replied`` by engagement time — when they were active. A
        reply is the strongest signal that a recipient is engaged at a given local
        time, so the peak (dow, hour) is chosen by a weighted engagement score
        (``opens + reply_weight * replies``) while ``opens_total`` stays a raw
        count of positive-engagement events (opens + clicks + replies) that drives
        the min-opens trust threshold. Returns the profile fields ready to persist.
        """
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            zone = ZoneInfo(DEFAULT_TZ)

        reply_weight = max(1, self.settings.smart_timing_reply_weight)
        buckets: dict[str, dict[str, int]] = {}
        opens_total = 0
        for ev in events:
            et = getattr(ev, "event_type", None)
            occurred = _aware(getattr(ev, "occurred_at", None))
            if occurred is None or et not in ("sent", "opened", "clicked", "replied"):
                continue
            local = occurred.astimezone(zone)
            key = f"{dow_sunday0(local)}_{local.hour}"
            slot = buckets.setdefault(key, {"sends": 0, "opens": 0, "replies": 0})
            if et == "sent":
                slot["sends"] += 1
            elif et == "replied":
                slot["replies"] += 1
                opens_total += 1
            else:  # opened / clicked
                slot["opens"] += 1
                opens_total += 1

        best_dow: Optional[int] = None
        best_hour: Optional[int] = None
        best_score = 0
        for key, slot in buckets.items():
            # A reply outweighs a mere open when locating the peak engagement hour.
            score = slot["opens"] + reply_weight * slot.get("replies", 0)
            if score > best_score:
                best_score = score
                d, h = key.split("_")
                best_dow, best_hour = int(d), int(h)

        # Confidence ramps with personal signal; below the min-opens threshold it
        # stays low so the window blends toward the cohort/best-practice default.
        min_opens = max(1, self.settings.smart_timing_min_opens)
        confidence = round(min(1.0, opens_total / (min_opens * 3)), 3)

        return {
            "buckets": buckets,
            "opens_total": opens_total,
            "best_dow": best_dow,
            "best_hour": best_hour,
            "confidence": confidence,
        }

    # ------------------------------------------------------------------
    # Optimal window
    # ------------------------------------------------------------------
    def compute_optimal_window(
        self,
        *,
        tz: str,
        best_dow: Optional[int] = None,
        best_hour: Optional[int] = None,
        opens_total: int = 0,
    ) -> dict:
        """Compute the recipient's optimal LOCAL send window.

        Strong personal data → a short window around their best hour on their
        best day. Thin/no data → the best-practice default (Tue-Thu 9-11 AM).
        The result is ALWAYS intersected with the business-hours send window and
        (when configured) restricted to weekdays, so smart timing can only ever
        pick *within* the allowed guardrails.
        """
        s = self.settings
        biz_start = s.send_window_start_hour
        biz_end = s.send_window_end_hour

        strong = opens_total >= s.smart_timing_min_opens and best_hour is not None
        if strong:
            hour_start = best_hour
            hour_end = best_hour + s.smart_timing_personal_window_hours
            dows = [best_dow] if best_dow is not None else list(s.smart_timing_default_dows)
            source = "personal"
            confidence = round(min(1.0, opens_total / (max(1, s.smart_timing_min_opens) * 3)), 3)
        else:
            hour_start = s.smart_timing_default_hour_start
            hour_end = s.smart_timing_default_hour_end
            dows = list(s.smart_timing_default_dows)
            source = "cohort" if opens_total > 0 else "default"
            confidence = round(min(0.5, opens_total / max(1, s.smart_timing_min_opens) * 0.5), 3)

        # Intersect the hour range with the business-hours window.
        hs = max(hour_start, biz_start)
        he = min(hour_end, biz_end)
        if hs >= he:
            # The personal best fell entirely outside business hours — fall back
            # to the default window clamped to the business window.
            hs = max(s.smart_timing_default_hour_start, biz_start)
            he = min(s.smart_timing_default_hour_end, biz_end)
            if hs >= he:  # pathological config → whole business window
                hs, he = biz_start, biz_end
            source = "default"

        # Restrict preferred days to weekdays when the window is weekday-only.
        allowed = set(dows)
        if s.send_window_weekdays_only:
            allowed &= _WEEKDAYS
            if not allowed:
                allowed = set(s.smart_timing_default_dows) & _WEEKDAYS
            if not allowed:
                allowed = set(_WEEKDAYS)

        return {
            "tz": tz,
            "dow_preference": sorted(allowed),
            "hour_start": hs,
            "hour_end": he,
            "confidence": confidence,
            "source": source,
        }

    # ------------------------------------------------------------------
    # Next scheduled slot
    # ------------------------------------------------------------------
    def next_send_slot(
        self, *, tz: str, window: dict, due_at: datetime, now: datetime
    ) -> datetime:
        """Earliest UTC instant inside ``window`` that is >= max(now, due_at).

        Capped at ``now + max_smart_delay_hours`` so a hard-to-satisfy window can
        never stall a sequence: if the next real slot is past the cap, the cap
        itself is returned (the queue's business-hours guard still prevents an
        actually-out-of-window send at that instant).
        """
        now = _aware(now)
        cap = now + timedelta(hours=self.settings.max_smart_delay_hours)
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            zone = ZoneInfo(DEFAULT_TZ)

        search_start = max(now, _aware(due_at))
        allowed = set(window.get("dow_preference") or [])
        hs = window.get("hour_start")
        he = window.get("hour_end")
        if not allowed or hs is None or he is None or hs >= he:
            return cap  # nothing valid to aim at → don't stall

        local = search_start.astimezone(zone)

        def _in_window(dt_local: datetime) -> bool:
            return dow_sunday0(dt_local) in allowed and hs <= dt_local.hour < he

        # Already inside an allowed window → send now.
        if _in_window(local) and search_start <= cap:
            return search_start

        for day_offset in range(0, 8):
            day_local = local + timedelta(days=day_offset)
            if dow_sunday0(day_local) not in allowed:
                continue
            candidate_local = day_local.replace(hour=hs, minute=0, second=0, microsecond=0)
            candidate_utc = candidate_local.astimezone(timezone.utc)
            if candidate_utc < search_start:
                # Window opened earlier today and we're past its start — the
                # in-window check above already handled "still inside"; here we're
                # past the close, so move to the next allowed day.
                continue
            if candidate_utc > cap:
                return cap
            return candidate_utc
        return cap

    # ------------------------------------------------------------------
    # A/B arm assignment
    # ------------------------------------------------------------------
    def assign_arm(self, enrollment_id) -> str:
        """Deterministically split an enrollment into optimized vs control.

        Deterministic (hash of the id) rather than RNG so a re-run can't flip an
        enrollment's arm and the split stays even at low N.
        """
        digest = hashlib.sha1(str(enrollment_id).encode()).hexdigest()
        frac = (int(digest[:8], 16) % 10_000) / 10_000
        return "optimized" if frac < self.settings.smart_timing_optimized_fraction else "control"

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    async def _get_profile(
        self, db: AsyncSession, contact_id
    ) -> Optional[ContactEngagementProfile]:
        return (
            await db.execute(
                select(ContactEngagementProfile).where(
                    ContactEngagementProfile.contact_id == contact_id
                )
            )
        ).scalar_one_or_none()

    async def _resolve_and_cache_tz(
        self, db: AsyncSession, contact: Contact
    ) -> tuple[str, str]:
        """Resolve the contact's tz, caching it to ``contact.timezone`` + profile."""
        if is_valid_tz(getattr(contact, "timezone", None)):
            return contact.timezone, "explicit"

        company = None
        if getattr(contact, "company_id", None) is not None:
            company = (
                await db.execute(select(Company).where(Company.id == contact.company_id))
            ).scalar_one_or_none()

        tz, source = self.resolve_timezone(contact, company)
        # Cache to the authoritative column so the send window honours it and we
        # never re-resolve; record the source on the profile for auditing.
        contact.timezone = tz
        profile = await self._get_profile(db, contact.id)
        if profile is None:
            profile = ContactEngagementProfile(contact_id=contact.id)
            db.add(profile)
        profile.tz = tz
        profile.tz_source = source
        return tz, source

    async def should_defer(
        self,
        db: AsyncSession,
        enrollment: OutreachEnrollment,
        contact: Contact,
        now: datetime,
        due_at: datetime,
    ) -> bool:
        """Decide whether to HOLD a due step for a better moment.

        Returns ``True`` to defer (don't dispatch this tick), ``False`` to send
        now. Assigns the A/B arm on first touch, resolves/caches the timezone,
        and stamps ``scheduled_send_at`` on the enrollment. ``control`` arm and a
        disabled feature both short-circuit to today's naive behaviour.
        """
        if not self.settings.smart_send_timing_enabled:
            return False

        now = _aware(now)
        due_at = _aware(due_at) or now

        if enrollment.timing_arm is None:
            enrollment.timing_arm = self.assign_arm(enrollment.id)
        if enrollment.timing_arm == "control":
            return False

        try:
            tz, _source = await self._resolve_and_cache_tz(db, contact)
            profile = await self._get_profile(db, contact.id)
            window = self.compute_optimal_window(
                tz=tz,
                best_dow=getattr(profile, "best_dow", None) if profile else None,
                best_hour=getattr(profile, "best_hour", None) if profile else None,
                opens_total=getattr(profile, "opens_total", 0) if profile else 0,
            )
            sched = _aware(enrollment.scheduled_send_at)
            # (Re)compute when unset or stale (left over from a prior step, i.e.
            # earlier than this step's due time).
            if sched is None or sched < due_at:
                sched = self.next_send_slot(tz=tz, window=window, due_at=due_at, now=now)
                enrollment.scheduled_send_at = sched
            return _aware(enrollment.scheduled_send_at) > now
        except Exception:  # pragma: no cover - never block a send on optimizer error
            logger.exception(
                "Smart send-time scheduling failed for enrollment %s — sending now",
                enrollment.id,
            )
            return False


    # ------------------------------------------------------------------
    # Nightly refresh
    # ------------------------------------------------------------------
    async def refresh_profiles(self, db: AsyncSession, limit: Optional[int] = None) -> dict:
        """Recompute every enrolled contact's engagement profile + backfill tz.

        Called nightly. For each contact with at least one enrollment: resolve
        (and backfill) their timezone, aggregate their send/open events into
        local (dow, hour) buckets, and upsert the profile. Fail-safe per contact
        — one bad row never aborts the run.
        """
        now = datetime.now(timezone.utc)
        rows = (
            await db.execute(select(OutreachEnrollment.contact_id).distinct())
        ).scalars().all()
        contact_ids = [c for c in rows if c is not None]
        if limit is not None:
            contact_ids = contact_ids[:limit]

        updated = 0
        tz_backfilled = 0
        for cid in contact_ids:
            try:
                contact = (
                    await db.execute(select(Contact).where(Contact.id == cid))
                ).scalar_one_or_none()
                if contact is None:
                    continue

                had_tz = is_valid_tz(getattr(contact, "timezone", None))
                company = None
                if not had_tz and getattr(contact, "company_id", None) is not None:
                    company = (
                        await db.execute(
                            select(Company).where(Company.id == contact.company_id)
                        )
                    ).scalar_one_or_none()
                tz, source = self.resolve_timezone(contact, company)
                if not had_tz:
                    contact.timezone = tz  # backfill the authoritative cache
                    tz_backfilled += 1

                events = (
                    await db.execute(
                        select(OutreachEvent)
                        .join(
                            OutreachEnrollment,
                            OutreachEvent.enrollment_id == OutreachEnrollment.id,
                        )
                        .where(OutreachEnrollment.contact_id == cid)
                    )
                ).scalars().all()
                fields = self.build_profile_fields(events, tz)

                profile = await self._get_profile(db, cid)
                if profile is None:
                    profile = ContactEngagementProfile(contact_id=cid)
                    db.add(profile)
                profile.tz = tz
                profile.tz_source = source if not had_tz else "explicit"
                profile.buckets = fields["buckets"]
                profile.opens_total = fields["opens_total"]
                profile.best_dow = fields["best_dow"]
                profile.best_hour = fields["best_hour"]
                profile.confidence = fields["confidence"]
                profile.last_computed = now
                updated += 1
            except Exception:  # pragma: no cover - never abort the whole run
                logger.exception("Failed to refresh engagement profile for contact %s", cid)

        return {"profiles_updated": updated, "timezones_backfilled": tz_backfilled}


_optimizer: Optional[SendTimeOptimizer] = None


def get_send_time_optimizer() -> SendTimeOptimizer:
    global _optimizer
    if _optimizer is None:
        _optimizer = SendTimeOptimizer()
    return _optimizer
