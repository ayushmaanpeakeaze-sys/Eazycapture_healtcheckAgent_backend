"""Shared constants + context helpers for the health-check engine.

Used by both the deterministic rules and the LLM detection passes.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Optional

from app.shared.transaction import BatchContext, BatchTransaction, FlaggedIssue


def _account_lines(
    tx: BatchTransaction,
) -> list[tuple[Optional[int], Optional[str], Optional[Decimal]]]:
    """(line_no, account_code, amount) per document line — falls back to the
    flat ``current_account_code`` / ``amount`` when there are no line items.
    Shared by the deterministic rules and the per-category check modules."""
    if tx.line_items:
        return [
            (idx + 1, item.account_code, item.amount)
            for idx, item in enumerate(tx.line_items)
        ]
    return [(None, tx.current_account_code, tx.amount)]

logger = __import__("logging").getLogger("uvicorn.error")

_BATCH_CONCURRENCY = 5
_CATEGORY_CHUNK_SIZE = 10
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_DUPLICATE_WINDOW_DAYS = 7
_VENDOR_FUZZ_THRESHOLD = 72
_LLM_MIN_CONFIDENCE = 0.80
_HIGH_VALUE_THRESHOLD = Decimal("500")
_OVERDUE_DAYS_THRESHOLD = 60
KNOWN_XERO_TYPE_CODES = {"ACCPAY", "ACCREC", "ACCRECCREDIT", "ACCPAYCREDIT"}
_OPEN_BILL_STATUSES = {"AUTHORISED", "SUBMITTED"}
_SUPPLIER_PATTERN_MIN_TXNS = 3
_SUPPLIER_PATTERN_DOMINANCE = 0.7
_FREQUENCY_MIN_BATCH = 100
_FREQUENCY_DOMINANT_MIN = 5
_UNAPPROVED_AGE_DAYS = 7
_UNAPPROVED_STATUSES = {"DRAFT", "SUBMITTED"}
_ZERO_VAT_LIKE_CODES = {"NONE", "ZERORATED", "EXEMPT", "NOTAX"}
_LOW_COST_ASSET_THRESHOLD = Decimal("500")
_CAPITAL_REVIEW_THRESHOLD = Decimal("1000")
_ASSET_ACCOUNT_TYPES = {"FIXEDASSET", "CURRENTASSET", "ASSET"}
_PURE_EXPENSE_ACCOUNT_TYPES = {"EXPENSE", "OVERHEADS", "DIRECTCOSTS"}
_CREDIT_DOC_TYPES = {"ACCRECCREDIT", "ACCPAYCREDIT"}

# Whitelist of account-name keywords eligible for capital_item_review; FRS 102
# bars capitalising rent, advertising, entertainment and other recurring costs.
_CAPITALIZABLE_NAME_KEYWORDS = {
    "equipment", "machinery", "machine", "furniture", "fixture",
    "vehicle", "fleet", "car", "van", "truck", "tool",
    "repair", "maintenance", "improvement", "renovation",
    "leasehold", "capital", "plant", "hardware",
}

# Account-name keywords marking an account as "vague" (catch-all), matched as
# substrings on the lowercased name; feeds the Misallocated-Items check.
_VAGUE_ACCOUNT_NAME_KEYWORDS = (
    "uncategorised", "uncategorized", "unapplied", "unspecified",
    "general expense", "general expenses", "sundry", "miscellaneous",
    "misc expense", "suspense", "to be allocated", "ask my accountant",
)

# Xero direction: ACCREC/ACCRECCREDIT post to revenue-side accounts,
# ACCPAY/ACCPAYCREDIT to expense- or asset-side accounts.
_SALES_DOC_TYPES = {"ACCREC", "ACCRECCREDIT"}
_PURCHASE_DOC_TYPES = {"ACCPAY", "ACCPAYCREDIT"}
# Bank transactions: RECEIVE is sales-side (money in), SPEND is purchase-side.
# These feed only the Unexpected-Account / Unexpected-Tax checks.
_MONEY_IN_TYPES = {"RECEIVE"}
_MONEY_OUT_TYPES = {"SPEND"}
_BANK_TXN_TYPES = _MONEY_IN_TYPES | _MONEY_OUT_TYPES
_REVENUE_ACCOUNT_TYPES = {"REVENUE", "OTHERINCOME", "SALES"}
_EXPENSE_ACCOUNT_TYPES = {
    # Granular Xero AccountType values
    "EXPENSE", "DIRECTCOSTS", "OVERHEADS", "DEPRECIATN",
    "CURRENTASSET", "FIXEDASSET", "INVENTORY", "PREPAYMENT",
    # Coarse Xero AccountClass values (some Django payloads send these instead)
    "ASSET", "LIABILITY",
}

CategoryCacheKey = tuple[str, str, str, str]

def _parse_json_object(raw: str) -> dict:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(raw)
        if not match:
            raise
        return json.loads(match.group(0))

async def _noop_issues() -> list[FlaggedIssue]:
    """Awaitable that yields no issues — used when an LLM pass is disabled."""
    return []

def _allowed_tax_codes(context: Optional[BatchContext]) -> Optional[set[str]]:
    """Return the set of valid tax codes for this org, or None if no context."""
    if context is None or not context.tax_rates:
        return None
    return {tr.code.strip().upper() for tr in context.tax_rates if tr.code}


def _tax_direction_map(context: Optional[BatchContext]) -> dict[str, tuple]:
    """{TAX_CODE_UPPER: (can_apply_to_expenses, can_apply_to_revenue)} from the
    org's TaxRates — Xero's authoritative direction flags. Empty when no
    context, so the wrong-direction checks fall back to keyword matching."""
    if context is None or not context.tax_rates:
        return {}
    out: dict[str, tuple] = {}
    for tr in context.tax_rates:
        if tr.code:
            out[tr.code.strip().upper()] = (
                tr.can_apply_to_expenses,
                tr.can_apply_to_revenue,
            )
    return out


def _coa_summary(context: Optional[BatchContext]) -> Optional[str]:
    """Pre-render the Chart of Accounts as a compact JSON string for prompts."""
    if context is None or not context.chart_of_accounts:
        return None
    trimmed = [
        {"code": a.code, "name": a.name, "type": a.type}
        for a in context.chart_of_accounts
        if a.code and a.name
    ]
    if not trimmed:
        return None
    return json.dumps(trimmed, separators=(",", ":"))


def _coa_lookup(context: Optional[BatchContext]) -> dict[str, str]:
    """Code → account name, so we can attach suggested_name without trusting the LLM."""
    if context is None:
        return {}
    return {
        a.code.strip(): a.name
        for a in context.chart_of_accounts
        if a.code and a.name
    }


def _coa_type_lookup(context: Optional[BatchContext]) -> dict[str, str]:
    """Code → account type (uppercase). Used to enforce ACCREC/ACCPAY direction."""
    if context is None:
        return {}
    return {
        a.code.strip(): (a.type or "").strip().upper()
        for a in context.chart_of_accounts
        if a.code and a.type
    }


def _allowed_account_types_for_doc(doc_type: Optional[str]) -> Optional[set[str]]:
    """Which Xero AccountType values are valid for this transaction's direction?"""
    if not doc_type:
        return None
    t = doc_type.strip().upper()
    if t in _SALES_DOC_TYPES:
        return _REVENUE_ACCOUNT_TYPES
    if t in _PURCHASE_DOC_TYPES:
        return _EXPENSE_ACCOUNT_TYPES
    return None


def _format_tax_codes_hint(context: Optional[BatchContext]) -> Optional[str]:
    """Short comma-separated list of valid tax codes (with rates) for error messages."""
    if context is None or not context.tax_rates:
        return None
    parts: list[str] = []
    for tr in context.tax_rates:
        if not tr.code:
            continue
        label = tr.code.strip()
        if tr.rate is not None and str(tr.rate).strip() != "":
            label += f" ({tr.rate}%)"
        parts.append(label)
        if len(parts) >= 5:
            break
    if not parts:
        return None
    return ", ".join(parts)
