"""Shared pytest fixtures.

We do NOT require a running Groq key or Redis to run these tests. The
client just exercises routes; the gate stays off by default so the
LLM/Redis paths are never invoked.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Force the gate OFF before importing the app so config picks it up.
os.environ.setdefault("HEALTHCHECK_AI_ENABLED", "false")

# Run the suite with auth OFF — these tests exercise business logic, not
# auth. ``test_auth.py`` patches ``settings.AUTH_DISABLED`` per-test to
# verify the enforced path. Set this BEFORE the app/config import so
# config._load() reads it (load_dotenv won't override an existing env var).
os.environ["AUTH_DISABLED"] = "true"

# Never report test runs to Sentry — force the DSN empty so init_sentry() no-ops.
os.environ["SENTRY_DSN"] = ""


@pytest.fixture(scope="session")
def client() -> TestClient:
    from app.main import app
    return TestClient(app)
