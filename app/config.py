"""Application configuration via pydantic-settings."""

from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database
    database_url: str = "postgresql+asyncpg://landline:landline_secret@localhost:5432/landline_db"

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # Short-TTL response cache for hot dashboard/analytics aggregates. Disabled in
    # the test suite (see conftest) so cached rows can't leak across tests.
    response_cache_enabled: bool = True
    response_cache_ttl: int = 30  # seconds

    # AWS SES (fallback email transport when no Gmail account is connected).
    # SES sends from its own DNS-verified domain, so this stays on the
    # SES-verified apprendtechnologies.com identity even though the *primary*
    # sender is the connected apprend.tech Gmail account (see primary_sender_email).
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    ses_sender_email: str = "landline@apprendtechnologies.com"

    # The canonical primary sender for outreach. Real sending goes through the
    # connected Gmail OAuth account whose address matches this (Google Workspace
    # signs DKIM for the domain automatically), so this is both the operator's
    # intended default sender and the domain reported by the deliverability /
    # test-send endpoints before any Gmail account is connected.
    primary_sender_email: str = "suman.das@apprend.tech"

    # Google / Gmail OAuth (per-user email sending)
    # Create these in Google Cloud Console — see deploy/GOOGLE-OAUTH-SETUP.md
    google_client_id: str = ""
    google_client_secret: str = ""
    # Must EXACTLY match an Authorized redirect URI configured in Google Cloud.
    # e.g. https://landline-api.apprendtechnologies.com/api/v1/gmail/oauth/callback
    google_oauth_redirect_uri: str = "http://localhost:8000/api/v1/gmail/oauth/callback"
    # Postmaster Tools OAuth callback (P2 audit). Empty = derive from the Gmail
    # redirect by swapping the path (see postmaster_oauth._redirect_uri).
    postmaster_oauth_redirect_uri: str = ""
    # Where to send the user's browser back to after a connect attempt (the frontend).
    # The OAuth callback closes the popup and notifies this origin via postMessage.
    frontend_url: str = "http://localhost:5173"
    # Fernet key used to encrypt stored OAuth refresh/access tokens at rest.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_encryption_key: str = ""

    # Claude API
    anthropic_api_key: str = ""

    # --- AI provider selection ---
    # Which LLM backend AIService uses. "auto" (default) picks OpenRouter when an
    # OPENROUTER_API_KEY is set, else Anthropic — so setting the key is enough to
    # fail over when Anthropic credits are depleted. Force a backend with
    # AI_PROVIDER=anthropic or AI_PROVIDER=openrouter.
    ai_provider: str = "auto"
    # OpenRouter (OpenAI-compatible) — a drop-in fallback that can serve free-tier
    # models. Base URL is the OpenAI-compatible endpoint; the default model is a
    # free-tier model so scoring keeps working at zero cost.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # NOTE: a ":free" model id is only valid while OpenRouter actually serves that
    # variant, and they retire them without notice — the previous default here
    # (meta-llama/llama-3.1-8b-instruct:free) had already stopped existing, which
    # 404s every call and looks exactly like an outage: silent template fallback.
    # Check a candidate against https://openrouter.ai/api/v1/models before setting.
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    # Cold-email copy is the quality-sensitive call, so it gets its own knob. It
    # currently resolves to the same model as everything else: on the free tier the
    # daily quota is per-account and shared across models, so a smaller model buys
    # no headroom, and the email system prompt is a long list of *negative*
    # constraints (no buzzwords, no fabricated proof, hold a tone) that small
    # models comply with poorly — an 8B will happily invent the customer story the
    # guardrail bans. Kept separate so email can move to a paid model without
    # dragging scoring/classification along.
    openrouter_email_model: str = "meta-llama/llama-3.3-70b-instruct:free"

    # Apollo.io
    apollo_api_key: str = ""
    apollo_base_url: str = "https://api.apollo.io/v1"
    # Client-side token bucket, so we back off before Apollo has to 429 us.
    apollo_max_requests_per_minute: int = 50
    # Credit guardrails. The gate is checked *before* each call, so an
    # exhausted budget costs nothing. 0 = unlimited (escape hatch).
    apollo_daily_credit_budget: int = 200
    apollo_monthly_credit_budget: int = 5000
    # Notify once when spend crosses this fraction of a budget.
    prospecting_credit_alert_threshold: float = 0.8

    # --- Hunter.io email verification (P3) ---
    # Per-mailbox SMTP verification, layered behind the free syntax + MX check.
    # MX proves a *domain* accepts mail; Hunter proves the *mailbox* exists —
    # which is what keeps a bad Apollo batch from tripping the Feature 2 bounce
    # auto-pause. Empty key = capability disabled; the pipeline degrades to
    # today's MX-only behaviour and never blocks.
    hunter_api_key: str = ""
    hunter_base_url: str = "https://api.hunter.io/v2"
    # A 'deliverable' below this confidence still sends, but is flagged
    # low-confidence and is excluded from the auto-enroll high-confidence band.
    hunter_min_score: int = 80
    # Re-verify a contact whose verification is older than this. People leave;
    # addresses rot. Only applied to contacts we could actually still email.
    hunter_reverify_days: int = 90
    # Guardrail (not billing truth) on Hunter calls per UTC day. 0 = unlimited.
    hunter_daily_credit_budget: int = 100
    # Verify newly-imported contacts before they can enter a sequence.
    hunter_verify_on_import: bool = True
    # Last-resort gate: verify at send time if a contact somehow reached a send
    # unverified. Set False if verification latency ever becomes a bottleneck —
    # the async task still covers the normal path.
    hunter_verify_before_send: bool = True
    # Max contacts accepted by one bulk-verify request, and how many Hunter calls
    # run concurrently within a batch (Hunter rate-limits per-second).
    hunter_max_batch: int = 100
    hunter_concurrency: int = 5

    # --- DIY email pattern discovery ---
    # The free fallback under Hunter's email-finder: derive likely addresses from
    # a name + the employer's domain (see app/services/email_pattern_generator.py).
    # Generation is always free and offline; what needs configuring is how — or
    # whether — a guess gets *proven*, because an unproven guess is never
    # promoted past email_status='unknown'.
    pattern_discovery_enabled: bool = True
    # A Reacher instance (https://reacher.email) — self-hostable, so unlike
    # Hunter it costs no per-address credit. Empty = no probe layer, which
    # degrades to generate-only: candidates are surfaced for a human, never
    # auto-marked valid.
    reacher_url: str = ""
    reacher_api_key: str = ""
    # Raw SMTP RCPT-TO probing as a last resort. **Off by default, deliberately.**
    # Two reasons, both load-bearing: (1) AWS blocks outbound port 25 from EC2
    # unless you file a request, so on this deployment it would simply time out
    # into 'unknown'; (2) probing from a sending IP is the behaviour DNSBLs
    # classify as address harvesting — the exact reputation damage
    # blacklist_check and domain_health exist to catch. Enable only from an IP
    # you do not send from.
    pattern_smtp_fallback_enabled: bool = False
    # MAIL FROM for the probe. A real, monitored address on a domain you own:
    # a bogus sender is itself a spam signal to the receiving MTA.
    pattern_smtp_from_address: str = ""
    # Probes per person. Seven patterns exist, but each probe is an SMTP
    # conversation with a stranger's mail server; stopping early keeps a bulk
    # import from looking like a dictionary attack.
    pattern_max_probes: int = 4

    # --- Google Places SMB discovery (P4) ---
    # Places API (New) — the legacy Places API is deprecated for new projects.
    # Apollo's coverage of small international manufacturers is thin; Places has
    # near-exhaustive local-business coverage. What it does *not* have is emails
    # or named people, so a Places result is a lead on a prospect, not a
    # prospect — see app/services/places_discovery.py. Empty key = capability
    # disabled; searches return 'skipped' and never raise.
    google_places_api_key: str = ""
    google_places_base_url: str = "https://places.googleapis.com/v1"
    # Guardrail on Places calls per UTC day. Broad coverage needs many narrow
    # queries (a grid over geography x category) and each page is billed, so
    # this is the one that stops a scheduled search from quietly compounding.
    # 0 = unlimited.
    google_places_daily_budget: int = 100
    google_places_monthly_budget: int = 2000
    # Client-side token bucket, so we back off before Google has to 429 us.
    google_places_max_requests_per_minute: int = 60
    # Places caps a text search at 20 results/page and 60 results total (3
    # pages). Encoded here so the pagination loop and its test agree on the
    # ceiling rather than both hardcoding it.
    google_places_page_size: int = 20
    google_places_max_results: int = 60

    # --- Multi-channel step execution (P2 audit) ---
    # LinkedIn and phone/SMS channels execute through provider stubs until a
    # real integration lands. An empty credential = "not configured": the
    # handler simulates the touch (status="simulated") instead of hitting an
    # API, so a cadence authored across channels still runs end-to-end.
    linkedin_api_token: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # Census Bureau
    census_api_key: str = ""

    # App
    app_name: str = "Landline"
    app_env: str = "development"

    # API key authentication (empty = disabled for backward compat)
    api_key: str = ""

    # Global sending kill switch — default state when the switch has never been
    # explicitly set in system_settings. Ships True (sending allowed) so existing
    # deployments and tests are unaffected; set SENDING_ENABLED_DEFAULT=false in
    # production so a fresh install stays PAUSED until an operator turns sending on
    # (belt-and-suspenders with the seeded "off" system_settings row, migration 059).
    sending_enabled_default: bool = True

    # JWT auth
    jwt_secret_key: str = ""  # Falls back to secret_key or token_encryption_key
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_minutes: int = 10080  # 7 days

    # Base URL for email tracking pixel / click-redirect endpoints
    tracking_base_url: str = "http://localhost:8000"
    # Instrument outreach *sequence* sends with an open pixel + wrapped links.
    # Without this the sequence path emits no engagement events at all, so the
    # send_condition branches, the optimizer, and per-variant A/B stats all read
    # counts that are structurally zero. Kept behind a flag because a pixel is a
    # (small) deliverability cost: turning it off restores the prior behaviour.
    sequence_tracking_enabled: bool = True

    # --- CAN-SPAM sender identity ---
    # Legal sender name + a FULL physical postal address are required on every
    # commercial email (CAN-SPAM / CASL). ``company_postal_address`` must be a
    # complete address (street/suite or registered PO box + city + region +
    # postal code + country), not just a city. Override via env for the real
    # registered address.
    company_name: str = "Apprend Technologies"
    company_postal_address: str = "Bangalore, Karnataka 560001, India"

    # Secret key for signing tokens (unsubscribe links, etc.)
    secret_key: str = ""
    # Mailbox that receives RFC 2369 mailto: unsubscribe requests. When set, it is
    # added to the List-Unsubscribe header alongside the one-click https URL —
    # Gmail/Yahoo bulk-sender guidelines recommend offering both methods. Leave
    # empty to use the sending account's own (monitored, reply-synced) address;
    # only set this to a mailbox that can actually receive mail (has MX records).
    unsubscribe_mailto: str = ""
    # Only send during these hours (sender-local) on weekdays — outreach that lands
    # at 3 AM reads as a bot and hurts reply rates. 24h clock; set send_window_enabled
    # False to send around the clock.
    send_window_enabled: bool = True
    send_window_start_hour: int = 8       # inclusive, e.g. 8 = 8:00am
    send_window_end_hour: int = 18        # exclusive, e.g. 18 = 6:00pm
    send_window_timezone: str = "America/Toronto"
    send_window_weekdays_only: bool = True
    notification_email: str = ""
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"


    # Anti-spam / sending limits
    max_emails_per_contact_per_sequence: int = 5       # Max steps in any sequence
    max_emails_per_day: int = 100                       # Steady-state daily cap (== fully-warmed week-3 volume)
    min_days_between_followups: int = 3                 # Minimum gap between follow-ups to same contact
    max_emails_per_contact_per_month: int = 8           # Hard cap per contact per 30 days
    warmup_enabled: bool = True                         # Gradual ramp-up
    # Conservative warm-up ramp for a brand-new sending domain/mailbox:
    # 20/day week 1 → 50/day week 2 → 100/day week 3+ (then steady at max_emails_per_day).
    warmup_daily_limit_week1: int = 20                  # Week 1 daily limit
    warmup_daily_limit_week2: int = 50                  # Week 2 daily limit
    warmup_daily_limit_week3: int = 100                 # Week 3+ daily limit
    # ISO date the warm-up ramp starts from. Defaults to the launch date; the
    # persisted system_settings value (auto-stamped on first send, or reset from
    # the UI) takes precedence over this static default.
    warmup_start_date: str = "2026-07-15"

    # Smart auto-reply
    auto_reply_enabled: bool = True                        # Auto-send AI-drafted replies for clear positive intents

    # Autonomous follow-up + re-scoring (Celery beat)
    stale_lead_followup_days: int = 7                      # Active-pipeline lead with no touch in N days → auto follow-up reminder
    stale_lead_followup_stages: List[str] = [             # Stages worth chasing automatically
        "contacted", "engaged", "qualified", "opportunity",
    ]
    auto_followup_batch_limit: int = 50                    # Max reminders created per beat run
    rescore_batch_limit: int = 50                          # Max companies re-scored per beat run

    # Scheduling link (Calendly or similar) — included in auto-replies when set.
    # Also the global fallback for the per-sender {{booking_link}} (Feature 4):
    # sender booking_url → scheduling_link → empty.
    scheduling_link: str = ""

    # --- Calendar Booking Injection (Feature 4) ---
    # Allowlist of scheduler hosts a booking URL may point at. A saved booking_url
    # must be https and its host must equal (or be a subdomain of) one of these —
    # defense against injecting an arbitrary link into outbound mail.
    booking_scheduler_hosts: List[str] = [
        "calendly.com",
        "cal.com",
        "acuityscheduling.com",
        "savvycal.com",
        "hubspot.com",          # meetings.hubspot.com
        "chilipiper.com",
        "youcanbook.me",
        "oncehub.com",          # ...scheduleonce
        "tidycal.com",
        "zcal.co",
        "koalendar.com",
        "outlook.office365.com", # Microsoft Bookings
        "calendar.google.com",   # Google appointment schedules
        "calendar.app.google",
    ]
    # Soft interest signal: how many opens of a contact's mail gate booking-link
    # injection (a repeated open reads as warming interest).
    booking_min_opens: int = 2

    # --- Daily autonomous-activity summary ---
    # Timezone whose calendar day the daily summary covers (the beat runs at
    # 6 PM in this zone). Also drives the day-boundary windowing.
    daily_summary_timezone: str = "Asia/Kolkata"
    # Rewrite the narrative into warmer prose via Claude (falls back to the
    # deterministic rule-based text if the model is unavailable).
    daily_summary_use_ai: bool = True
    # Email the daily digest to notification_email when True.
    daily_summary_email_enabled: bool = False

    # --- Phase 1 AI features ---
    # 1.2 AI-Optimized Sequence Engine: when enabled, the optimizer may adjust
    # timing, rewrite content, switch channel, or skip a step before each send.
    # Falls back to sending the static step verbatim on any failure.
    sequence_optimizer_enabled: bool = True
    # AI-composed outreach: ground the copy in the company's research brief
    # instead of sending static mail-merge. Always falls back to the static
    # template on any failure, so this can be turned off at any time without
    # stalling outreach.
    ai_compose_enabled: bool = True
    # Compose every step, not just the first touch. On: the sequence's ``tone``
    # only shapes copy the copywriter writes, so with this off a sequence spoke
    # in its own voice for one email and in the static templates' voice for the
    # other three. Each step writes under its own prompt hint (see
    # ``ai_compose.STEP_TEMPLATE_TYPES``) so the arc survives — a follow-up is
    # still written as a follow-up, not as a second first touch. The optimizer
    # still rewrites off engagement history afterwards.
    ai_compose_all_steps: bool = True
    # How many AI-composed emails per sequence *step* go to a human before that
    # step is trusted to send them unattended. Cold outreach is irreversible and
    # charged against the sending domain, so the first batch gets read. Counted
    # per step, not per sequence: a quota the first touch can exhaust on its own
    # would let every follow-up the model has ever written send unread.
    ai_compose_review_batch_size: int = 10
    # 1.4 AI Company Research Agent: auto-trigger a research brief when a company
    # is created / before first outreach.
    auto_research_enabled: bool = True
    # 1.5 Conversation Intelligence: batch analysis of active threads.
    conversation_intelligence_enabled: bool = True
    # Threads with activity in the last N days are considered "active" for the
    # analyze_active_threads beat task.
    conversation_active_days: int = 14

    # --- Phase 3 AI features ---
    # 3.1 Autonomous Prospecting Agent.
    prospecting_enabled: bool = True
    prospecting_target_volume: int = 25            # candidates evaluated per run
    prospecting_daily_max: int = 25                # hard cap on companies enrolled per day
    prospecting_auto_enroll_threshold: int = 65    # fit >= → auto-enroll into outreach
    prospecting_review_threshold: int = 50         # fit >= → human review queue
    # Auto-pause the agent if the rolling enroll→engaged conversion drops below
    # this fraction (guardrail against enrolling junk). 0 disables the check.
    prospecting_min_conversion_rate: float = 0.0
    # Places businesses arrive with no email, so the prospector enriches them
    # inline (Hunter, then a scrape) to have anyone to enrol. Both hops are slow
    # and `POST /prospecting/run` awaits the run, so this caps how many a single
    # run will pay for. The nightly sweep in tasks/places_discovery.py picks up
    # whatever is left over, unbounded by the request clock. 0 disables inline
    # enrichment and defers entirely to that sweep.
    prospecting_places_enrich_max: int = 10

    # --- Market policy ---
    # Countries we actually import and prospect into. Narrower than the schema
    # allowlist (``app.constants.ALLOWED_COUNTRIES``) and tunable without a
    # migration. A record whose country is outside this list is **rejected**,
    # never relabelled — a mislabelled company is worse than a rejected one.
    # Must stay a subset of ALLOWED_COUNTRIES or inserts fail the CHECK.
    supported_countries: list[str] = ["US", "CA", "IN"]

    # 3.3 Deliverability Optimizer.
    deliverability_optimizer_enabled: bool = True
    sender_bounce_rate_throttle: float = 0.05      # 7d bounce rate above this → throttle
    sender_complaint_rate_throttle: float = 0.003  # 7d complaint rate above this → throttle
    sender_min_reputation: int = 40                # below this the account is 'critical'

    # Feature 2 — Auto-Pause on Danger Signals (deliverability safety rail).
    # A 5-minute monitor task computes 24h rolling bounce/complaint metrics per
    # sending account and globally, and auto-engages the kill switch on breach.
    danger_signals_enabled: bool = True
    # 24h bounce rate: >5% auto-pauses, >3% warns (needs the min sample below).
    danger_bounce_rate_threshold: float = 0.05
    danger_bounce_rate_warn: float = 0.03
    # 24h spam-complaint rate: >0.1% auto-pauses, >0.05% warns (min sample too).
    danger_complaint_rate_threshold: float = 0.001
    danger_complaint_rate_warn: float = 0.0005
    # Absolute hard-bounce floor in 24h — trips regardless of sample size, so a
    # bad list burns the domain even during low-volume warm-up (rates lie at
    # small N). >=5 auto-pauses, >=3 warns.
    danger_hard_bounce_count_threshold: int = 5
    danger_hard_bounce_count_warn: int = 3
    # Minimum sends in the window before a *rate* trip is trusted (rates are
    # meaningless at low volume — 1 bounce of 20 is 5%). The hard-bounce floor is
    # the low-volume backstop and is NOT gated by this.
    danger_min_sample_size: int = 20

    # 3.4 RAG Knowledge System.
    rag_enabled: bool = True
    rag_top_k: int = 5                             # retrieved chunks per query
    rag_min_similarity: float = 0.05               # drop weak matches below this cosine sim

    # --- Autonomous lead-gen enhancements ---
    # Per-account warm-up: a newly connected mailbox ramps its own daily cap
    # based on how long ago it was connected, independent of the global warm-up
    # date. Applied by send_limiter.account_capacity() alongside the reputation
    # throttle. Disable to fall back to the reputation-only cap.
    account_warmup_enabled: bool = True
    account_warmup_daily_limit_week1: int = 20   # days 0-6 since connected_at
    account_warmup_daily_limit_week2: int = 50   # days 7-13
    account_warmup_daily_limit_week3: int = 100  # days 14-20 (then uncapped by warm-up)
    # Bounce handling: after this many soft bounces a contact is suppressed
    # (treated like a hard bounce). A single hard bounce suppresses immediately.
    soft_bounce_suppress_threshold: int = 3
    # DKIM has no directory record; we probe these common selectors at
    # <selector>._domainkey.<domain> when no explicit selector is supplied.
    dkim_default_selectors: List[str] = ["google", "default", "selector1", "selector2", "k1"]

    # --- Feature 5: Domain Health Dashboard ---
    # DNSBL zones queried per sending domain/IP by the daily domain-health check.
    # IP-based zones (reversed-IP.<zone> A-record lookup). For Gmail domains the
    # sending IP is Google's shared pool, so these are informational only.
    dnsbl_zones: List[str] = ["zen.spamhaus.org", "b.barracudacentral.org", "bl.spamcop.net"]
    # Domain-based zones (<domain>.<zone>) — the meaningful signal for shared-pool
    # senders; these DO count toward the health score.
    dnsbl_domain_zones: List[str] = ["dbl.spamhaus.org"]
    # Spamhaus Data Query Service key (26 chars). Optional. Spamhaus refuses
    # public-mirror queries that arrive via an open/public resolver — which is
    # exactly what our EC2 box uses — answering 127.255.255.254 ("no data for
    # you"), so every spamhaus.org zone currently reports `unknown`. A DQS key
    # authenticates the query and makes the answer real. When set, the
    # spamhaus.org zones above are transparently queried as
    # <key>.<zone>.dq.spamhaus.net (see blacklist_check.dqs_zone); non-Spamhaus
    # zones (Barracuda, SpamCop) are unaffected. Note the free DQS tier is
    # non-commercial-only, so a commercial deployment wants the trial or a paid
    # subscription — see .env.example. Treat the key as a credential.
    spamhaus_dqs_key: str = ""
    # Composite health-score weights (unknown/unverifiable components are dropped
    # from the denominator so the score reflects only what we could verify).
    domain_health_weight_spf: int = 20
    domain_health_weight_dkim: int = 20
    domain_health_weight_dmarc: int = 25
    domain_health_weight_blacklists: int = 30
    # Band boundaries: >= healthy_min → healthy; >= watch_min → watch; else at_risk.
    domain_health_band_healthy_min: int = 90
    domain_health_band_watch_min: int = 70
    # On-demand recheck cooldown (seconds) to be polite to the DNSBL zones.
    domain_health_recheck_cooldown_seconds: int = 60

    # --- Feature 6: Smart Send-Time Optimization ---
    # Master switch. When off, the send queue keeps today's naive next-window
    # timing for every enrollment (no scheduling layer, no A/B split).
    smart_send_timing_enabled: bool = True
    # A contact needs at least this many prior opens before its PERSONAL pattern
    # is trusted; below it we blend to the cohort/best-practice default. Cold
    # lists have 0-2 opens, so the default is load-bearing.
    smart_timing_min_opens: int = 3
    # Hard ceiling on how far past a step's due time smart timing may defer it, so
    # optimization can never stall a sequence. Warm-up caps + danger-pause always
    # take precedence over this.
    max_smart_delay_hours: int = 48
    # Best-practice default window (recipient-local) when personal data is thin:
    # Tue-Thu, 9-11 AM. Days are 0=Sunday..6=Saturday (matches send_timing).
    smart_timing_default_dows: List[int] = [2, 3, 4]   # Tue, Wed, Thu
    smart_timing_default_hour_start: int = 9            # inclusive
    smart_timing_default_hour_end: int = 11            # exclusive
    # Width (hours) of the window built around a recipient's personal best hour.
    smart_timing_personal_window_hours: int = 2
    # How many opens one reply is worth when picking a recipient's best (dow, hour)
    # bucket. A reply is a far stronger "they were engaged at this local time"
    # signal than an open, so it dominates the peak selection. Raw event counts
    # (not this weight) still drive the smart_timing_min_opens trust threshold.
    smart_timing_reply_weight: int = 3
    # Fraction of newly-scheduled enrollments assigned to the 'optimized' arm; the
    # rest are 'control' (naive timing) so the lift can be measured. 0.5 = even.
    smart_timing_optimized_fraction: float = 0.5

    # 3.5 Voice Intelligence.
    voice_intelligence_enabled: bool = True
    # OpenAI key for Whisper transcription (optional — a transcript can also be
    # supplied directly on upload, and analysis works from any transcript).
    openai_api_key: str = ""
    whisper_model: str = "whisper-1"

    # CORS
    cors_origins: List[str] = ["http://localhost:3000", "http://localhost:5173"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors(cls, v):
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return []
            # Accept a JSON array or a plain comma-separated list.
            if value.startswith("["):
                import json
                return json.loads(value)
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return v

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "")


@lru_cache()
def get_settings() -> Settings:
    return Settings()
