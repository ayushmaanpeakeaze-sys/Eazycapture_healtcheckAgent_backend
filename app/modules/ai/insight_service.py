"""LLM enrichment for the post-ledger health-check trapped rows.

Two entry points:
  * ``enrich_audit_async`` — fire-and-forget enrichment for a whole batch.
    Per-row records land at ``health_check_ai:{transaction_id}``; the batch
    summary goes into the existing ``xero_historical_audit_batch:{batch_id}``
    hash under ``_meta.audit_summary``.
  * ``suggest_fix`` — synchronous fix recommendation for a single row.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from app.core.config import settings
from app.modules.ai._json import _parse_json_object
from app.modules.ai.client import get_groq
from app.core.redis_client import get_redis
from app.modules.ai.facts import (
    build_row_facts,
    contact_noun as _contact_noun,
    extract_doc_type as _extract_doc_type,
    pull as _pull,
)
from app.modules.ai.prompts import (
    _FIX_SYSTEM_PROMPT,
    _ROW_BATCH_SYSTEM_PROMPT,
    _SUMMARY_SYSTEM_PROMPT,
)
from app.modules.ai.schemas import (
    AuditSummary,
    EnrichAuditRequest,
    HealthCheckAIRecord,
    SuggestFixRequest,
    SuggestFixResponse,
    TrappedRow,
)

logger = logging.getLogger("uvicorn.error")

_ENRICH_CONCURRENCY = 2  # 8000 TPM free-tier cap: ~3k tokens × 2 in flight fits.
_ENRICH_CHUNK_SIZE = 5   # rows packed into one LLM call — amortises the system prompt.
_BATCH_TOTAL_FIELD = "_meta.ai_enriched_total"
_BATCH_COUNTER_FIELD = "_meta.ai_enriched_count"
_BATCH_TTL_SECONDS = 3600
_BATCH_HASH_PREFIX = "xero_historical_audit_batch"
_ROW_KEY_PREFIX = "health_check_ai"
_BATCH_SUMMARY_FIELD = "_meta.audit_summary"
_VALID_SEVERITIES = {"critical", "high", "medium", "low"}


# =====================================================================
# Async batch enrichment
# =====================================================================

async def enrich_audit_async(req: EnrichAuditRequest) -> int:
    """Kick off background enrichment. Returns number of rows queued."""
    if not req.trapped_rows:
        # Still write a (trivial) summary so the batch hash has the field.
        asyncio.create_task(_run_enrichment(req))
        return 0
    asyncio.create_task(_run_enrichment(req))
    return len(req.trapped_rows)


async def enrich_row_sync(
    row: TrappedRow,
    batch_id: Optional[str] = None,
) -> Optional[HealthCheckAIRecord]:
    """On-demand enrichment for a single row.

    Used when the user opens a trapped row whose AI insight hasn't landed
    yet (the batch is still working through earlier chunks). Reuses the
    same chunked prompt path with a 1-item chunk, so the wording and
    schema match the background batch. Writes through to Redis so the next
    polling tick from Django hits the cache.
    """
    records = await _classify_chunk([row])
    record = records[0] if records else None
    if record is None:
        return None
    redis = get_redis()
    try:
        await redis.set(
            f"{_ROW_KEY_PREFIX}:{row.transaction_id}",
            json.dumps(record.model_dump(mode="json")),
            ex=settings.HEALTHCHECK_AI_TTL_SECONDS,
        )
        # Keep the batch progress counter consistent when a row gets
        # ahead of the background batch.
        if batch_id:
            await redis.hincrby(
                f"{_BATCH_HASH_PREFIX}:{batch_id}",
                _BATCH_COUNTER_FIELD,
                1,
            )
    except Exception:
        logger.exception(
            "redis write failed for on-demand row=%s batch=%s",
            row.transaction_id, batch_id,
        )
    return record


async def _run_enrichment(req: EnrichAuditRequest) -> None:
    # Initialise the progress counter immediately so Django can poll
    # ai_enriched_count/ai_enriched_total with no empty-hash race.
    try:
        await _init_progress(req)
    except Exception:
        logger.exception("init_progress failed for batch=%s", req.batch_id)
    # Summary and per-row enrichment run in parallel so neither blocks
    # or loses the other on failure.
    results = await asyncio.gather(
        _enrich_rows(req),
        _write_summary(req),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            logger.exception(
                "enrich-audit subtask failed for batch=%s company=%s",
                req.batch_id, req.company_id, exc_info=r,
            )


async def _init_progress(req: EnrichAuditRequest) -> None:
    redis = get_redis()
    batch_key = f"{_BATCH_HASH_PREFIX}:{req.batch_id}"
    await redis.hset(
        batch_key,
        mapping={
            _BATCH_TOTAL_FIELD: len(req.trapped_rows),
            _BATCH_COUNTER_FIELD: 0,
        },
    )
    await redis.expire(batch_key, _BATCH_TTL_SECONDS)


async def _enrich_rows(req: EnrichAuditRequest) -> None:
    if not req.trapped_rows:
        return
    semaphore = asyncio.Semaphore(_ENRICH_CONCURRENCY)
    redis = get_redis()
    batch_key = f"{_BATCH_HASH_PREFIX}:{req.batch_id}"

    chunks = [
        req.trapped_rows[i:i + _ENRICH_CHUNK_SIZE]
        for i in range(0, len(req.trapped_rows), _ENRICH_CHUNK_SIZE)
    ]

    async def _do_chunk(chunk: list[TrappedRow]) -> None:
        async with semaphore:
            records = await _classify_chunk(chunk)
        for row, record in zip(chunk, records):
            if record is None:
                # Row enrichment failed; leave the counter so absence
                # signals Django to use the rule-based message.
                logger.warning(
                    "enrich-audit: no AI record for row=%s in batch=%s",
                    row.transaction_id, req.batch_id,
                )
                continue
            key = f"{_ROW_KEY_PREFIX}:{row.transaction_id}"
            try:
                await redis.set(
                    key,
                    json.dumps(record.model_dump(mode="json")),
                    ex=settings.HEALTHCHECK_AI_TTL_SECONDS,
                )
                # Per-row counter bump → Django sees "13 of 21" live even
                # when rows arrive in chunks of N.
                await redis.hincrby(batch_key, _BATCH_COUNTER_FIELD, 1)
            except Exception:
                logger.exception(
                    "redis write failed for batch=%s row=%s",
                    req.batch_id, row.transaction_id,
                )

    await asyncio.gather(
        *(_do_chunk(c) for c in chunks),
        return_exceptions=True,
    )


async def _classify_chunk(
    chunk: list[TrappedRow],
) -> list[Optional[HealthCheckAIRecord]]:
    """One LLM call for up to _ENRICH_CHUNK_SIZE rows. Returns N records (or Nones)."""
    if not chunk:
        return []
    # The grounding contract — exactly the facts the LLM may see (see facts.py).
    items = [build_row_facts(row, i) for i, row in enumerate(chunk)]
    user_prompt = (
        f"Transactions to analyse ({len(items)} items): {json.dumps(items, default=str)}\n"
        "Return one result per item matched by id. Use the vendor name, amount, and "
        "account code. Ground the explanation in the business_impact — what financially "
        "goes wrong if this is not fixed — and reference the recommended_action."
    )
    try:
        client = get_groq()
        completion = await client.chat.completions.create(
            model=settings.GROQ_INSIGHT_MODEL,
            # ~300 output tokens per row + ~800 base prompt.
            max_tokens=350 * len(chunk) + 800,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _ROW_BATCH_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = completion.choices[0].message.content or ""
        data = _parse_json_object(raw)
    except Exception:
        logger.exception(
            "LLM chunk enrichment failed for %d rows starting %s",
            len(chunk), chunk[0].transaction_id,
        )
        return [None] * len(chunk)

    results_arr = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results_arr, list):
        return [None] * len(chunk)

    by_id: dict[int, dict] = {}
    for item in results_arr:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        by_id[idx] = item

    out: list[Optional[HealthCheckAIRecord]] = []
    for i in range(len(chunk)):
        item = by_id.get(i)
        out.append(_record_from_item(item) if item else None)
    return out


def _record_from_item(item: dict) -> Optional[HealthCheckAIRecord]:
    severity = str(item.get("severity_ai", "medium")).strip().lower()
    if severity not in _VALID_SEVERITIES:
        severity = "medium"
    try:
        confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    reg_ref = item.get("regulatory_ref")
    if isinstance(reg_ref, str) and not reg_ref.strip():
        reg_ref = None
    explanation = str(item.get("explanation", "")).strip()[:450]
    if not explanation:
        return None
    # If model gave a real explanation but output 0% confidence, treat as
    # uncertain (0.6) rather than showing "0% confident" which misleads users.
    if confidence == 0.0 and len(explanation) > 40:
        confidence = 0.6
    return HealthCheckAIRecord(
        explanation=explanation,
        severity_ai=severity,  # type: ignore[arg-type]
        confidence=confidence,
        regulatory_ref=reg_ref if isinstance(reg_ref, str) else None,
    )


async def _write_summary(req: EnrichAuditRequest) -> None:
    summary = await _summarise_batch(req)
    if summary is None:
        return
    redis = get_redis()
    await redis.hset(
        f"{_BATCH_HASH_PREFIX}:{req.batch_id}",
        _BATCH_SUMMARY_FIELD,
        json.dumps(summary.model_dump(mode="json")),
    )


async def _summarise_batch(req: EnrichAuditRequest) -> Optional[AuditSummary]:
    # Compress rows to the bits the LLM needs — rule_ids + the deterministic
    # message is enough signal for theming without burning tokens on raw payloads.
    digest = [
        {
            "id": row.transaction_id,
            "type": _extract_doc_type(row.transaction) or "unknown",
            "rules": row.rule_ids,
            "msg": (row.messages or "")[:160],
        }
        for row in req.trapped_rows[:200]
    ]
    user_prompt = (
        f"Company: {req.company_id}\n"
        f"Batch: {req.batch_id}\n"
        f"Total documents in batch: {req.total_documents}\n"
        f"Trapped rows: {len(req.trapped_rows)}\n"
        f"Sample digest (first {len(digest)}): {json.dumps(digest)}\n"
        "Summarise the state of this ledger and what to clean up first."
    )
    try:
        client = get_groq()
        completion = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            max_tokens=2000,
            temperature=0,
            response_format={"type": "json_object"},
            extra_body={"reasoning_effort": "low"},
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = completion.choices[0].message.content or ""
        data = _parse_json_object(raw)
    except Exception:
        logger.exception("LLM batch summary failed for %s", req.batch_id)
        return None

    themes = data.get("top_themes") or []
    order = data.get("suggested_cleanup_order") or []
    return AuditSummary(
        summary=str(data.get("summary", "")).strip()[:400],
        top_themes=[str(t).strip() for t in themes if str(t).strip()][:5],
        suggested_cleanup_order=[str(s).strip() for s in order if str(s).strip()][:5],
    )


# =====================================================================
# Sync fix suggester
# =====================================================================

_RECODE_RULES = [
    ("furniture", "461"),
    ("bt ", "429"), ("virgin media", "429"), ("sky ", "429"), ("vodafone", "429"),
    ("aws", "485"), ("azure", "485"), ("google cloud", "485"), ("xero", "485"),
    ("salesforce", "485"), ("microsoft", "485"), ("adobe", "485"),
    ("uber", "493"), ("addison lee", "493"), ("taxi", "493"),
    ("tesco", "425"), ("sainsbury", "425"), ("waitrose", "425"), ("costa", "425"),
    ("shell", "437"), ("bp ", "437"), ("fuel", "437"), ("esso", "437"),
    ("amazon", "400"), ("staples", "400"),
]

_RECODE_ISSUE_TYPES = {
    "wrong_category", "wrong_direction_account",
    "unexpected_account", "multi_account_supplier",
}


def _recode_instruction(
    issue_type: str,
    vendor: str,
    current_code: str,
    suggested_code: Optional[str],
) -> str:
    if issue_type not in _RECODE_ISSUE_TYPES:
        return ""
    # Use rules engine suggestion first
    if suggested_code and suggested_code.strip():
        target = suggested_code.strip()
        return (
            f"\nThis is an account recoding issue. The rules engine already determined "
            f"the correct account is {target}. "
            f"Set line_item_updates={{\"AccountCode\":\"{target}\"}} — do not use manual_only."
        )
    # Infer from vendor name
    vendor_lower = (vendor or "").lower()
    target = None
    for keyword, code in _RECODE_RULES:
        if keyword in vendor_lower:
            target = code
            break
    if target:
        return (
            f"\nThis is an account recoding issue. Vendor '{vendor}' maps to account {target}. "
            f"Current account {current_code} is incorrect. "
            f"Set line_item_updates={{\"AccountCode\":\"{target}\"}} — do not use manual_only."
        )
    return (
        f"\nThis is an account recoding issue. Current account {current_code} is wrong. "
        f"Vendor '{vendor}': infer the most appropriate UK SME expense account code "
        f"(400-500 range) and set line_item_updates with that AccountCode. "
        f"Do not use manual_only — pick the most plausible code."
    )


async def suggest_fix(req: SuggestFixRequest) -> SuggestFixResponse:
    tx = req.transaction
    doc_type = (tx.document_type or "").strip().upper() or None
    contact_label = _contact_noun(doc_type)
    extra = tx.model_dump(
        exclude={"transaction_id", "document_type", "rule_id", "messages", "result"}
    )
    # Surface the structured fields the LLM needs: status (PAID-void
    # avoidance) and duplicate-pair metadata (target_transaction_id).
    status = _pull(["status", "Status"], tx.result, extra)
    dup_of = _pull(
        ["duplicate_of_transaction_id", "DuplicateOfTransactionId"],
        tx.result, extra,
    )
    dup_inv = _pull(
        ["duplicate_of_invoice_number", "DuplicateOfInvoiceNumber"],
        tx.result, extra,
    )
    is_original = _pull(
        ["this_is_likely_original", "ThisIsLikelyOriginal"],
        tx.result, extra,
    )
    vendor_name = _pull(["vendor_name"], tx.result, extra) or "Unknown"
    current_account = _pull(["current_account_code"], tx.result, extra) or "unknown"
    suggested_account = _pull(["suggested_account_code"], tx.result, extra)
    issue_type = _pull(["issue_type"], tx.result, extra) or req.rule_id
    flagged_items = _pull(["flagged_items"], tx.result, extra)
    coa = _pull(["chart_of_accounts"], tx.result, extra)

    # Build a compact COA line for recoding issues so the LLM picks
    # from REAL account codes in this org, not generic guesses.
    coa_line = ""
    if coa and isinstance(coa, list):
        expense_accounts = [
            f"{a['code']} ({a['name']})"
            for a in coa
            if isinstance(a, dict)
            and a.get("type", "") in {
                "EXPENSE", "OVERHEADS", "DIRECTCOSTS",
                "FIXEDASSET", "CURRENTASSET", "ASSET",
            }
        ][:40]
        if expense_accounts:
            coa_line = (
                f"\nChart of Accounts — expense + asset accounts for this org: "
                f"{', '.join(expense_accounts)}"
            )

    user_prompt = (
        f"Document type: {doc_type or 'unknown'} "
        f"(refer to the contact as '{contact_label}')\n"
        f"Vendor / contact name: {vendor_name}\n"
        f"Current account code: {current_account}\n"
        f"Suggested account code (from rules engine): {suggested_account or 'none'}\n"
        f"Issue type: {issue_type}\n"
        f"Current status: {status or 'unknown'}\n"
        f"Duplicate-pair: of_id={dup_of or 'none'}  of_invoice={dup_inv or 'none'}  "
        f"this_is_likely_original={is_original}\n"
        f"Rule that fired: {req.rule_id}\n"
        f"Transaction id: {tx.transaction_id}\n"
        f"Deterministic finding: {tx.messages or 'none'}\n"
        f"Flagged items detail: {json.dumps(flagged_items, default=str)[:400] if flagged_items else 'none'}\n"
        + coa_line + "\n"
        + _recode_instruction(issue_type, vendor_name, current_account, suggested_account)
        + "\nReturn the JSON fix plan."
        + (" Pick AccountCode only from the Chart of Accounts list above." if coa_line else "")
    )
    try:
        client = get_groq()
        completion = await client.chat.completions.create(
            model=settings.GROQ_INSIGHT_MODEL,
            max_tokens=1500,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _FIX_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = completion.choices[0].message.content or ""
        data = _parse_json_object(raw)
    except Exception:
        logger.exception("LLM suggest-fix failed for rule=%s tx=%s", req.rule_id, tx.transaction_id)
        return _fallback_fix(req)

    steps = data.get("human_steps") or []
    try:
        minutes = int(data.get("estimated_minutes", 5))
    except (TypeError, ValueError):
        minutes = 5
    minutes = max(1, min(60, minutes))
    raw_target = data.get("target_transaction_id")
    target = str(raw_target).strip()[:128] if isinstance(raw_target, str) and raw_target.strip() else req.transaction.transaction_id
    strategy = str(data.get("fix_strategy", "manual_review")).strip()[:64] or "manual_review"
    field_updates = _sanitize(
        data.get("field_updates"), _ALLOWED_FIELD_UPDATE_KEYS,
    )
    line_item_updates = _sanitize(
        data.get("line_item_updates"), _ALLOWED_LINE_ITEM_UPDATE_KEYS,
        aliases={"TaxCode": "TaxType"},
    )
    # A manual_only fix must not carry update payloads, even if the LLM
    # slipped invented values in.
    if strategy == "manual_only":
        field_updates = None
        line_item_updates = None
    return SuggestFixResponse(
        fix_strategy=strategy,
        xero_action=str(data.get("xero_action", "")).strip()[:200],
        human_steps=[str(s).strip() for s in steps if str(s).strip()][:5],
        rationale=str(data.get("rationale", "")).strip()[:240],
        estimated_minutes=minutes,
        target_transaction_id=target,
        field_updates=field_updates,
        line_item_updates=line_item_updates,
    )


# Allow-lists matching Django's apply-ai-fix use case; any key outside
# these is dropped server-side before reaching Xero.
_ALLOWED_FIELD_UPDATE_KEYS = {
    "Date", "DueDate", "InvoiceNumber", "Reference",
    "Status", "LineAmountTypes",
}
_ALLOWED_LINE_ITEM_UPDATE_KEYS = {"AccountCode", "TaxType"}


def _sanitize(
    raw: object,
    allowed: set[str],
    aliases: Optional[dict[str, str]] = None,
) -> Optional[dict]:
    """Keep only allow-listed scalar keys; map aliases (e.g. TaxCode→TaxType)."""
    if not isinstance(raw, dict):
        return None
    cleaned: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        if aliases and key in aliases:
            key = aliases[key]
        if key not in allowed:
            continue
        if value is None or isinstance(value, (dict, list)):
            continue
        cleaned[key] = value
    return cleaned or None


def _fallback_fix(req: SuggestFixRequest) -> SuggestFixResponse:
    tx = req.transaction
    rule = req.rule_id.replace("_", " ")
    vendor = str((tx.result or {}).get("vendor_name") or "").strip()
    vendor_label = f" for {vendor}" if vendor else ""
    return SuggestFixResponse(
        fix_strategy="manual_review",
        xero_action="Open in Xero and correct manually.",
        human_steps=[
            f"Open this transaction{vendor_label} in Xero.",
            f"Review the {rule} issue flagged by the health check.",
            "Apply the correct account code, tax code, or status as needed.",
            "Save the changes.",
        ],
        rationale=f"{rule.capitalize()} requires manual review{vendor_label}. Apply the appropriate correction in Xero.",
        estimated_minutes=5,
        target_transaction_id=tx.transaction_id,
    )
