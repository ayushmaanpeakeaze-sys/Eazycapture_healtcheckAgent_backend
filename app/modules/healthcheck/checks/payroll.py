"""Bank payments to people not in payroll (SOP) — a review-and-confirm pass.

Flags an outgoing bank payment (Spend Money) whose payee name is not in the
approved payroll employee list. Name match is case-insensitive and
whitespace-normalised. Review-only; the client confirms whether the payment
relates to an employee, a subcontractor, or something else.
"""
from __future__ import annotations

from decimal import Decimal

from app.modules.healthcheck.checks.base import SettingField
from app.shared.transaction import FlaggedIssue

ISSUE_TYPE = "non_payroll_payment"
_SPEND_TYPE = "SPEND"


def _norm(name: str) -> str:
    return " ".join((name or "").split()).strip().lower()


def _symbol(tx) -> str:
    return "£" if (tx.currency_code or "GBP").strip().upper() == "GBP" else f"{tx.currency_code} "


def find_non_payroll_payments(bank_transactions, employee_names) -> list[FlaggedIssue]:
    payroll = {_norm(n) for n in (employee_names or []) if _norm(n)}
    if not payroll:
        return []  # no payroll list to compare against → silent
    findings: list[FlaggedIssue] = []
    for tx in bank_transactions:
        if (tx.type or "").strip().upper() != _SPEND_TYPE:
            continue
        payee = (tx.vendor_name or "").strip()
        if not payee or _norm(payee) in payroll:
            continue
        amt = abs(tx.amount or Decimal("0"))
        findings.append(FlaggedIssue(
            transaction_id=tx.transaction_id,
            issue_type=ISSUE_TYPE,
            severity="medium",
            message=(f"{payee}: {_symbol(tx)}{amt:.2f} paid from the bank but the payee is "
                     f"not in payroll — confirm if employee, subcontractor, or other.")[:200],
            current_code=(tx.current_account_code or "").strip() or None,
            match_reasons={
                "payee": payee,
                "date": tx.date.isoformat(),
                "amount": f"{amt:.2f}",
            },
        ))
    return findings


SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField("payroll_employee_names", "Payments & Anomalies", "non_payroll_payment",
                 "Payroll employee names", "list",
                 "Approved employee names from the payroll summary. A bank payment "
                 "whose payee isn't in this list (and isn't a synced Xero Payroll "
                 "employee) is flagged for confirmation. Case-insensitive; extra "
                 "spaces ignored."),
)

META: tuple[tuple[str, str, bool], ...] = (
    ("non_payroll_payment", "Payments to non-payroll people", True),
)
