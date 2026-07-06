"""Sentry error + performance monitoring.

No-op unless SENTRY_DSN is set. Call init_sentry() once, before the FastAPI app
or Celery worker is constructed. The SDK auto-enables its FastAPI, Starlette,
Celery, SQLAlchemy and Redis integrations from the installed packages, so both
the API and the worker report errors + latency to the same project.
"""
from __future__ import annotations

import sentry_sdk

from app.core.config import settings


def init_sentry() -> None:
    if not settings.SENTRY_DSN:
        return
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.APP_ENV,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        send_default_pii=True,
    )
