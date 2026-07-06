import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # --- Existing AI service (FastAPI :8001) ---
    GROQ_API_KEY: str
    GROQ_MODEL: str
    # Fast model for latency-sensitive per-row insight generation.
    GROQ_INSIGHT_MODEL: str
    REDIS_URL: str
    HEALTHCHECK_AI_ENABLED: bool
    # When false, the audit skips all LLM passes and runs purely deterministically.
    LLM_CHECKS_ENABLED: bool
    HEALTHCHECK_AI_TTL_SECONDS: int
    CORS_ALLOWED_ORIGINS: tuple[str, ...]

    # --- DB-backed /api/v1/health/* routes ---
    DATABASE_URL: str
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str

    # AI FastAPI endpoints — default to this same process; override if the
    # engine runs in a separate container.
    HEALTHCHECK_AI_BASE_URL: str
    HEALTHCHECK_AI_BATCH_URL: str
    HEALTHCHECK_AI_ENRICH_URL: str
    HEALTHCHECK_AI_SUGGEST_FIX_URL: str
    HEALTHCHECK_AI_TIMEOUT_MS: int
    HEALTHCHECK_AI_ENRICH_TIMEOUT_MS: int
    HEALTHCHECK_BATCH_HASH_TTL_SECONDS: int

    # Nango (placeholders until Xero wiring lands)
    NANGO_BASE_URL: str
    NANGO_SECRET_KEY: str
    NANGO_WEBHOOK_SECRET: str
    NANGO_USER_ID: str
    NANGO_XERO_INTEGRATION_ID: str
    # Cap pagination so a misconfigured connection can't pull forever.
    MAX_NANGO_PAGES: int
    # Source for Xero documents: "proxy" (live proxy_get) or "action"
    # (deployed custom list-*-full Nango actions, line items intact).
    AUDIT_SOURCE: str

    # Deployment environment: development | staging | production. In production
    # the app refuses to boot with insecure settings (see assert_safe_for_environment).
    APP_ENV: str

    # Sentry error + performance monitoring. Empty DSN → disabled (no-op).
    SENTRY_DSN: str
    SENTRY_TRACES_SAMPLE_RATE: float

    # Auth — empty JWT_SECRET = demo mode. Set AUTH_DISABLED=true to force the
    # open demo path even when a JWT secret is present.
    AUTH_DISABLED: bool
    JWT_SECRET: str
    JWT_ALGORITHM: str
    JWT_TTL_HOURS: int

    # Brute-force protection: lock an account after this many failures within
    # the window. A successful login resets the counter.
    LOGIN_MAX_FAILURES: int
    LOGIN_FAILURE_WINDOW_SECONDS: int

    # --- Notifications / email ---
    # Empty SMTP_HOST → email channel is unconfigured and the notification
    # service falls back to logging the message (console channel).
    SMTP_HOST: str
    SMTP_PORT: int
    SMTP_USERNAME: str
    SMTP_PASSWORD: str
    SMTP_FROM: str
    SMTP_STARTTLS: bool   # True for port 587 (default), False for SSL
    SMTP_SSL: bool        # True for implicit SSL on port 465
    # Resend (HTTP email API) — sends over HTTPS, works where outbound SMTP is
    # blocked. Used as a fallback behind Mailgun.
    RESEND_API_KEY: str
    RESEND_FROM: str
    # Mailgun (HTTP email API) — primary provider for invites + signup OTPs,
    # auto-selected first when configured. Authenticates via HTTP Basic
    # ('api:<key>'); region is set by the base URL. Values from APP_MAILGUN_* env vars.
    MAILGUN_API_KEY: str
    MAILGUN_SENDING_API_KEY: str
    MAILGUN_SENDING_DOMAIN: str
    MAILGUN_API_BASE_URL: str
    MAILGUN_FROM: str  # blank → "<APP_NAME> <noreply@<sending-domain>>"
    # HMAC key Mailgun signs delivery/bounce webhooks with (verified in webhooks.py).
    MAILGUN_WEBHOOK_SIGNING_KEY: str
    # Optional probe recipient for a send self-test, and the inbound domain.
    MAILGUN_PROBE_TO: str
    MAILGUN_INBOUND_DOMAIN: str
    # Frontend origin + route used to build the accept-invite link in emails.
    APP_NAME: str
    APP_BASE_URL: str
    ACCEPT_INVITE_PATH: str
    # Shared secret the email provider sends on delivery/bounce webhooks.
    # Empty → webhook accepted with a logged warning (dev). Set in production.
    EMAIL_WEBHOOK_SECRET: str
    # --- Companies House (Opening Balance Differences check) ---
    # Empty API key → the check falls back to manually-entered filed Net Assets
    # (no auto-fetch).
    COMPANIES_HOUSE_API_KEY: str
    COMPANIES_HOUSE_BASE_URL: str
    COMPANIES_HOUSE_DOCUMENT_URL: str


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_origins(raw: str) -> tuple[str, ...]:
    return tuple(o.strip() for o in raw.split(",") if o.strip())


