"""Unusual payments (pattern-based SOP, deterministic) — a review-and-confirm pass.

Flags a payment (bill / Spend Money) with no/generic description, or a large one-off
from a supplier seen only once or twice. Vague-account (misallocated_item) and
amount-outlier already exist; missing-regular sits in missing_accrual. No conclusions.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal

from app.shared.transaction import FlaggedIssue

_PAYMENT_DOC_TYPES = {"ACCPAY", "SPEND"}
_GENERIC_DESC = {
    "payment", "transfer", "online", "bank", "misc", "miscellaneous", "chq",
    "cheque", "tfr", "bacs", "dd", "direct debit", "card", "cash", "sundry", "expense",
}
ISSUE_TYPE = "unusual_payment"


def _contact_key(tx) -> str:
    return (getattr(tx, "contact_id", None) or getattr(tx, "vendor_name", "") or "").strip().lower()


def _is_unclear(text: str) -> bool:
    clean = " ".join((text or "").split()).strip().lower()
    return not clean or clean in _GENERIC_DESC


def _doc_text(tx) -> str:
    parts = [tx.description, tx.reference]
    parts += [li.description for li in (tx.line_items or [])]
    return " ".join(p for p in parts if p)


def find_unusual_payments(
    transactions,
    large_amount: Decimal | str = "1000",
    one_off_max_count: int = 2,
) -> list[FlaggedIssue]:
    large = Decimal(str(large_amount))
    payments = [tx for tx in transactions
                if (tx.type or "").strip().upper() in _PAYMENT_DOC_TYPES]
    freq = Counter(_contact_key(tx) for tx in payments)

    findings: list[FlaggedIssue] = []
    for tx in payments:
        amt = abs(tx.amount or Decimal("0"))
        supplier = (tx.vendor_name or "").strip()
        if _is_unclear(_doc_text(tx)):
            findings.append(_finding(
                tx, "unclear_description", "medium", amt,
                f"{supplier or 'Payment'}: {_symbol(tx)}{amt:.2f} has no or unclear "
                f"description — confirm the nature of the expense."))
        elif amt >= large and freq[_contact_key(tx)] <= one_off_max_count:
            findings.append(_finding(
                tx, "one_off_supplier", "medium", amt,
                f"{supplier or 'Supplier'}: one-off {_symbol(tx)}{amt:.2f} payment "
                f"(seen {freq[_contact_key(tx)]}x this year) — confirm the nature."))
    return findings


def _symbol(tx) -> str:
    return "£" if (tx.currency_code or "GBP").strip().upper() == "GBP" else f"{tx.currency_code} "


def _finding(tx, reason: str, severity: str, amt: Decimal, msg: str) -> FlaggedIssue:
    return FlaggedIssue(
        transaction_id=tx.transaction_id,
        issue_type=ISSUE_TYPE,
        severity=severity,
        message=msg[:200],
        current_code=(tx.current_account_code or "").strip() or None,
        match_reasons={
            "reason": reason,
            "supplier": (tx.vendor_name or "").strip(),
            "date": tx.date.isoformat(),
            "amount": f"{amt:.2f}",
        },
    )


SETTING_FIELDS: tuple = ()
META: tuple[tuple[str, str, bool], ...] = (("unusual_payment", "Unusual payments", True),)
