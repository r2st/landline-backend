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

    # AWS SES (fallback email transport when no Gmail account is connected)
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    ses_sender_email: str = "landline@apprendtechnologies.com"

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

    # JWT auth
    jwt_secret_key: str = ""  # Falls back to secret_key or token_encryption_key
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_minutes: int = 10080  # 7 days

    # Base URL for email tracking pixel / click-redirect endpoints
    tracking_base_url: str = "http://localhost:8000"

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
    max_emails_per_day: int = 50                        # Max emails per day across all sequences
    min_days_between_followups: int = 3                 # Minimum gap between follow-ups to same contact
    max_emails_per_contact_per_month: int = 8           # Hard cap per contact per 30 days
    warmup_enabled: bool = True                         # Gradual ramp-up
    warmup_daily_limit_week1: int = 10                  # Week 1 daily limit
    warmup_daily_limit_week2: int = 25                  # Week 2 daily limit
    warmup_daily_limit_week3: int = 40                  # Week 3+ daily limit
    warmup_start_date: str = ""                         # ISO date when warmup started

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
