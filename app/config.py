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
    openrouter_model: str = "meta-llama/llama-3.1-8b-instruct:free"

    # Apollo.io
    apollo_api_key: str = ""
    apollo_base_url: str = "https://api.apollo.io/v1"

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

    # Scheduling link (Calendly or similar) — included in auto-replies when set
    scheduling_link: str = ""

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
    prospecting_auto_enroll_threshold: int = 75    # fit >= → auto-enroll into outreach
    prospecting_review_threshold: int = 50         # fit >= → human review queue
    # Auto-pause the agent if the rolling enroll→engaged conversion drops below
    # this fraction (guardrail against enrolling junk). 0 disables the check.
    prospecting_min_conversion_rate: float = 0.0

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
