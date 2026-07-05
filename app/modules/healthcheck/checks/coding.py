"""Categorisation & Coding check group.

Account-coding checks: misallocated (vague account), multi-account suppliers
(vendor drift), unexpected account vs contact default, wrong income/expense
direction, and amount outliers. ``wrong_category`` / ``invoice|bill_or_direct_booking``
/ ``anomaly`` are LLM/inspection paths, so only their registry entries are in META.

Truly-shared helpers (_contact_key, _dominant) live in deterministic and are
reached via lazy proxies below to avoid the package import cycle.
"""
from __future__ import annotations

from collections import Counter, defaultdict  # noqa: F401
from decimal import Decimal  # noqa: F401
from statistics import median
from typing import Any, Optional  # noqa: F401

from app.modules.healthcheck.checks.base import SettingField
from app.schemas.transaction import BatchTransaction, FlaggedIssue
from app.modules.healthcheck.engine.audit_settings import AuditSettings, DEFAULT_SETTINGS  # noqa: F401
from app.modules.healthcheck.engine.shared import (  # noqa: F401
    _account_lines,
    _allowed_account_types_for_doc,
    _CREDIT_DOC_TYPES,
    _EXPENSE_ACCOUNT_TYPES,
    _MONEY_IN_TYPES,
    _MONEY_OUT_TYPES,
    _PURCHASE_DOC_TYPES,
    _REVENUE_ACCOUNT_TYPES,
    _SALES_DOC_TYPES,
    _VAGUE_ACCOUNT_NAME_KEYWORDS,
)


def _contact_key(*a, **k):
    from app.modules.healthcheck.engine.deterministic import _contact_key as _f
    return _f(*a, **k)


def _dominant(*a, **k):
    from app.modules.healthcheck.engine.deterministic import _dominant as _f
    return _f(*a, **k)


def _find_direction_mismatches(
    transactions: list[BatchTransaction],
    coa_type_lookup: dict[str, str],
) -> list[FlaggedIssue]:
    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        code = (tx.current_account_code or "").strip()
        allowed = _allowed_account_types_for_doc(tx.type)
        current_type = coa_type_lookup.get(code, "")
        if code and allowed and current_type and current_type not in allowed:
            is_sale = (tx.type or "").strip().upper() in _SALES_DOC_TYPES
            flagged.append(FlaggedIssue(
                transaction_id=tx.transaction_id,
                issue_type="wrong_direction_account",
                severity="high",
                message=f"{'Sales invoice' if is_sale else 'Purchase bill'} coded to {current_type} account ({code})."[:140],
                current_code=code,
            ))
    return flagged


