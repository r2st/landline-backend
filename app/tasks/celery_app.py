"""Celery application configuration."""

from celery import Celery
from celery.schedules import crontab

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
        "app.tasks.calls.*": {"queue": "enrichment"},
        "app.tasks.research.*": {"queue": "enrichment"},
        "app.tasks.conversation.*": {"queue": "enrichment"},
        "app.tasks.intent.*": {"queue": "enrichment"},
        "app.tasks.coaching.*": {"queue": "enrichment"},
        "app.tasks.competitive.*": {"queue": "enrichment"},
        "app.tasks.orchestration.*": {"queue": "outreach"},
        "app.tasks.pipeline_predictions.*": {"queue": "enrichment"},
        "app.tasks.lead_routing.*": {"queue": "enrichment"},
        "app.tasks.smart_suggestions_task.*": {"queue": "enrichment"},
        "app.tasks.prospecting.*": {"queue": "enrichment"},
        "app.tasks.client_health.*": {"queue": "enrichment"},
        "app.tasks.deliverability.*": {"queue": "enrichment"},
        "app.tasks.bounces.*": {"queue": "enrichment"},
        "app.tasks.danger_signals.*": {"queue": "enrichment"},
        "app.tasks.domain_health.*": {"queue": "enrichment"},
        "app.tasks.rag.*": {"queue": "enrichment"},
        "app.tasks.voice.*": {"queue": "enrichment"},
        "app.tasks.replies.*": {"queue": "enrichment"},
        "app.tasks.ab_testing.*": {"queue": "enrichment"},
        "app.tasks.send_timing.*": {"queue": "enrichment"},
        "app.tasks.verification.*": {"queue": "enrichment"},
        "app.tasks.places_discovery.*": {"queue": "enrichment"},
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
        # Weekly lead-quality report (grade distribution, qualified vs disqualified).
        "weekly-lead-quality-report": {
            "task": "app.tasks.followups.generate_weekly_lead_quality_report",
            "schedule": crontab(hour=6, minute=15, day_of_week=1),  # Mondays 6:15 AM UTC
        },
        "sync-email-replies": {
            "task": "app.tasks.outreach.sync_email_replies",
            "schedule": 300.0,  # every 5 minutes
        },
        # --- Cold-call notification system ---
        "generate-morning-call-queue": {
            "task": "app.tasks.calls.generate_morning_call_queue",
            "schedule": crontab(hour=8, minute=0),  # daily at 8 AM UTC
        },
        "send-precall-reminders": {
            "task": "app.tasks.calls.send_precall_reminders",
            "schedule": 300.0,  # every 5 minutes (15-min look-ahead window)
        },
        "send-call-followup-reminders": {
            "task": "app.tasks.calls.send_followup_reminders",
            "schedule": 900.0,  # every 15 minutes
        },
        "detect-hot-leads": {
            "task": "app.tasks.calls.detect_hot_leads",
            "schedule": 1800.0,  # every 30 minutes
        },
        "flag-missed-calls": {
            "task": "app.tasks.calls.flag_missed_calls",
            "schedule": crontab(hour=18, minute=0),  # end of day, 6 PM UTC
        },
        # --- Phase 1 AI features ---
        # 1.4: backfill research briefs for active-pipeline companies missing one.
        "research-stale-companies": {
            "task": "app.tasks.research.research_stale_companies",
            "schedule": 21600.0,  # every 6 hours
        },
        # 1.5: analyze active email threads for momentum / next-best-message.
        "analyze-active-threads": {
            "task": "app.tasks.conversation.analyze_active_threads",
            "schedule": 3600.0,  # hourly
        },
        # 2.1: daily buyer-intent signal scan across active-pipeline companies.
        "scan-intent-signals": {
            "task": "app.tasks.intent.scan_intent_signals",
            "schedule": crontab(hour=7, minute=0),  # daily at 7 AM UTC
        },
        # 2.4: weekly per-rep performance + AI coaching snapshots.
        "generate-weekly-coaching": {
            "task": "app.tasks.coaching.generate_weekly_coaching",
            "schedule": crontab(hour=6, minute=0, day_of_week=1),  # Mondays 6 AM UTC
        },
        # 2.6: monitor competitor news for battle-card-worthy mentions.
        "monitor-competitor-news": {
            "task": "app.tasks.competitive.monitor_competitor_news",
            "schedule": 43200.0,  # twice a day
        },
        # 1.1: weekly retrain + rescore of deal close-probability predictions.
        "rescore-deal-predictions": {
            "task": "app.tasks.pipeline_predictions.rescore_deal_predictions",
            "schedule": crontab(hour=5, minute=0, day_of_week=1),  # Mondays 5 AM UTC
        },
        # 1.3: keep propensity / signal-velocity current for active leads.
        "refresh-propensity-scores": {
            "task": "app.tasks.lead_routing.refresh_propensity_scores",
            "schedule": 1800.0,  # every 30 minutes
        },
        # 1.3: refresh rep win-rates, velocity, and capacity daily.
        "recompute-rep-profiles": {
            "task": "app.tasks.lead_routing.recompute_rep_profiles",
            "schedule": crontab(hour=4, minute=0),  # daily 4 AM UTC
        },
        # 1.6: produce the prioritized daily AI action plan.
        "generate-daily-suggestions": {
            "task": "app.tasks.smart_suggestions_task.generate_daily_suggestions",
            "schedule": crontab(hour=7, minute=30),  # daily 7:30 AM UTC
        },
        # --- Phase 3 AI features ---
        # 3.1: autonomous prospecting run (daily) + weekly ICP relearn.
        "run-prospecting-agent": {
            "task": "app.tasks.prospecting.run_prospecting_agent",
            "schedule": crontab(hour=9, minute=0),  # daily 9 AM UTC
        },
        "relearn-icp": {
            "task": "app.tasks.prospecting.relearn_icp",
            "schedule": crontab(hour=3, minute=30, day_of_week=1),  # Mondays 3:30 AM UTC
        },
        # 3.2: weekly predictive churn + expansion pass across clients.
        "predict-client-health": {
            "task": "app.tasks.client_health.predict_client_health",
            "schedule": crontab(hour=6, minute=30, day_of_week=1),  # Mondays 6:30 AM UTC
        },
        # 3.3: hourly sender deliverability health assessment.
        "assess-sender-health": {
            "task": "app.tasks.deliverability.assess_sender_health",
            "schedule": 3600.0,  # hourly
        },
        # Ingest delivery-failure notifications from the mailbox. Gmail is the
        # primary transport and has no bounce webhook — a DSN just lands in the
        # inbox — so this sweep is the only thing that reaches bounce_handler
        # automatically. Every 15 minutes: fast enough to stop mailing a dead
        # address early in a run, cheap because it dedups on message id.
        "ingest-bounces": {
            "task": "app.tasks.bounces.ingest_bounces",
            "schedule": 900.0,  # every 15 minutes
        },
        # Feature 2: auto-pause on danger signals — 24h bounce/complaint monitor.
        # Every 5 minutes so an acute spike (bad list, burning domain) is stopped
        # fast and cheaply.
        "monitor-danger-signals": {
            "task": "app.tasks.danger_signals.monitor_danger_signals",
            "schedule": 300.0,  # every 5 minutes
        },
        # 3.4: index new content into the RAG knowledge base.
        "index-new-content": {
            "task": "app.tasks.rag.index_new_content",
            "schedule": 21600.0,  # every 6 hours
        },
        # 2.2: autonomous multi-channel orchestration — decide the next touch for
        # every autonomous-mode enrollment.
        "run-orchestrator": {
            "task": "app.tasks.orchestration.run_orchestrator",
            "schedule": 900.0,  # every 15 minutes
        },
        # Daily autonomous-activity digest — 6 PM IST (12:30 UTC).
        "generate-daily-summary": {
            "task": "app.tasks.reporting.generate_daily_summary",
            "schedule": crontab(hour=12, minute=30),
        },
        # Feature 1: classify newly-arrived replies and route them (hot leads →
        # priority inbox, unsubscribes → suppression, OOO → re-queue).
        "classify-new-replies": {
            "task": "app.tasks.replies.classify_new_replies",
            "schedule": 300.0,  # every 5 minutes
        },
        # Feature 5: daily SPF/DKIM/DMARC + DNSBL sweep per sending domain,
        # scored, persisted, and alerted on change. Daily is plenty (DNS moves
        # slowly) and polite to the DNSBL zones. 5 AM UTC.
        "check-domain-health": {
            "task": "app.tasks.domain_health.check_domain_health",
            "schedule": crontab(hour=5, minute=30),
        },
        # Feature 3: auto-evaluate running A/B tests and declare/promote winners
        # once they reach significance (or the hard cap). Daily is plenty — copy
        # tests accrue sample slowly. 4:30 AM UTC.
        "evaluate-ab-tests": {
            "task": "app.tasks.ab_testing.evaluate_ab_tests",
            "schedule": crontab(hour=4, minute=30),
        },
        # Feature 6: nightly recompute of per-contact engagement profiles (local
        # (dow, hour) buckets, best day/hour) + timezone backfill, feeding the
        # per-recipient send-time scheduling done inside process_outreach_queue.
        # 3 AM UTC — quiet hours, and ahead of the morning sending window.
        "refresh-engagement-profiles": {
            "task": "app.tasks.send_timing.refresh_engagement_profiles",
            "schedule": crontab(hour=3, minute=0),
        },
        # P3: verify never-verified contacts (Hunter, behind the free syntax+MX
        # pre-filter). Every 15 min so an imported/prospected contact is verified
        # well before its sequence's first send, without making import block on a
        # paid API call. Idempotent via contacts.email_verified_at.
        "verify-pending-contacts": {
            "task": "app.tasks.verification.verify_pending_contacts",
            "schedule": 900.0,  # every 15 minutes
        },
        # P3: re-verify addresses that have gone stale (default >90 days) —
        # people leave and mailboxes are retired. Weekly, oldest-first, and
        # limited to contacts we could still email, so it can't burn credits on
        # a list we'd never send to. 2 AM Sunday UTC.
        "reverify-stale-contacts": {
            "task": "app.tasks.verification.reverify_stale_contacts",
            "schedule": crontab(hour=2, minute=0, day_of_week=0),
        },
        # P4: re-run saved Google Places searches. Weekly rather than daily on
        # purpose — local business data changes slowly, so a daily cadence pays
        # seven times over for nearly the same results. 4 AM Monday UTC, clear
        # of the Sunday-night verification sweep.
        "run-places-discoveries": {
            "task": "app.tasks.places_discovery.run_scheduled_discoveries",
            "schedule": crontab(hour=4, minute=0, day_of_week=1),
        },
        # P4: recover emails for discovered businesses that have a website but
        # no address (Hunter domain-search, then the free scraper). Daily and
        # separate from discovery: this is the hop where the funnel narrows, and
        # it shouldn't be gated on a Places search succeeding. 5 AM UTC.
        "enrich-discovered-emails": {
            "task": "app.tasks.places_discovery.enrich_discovered_emails",
            "schedule": crontab(hour=5, minute=0),
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
import app.tasks.calls  # noqa: F401
import app.tasks.research  # noqa: F401
import app.tasks.conversation  # noqa: F401
import app.tasks.intent  # noqa: F401
import app.tasks.coaching  # noqa: F401
import app.tasks.competitive  # noqa: F401
import app.tasks.orchestration  # noqa: F401
import app.tasks.pipeline_predictions  # noqa: F401
import app.tasks.lead_routing  # noqa: F401
import app.tasks.smart_suggestions_task  # noqa: F401
import app.tasks.prospecting  # noqa: F401
import app.tasks.client_health  # noqa: F401
import app.tasks.deliverability  # noqa: F401
import app.tasks.bounces  # noqa: F401
import app.tasks.danger_signals  # noqa: F401
import app.tasks.domain_health  # noqa: F401
import app.tasks.rag  # noqa: F401
import app.tasks.voice  # noqa: F401
import app.tasks.replies  # noqa: F401
import app.tasks.ab_testing  # noqa: F401
import app.tasks.send_timing  # noqa: F401
import app.tasks.verification  # noqa: F401
import app.tasks.places_discovery  # noqa: F401
