"""Post-ledger batch audit: sync, async-with-SSE-progress."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import redis.asyncio as async_redis
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.modules.healthcheck.services.audit_service import META_FIELD, batch_key
from app.schemas.transaction import BatchHealthCheckRequest, BatchHealthCheckResponse
from app.modules.healthcheck.engine import run_batch_health_check

# Deterministic namespace so re-checking the same pre-ledger document_id
# (which may not be a Xero UUID yet) maps to a stable health_check_result row.
_PRELEDGER_NS = uuid.UUID("9b6f7e2a-1c3d-4e5f-8a9b-0c1d2e3f4a5b")

logger = logging.getLogger("uvicorn.error")

router = APIRouter(tags=["health-check"])

# Async-batch progress is kept in-process. This is fine for a single worker
# (the default deployment). Multi-worker setups should move this to Redis —
# tracked as known debt in README.
_BATCH_TTL_SECONDS = 300


@dataclass
class _BatchProgress:
    total_txns: int = 0
    unique_txns: int = 0
    processed: int = 0
    status: str = "queued"  # queued | running | done | error
    result: Optional[BatchHealthCheckResponse] = None
    error: Optional[str] = None
    events: list[dict] = field(default_factory=list)


_batches: dict[str, _BatchProgress] = {}


@router.post(
    "/health-check/batch",
    response_model=BatchHealthCheckResponse,
    status_code=status.HTTP_200_OK,
    summary="Post-ledger cleanup: bulk audit of historical transactions (sync).",
)
async def health_check_batch(
    payload: BatchHealthCheckRequest,
    db: AsyncSession = Depends(get_db),
) -> BatchHealthCheckResponse:
    try:
        result = await run_batch_health_check(payload)
    except Exception as exc:
        logger.exception("health_check_batch endpoint failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Batch health check failed.",
        ) from exc

    # When a company_id is supplied, log the BLOCKED pre-checks to the audit
    # trail (kind=pre_ledger). Clean transactions aren't recorded. Without a
    # company_id the call stays stateless (EazyCapture inspector contract).
    if payload.company_id:
        try:
            await _persist_preledger_blocked(db, payload, result)
        except Exception:
            logger.exception("pre-ledger audit-log persist failed (non-fatal)")

    return result


async def _persist_preledger_blocked(
    db: AsyncSession,
    payload: BatchHealthCheckRequest,
    result: BatchHealthCheckResponse,
) -> None:
    """Persist one health_check_result per BLOCKED pre-check transaction.

    document_id must be a UUID column, but a pre-ledger transaction_id may
    not be a Xero UUID yet — so we map it deterministically via uuid5 and
    keep the original id in the result JSONB.
    """
    from uuid import UUID
    from sqlalchemy import select
    from app.modules.healthcheck.models import Company, HealthCheckResult

    try:
        company_uuid = UUID(str(payload.company_id))
    except (TypeError, ValueError):
        return  # not a real company id — skip silently

    # Company must exist (FK + scoping).
    if (await db.execute(
        select(Company.id).where(Company.id == company_uuid)
    )).scalar_one_or_none() is None:
        return

    kind = (payload.kind or "pre_ledger").strip().lower()
    if kind not in {"pre_ledger", "preview"}:
        kind = "pre_ledger"

    # Group flags by transaction (one row per blocked document).
    by_tx: dict[str, list] = defaultdict(list)
    for f in result.flagged:
        by_tx[f.transaction_id].append(f)
    if not by_tx:
        return

    tx_type = {t.transaction_id: (t.type or "unknown") for t in payload.transactions}

    persisted = 0
    for tx_id, items in by_tx.items():
        try:
            document_uuid = UUID(tx_id)
        except (TypeError, ValueError):
            document_uuid = uuid.uuid5(_PRELEDGER_NS, f"{payload.company_id}:{tx_id}")

        rule_ids = [i.issue_type for i in items if i.issue_type]
        messages = " | ".join(i.message for i in items if i.message)

        # Idempotent: skip if this doc already has a pre_ledger blocked row.
        existing = (await db.execute(
            select(HealthCheckResult.id).where(
                HealthCheckResult.document_id == document_uuid,
                HealthCheckResult.company_id == company_uuid,
                HealthCheckResult.kind == kind,
                HealthCheckResult.status == "blocked",
            ).limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            continue

        db.add(HealthCheckResult(
            company_id=company_uuid,
            document_id=document_uuid,
            document_type=str(tx_type.get(tx_id, "unknown")),
            kind=kind,
            status="blocked",
            error_msgs=(messages[:1000] or None),
            result={
                "flagged": [i.model_dump(mode="json") for i in items],
                "rule_ids": rule_ids,
                "messages": messages,
                "source_transaction_id": tx_id,
                "target_ledger": "xero",
            },
        ))
        persisted += 1

    if persisted:
        await db.commit()
        logger.info(
            "[pre-ledger] logged %d blocked pre-check(s) for company=%s kind=%s",
            persisted, payload.company_id, kind,
        )


@router.post(
    "/health-check/batch/async",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a batch audit in the background. Returns a batch_id for the SSE progress stream.",
)
async def health_check_batch_async(
    payload: BatchHealthCheckRequest,
) -> dict[str, str]:
    batch_id = uuid.uuid4().hex
    progress = _BatchProgress(total_txns=len(payload.transactions))
    _batches[batch_id] = progress
    asyncio.create_task(_run_async(batch_id, payload, progress))
    return {"batch_id": batch_id}


async def _run_async(
    batch_id: str,
    payload: BatchHealthCheckRequest,
    progress: _BatchProgress,
) -> None:
    progress.status = "running"
    progress.events.append({"event": "started", "total_txns": progress.total_txns})

    async def cb(evt: dict) -> None:
        if evt.get("event") == "categorize_started":
            progress.unique_txns = int(evt.get("unique_txns", 0))
        elif evt.get("event") == "categorize_progress":
            progress.processed = int(evt.get("processed", 0))
        progress.events.append(evt)

    try:
        result = await run_batch_health_check(payload, progress_callback=cb)
        progress.result = result
        progress.status = "done"
        progress.events.append({
            "event": "complete",
            "flagged_count": len(result.flagged),
            "result": result.model_dump(mode="json"),
        })
    except Exception as exc:
        logger.exception("Async batch %s failed", batch_id)
        progress.status = "error"
        progress.error = str(exc)
        progress.events.append({"event": "error", "error": str(exc)})
    finally:
        progress.events.append({"event": "end"})

    await asyncio.sleep(_BATCH_TTL_SECONDS)
    _batches.pop(batch_id, None)


@router.get(
    "/audit/progress/{batch_id}",
    summary="Server-Sent Events stream of progress for an in-flight audit batch.",
)
async def audit_progress(batch_id: str) -> StreamingResponse:
    """Progress stream for BOTH audit paths.

    * In-process async batch (``/health-check/batch/async``) → buffered in
      ``_batches`` and streamed event-by-event.
    * Celery historical audit (``/health/sync-xero-history/``) → its progress
      lives in the Redis meta hash (written by the worker — a *different*
      process), so stream that by polling the hash until a terminal status.
      Without this, a Celery ``batch_id`` 404s here even though the audit is
      running fine; the frontend's audit-progress UI was reading from this
      endpoint and getting nothing.
    """
    progress = _batches.get(batch_id)
    if progress is not None:
        async def event_stream():
            last_index = 0
            idle_ticks = 0
            while True:
                while last_index < len(progress.events):
                    evt = progress.events[last_index]
                    last_index += 1
                    idle_ticks = 0
                    yield f"data: {json.dumps(evt)}\n\n"
                    if evt.get("event") == "end":
                        return
                await asyncio.sleep(0.1)
                idle_ticks += 1
                # Heartbeat every ~10s so proxies/clients don't drop the connection.
                if idle_ticks >= 100:
                    idle_ticks = 0
                    yield ": heartbeat\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # Not an in-process batch → treat it as a Celery historical-audit batch and
    # stream its progress from the Redis meta hash.
    redis: async_redis.Redis = async_redis.from_url(
        settings.REDIS_URL, decode_responses=True,
    )
    raw = await redis.hget(batch_key(batch_id), META_FIELD)
    if not raw:
        await redis.aclose()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown batch_id (expired or never started).",
        )

    async def redis_event_stream():
        try:
            last_snapshot = None
            idle_ticks = 0
            while True:
                raw = await redis.hget(batch_key(batch_id), META_FIELD)
                meta = json.loads(raw) if raw else None
                if meta is None:                    # hash expired mid-stream
                    yield f"data: {json.dumps({'event': 'end', 'status': 'expired'})}\n\n"
                    return
                snapshot = (
                    meta.get("stage"), meta.get("stage_label"),
                    meta.get("status"), meta.get("total"), meta.get("trapped"),
                )
                if snapshot != last_snapshot:
                    last_snapshot = snapshot
                    idle_ticks = 0
                    yield f"data: {json.dumps({'event': 'progress', **meta})}\n\n"
                if meta.get("status") in ("completed", "failed"):
                    yield f"data: {json.dumps({'event': 'end', 'status': meta.get('status')})}\n\n"
                    return
                await asyncio.sleep(0.5)
                idle_ticks += 1
                if idle_ticks >= 20:                # ~10s heartbeat
                    idle_ticks = 0
                    yield ": heartbeat\n\n"
        finally:
            await redis.aclose()

    return StreamingResponse(redis_event_stream(), media_type="text/event-stream")
