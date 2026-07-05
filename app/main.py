"""FastAPI application entry point.

Composition:
    * lifespan       — graceful Redis + Groq client shutdown
    * middleware     — CORS (env-driven; off by default since Django proxies)
    * routers        — /api/v1 router aggregator
    * exception hdlr — unhandled exception → stable JSON envelope + log
    * /health        — liveness probe (no Redis / no Groq dependency)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.modules.ai.router import router as enrichment_router
from app.modules.auth.routers import router as auth_router
from app.modules.healthcheck.batch_router import router as batch_router
from app.modules.healthcheck.demo_router import router as demo_router
from app.modules.healthcheck.validation_router import router as validation_router
from app.core.config import settings
from app.core.db import dispose_engine
from app.modules.ai.client import close_groq
from app.core.redis_client import close_redis
from app.modules.healthcheck.routers import router as healthcheck_router
from app.modules.insights.router import router as insights_router
from app.modules.integrations.nango.routers import router as nango_router
from app.modules.notifications.webhooks import router as email_webhook_router

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Startup: nothing eager — clients lazily init on first use.
    yield
    # Shutdown: close connection pools so Docker/k8s SIGTERM is clean.
    for closer, label in (
        (close_redis, "Redis"),
        (close_groq, "Groq"),
        (dispose_engine, "SQLAlchemy engine"),
    ):
        try:
            await closer()
        except Exception:
            logger.exception("%s close failed on shutdown", label)


app = FastAPI(
    title="EazyCapture AI Agent",
    description="Pre-ledger firewall and post-ledger health-check service.",
    version="0.1.0",
    lifespan=lifespan,
)

# Allow local dev frontend origins alongside the env-configured production
# origins; localhost is low-risk since only the developer's own browser can use it.
_LOCAL_DEV_ORIGINS = (
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:5173", "http://127.0.0.1:5173",
)
_cors_origins = tuple(
    dict.fromkeys((*settings.CORS_ALLOWED_ORIGINS, *_LOCAL_DEV_ORIGINS))
)
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """422 errors → a consistent, readable envelope.

    FastAPI's default returns ``detail`` as an *array of objects*, which a
    naive frontend renders as ``[object Object]``. We flatten it to a single
    human-readable string while keeping the structured list under ``errors``
    for debugging. Same ``{detail: str, ...}`` shape as every other error."""
    parts: list[str] = []
    clean: list[dict] = []
    for err in exc.errors():
        # loc looks like ("body", "email") — show the field name, skip "body".
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
        field = ".".join(loc)
        msg = str(err.get("msg", "Invalid value")).replace("Value error, ", "")
        parts.append(f"{field}: {msg}" if field else msg)
        # Build a JSON-safe error list — raw exc.errors() may carry a
        # non-serializable ValueError under ``ctx`` that breaks JSONResponse.
        clean.append({"field": field, "message": msg})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "; ".join(parts) or "Invalid request.", "errors": clean},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """HTTPException → guarantee ``detail`` is always a string, never a dict
    or list (so the frontend never sees ``[object Object]``)."""
    detail = exc.detail
    if not isinstance(detail, str):
        detail = str(detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": detail},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, _exc: Exception):
    """Last-resort: log with traceback, return stable JSON envelope.

    Per-endpoint try/except still wraps specific errors with friendlier
    messages; this catches anything that slips through. ``_exc`` is unused
    directly because ``logger.exception`` already pulls the active traceback.
    """
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error."},
    )


app.include_router(validation_router, prefix="/api/v1")
app.include_router(batch_router, prefix="/api/v1")
app.include_router(enrichment_router, prefix="/api/v1")
app.include_router(healthcheck_router)
app.include_router(insights_router)
app.include_router(nango_router)
app.include_router(demo_router, prefix="/api/v1")
app.include_router(auth_router)
app.include_router(email_webhook_router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    """Liveness probe. No downstream dependencies — true if the process runs."""
    return {"status": "ok"}
