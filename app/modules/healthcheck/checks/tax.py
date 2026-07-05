"""Tax & VAT check group.

Standalone detection functions for the tax checks live here, with their
tax-only helpers + constants. ``missing_tax`` / ``invalid_tax_code`` are emitted
inside ``deterministic._inspect_transaction`` (per-document inspection), so only
their registry entries are listed in META.

Truly-shared helpers (``_dominant``, ``_contact_key``, ``_tax_lines``,
``_lines_with_account_and_tax``) stay in ``deterministic`` and are imported
lazily inside the functions that need them — avoids the package import cycle.
The tax checks have no tunable settings (no gear), so SETTING_FIELDS is empty.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal  # noqa: F401  (kept for future use)
from typing import Optional

from app.schemas.transaction import BatchTransaction, FlaggedIssue
from app.services.healthcheck.shared import (
    _EXPENSE_ACCOUNT_TYPES,
    _MONEY_IN_TYPES,
    _MONEY_OUT_TYPES,
    _PURCHASE_DOC_TYPES,
    _REVENUE_ACCOUNT_TYPES,
    _SALES_DOC_TYPES,
)

# --- tax-only constants ------------------------------------------------------
# "No VAT" / "Outside scope" — treated as MISSING tax. EXCLUDES zero-rated /
# exempt (those are intentional 0% with a real code).
_NO_VAT_TAX_CODES = {"NONE", "NOTAX", "NOVAT"}
# Purchase-side expense accounts that legitimately carry no VAT (matched on name).
_PURCHASE_NO_VAT_BY_NATURE = (
    "wage", "salary", "salaries", "payroll", "employer ni", "national insurance",
    "paye", "director remuneration", "pension", "depreciat", "amortis",
    "corporation tax", "income tax", "donation", "rates", "grant", "dividend",
)
_OUTPUT_TAX_KEYWORDS = {"OUTPUT", "SALES", "ZERORATEDSUPPLIES", "EXEMPTOUTPUT", "GSTONIMPORTS"}
_INPUT_TAX_KEYWORDS = {"INPUT", "PURCHASE", "BASEXCLUSIVE", "EXEMPTINPUT"}


# --- tax-only helpers --------------------------------------------------------
def _is_no_vat_code(tax: Optional[str]) -> bool:
    """True for 'No VAT' / 'Outside scope' codes — NOT zero-rated / exempt."""
    norm = (tax or "").strip().upper().replace(" ", "")
    if not norm:
        return False
    return norm in _NO_VAT_TAX_CODES or "OUTSIDE" in norm


def _is_wrong_for_bill(code: str, tax_dir: Optional[dict[str, tuple]]) -> bool:
    clean = code.strip().upper()
    if tax_dir and clean in tax_dir and tax_dir[clean][0] is not None:
        return tax_dir[clean][0] is False
    return any(keyword in clean for keyword in _OUTPUT_TAX_KEYWORDS)


def _is_wrong_for_invoice(code: str, tax_dir: Optional[dict[str, tuple]]) -> bool:
    clean = code.strip().upper()
    if tax_dir and clean in tax_dir and tax_dir[clean][1] is not None:
        return tax_dir[clean][1] is False
    return any(keyword in clean for keyword in _INPUT_TAX_KEYWORDS)


def _tax_lines_with_amounts(
    tx: BatchTransaction,
) -> list[tuple[Optional[int], Optional[str], Optional[Decimal], Optional[Decimal]]]:
    """(line_no, tax_code, net_amount, tax_amount) per line — for the wrong-tax
    checks, which surface the Net + Tax columns shown."""
    if tx.line_items:
        return [
            (i + 1, li.tax_code, li.amount, li.tax_amount)
            for i, li in enumerate(tx.line_items)
        ]
    return [(None, tx.tax_code, tx.amount, None)]


def _tax_direction_reasons(code: str, net: Optional[Decimal], tax_amt: Optional[Decimal]) -> dict:
    reasons: dict = {"tax_code": code}
    if net is not None:
        reasons["net_amount"] = f"{abs(net):.2f}"
    if tax_amt is not None:
        reasons["tax_amount"] = f"{abs(tax_amt):.2f}"
    return reasons


def _settings(settings):
    if settings is None:
        from app.services.healthcheck.audit_settings import DEFAULT_SETTINGS
        return DEFAULT_SETTINGS
    return settings


# --- detection ---------------------------------------------------------------
def _find_tax_missing(
    transactions: list[BatchTransaction],
    account_types: set[str],
    issue_type: str,
    noun: str,
    coa_lookup: dict[str, str],
    coa_type_lookup: dict[str, str],
    settings,
    *,
    ignore_name_keywords: tuple[str, ...] = (),
) -> list[FlaggedIssue]:
    """Tax-missing: a line on an in-scope account (Sales/Income for sales,
    Expense/Asset for purchase) with a No-VAT / Outside-Scope tax code → flag."""
    from app.services.healthcheck.deterministic import _lines_with_account_and_tax
    ignore_codes = frozenset(
        c.strip().upper() for c in (settings.tax_missing_ignore_accounts or ()) if c
    )
    ignore_contacts = frozenset(
        c.strip().upper() for c in (settings.tax_missing_ignore_contacts or ()) if c
    )
    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        contact = (tx.contact_id or "").strip().upper()
        name = (tx.vendor_name or "").strip().upper()
        if (contact and contact in ignore_contacts) or (name and name in ignore_contacts):
            continue
        for _line_no, code, _amount, tax in _lines_with_account_and_tax(tx):
            acct = (code or "").strip()
            if not acct or acct.upper() in ignore_codes:
                continue
            if (coa_type_lookup.get(acct) or "").strip().upper() not in account_types:
                continue
            acct_name = (coa_lookup.get(acct) or "").lower()
            if any(kw in acct_name for kw in ignore_name_keywords):
                continue
            if not _is_no_vat_code(tax):
                continue
            flagged.append(FlaggedIssue(
                transaction_id=tx.transaction_id,
                issue_type=issue_type,
                severity="medium",
                message=(f"{tx.vendor_name}: {noun} on {acct} "
                         f"({coa_lookup.get(acct) or acct}) has no VAT "
                         f"(tax {(tax or '').strip() or 'none'}) - review.")[:140],
                current_code=(tax or "").strip() or None,
            ))
            break
    return flagged


def _find_purchase_tax_missing(
    transactions: list[BatchTransaction],
    coa_lookup: dict[str, str],
    coa_type_lookup: dict[str, str],
    settings=None,
) -> list[FlaggedIssue]:
    return _find_tax_missing(
        transactions, _EXPENSE_ACCOUNT_TYPES, "purchase_tax_missing", "bill",
        coa_lookup, coa_type_lookup, _settings(settings),
        ignore_name_keywords=_PURCHASE_NO_VAT_BY_NATURE,
    )


def _find_sales_tax_missing(
    transactions: list[BatchTransaction],
    coa_lookup: dict[str, str],
    coa_type_lookup: dict[str, str],
    settings=None,
) -> list[FlaggedIssue]:
    return _find_tax_missing(
        transactions, _REVENUE_ACCOUNT_TYPES, "sales_tax_missing", "income",
        coa_lookup, coa_type_lookup, _settings(settings),
    )


def _find_sales_tax_on_bills(
    transactions: list[BatchTransaction],
    tax_dir: Optional[dict[str, tuple]] = None,
) -> list[FlaggedIssue]:
    """A purchase document (bill OR Money Out) using a SALES-side VAT code."""
    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        if (tx.type or "").strip().upper() not in (_PURCHASE_DOC_TYPES | _MONEY_OUT_TYPES):
            continue
        for line_no, code, net, tax_amt in _tax_lines_with_amounts(tx):
            clean = (code or "").strip().upper()
            if clean and _is_wrong_for_bill(clean, tax_dir):
                where = f" (line {line_no})" if line_no else ""
                flagged.append(FlaggedIssue(
                    transaction_id=tx.transaction_id,
                    issue_type="sales_tax_on_bills",
                    severity="high",
                    message=f"{tx.vendor_name} bill uses sales tax code {clean}{where}."[:140],
                    current_code=clean,
                    match_reasons=_tax_direction_reasons(clean, net, tax_amt),
                ))
                break
    return flagged


def _find_purchase_tax_on_invoices(
    transactions: list[BatchTransaction],
    tax_dir: Optional[dict[str, tuple]] = None,
) -> list[FlaggedIssue]:
    """A sales document (invoice OR Money In) using a PURCHASE-side VAT code."""
    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        if (tx.type or "").strip().upper() not in (_SALES_DOC_TYPES | _MONEY_IN_TYPES):
            continue
        for line_no, code, net, tax_amt in _tax_lines_with_amounts(tx):
            clean = (code or "").strip().upper()
            if clean and _is_wrong_for_invoice(clean, tax_dir):
                where = f" (line {line_no})" if line_no else ""
                flagged.append(FlaggedIssue(
                    transaction_id=tx.transaction_id,
                    issue_type="purchase_tax_on_invoices",
                    severity="high",
                    message=f"{tx.vendor_name} invoice uses purchase tax code {clean}{where}."[:140],
                    current_code=clean,
                    match_reasons=_tax_direction_reasons(clean, net, tax_amt),
                ))
                break
    return flagged


def _find_multi_tax_code_suppliers(
    transactions: list[BatchTransaction],
    contact_alias: Optional[dict[str, str]] = None,
    settings=None,
) -> list[FlaggedIssue]:
    """Multi-Tax-Code Suppliers: a contact whose postings use MORE THAN ONE
    tax code (2+ distinct → flag), across every line item of bills + Money-Out."""
    from app.services.healthcheck.deterministic import _contact_key, _dominant, _tax_lines
    alias = contact_alias or {}
    by_contact: dict[str, list[tuple[BatchTransaction, str]]] = defaultdict(list)
    for tx in transactions:
        for _line_no, tax in _tax_lines(tx):
            tax = (tax or "").strip().upper()
            if tax:
                by_contact[_contact_key(tx, alias)].append((tx, tax))

    flagged: list[FlaggedIssue] = []
    for entries in by_contact.values():
        codes = [tax for _tx, tax in entries]
        if len(set(codes)) < 2:
            continue
        dominant = _dominant(codes)
        seen: set[str] = set()
        for tx, tax in entries:
            if tax != dominant and tx.transaction_id not in seen:
                seen.add(tx.transaction_id)
                flagged.append(FlaggedIssue(
                    transaction_id=tx.transaction_id,
                    issue_type="multi_tax_code_supplier",
                    severity="medium",
                    message=f"{tx.vendor_name} usually uses tax {dominant}; this one is {tax}."[:140],
                    suggested_code=dominant,
                    current_code=tax,
                ))
    return flagged


def _find_unexpected_tax_codes(
    transactions: list[BatchTransaction],
    contact_defaults: Optional[dict[str, dict[str, Optional[str]]]] = None,
) -> list[FlaggedIssue]:
    """Flag a transaction whose tax code differs from the contact's DEFAULT tax."""
    if not contact_defaults:
        return []
    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        defaults = contact_defaults.get((tx.contact_id or "").strip())
        if not defaults:
            continue
        doc_type = (tx.type or "").strip().upper()
        is_sales = doc_type in _SALES_DOC_TYPES or doc_type in _MONEY_IN_TYPES
        default = defaults.get("sales_tax") if is_sales else defaults.get("purchase_tax")
        used = (tx.tax_code or "").strip().upper()
        if default and used and used != default.strip().upper():
            flagged.append(FlaggedIssue(
                transaction_id=tx.transaction_id,
                issue_type="unexpected_tax_code",
                severity="medium",
                message=f"{tx.vendor_name} usually uses tax {default}; this used {used}."[:140],
                current_code=used,
                suggested_code=default,
            ))
    return flagged


# --- settings + registry -----------------------------------------------------
SETTING_FIELDS: tuple = ()   # Tax checks have no tunable thresholds.

META: tuple[tuple[str, str, bool], ...] = (
    ("missing_tax", "Missing tax code (any document)", True),
    ("invalid_tax_code", "Invalid tax code", True),
    ("sales_tax_missing", "Sales tax missing", True),
    ("purchase_tax_missing", "Purchase tax missing", True),
    ("sales_tax_on_bills", "Sales tax on bills (suspicious)", True),
    ("purchase_tax_on_invoices", "Purchase tax on invoices (suspicious)", True),
    ("unexpected_tax_code", "Unexpected tax code", True),
    ("multi_tax_code_supplier", "Multi-tax suppliers (vendor drift)", True),
)
