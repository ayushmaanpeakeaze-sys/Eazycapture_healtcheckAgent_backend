"""LLM-based health-check detection.

Two passes, both calling Groq:
* ``_batched_category_audit``  — wrong_category (miscoded accounts).
* ``_batched_capital_review``  — capital_item_review + low_cost_fixed_asset.

These are the only checks that need a model; everything else is
deterministic (see ``deterministic.py``).
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from decimal import Decimal
from typing import Optional

from app.core.config import settings
from app.modules.ai._json import _parse_json_object
from app.modules.ai.client import get_groq
from app.shared.transaction import BatchTransaction, FlaggedIssue
from app.modules.healthcheck.engine.audit_settings import AuditSettings, DEFAULT_SETTINGS
from app.modules.healthcheck.engine.shared import *  # noqa: F401,F403
from app.modules.healthcheck.engine.shared import (
    _allowed_account_types_for_doc,
    _ASSET_ACCOUNT_TYPES,
    _BATCH_CONCURRENCY,
    _CATEGORY_CHUNK_SIZE,
    _CAPITALIZABLE_NAME_KEYWORDS,
    _HIGH_VALUE_THRESHOLD,
    _LLM_MIN_CONFIDENCE,
    _PURE_EXPENSE_ACCOUNT_TYPES,
    CategoryCacheKey,
    KNOWN_XERO_TYPE_CODES,
)

logger = __import__("logging").getLogger("uvicorn.error")

_CATEGORY_BATCH_SYSTEM_PROMPT = (
    "You are a UK bookkeeping reviewer auditing a batch of transactions against "
    "a client's Xero Chart of Accounts. For EACH transaction in the input array, "
    "decide if the posting is right. Only suggest codes from the allowed list. "
    "Return ONLY a JSON object: "
    '{"results": [{"id": int, "looks_correct": boolean, '
    '"suggested_code": string|null, "confidence": number 0-1, '
    '"message": string, "reasoning": string}, ...]} '
    "with one entry per input transaction, matched by id. "
    "Rules per result message: <=140 chars; format as "
    "'<short fact> — code <CODE> (<Name>), not <CURRENT_CODE> (<Current Name>).' "
    "Strip 'Ltd/Inc/LLC' from vendors. No hedging words. "
    "Only flag (looks_correct=false) when confidence >= 0.8 AND the change is "
    "materially better. If the current posting is defensible, looks_correct=true.\n\n"
    "DIRECTION RULE (CRITICAL — read before anything else):\n"
    "Each transaction has a `type` field:\n"
    "  - ACCREC / ACCRECCREDIT  = the business is SELLING. Suggested code "
    "MUST be a revenue account (account_type in REVENUE / OTHERINCOME / SALES).\n"
    "  - ACCPAY / ACCPAYCREDIT  = the business is BUYING. Suggested code "
    "MUST be an expense or asset account (account_type in EXPENSE / "
    "DIRECTCOSTS / OVERHEADS / CURRENTASSET / FIXEDASSET).\n"
    "NEVER suggest an expense code for an ACCREC transaction. NEVER suggest a "
    "revenue code for an ACCPAY transaction. If the current code is already on "
    "the correct side of the ledger, do NOT flag wrong_category — even when a "
    "more specific code exists. Cross-direction flags are the worst failure mode "
    "and will be auto-dropped server-side anyway.\n\n"
    "EXAMPLES (each pattern is per-item):\n\n"
    "In: type=ACCREC. Description 'Consulting work', £5000, current 200 (Sales, REVENUE).\n"
    'Out: {"looks_correct": true, "suggested_code": null, "confidence": 0.95, '
    '"message": "", "reasoning": "Sales code on a sales invoice is correct."}\n\n'
    "In: type=ACCREC. Description 'Freight services', £800, current 461 (Office Supplies, EXPENSE).\n"
    'Out: {"looks_correct": false, "suggested_code": "200", "confidence": 0.95, '
    '"message": "Freight income — code 200 (Sales), not 461 (Office Supplies).", '
    '"reasoning": "ACCREC must post to a revenue account."}\n\n'
    "In: DigitalOcean droplet, £47, current 461 (Office Supplies). Allowed has 412 (Software Subscriptions).\n"
    'Out: {"looks_correct": false, "suggested_code": "412", "confidence": 0.95, '
    '"message": "DigitalOcean — code 412 (Software Subscriptions), not 461 (Office Supplies).", '
    '"reasoning": "Cloud infra is a subscription, not consumables."}\n\n'
    "In: Apple iPad Pro, £2000, current 461. Allowed has 720 (Computer Equipment).\n"
    'Out: {"looks_correct": false, "suggested_code": "720", "confidence": 0.95, '
    '"message": "iPad Pro £2000 — code 720 (Computer Equipment), not 461 (Office Supplies).", '
    '"reasoning": "Hardware over £500 is a capital asset."}\n\n'
    "In: Uber ride to client meeting, £18, current 461. Allowed has 493 (Travel - Local).\n"
    'Out: {"looks_correct": false, "suggested_code": "493", "confidence": 0.90, '
    '"message": "Uber ride to client — code 493 (Travel - Local), not 461 (Office Supplies).", '
    '"reasoning": "Local travel for client meetings belongs in travel."}\n\n'
    "In: Stripe processing fee, £15, current 477 (Bank Fees).\n"
    'Out: {"looks_correct": true, "suggested_code": null, "confidence": 0.90, '
    '"message": "", "reasoning": "Stripe fees in Bank Fees is conventional."}\n\n'
    "In: Monthly electricity, £120, current 445 (Utilities).\n"
    'Out: {"looks_correct": true, "suggested_code": null, "confidence": 0.95, '
    '"message": "", "reasoning": "Utilities posting matches vendor type."}\n\n'
    "In: Generic description ('Invoice', 'Payment') with no other clues.\n"
    'Out: {"looks_correct": true, "suggested_code": null, "confidence": 0.5, '
    '"message": "", "reasoning": "Insufficient signal to override current posting."}'
)


def _cache_key(tx: BatchTransaction) -> CategoryCacheKey:
    """Signature for deduping recurring transactions inside one batch."""
    return (
        tx.vendor_name.strip().lower(),
        str(tx.amount),
        (tx.description or "").strip().lower()[:80],
        (tx.current_account_code or "").strip(),
    )

async def _batched_category_audit(
    transactions: list[BatchTransaction],
    coa_summary: str,
    coa_lookup: dict[str, str],
    coa_type_lookup: dict[str, str],
    progress_callback: Optional[ProgressCallback] = None,
    audit_settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[FlaggedIssue]:
    # Filter out the ACCPAY-style "descriptions" before spending any tokens.
    eligible = [
        tx for tx in transactions
        if (tx.description or "").strip().upper() not in KNOWN_XERO_TYPE_CODES
        and (tx.description or "").strip()
    ]
    if not eligible:
        if progress_callback:
            await progress_callback({
                "event": "categorize_started",
                "unique_txns": 0,
                "chunks": 0,
            })
        return []

    # Group by signature — recurring vendors collapse to a single LLM call.
    by_key: dict[CategoryCacheKey, list[BatchTransaction]] = defaultdict(list)
    for tx in eligible:
        by_key[_cache_key(tx)].append(tx)
    unique_txns = [group[0] for group in by_key.values()]

    chunks = [
        unique_txns[i:i + _CATEGORY_CHUNK_SIZE]
        for i in range(0, len(unique_txns), _CATEGORY_CHUNK_SIZE)
    ]

    if progress_callback:
        await progress_callback({
            "event": "categorize_started",
            "unique_txns": len(unique_txns),
            "total_txns": len(eligible),
            "chunks": len(chunks),
        })

    semaphore = asyncio.Semaphore(_BATCH_CONCURRENCY)
    processed = {"n": 0}

    async def _bounded(chunk: list[BatchTransaction]) -> list[Optional[dict]]:
        async with semaphore:
            output = await _llm_categorize_chunk(
                chunk, coa_summary, coa_lookup, coa_type_lookup,
                min_confidence=audit_settings.llm_min_confidence,
            )
        processed["n"] += len(chunk)
        if progress_callback:
            await progress_callback({
                "event": "categorize_progress",
                "processed": processed["n"],
                "unique_txns": len(unique_txns),
            })
        return output

    chunk_outputs = await asyncio.gather(
        *(_bounded(c) for c in chunks), return_exceptions=True,
    )

    # Build cache: signature → normalized LLM result (or None for no-flag).
    results_by_key: dict[CategoryCacheKey, Optional[dict]] = {}
    for chunk, output in zip(chunks, chunk_outputs):
        if isinstance(output, Exception):
            logger.exception("Category chunk failed", exc_info=output)
            for tx in chunk:
                results_by_key[_cache_key(tx)] = None
            continue
        for tx, llm_data in zip(chunk, output):
            results_by_key[_cache_key(tx)] = llm_data

    # Fan out cached results to every transaction sharing each signature.
    flagged: list[FlaggedIssue] = []
    for tx in eligible:
        llm_data = results_by_key.get(_cache_key(tx))
        if llm_data is None:
            continue
        flag = _build_category_flag(tx, llm_data, coa_lookup)
        if flag is not None:
            flagged.append(flag)
    return flagged

_CAPITAL_REVIEW_SYSTEM_PROMPT = (
    "You are a UK chartered accountant reviewing transactions for capitalisation issues. "
    "Each item is either a LARGE EXPENSE that might need capitalising, or a SMALL ASSET "
    "that might need expensing. Return ONLY a JSON object: "
    '{"results": [{"id": int, "verdict": "capitalise"|"expense"|"correct", '
    '"issue_type": "capital_item_review"|"low_cost_fixed_asset"|null, '
    '"confidence": number 0-1, "message": string (<=140 chars)}]} '
    "Verdict guide:\n"
    "  'capitalise' → transaction is on a P&L expense account but should be a fixed asset "
    "(e.g. new equipment, furniture, vehicle, renovation — anything with lasting economic benefit > 1 year).\n"
    "  'expense' → transaction is on a fixed asset account but the amount is too small or "
    "the item is clearly consumable (e.g. £99 printer cartridge on Computer Equipment, "
    "£200 phone case on Fixtures & Fittings).\n"
    "  'correct' → the current treatment is fine. Use this when uncertain — do NOT flag borderline cases.\n"
    "Only flag (verdict != 'correct') when confidence >= 0.80.\n"
    "FRS 102 guidance: capitalise items with a useful life > 1 year AND cost above the "
    "company's capitalisation threshold (typically £500-£1000). Repairs/maintenance that "
    "restore but do not enhance an asset are EXPENSES even at high value. "
    "Recurring subscription-style charges are ALWAYS expenses regardless of amount.\n"
    "message format: '<vendor> £<amount> — <reason>.' ≤140 chars, no hedging words."
)

_CAPITAL_CHUNK_SIZE = 8
# Capital pre-filter gates are per-client (``AuditSettings``): capital_pre_filter_min
# is the lower gate (LLM decides the real threshold above it); low_cost_asset_max caps
# a low_cost_fixed_asset candidate.


async def _batched_capital_review(
    transactions: list[BatchTransaction],
    coa_type_lookup: dict[str, str],
    coa_lookup: dict[str, str],
    audit_settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[FlaggedIssue]:
    """LLM-based replacement for the deterministic capital_item_review and
    low_cost_fixed_asset checks.

    Two candidate pools:
    - high_expense: amount >= £300 on a P&L account whose name suggests
      something capitalisable (equipment, furniture, renovation, etc.).
    - low_asset:    amount < £10k on a fixed-asset account — might be too
      small to capitalise and should be expensed.

    Both pools go to a single batched LLM call.
    """
    candidates: list[tuple[str, BatchTransaction]] = []  # (check_type, tx)

    for tx in transactions:
        if not tx.current_account_code:
            continue
        code = tx.current_account_code.strip()
        acc_type = coa_type_lookup.get(code, "")
        acc_name = (coa_lookup.get(code) or "").lower()

        # high_expense: large P&L expense → could be capital
        if (
            acc_type in _PURE_EXPENSE_ACCOUNT_TYPES
            and tx.amount >= audit_settings.capital_pre_filter_min
            and any(kw in acc_name for kw in _CAPITALIZABLE_NAME_KEYWORDS)
        ):
            candidates.append(("high_expense", tx))

        # low_asset: small fixed-asset posting → could be expense
        elif (
            acc_type in _ASSET_ACCOUNT_TYPES
            and Decimal("0") < tx.amount < audit_settings.low_cost_asset_max
        ):
            candidates.append(("low_asset", tx))

    if not candidates:
        return []

    chunks = [
        candidates[i:i + _CAPITAL_CHUNK_SIZE]
        for i in range(0, len(candidates), _CAPITAL_CHUNK_SIZE)
    ]

    flagged: list[FlaggedIssue] = []
    for chunk in chunks:
        chunk_flags = await _llm_capital_chunk(
            chunk, coa_lookup, coa_type_lookup,
            min_confidence=audit_settings.llm_min_confidence,
        )
        flagged.extend(chunk_flags)
    return flagged

async def _llm_capital_chunk(
    chunk: list[tuple[str, BatchTransaction]],
    coa_lookup: dict[str, str],
    coa_type_lookup: dict[str, str],
    min_confidence: float = _LLM_MIN_CONFIDENCE,
) -> list[FlaggedIssue]:
    items = []
    for i, (check_type, tx) in enumerate(chunk):
        code = (tx.current_account_code or "").strip()
        items.append({
            "id": i,
            "check_type": check_type,
            "vendor": tx.vendor_name,
            "description": (tx.description or "").strip()[:200],
            "amount": str(tx.amount),
            "currency": tx.currency_code or "GBP",
            "current_account_code": code,
            "current_account_name": coa_lookup.get(code) or "",
            "current_account_type": coa_type_lookup.get(code) or "",
        })

    user_prompt = (
        f"Transactions to review ({len(items)} items):\n"
        f"{json.dumps(items, default=str)}\n"
        "For each: should it be capitalised, expensed, or is it already correct?"
    )

    try:
        client = get_groq()
        completion = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            max_tokens=200 * len(chunk) + 400,
            temperature=0,
            response_format={"type": "json_object"},
            extra_body={"reasoning_effort": "low"},
            messages=[
                {"role": "system", "content": _CAPITAL_REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = completion.choices[0].message.content or ""
        data = _parse_json_object(raw)
    except Exception:
        logger.exception("Capital review LLM chunk failed for %d items", len(chunk))
        return []

    results_arr = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results_arr, list):
        return []

    by_id = {
        int(r["id"]): r
        for r in results_arr
        if isinstance(r, dict) and "id" in r
    }

    flagged: list[FlaggedIssue] = []
    for i, (check_type, tx) in enumerate(chunk):
        item = by_id.get(i)
        if not item:
            continue
        verdict = str(item.get("verdict") or "correct").strip().lower()
        if verdict == "correct":
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < min_confidence:
            continue

        # Enforce pool/verdict consistency: high_expense can only become
        # capital_item_review, low_asset only low_cost_fixed_asset. Contradicting
        # verdicts are dropped; the model's own `issue_type` is ignored.
        if check_type == "high_expense":
            if verdict != "capitalise":
                continue
            issue_type = "capital_item_review"
        else:  # "low_asset"
            if verdict != "expense":
                continue
            issue_type = "low_cost_fixed_asset"

        message = str(item.get("message") or "").strip()[:140]
        if not message:
            continue
        flagged.append(FlaggedIssue(
            transaction_id=tx.transaction_id,
            issue_type=issue_type,  # type: ignore[arg-type]
            severity="medium",
            message=message,
            current_code=(tx.current_account_code or "").strip() or None,
            confidence=confidence,
        ))
    return flagged

async def _llm_categorize_chunk(
    chunk: list[BatchTransaction],
    coa_summary: str,
    coa_lookup: dict[str, str],
    coa_type_lookup: dict[str, str],
    min_confidence: float = _LLM_MIN_CONFIDENCE,
) -> list[Optional[dict]]:
    """One LLM call for N transactions. Returns N normalized dicts (or Nones)."""
    items = [
        {
            "id": i,
            "type": (tx.type or "").strip().upper() or None,
            "vendor": tx.vendor_name,
            "description": (tx.description or "").strip()[:200],
            "amount": str(tx.amount),
            "current_code": tx.current_account_code,
            "current_name": (
                coa_lookup.get(tx.current_account_code.strip(), "unknown")
                if tx.current_account_code
                else "unknown"
            ),
            "current_account_type": (
                coa_type_lookup.get(tx.current_account_code.strip(), "unknown")
                if tx.current_account_code
                else "unknown"
            ),
        }
        for i, tx in enumerate(chunk)
    ]
    user_prompt = (
        f"Allowed accounts (JSON): {coa_summary}\n\n"
        f"Transactions (JSON array, {len(items)} items): {json.dumps(items)}\n\n"
        "Return one result object per input, matched by id."
    )
    try:
        client = get_groq()
        completion = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            max_tokens=180 * len(chunk) + 200,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _CATEGORY_BATCH_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = completion.choices[0].message.content or ""
        data = _parse_json_object(raw)
    except Exception:
        logger.exception("Groq batched category audit failed")
        return [None] * len(chunk)

    results_arr = data.get("results") or []
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

    out: list[Optional[dict]] = []
    for i, tx in enumerate(chunk):
        item = by_id.get(i)
        if item is None:
            out.append(None)
            continue
        out.append(_normalize_llm_item(
            tx, item, coa_lookup, coa_type_lookup, min_confidence,
        ))
    return out

def _normalize_llm_item(
    tx: BatchTransaction,
    item: dict,
    coa_lookup: dict[str, str],
    coa_type_lookup: dict[str, str],
    min_confidence: float = _LLM_MIN_CONFIDENCE,
) -> Optional[dict]:
    """Apply confidence threshold + strict COA enforcement. Returns None to drop."""
    if item.get("looks_correct", True):
        return None
    try:
        confidence = float(item.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < min_confidence:
        return None
    suggested_code = item.get("suggested_code")
    if not isinstance(suggested_code, str):
        return None
    suggested_code = suggested_code.strip()
    if suggested_code not in coa_lookup:
        return None
    if tx.current_account_code and suggested_code == tx.current_account_code.strip():
        return None

    # Direction guard: drop flags that cross the ACCREC/ACCPAY ledger side,
    # e.g. posting a sales invoice to an expense code.
    allowed_types = _allowed_account_types_for_doc(tx.type)
    if allowed_types is not None:
        suggested_type = coa_type_lookup.get(suggested_code, "")
        if suggested_type and suggested_type not in allowed_types:
            logger.info(
                "Dropping wrong-direction flag for %s: tx.type=%s "
                "suggested=%s (%s), allowed=%s",
                tx.transaction_id, tx.type, suggested_code, suggested_type,
                ",".join(sorted(allowed_types)),
            )
            return None
        # If the current code is already on the right side, only let
        # high-confidence (>=0.9) flags through.
        current_code = (tx.current_account_code or "").strip()
        current_type = coa_type_lookup.get(current_code, "")
        if current_type in allowed_types and confidence < 0.9:
            return None

    return {
        "confidence": confidence,
        "suggested_code": suggested_code,
        "message": str(item.get("message") or "").strip(),
        "reasoning": str(item.get("reasoning") or "").strip(),
    }


def _build_category_flag(
    tx: BatchTransaction,
    llm_data: dict,
    coa_lookup: dict[str, str],
) -> Optional[FlaggedIssue]:
    suggested_code = llm_data["suggested_code"]
    suggested_name = coa_lookup.get(suggested_code)
    current_code = tx.current_account_code
    severity: Severity = "high" if tx.amount > _HIGH_VALUE_THRESHOLD else "medium"
    message = llm_data["message"] or (
        f"{tx.vendor_name} — code {suggested_code} ({suggested_name})."
    )
    return FlaggedIssue(
        transaction_id=tx.transaction_id,
        issue_type="wrong_category",
        severity=severity,
        message=message[:140],
        suggested_code=suggested_code,
        suggested_name=suggested_name,
        current_code=current_code,
        confidence=llm_data["confidence"],
        reasoning=llm_data["reasoning"],
    )


# =====================================================================
# Anomaly review (LLM) — holistic verdict on amount-outlier candidates
# =====================================================================

_ANOMALY_SYSTEM_PROMPT = (
    "You are a forensic UK accountant reviewing transactions that a "
    "statistical check has already flagged as unusually large for their "
    "vendor. For EACH item you are given the transaction plus the vendor's "
    "baseline (typical amount, usual account, usual tax code, how many "
    "transactions that vendor has). Decide whether the item is a GENUINE "
    "anomaly worth a human's attention, or explainable/benign.\n"
    "Genuine anomaly signals: amount wildly off baseline AND a second oddity "
    "(different account than usual, different tax code, vague description, "
    "round-number that looks like a typo, possible duplicate of another "
    "vendor). Benign: an annual renewal vs monthly fees, a known one-off "
    "capital purchase, a deposit — explainable by the description.\n"
    "Return ONLY JSON: {\"results\": [{\"id\": int, \"is_anomaly\": boolean, "
    "\"confidence\": number 0..1, \"explanation\": string (<=240 chars), "
    "\"severity\": one of \"high\"|\"medium\"}]} one entry per input item. "
    "Be specific: name the vendor, the amount, and WHY it is or isn't an "
    "anomaly. No markdown, no extra keys."
)

_ANOMALY_CHUNK_SIZE = 8


async def _llm_anomaly_review(
    candidates: list[dict],
    audit_settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[FlaggedIssue]:
    """Take deterministic amount-outlier candidates and let the LLM decide
    which are genuine anomalies, with a business explanation.

    Confirmed anomalies (is_anomaly + confidence >= 0.80) become ``anomaly``
    flags. Candidates the LLM judges explainable are dropped. On any LLM
    failure the caller falls back to the raw deterministic ``amount_outlier``.
    """
    if not candidates:
        return []

    chunks = [
        candidates[i:i + _ANOMALY_CHUNK_SIZE]
        for i in range(0, len(candidates), _ANOMALY_CHUNK_SIZE)
    ]
    flagged: list[FlaggedIssue] = []
    for chunk in chunks:
        flagged.extend(await _llm_anomaly_chunk(
            chunk, min_confidence=audit_settings.llm_min_confidence,
        ))
    return flagged


async def _llm_anomaly_chunk(
    chunk: list[dict],
    min_confidence: float = _LLM_MIN_CONFIDENCE,
) -> list[FlaggedIssue]:
    items = []
    for i, c in enumerate(chunk):
        tx = c["tx"]
        items.append({
            "id": i,
            "vendor": tx.vendor_name,
            "amount": str(tx.amount),
            "currency": tx.currency_code or "GBP",
            "description": (tx.description or "")[:160],
            "account_code": tx.current_account_code or "unknown",
            "tax_code": tx.tax_code or "none",
            "doc_type": (tx.type or "unknown"),
            "vendor_typical_amount": str(c["median"]),
            "times_above_typical": round(c["ratio"], 1),
            "vendor_usual_account": c.get("usual_account") or "unknown",
            "vendor_usual_tax": c.get("usual_tax") or "unknown",
            "vendor_txn_count": c["vendor_txn_count"],
        })
    user_prompt = (
        f"Review these {len(items)} flagged transactions: "
        f"{json.dumps(items, default=str)}\n"
        "Return one verdict per item. Only mark is_anomaly=true when it is "
        "genuinely suspicious, not merely large."
    )
    try:
        client = get_groq()
        completion = await client.chat.completions.create(
            model=settings.GROQ_INSIGHT_MODEL,
            max_tokens=250 * len(chunk) + 500,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _ANOMALY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        data = _parse_json_object(completion.choices[0].message.content or "")
    except Exception:
        # LLM failed → fall back to deterministic amount_outlier for this chunk.
        logger.exception("anomaly LLM chunk failed — using deterministic outlier")
        from app.modules.healthcheck.engine.deterministic import amount_outlier_flag
        return [amount_outlier_flag(c) for c in chunk]

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        from app.modules.healthcheck.engine.deterministic import amount_outlier_flag
        return [amount_outlier_flag(c) for c in chunk]

    by_id = {int(r["id"]): r for r in results if isinstance(r, dict) and "id" in r}
    out: list[FlaggedIssue] = []
    for i, c in enumerate(chunk):
        verdict = by_id.get(i)
        tx = c["tx"]
        if not verdict:
            continue
        if not verdict.get("is_anomaly"):
            continue  # LLM says explainable → drop
        try:
            confidence = max(0.0, min(1.0, float(verdict.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < min_confidence:
            continue
        severity = str(verdict.get("severity") or "medium").lower()
        if severity not in {"high", "medium"}:
            severity = "medium"
        explanation = str(verdict.get("explanation") or "").strip()[:240]
        if not explanation:
            explanation = amount_outlier_msg(c)
        out.append(FlaggedIssue(
            transaction_id=tx.transaction_id,
            issue_type="anomaly",
            severity=severity,  # type: ignore[arg-type]
            message=explanation,
            confidence=confidence,
        ))
    return out


def amount_outlier_msg(c: dict) -> str:
    tx = c["tx"]
    cur = (tx.currency_code or "GBP").strip().upper()
    sym = "£" if cur == "GBP" else f"{cur} "
    return (
        f"{tx.vendor_name} {sym}{tx.amount:.2f} is {c['ratio']:.1f}x its "
        f"typical {sym}{c['median']:.2f} — unusual."
    )[:240]
