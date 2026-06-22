"""Celery application configuration."""

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "landline",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    broker_connection_retry_on_startup=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=300,
    task_time_limit=600,
    task_routes={
        "app.tasks.scraping.*": {"queue": "scraping"},
        "app.tasks.enrichment.*": {"queue": "enrichment"},
        "app.tasks.outreach.*": {"queue": "outreach"},
        "app.tasks.news.*": {"queue": "enrichment"},
        "app.tasks.followups.*": {"queue": "enrichment"},
    },
    beat_schedule={
        "process-outreach-queue": {
            "task": "app.tasks.outreach.process_outreach_queue",
            "schedule": 300.0,  # every 5 minutes
        },
        "check-email-replies": {
            "task": "app.tasks.outreach.check_email_replies",
            "schedule": 600.0,  # every 10 minutes
        },
        "check-company-news": {
            "task": "app.tasks.news.check_company_news_batch",
            "schedule": 21600.0,  # every 6 hours
        },
        "auto-enrich-companies": {
            "task": "app.tasks.enrichment.auto_enrich_companies",
            "schedule": 3600.0,  # every hour
        },
        "auto-score-new-leads": {
            "task": "app.tasks.enrichment.auto_score_leads",
            "schedule": 1800.0,  # every 30 minutes
        },
        "generate-stale-lead-followups": {
            "task": "app.tasks.followups.generate_stale_lead_followups",
            "schedule": 14400.0,  # every 4 hours
        },
        "rescore-active-leads": {
            "task": "app.tasks.followups.rescore_active_leads",
            "schedule": 43200.0,  # twice a day
        },
    },
)

# Explicitly include task modules so autodiscover finds them
celery_app.autodiscover_tasks(["app.tasks"], force=True)

# Force-import all task modules to guarantee registration
import app.tasks.outreach  # noqa: F401
import app.tasks.news  # noqa: F401
import app.tasks.scraping  # noqa: F401
import app.tasks.enrichment  # noqa: F401
import app.tasks.followups  # noqa: F401
