"""Re-enrichment sweep — finds trapped rows whose ``health_check_ai``
Redis key is missing/empty and re-runs the per-row enrichment one
at a time (with a small inter-call sleep) so Groq's TPM cap doesn't
clip the batch again.

Why this exists: the audit's fire-and-forget enrich call processes all
trapped rows in parallel and the free-tier Groq budget sometimes
drops a few. The dashboard ends up with 12/15 rows enriched. Running
this sweep tops it back up to 15/15 without re-running the audit.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.healthcheck.models import HealthCheckResult

logger = logging.getLogger("eazycapture.reenrich")

_AI_KEY_PREFIX = "health_check_ai"


class ReenrichService:
    """Lists trapped rows whose AI annotation is missing in Redis."""

    def __init__(self, db: AsyncSession, redis_client: Redis) -> None:
        self._db = db
        self._redis = redis_client

    async def list_missing_rows(
        self,
        company_id: UUID,
    ) -> list[dict[str, Any]]:
        """Return the payloads we need to re-feed to the rules engine."""
        rows = (
            await self._db.execute(
                select(HealthCheckResult)
                .where(
                    HealthCheckResult.company_id == company_id,
                    HealthCheckResult.kind == "post_ledger",
                    HealthCheckResult.status == "blocked",
                    ~HealthCheckResult.result.contains({"resolved": True}),
                    ~HealthCheckResult.result.contains({"dismissed": True}),
                )
            )
        ).scalars().all()
        if not rows:
            return []

        keys = [f"{_AI_KEY_PREFIX}:{row.document_id}" for row in rows]
        try:
            existing = await self._redis.mget(keys)
        except Exception:
            logger.exception(
                "[SuHe][Reenrich] Redis MGET failed — assuming all rows need enrichment",
            )
            existing = [None] * len(rows)

        missing: list[dict[str, Any]] = []
        for row, raw in zip(rows, existing):
            if _has_annotation(raw):
                continue
            missing.append(_row_to_enrich_payload(row))
        return missing


# ---------------------- helpers ------------------------------------

def _has_annotation(raw: Any) -> bool:
    if raw is None:
        return False
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    if isinstance(raw, str):
        return raw.strip() != ""
    return bool(raw)


def _row_to_enrich_payload(row: HealthCheckResult) -> dict[str, Any]:
    """The shape ``/api/v1/enrich-row`` expects, sourced from the
    stored ``result`` JSONB so we don't need to re-fetch from Xero."""
    result = row.result or {}
    rule_ids = result.get("rule_ids") or []
    messages = result.get("messages") or row.error_msgs or ""
    flagged_items = result.get("flagged") or []
    # ``transaction`` carries whatever fields the audit had at the
    # time the row was first trapped — enough for the LLM to give a
    # specific explanation without another Xero hop.
    transaction: dict[str, Any] = {
        "type": row.document_type,
        "document_id": str(row.document_id),
    }
    return {
        "row_id": str(row.id),
        "document_id": str(row.document_id),
        "row": {
            "transaction_id": str(row.document_id),
            "rule_ids": [str(r) for r in rule_ids if isinstance(r, str)],
            "messages": str(messages),
            "transaction": transaction,
            "flagged_items": flagged_items if isinstance(flagged_items, list) else [],
        },
    }