def _find_multi_account_suppliers(
    transactions: list[BatchTransaction],
    coa_lookup: dict[str, str],
    contact_alias: Optional[dict[str, str]] = None,
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[FlaggedIssue]:
    """Multi-Account Suppliers: a contact whose postings span MORE THAN ONE
    account code (pure distinct-count — 2+ distinct → flag). Checked across every
    LINE ITEM of the contact's bills AND Money-Out bank payments — the account
    code lives on the line, not the header. The most-used account is treated as
    the 'usual' one; the differing postings are flagged with it as the suggestion.
    """
    alias = contact_alias or {}
    whitelist = frozenset(
        c.strip().upper() for c in (settings.multi_account_whitelist_contacts or ()) if c
    )
    # Every (transaction, account_code) pair from line items, grouped by contact.
    by_contact: dict[str, list[tuple[BatchTransaction, str]]] = defaultdict(list)
    for tx in transactions:
        for _line_no, code, _amount in _account_lines(tx):
            code = (code or "").strip()
            if code:
                by_contact[_contact_key(tx, alias)].append((tx, code))

    flagged: list[FlaggedIssue] = []
    for key, entries in by_contact.items():
        accounts = [code for _tx, code in entries]
        if len(set(accounts)) < 2:           # trigger: 2+ distinct accounts
            continue
        # Whitelisted suppliers are allowed to split across accounts — skip them.
        sample = entries[0][0]
        ids = {key.strip().upper(), (sample.contact_id or "").strip().upper(),
               sample.vendor_name.strip().upper()}
        if whitelist & ids:
            continue
        dominant = _dominant(accounts)        # the 'usual' account
        accounts_used = sorted(set(accounts))
        seen: set[str] = set()                # one flag per transaction
        for tx, code in entries:
            if code != dominant and tx.transaction_id not in seen:
                seen.add(tx.transaction_id)
                flagged.append(FlaggedIssue(
                    transaction_id=tx.transaction_id,
                    issue_type="multi_account_supplier",
                    severity="medium",
                    message=f"{tx.vendor_name} usually posts to {dominant}; this one is {code}."[:140],
                    suggested_code=dominant,
                    suggested_name=coa_lookup.get(dominant),
                    current_code=code,
                    accounts_used=accounts_used,
                ))
    return flagged


def _find_unexpected_accounts(
    transactions: list[BatchTransaction],
    coa_lookup: dict[str, str],
    contact_defaults: Optional[dict[str, dict[str, Optional[str]]]] = None,
) -> list[FlaggedIssue]:
    """Flag a transaction whose account code differs from the contact's DEFAULT
    (sales default for customer invoices / money IN, purchase default for
    supplier bills / money OUT).

    Covers ACCREC/ACCPAY invoices+bills AND bank transactions (RECEIVE → sales,
    SPEND → purchase) — exactly the four transaction types.

    Rule: a contact with NO default configured is SILENT — without a
    baseline there is nothing to call "unexpected". Frequency-based detection
    (compare a posting against the contact's OWN history) is the separate
    Multi-Account Suppliers check, deliberately not duplicated here.
    """
    if not contact_defaults:
        return []
    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        defaults = contact_defaults.get((tx.contact_id or "").strip())
        if not defaults:
            continue
        doc_type = (tx.type or "").strip().upper()
        is_sales = doc_type in _SALES_DOC_TYPES or doc_type in _MONEY_IN_TYPES
        default = defaults.get("sales") if is_sales else defaults.get("purchase")
        used = (tx.current_account_code or "").strip()
        if default and used and used != default:
            flagged.append(FlaggedIssue(
                transaction_id=tx.transaction_id,
                issue_type="unexpected_account",
                severity="medium",
                message=f"{tx.vendor_name} usually posts to {default}; this used {used}."[:140],
                current_code=used,
                suggested_code=default,
                suggested_name=coa_lookup.get(default),
            ))
    return flagged


def _is_vague_account(code: Optional[str], coa_lookup: dict[str, str], extra_codes: frozenset[str]) -> bool:
    clean = (code or "").strip()
    return bool(clean and (clean.upper() in extra_codes or any(keyword in (coa_lookup.get(clean) or "").lower() for keyword in _VAGUE_ACCOUNT_NAME_KEYWORDS)))


def _find_misallocated_items(
    transactions: list[BatchTransaction],
    coa_lookup: dict[str, str],
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[FlaggedIssue]:
    extra = frozenset(settings.misallocated_vague_codes or ())
    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        for line_no, code, amount in _account_lines(tx):
            if amount is None or abs(amount) < settings.misallocated_materiality:
                continue
            if _is_vague_account(code, coa_lookup, extra):
                where = f" (line {line_no})" if line_no else ""
                flagged.append(FlaggedIssue(
                    transaction_id=tx.transaction_id,
                    issue_type="misallocated_item",
                    severity="medium",
                    message=f"{tx.vendor_name} £{abs(amount):.2f} coded to vague account{where} - review."[:140],
                    current_code=(code or "").strip() or None,
                ))
                break
    return flagged


def find_amount_outlier_candidates(
    transactions: list[BatchTransaction],
    contact_alias: Optional[dict[str, str]] = None,
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[dict]:
    alias = contact_alias or {}
    by_contact: dict[str, list[BatchTransaction]] = defaultdict(list)
    for tx in transactions:
        if tx.amount > 0:
            by_contact[_contact_key(tx, alias)].append(tx)
    candidates: list[dict] = []
    for txns in by_contact.values():
        if len(txns) < settings.outlier_min_txns:
            continue
        usual = Decimal(str(median([tx.amount for tx in txns])))
        if usual <= 0:
            continue
        for tx in txns:
            if tx.amount >= usual * settings.outlier_multiple and tx.amount >= settings.outlier_min_amount:
                candidates.append({
                    "tx": tx,
                    "median": usual,
                    "ratio": float(tx.amount / usual),
                    "vendor_txn_count": len(txns),
                    "usual_account": _dominant([item.current_account_code for item in txns]),
                    "usual_tax": _dominant([item.tax_code for item in txns]),
                })
    return candidates


def amount_outlier_flag(candidate: dict) -> FlaggedIssue:
    tx = candidate["tx"]
    currency = (tx.currency_code or "GBP").strip().upper()
    symbol = "£" if currency == "GBP" else f"{currency} "
    return FlaggedIssue(
        transaction_id=tx.transaction_id,
        issue_type="amount_outlier",
        severity="medium",
        message=(
            f"{tx.vendor_name} usually ~{symbol}{candidate['median']:.2f}, "
            f"but this is {symbol}{tx.amount:.2f} ({candidate['ratio']:.1f}x higher) - verify."
        )[:140],
        confidence=0.85,
    )


# --- settings + registry -----------------------------------------------------
SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField("misallocated_materiality", "Categorisation & Coding", "misallocated_item",
                 "Flag vague-account items over …", "amount",
                 "Flag a line posted to a broad/vague account (General Expenses, "
                 "Uncategorised, Unapplied, Sundry, Suspense, …) when its net "
                 "amount is at least this much. Default 100 — raise it to your "
                 "materiality policy.",
                 unit="currency", min=0, step=50),
    SettingField("misallocated_vague_codes", "Categorisation & Coding", "misallocated_item",
                 "Additional vague accounts to monitor", "list",
                 "Extra account CODES to treat as vague (on top of the built-in "
                 "name match: General Expenses, Uncategorised, Unapplied, Sundry, "
                 "Miscellaneous, Suspense …). E.g. a custom catch-all code."),
)

META: tuple[tuple[str, str, bool], ...] = (
    ("wrong_category", "Wrong category", True),
    ("unexpected_account", "Unexpected account", True),
    ("multi_account_supplier", "Multi-account suppliers (vendor drift)", True),
    ("misallocated_item", "Misallocated items (vague account)", True),
    ("wrong_direction_account", "Wrong-direction account (income vs expense)", True),
    ("invoice_or_direct_booking", "Invoice or direct booking", True),
    ("bill_or_direct_booking", "Bill or direct booking", True),
    ("anomaly", "Anomaly (LLM)", True),
    ("amount_outlier", "Amount outlier vs vendor history", True),
)