def _normalize_async_db_url(url: str) -> str:
    """Ensure the URL uses the async (asyncpg) driver.

    Managed Postgres providers (Render, Heroku, …) hand out
    ``postgres://`` / ``postgresql://`` connection strings, which SQLAlchemy
    would route to a SYNC driver. The async engine needs ``+asyncpg``, so add
    it when missing (the Alembic helper later swaps it for psycopg)."""
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


def _ai_base_url() -> str:
    """Base URL of the rules/LLM endpoints the worker calls. A managed host
    (e.g. Render's ``fromService`` host) may arrive without a scheme — assume
    HTTPS. Trailing slash stripped so paths append cleanly."""
    raw = os.environ.get("HEALTHCHECK_AI_BASE_URL", "http://127.0.0.1:8001").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    return raw.rstrip("/")


_AI_BASE = _ai_base_url()


def _load() -> Settings:
    return Settings(
        # --- existing ---
        GROQ_API_KEY=os.environ.get("GROQ_API_KEY", ""),
        GROQ_MODEL=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
        GROQ_INSIGHT_MODEL=os.environ.get("GROQ_INSIGHT_MODEL", "llama-3.1-8b-instant"),
        REDIS_URL=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        HEALTHCHECK_AI_ENABLED=_as_bool(os.environ.get("HEALTHCHECK_AI_ENABLED", "false")),
        LLM_CHECKS_ENABLED=_as_bool(os.environ.get("LLM_CHECKS_ENABLED", "true")),
        HEALTHCHECK_AI_TTL_SECONDS=int(os.environ.get("HEALTHCHECK_AI_TTL_SECONDS", "2592000")),
        CORS_ALLOWED_ORIGINS=_as_origins(os.environ.get("CORS_ALLOWED_ORIGINS", "")),
        # --- Database / Celery / Redis ---
        DATABASE_URL=_normalize_async_db_url(os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://hcpoc:hcpoc@127.0.0.1:5434/healthcheck_poc",
        )),
        CELERY_BROKER_URL=os.environ.get(
            "CELERY_BROKER_URL",
            "redis://:peakeaze-redis@127.0.0.1:6379/1",
        ),
        CELERY_RESULT_BACKEND=os.environ.get(
            "CELERY_RESULT_BACKEND",
            "redis://:peakeaze-redis@127.0.0.1:6379/2",
        ),
        # Set only HEALTHCHECK_AI_BASE_URL; the endpoints below derive from it
        # but each can still be overridden individually.
        HEALTHCHECK_AI_BASE_URL=_AI_BASE,
        HEALTHCHECK_AI_BATCH_URL=(
            os.environ.get("HEALTHCHECK_AI_BATCH_URL")
            or f"{_AI_BASE}/api/v1/health-check/batch"
        ),
        HEALTHCHECK_AI_ENRICH_URL=(
            os.environ.get("HEALTHCHECK_AI_ENRICH_URL")
            or f"{_AI_BASE}/api/v1/enrich-audit"
        ),
        HEALTHCHECK_AI_SUGGEST_FIX_URL=(
            os.environ.get("HEALTHCHECK_AI_SUGGEST_FIX_URL")
            or f"{_AI_BASE}/api/v1/suggest-fix"
        ),
        HEALTHCHECK_AI_TIMEOUT_MS=int(os.environ.get("HEALTHCHECK_AI_TIMEOUT_MS", "600000")),
        HEALTHCHECK_AI_ENRICH_TIMEOUT_MS=int(os.environ.get("HEALTHCHECK_AI_ENRICH_TIMEOUT_MS", "5000")),
        HEALTHCHECK_BATCH_HASH_TTL_SECONDS=int(os.environ.get("HEALTHCHECK_BATCH_HASH_TTL_SECONDS", "3600")),
        NANGO_BASE_URL=os.environ.get("NANGO_BASE_URL", "https://api.nango.dev"),
        NANGO_SECRET_KEY=os.environ.get("NANGO_SECRET_KEY", ""),
        NANGO_WEBHOOK_SECRET=os.environ.get("NANGO_WEBHOOK_SECRET", ""),
        NANGO_USER_ID=os.environ.get("NANGO_USER_ID", "demo-user"),
        NANGO_XERO_INTEGRATION_ID=os.environ.get("NANGO_XERO_INTEGRATION_ID", "xero"),
        MAX_NANGO_PAGES=int(os.environ.get("MAX_NANGO_PAGES", "10")),
        # Default to the DB-backed source; falls back to a live fetch when an
        # org has no synced data yet.
        AUDIT_SOURCE=os.environ.get("AUDIT_SOURCE", "db").strip().lower(),
        APP_ENV=os.environ.get("APP_ENV", "development").strip().lower(),
        SENTRY_DSN=os.environ.get("SENTRY_DSN", "").strip(),
        SENTRY_TRACES_SAMPLE_RATE=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "1.0")),
        AUTH_DISABLED=_as_bool(os.environ.get("AUTH_DISABLED", "false")),
        JWT_SECRET=os.environ.get("JWT_SECRET", ""),
        JWT_ALGORITHM=os.environ.get("JWT_ALGORITHM", "HS256"),
        JWT_TTL_HOURS=int(os.environ.get("JWT_TTL_HOURS", "12")),
        LOGIN_MAX_FAILURES=int(os.environ.get("LOGIN_MAX_FAILURES", "5")),
        LOGIN_FAILURE_WINDOW_SECONDS=int(
            os.environ.get("LOGIN_FAILURE_WINDOW_SECONDS", "300")
        ),
        # --- Notifications / email ---
        SMTP_HOST=os.environ.get("SMTP_HOST", ""),
        SMTP_PORT=int(os.environ.get("SMTP_PORT", "587")),
        SMTP_USERNAME=os.environ.get("SMTP_USERNAME", ""),
        SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD", ""),
        SMTP_FROM=os.environ.get("SMTP_FROM", ""),
        SMTP_STARTTLS=_as_bool(os.environ.get("SMTP_STARTTLS", "true")),
        SMTP_SSL=_as_bool(os.environ.get("SMTP_SSL", "false")),
        RESEND_API_KEY=os.environ.get("RESEND_API_KEY", ""),
        RESEND_FROM=os.environ.get("RESEND_FROM", "onboarding@resend.dev"),
        MAILGUN_API_KEY=os.environ.get("APP_MAILGUN_API_KEY", ""),
        MAILGUN_SENDING_API_KEY=os.environ.get("APP_MAILGUN_SENDING_API_KEY", ""),
        MAILGUN_SENDING_DOMAIN=os.environ.get("APP_MAILGUN_SENDING_DOMAIN", ""),
        MAILGUN_API_BASE_URL=os.environ.get(
            "APP_MAILGUN_API_BASE_URL", "https://api.mailgun.net"
        ),
        MAILGUN_FROM=os.environ.get("APP_MAILGUN_FROM", ""),
        MAILGUN_WEBHOOK_SIGNING_KEY=os.environ.get(
            "APP_MAILGUN_WEBHOOK_SIGNING_KEY", ""
        ),
        MAILGUN_PROBE_TO=os.environ.get("APP_MAILGUN_PROBE_TO", ""),
        MAILGUN_INBOUND_DOMAIN=os.environ.get("APP_MAILGUN_INBOUND_DOMAIN", ""),
        APP_NAME=os.environ.get("APP_NAME", "EazyCapture"),
        APP_BASE_URL=os.environ.get("APP_BASE_URL", "http://localhost:5173"),
        ACCEPT_INVITE_PATH=os.environ.get("ACCEPT_INVITE_PATH", "/accept-invite"),
        EMAIL_WEBHOOK_SECRET=os.environ.get("EMAIL_WEBHOOK_SECRET", ""),
        COMPANIES_HOUSE_API_KEY=os.environ.get("COMPANIES_HOUSE_API_KEY", ""),
        COMPANIES_HOUSE_BASE_URL=os.environ.get(
            "COMPANIES_HOUSE_BASE_URL",
            "https://api.company-information.service.gov.uk",
        ),
        COMPANIES_HOUSE_DOCUMENT_URL=os.environ.get(
            "COMPANIES_HOUSE_DOCUMENT_URL",
            "https://document-api.company-information.service.gov.uk",
        ),
    )


