"""Unusual payments (pattern-based SOP, deterministic) — a review-and-confirm pass.

Flags a payment (bill / Spend Money) that is unusual: no/generic description,
a one-off supplier with no clear description, a large one-off, or a line far
above the account's usual size. Vague-account (misallocated_item) and
vendor-outlier (amount_outlier) live elsewhere. No conclusions — confirm only.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from statistics import median

from app.modules.healthcheck.engine.shared import _account_lines
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
    account_min_txns: int = 4,
    account_multiple: Decimal | str = "3",
) -> list[FlaggedIssue]:
    large = Decimal(str(large_amount))
    acc_mult = Decimal(str(account_multiple))
    payments = [tx for tx in transactions
                if (tx.type or "").strip().upper() in _PAYMENT_DOC_TYPES]
    freq = Counter(_contact_key(tx) for tx in payments)

    by_account: dict[str, list[Decimal]] = defaultdict(list)
    for tx in payments:
        for _line_no, code, amount in _account_lines(tx):
            code = (code or "").strip()
            if code and amount is not None:
                by_account[code].append(abs(amount))
    account_median: dict[str, Decimal] = {}
    for code, amounts in by_account.items():
        if len(amounts) >= account_min_txns:
            med = Decimal(str(median(amounts)))
            if med > 0:
                account_median[code] = med

    findings: list[FlaggedIssue] = []
    flagged_tx: set[str] = set()
    for tx in payments:
        amt = abs(tx.amount or Decimal("0"))
        supplier = (tx.vendor_name or "").strip()
        seen_n = freq[_contact_key(tx)]
        if _is_unclear(_doc_text(tx)):
            if seen_n <= one_off_max_count:
                findings.append(_finding(
                    tx, "unidentified_supplier", "medium", amt,
                    f"{supplier or 'Payment'}: {_symbol(tx)}{amt:.2f} to a one-off supplier "
                    f"(seen {seen_n}x) with no clear description — confirm the nature."))
            else:
                findings.append(_finding(
                    tx, "unclear_description", "medium", amt,
                    f"{supplier or 'Payment'}: {_symbol(tx)}{amt:.2f} has no or unclear "
                    f"description — confirm the nature of the expense."))
            flagged_tx.add(tx.transaction_id)
        elif amt >= large and seen_n <= one_off_max_count:
            findings.append(_finding(
                tx, "one_off_supplier", "medium", amt,
                f"{supplier or 'Supplier'}: one-off {_symbol(tx)}{amt:.2f} payment "
                f"(seen {seen_n}x this year) — confirm the nature."))
            flagged_tx.add(tx.transaction_id)

    for tx in payments:
        if tx.transaction_id in flagged_tx:
            continue
        supplier = (tx.vendor_name or "").strip()
        for _line_no, code, amount in _account_lines(tx):
            code = (code or "").strip()
            med = account_median.get(code)
            if med is None or amount is None:
                continue
            line_amt = abs(amount)
            if line_amt >= large and line_amt >= med * acc_mult:
                ratio = line_amt / med
                findings.append(_finding(
                    tx, "large_vs_account", "medium", line_amt,
                    f"{supplier or 'Payment'}: {_symbol(tx)}{line_amt:.2f} to {code} is "
                    f"{ratio:.1f}x the usual {_symbol(tx)}{med:.2f} for that account — verify.",
                    extra={"account": code, "usual": f"{med:.2f}", "ratio": f"{ratio:.1f}"}))
                flagged_tx.add(tx.transaction_id)
                break
    return findings


def _symbol(tx) -> str:
    return "£" if (tx.currency_code or "GBP").strip().upper() == "GBP" else f"{tx.currency_code} "


def _finding(tx, reason: str, severity: str, amt: Decimal, msg: str,
             extra: dict | None = None) -> FlaggedIssue:
    reasons = {
        "reason": reason,
        "supplier": (tx.vendor_name or "").strip(),
        "date": tx.date.isoformat(),
        "amount": f"{amt:.2f}",
    }
    if extra:
        reasons.update(extra)
    return FlaggedIssue(
        transaction_id=tx.transaction_id,
        issue_type=ISSUE_TYPE,
        severity=severity,
        message=msg[:200],
        current_code=(tx.current_account_code or "").strip() or None,
        match_reasons=reasons,
    )


SETTING_FIELDS: tuple = ()
META: tuple[tuple[str, str, bool], ...] = (("unusual_payment", "Payment Anomalies", True),)
