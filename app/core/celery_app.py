"""Celery application for the healthcheck service.

Owns one queue used by the historical-audit task. Broker + result
backend point at the same Redis the rest of the app uses, but on
separate db indices (1 + 2) so Celery traffic doesn't interleave with
the enrichment write-through on db 0.
"""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings
from app.core.observability import init_sentry

init_sentry()

celery_app = Celery(
    "healthcheck_poc",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.modules.healthcheck.tasks",
        "app.modules.insights.tasks",
        "app.modules.integrations.sync.tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Force-kill (and, with acks_late, redeliver) any task exceeding the hard
    # limit so a stuck upstream call cannot wedge a worker slot indefinitely.
    task_soft_time_limit=540,
    task_time_limit=600,
    # LLM enrichment runs on its own queue + worker so a slow/looping Groq call
    # can never starve the sync + audit worker on the default queue.
    task_routes={
        "healthcheck.prewarm_insights": {"queue": "enrich"},
        "healthcheck.reenrich_missing": {"queue": "enrich"},
    },
    # Daily reconcile: re-enumerate each accountant's Xero orgs to pick up
    # newly-granted clients and deactivate revoked ones. Requires a Celery beat
    # process alongside the worker: celery -A app.core.celery_app beat
    beat_schedule={
        "reconcile-xero-connections": {
            "task": "healthcheck.reconcile_connections",
            "schedule": crontab(hour=3, minute=0),  # 03:00 UTC daily
        },
        # Nightly Insights snapshot — pre-compute every client's KPIs so the
        # dashboard + firm-summary serve from the DB (no live Xero on request).
        "refresh-insight-snapshots": {
            "task": "insights.refresh_all",
            "schedule": crontab(hour=2, minute=30),  # 02:30 UTC daily
        },
        # Nightly Xero auto-sync — incrementally pull each connected org's new /
        # modified records into the DB. Runs before the insight snapshot so KPIs
        # compute off the freshly-synced data.
        "nightly-xero-sync": {
            "task": "healthcheck.sync_all_xero",
            "schedule": crontab(hour=2, minute=0),  # 02:00 UTC daily
        },
    },
)


# Make ``celery -A app.core.celery_app`` import-friendly.
__all__ = ["celery_app"]