_INSECURE_SECRET_MARKERS = ("change-this", "change-me", "insecure", "dev-")


def assert_safe_for_environment(cfg: Settings) -> None:
    """Fail-fast guard. When ``APP_ENV=production`` the app must not boot with
    demo/insecure settings — a wrong env var should crash on startup, not
    silently expose the service. No-op outside production so dev/test/staging
    stay convenient.

    Runs at import so EVERY entrypoint (API, Celery, Alembic) is protected.
    """
    if cfg.APP_ENV != "production":
        return

    errors: list[str] = []
    if cfg.AUTH_DISABLED:
        errors.append("AUTH_DISABLED must be false in production.")
    secret = (cfg.JWT_SECRET or "").strip()
    if not secret:
        errors.append("JWT_SECRET must be set in production.")
    elif len(secret) < 32 or any(m in secret.lower() for m in _INSECURE_SECRET_MARKERS):
        errors.append(
            "JWT_SECRET looks weak/default — use a random secret of 32+ chars."
        )
    if not (cfg.DATABASE_URL or "").strip():
        errors.append("DATABASE_URL must be set in production.")

    if errors:
        raise RuntimeError(
            "Refusing to start in production with insecure config:\n  - "
            + "\n  - ".join(errors)
        )


settings = _load()
assert_safe_for_environment(settings)


def sync_database_url() -> str:
    """Sync variant of DATABASE_URL for Alembic (which needs a non-async driver)."""
    url = settings.DATABASE_URL
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    return url
