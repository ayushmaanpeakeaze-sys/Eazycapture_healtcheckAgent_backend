"""Deterministic health-check rules.

This file was empty on disk, which prevented the FastAPI app from importing.
The functions below provide the deterministic rule surface expected by the
orchestrator.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from app.schemas.transaction import BatchTransaction, FlaggedIssue
from app.modules.healthcheck.engine.audit_settings import AuditSettings
from app.modules.healthcheck.engine.shared import (
    _CREDIT_DOC_TYPES,
    _OPEN_BILL_STATUSES,
    _PURCHASE_DOC_TYPES,
    _SALES_DOC_TYPES,
    _UNAPPROVED_STATUSES,
)


DEFAULT_SETTINGS = AuditSettings()


def _contact_key(tx: BatchTransaction, alias: Optional[dict[str, str]] = None) -> str:
    raw = (tx.contact_id or "").strip()
    canon = (alias or {}).get(raw, raw)
    return canon or f"name:{tx.vendor_name.strip().lower()}"


def _tax_lines(tx: BatchTransaction) -> list[tuple[Optional[int], Optional[str]]]:
    if tx.line_items:
        return [(idx + 1, item.tax_code) for idx, item in enumerate(tx.line_items)]
    return [(None, tx.tax_code)]


def _lines_with_account_and_tax(
    tx: BatchTransaction,
) -> list[tuple[Optional[int], Optional[str], Optional[Decimal], Optional[str]]]:
    """(line_no, account_code, amount, tax_code) per line — for the tax-missing
    checks, which need the account AND its tax code together on the same line."""
    if tx.line_items:
        return [
            (idx + 1, item.account_code, item.amount, item.tax_code)
            for idx, item in enumerate(tx.line_items)
        ]
    return [(None, tx.current_account_code, tx.amount, tx.tax_code)]


def _build_contact_alias(pairs: Optional[list]) -> dict[str, str]:
    parent: dict[str, str] = {}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]

    for pair in pairs or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] and pair[1]:
            left, right = find(str(pair[0])), find(str(pair[1]))
            if left != right:
                parent[max(left, right)] = min(left, right)
    return {key: find(key) for key in parent}


def _inspect_transaction(
    tx: BatchTransaction,
    allowed_tax_codes: Optional[set[str]],
    tax_codes_hint: Optional[str],
    today: date,
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[FlaggedIssue]:
    issues: list[FlaggedIssue] = []
    tax_missing = False          # header-level (line_no=None) miss still counts
    missing_line: Optional[int] = None
    invalid_code: Optional[str] = None
    invalid_line: Optional[int] = None

    for line_no, code in _tax_lines(tx):
        clean = (code or "").strip().upper()
        if not clean and not tax_missing:
            tax_missing = True
            missing_line = line_no
        elif allowed_tax_codes is not None and clean not in allowed_tax_codes and invalid_code is None:
            invalid_code, invalid_line = code, line_no

    if tax_missing:
        where = f" (line {missing_line})" if missing_line else ""
        suffix = f" Use {tax_codes_hint}." if tax_codes_hint else " Required by Xero."
        issues.append(FlaggedIssue(
            transaction_id=tx.transaction_id,
            issue_type="missing_tax",
            severity="critical",
            message=f"Tax code missing{where}.{suffix}"[:140],
        ))
    elif invalid_code is not None:
        where = f" (line {invalid_line})" if invalid_line else ""
        suffix = f" Use {tax_codes_hint}." if tax_codes_hint else ""
        issues.append(FlaggedIssue(
            transaction_id=tx.transaction_id,
            issue_type="invalid_tax_code",
            severity="critical",
            message=f"Tax code {invalid_code}{where} not in this org's Xero.{suffix}"[:140],
            current_code=invalid_code,
        ))

    if not tx.vendor_name.strip():
        issues.append(FlaggedIssue(
            transaction_id=tx.transaction_id,
            issue_type="missing_vendor",
            severity="critical",
            message="Vendor name missing - required by Xero.",
        ))

    if not (tx.invoice_number or "").strip():
        status = (tx.status or "").strip().upper()
        doc_type = (tx.type or "").strip().upper()
        if status in {"AUTHORISED", "PAID"} and doc_type in _SALES_DOC_TYPES:
            issue_type = "invoice_or_direct_booking"
            message = "Authorised sale has no invoice number - confirm a proper invoice was raised."
        elif status in {"AUTHORISED", "PAID"} and doc_type in _PURCHASE_DOC_TYPES:
            issue_type = "bill_or_direct_booking"
            message = "Authorised purchase has no bill reference - confirm a proper bill was raised."
        else:
            issue_type = "missing_invoice_number"
            message = "Invoice number missing - hurts reconciliation and audit trail."
        issues.append(FlaggedIssue(
            transaction_id=tx.transaction_id,
            issue_type=issue_type,
            severity="medium",
            message=message[:140],
        ))

    if tx.date > today:
        days = (tx.date - today).days
        issues.append(FlaggedIssue(
            transaction_id=tx.transaction_id,
            issue_type="future_dated",
            severity="medium",
            message=f"Invoice dated {tx.date.isoformat()} - {days} day(s) in the future.",
        ))

    for check in (_check_old_unpaid, _check_invalid_status_combo, _check_unapproved, _check_old_unsettled_credit):
        issue = check(tx, today, settings) if check is not _check_invalid_status_combo else check(tx)
        if issue is not None:
            issues.append(issue)
    return issues


def _outstanding_amount(tx: BatchTransaction) -> Decimal:
    if tx.amount_due is not None:
        return tx.amount_due
    return tx.amount - ((tx.amount_paid or Decimal("0")) + (tx.allocated_amount or Decimal("0")))


def _check_old_unpaid(
    tx: BatchTransaction,
    today: date,
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> Optional[FlaggedIssue]:
    doc_type = (tx.type or "").strip().upper()
    if doc_type in _CREDIT_DOC_TYPES:
        return None
    status = (tx.status or "").strip().upper()
    if status and status not in _OPEN_BILL_STATUSES:
        return None
    outstanding = _outstanding_amount(tx)
    if outstanding <= 0:
        return None
    is_sale = doc_type in _SALES_DOC_TYPES
    ref_date = (tx.due_date or tx.date) if settings.old_unpaid_age_basis == "due_date" else tx.date
    age = (today - ref_date).days
    threshold = settings.old_unpaid_invoice_days if is_sale else settings.old_unpaid_bill_days
    if age < threshold:
        return None
    currency = (tx.currency_code or "GBP").strip().upper()
    symbol = "£" if currency == "GBP" else f"{currency} "
    if settings.old_unpaid_age_basis == "due_date":
        msg = f"{tx.vendor_name} overdue by {age} days — {symbol}{outstanding:.2f} outstanding."
    else:
        msg = f"{tx.vendor_name} outstanding {symbol}{outstanding:.2f} for {age} days."
    return FlaggedIssue(
        transaction_id=tx.transaction_id,
        issue_type="old_unpaid_invoice" if is_sale else "old_unpaid_bill",
        severity="high",
        message=msg[:140],
        # Age and outstanding so the frontend renders without any date math.
        match_reasons={
            "age_days": age,
            "age_basis": settings.old_unpaid_age_basis,
            "outstanding": f"{outstanding:.2f}",
            "currency": currency,
        },
    )


def _check_invalid_status_combo(tx: BatchTransaction) -> Optional[FlaggedIssue]:
    status = (tx.status or "").strip().upper()
    if status == "PAID" and tx.amount_due is not None and tx.amount_due > 0:
        return FlaggedIssue(
            transaction_id=tx.transaction_id,
            issue_type="invalid_status_combo",
            severity="high",
            message=f"Marked PAID but £{tx.amount_due:.2f} still outstanding."[:140],
            current_code=status,
        )
    if status == "AUTHORISED":
        paid = (tx.amount_paid or Decimal("0")) + (tx.allocated_amount or Decimal("0"))
        if paid > tx.amount:
            return FlaggedIssue(
                transaction_id=tx.transaction_id,
                issue_type="invalid_status_combo",
                severity="high",
                message=f"Overpaid - £{paid:.2f} recorded against £{tx.amount:.2f}."[:140],
                current_code=status,
            )
    return None


def _check_old_unsettled_credit(
    tx: BatchTransaction,
    today: date,
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> Optional[FlaggedIssue]:
    doc_type = (tx.type or "").strip().upper()
    if doc_type not in _CREDIT_DOC_TYPES:
        return None
    outstanding = _outstanding_amount(tx)
    age = (today - tx.date).days
    # Flag when the credit note is at least X days old and still has unapplied credit.
    if outstanding <= 0 or age < settings.credit_age_days:
        return None
    is_sale = doc_type == "ACCRECCREDIT"
    return FlaggedIssue(
        transaction_id=tx.transaction_id,
        issue_type="old_unsettled_sales_credit" if is_sale else "old_unsettled_purchase_credit",
        severity="high",
        message=f"{tx.vendor_name} credit has £{outstanding:.2f} unapplied for {age} days."[:140],
    )


def _check_unapproved(
    tx: BatchTransaction,
    today: date,
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> Optional[FlaggedIssue]:
    status = (tx.status or "").strip().upper()
    doc_type = (tx.type or "").strip().upper()
    if status not in _UNAPPROVED_STATUSES:
        return None
    is_sale = doc_type in _SALES_DOC_TYPES
    is_purchase = doc_type in _PURCHASE_DOC_TYPES
    if not (is_sale or is_purchase):
        return None
    # Flag when the invoice is at least the configured minimum days old (default 0).
    age = (today - (tx.posted_date or tx.date)).days
    if age < settings.unapproved_grace_days:
        return None
    return FlaggedIssue(
        transaction_id=tx.transaction_id,
        issue_type="unapproved_invoice" if is_sale else "unapproved_bill",
        severity="medium",
        message=f"{'Sales invoice' if is_sale else 'Bill'} in {status} for {age} days - needs approval."[:140],
        current_code=status,
    )


def _is_paid(tx: BatchTransaction) -> bool:
    return (tx.status or "").strip().upper() == "PAID" or (
        tx.amount_due is not None and tx.amount_due == 0 and tx.amount > 0
    )


# A same-(contact, amount) charge on its usual cadence is a subscription;
# one entered much sooner than the cadence is a same-period duplicate.
def _dominant(values: list[Any]) -> Optional[str]:
    cleaned = [str(value).strip() for value in values if value and str(value).strip()]
    return Counter(cleaned).most_common(1)[0][0] if cleaned else None


# Tax codes treated as missing by the tax-missing checks; excludes zero-rated
# and exempt, which are intentional 0% treatments with a real code.
# Purchase-side expense accounts that legitimately carry no VAT, ignored to
# avoid false flags; matched on account name plus the configurable code ignore-list.
