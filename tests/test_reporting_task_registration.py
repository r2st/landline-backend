"""The daily-summary beat entry must map to a *registered* task.

The ``daily-summary`` beat schedule dispatches
``app.tasks.reporting.generate_daily_summary``, but ``app.tasks.reporting`` was
missing from celery_app's force-import block, so the task was never registered
and every beat-dispatched run failed with a NotRegistered error (silent — the
summary just never arrived). Guard against that regression: every beat entry
must reference a task Celery actually knows about, and reporting in particular.
"""

from app.tasks.celery_app import celery_app


def test_generate_daily_summary_is_registered():
    assert "app.tasks.reporting.generate_daily_summary" in celery_app.tasks


def test_daily_summary_beat_entry_points_at_a_registered_task():
    beat = celery_app.conf.beat_schedule
    # Find the beat entry that dispatches the daily summary.
    entries = [
        cfg for cfg in beat.values()
        if cfg.get("task") == "app.tasks.reporting.generate_daily_summary"
    ]
    assert entries, "no beat entry dispatches generate_daily_summary"


def test_every_beat_task_is_registered():
    """No beat entry may reference an unregistered task — this is exactly the
    class of bug that left the daily summary dead."""
    beat = celery_app.conf.beat_schedule
    missing = sorted(
        cfg["task"] for cfg in beat.values()
        if cfg.get("task") not in celery_app.tasks
    )
    assert not missing, f"beat schedule references unregistered tasks: {missing}"
