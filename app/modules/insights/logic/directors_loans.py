"""Directors' Loan Accounts — auto-detect from the Xero Trial Balance.

Pure logic. Scans every Trial Balance row for accounts whose name looks like a
director's loan ("Director's Loan", "Directors Loan", "DLA", "Loan - Director")
and returns each with its balance and whether it is OVERDRAWN (a debit balance
= the director owes the company → s455 tax risk).

Auto-detect is a suggestion; the firm confirms the mapping per client. If no
account matches, ``detected`` is False and the UI should prompt for a manual
account selection.

Trial Balance row shape (Xero):
  Cells = [ {Account "Name (code)"}, {Debit}, {Credit}, {Debit balance}, {Credit balance} ]
The last two cells are the closing balances.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

_DLA_KEYWORDS = ("director", "dla")


def _num(v: Any) -> float:
    s = str(v or "").replace(",", "").strip()
    if not s:
        return 0.0
    try:
        return float(Decimal(s))
    except (InvalidOperation, ValueError):
        return 0.0


def _split_name_code(label: str) -> tuple[str, Optional[str]]:
    """'Director's Loan (835)' → ('Director's Loan', '835')."""
    label = (label or "").strip()
    if label.endswith(")") and "(" in label:
        i = label.rfind("(")
        return label[:i].strip(), label[i + 1:-1].strip()
    return label, None


def find_director_loans(report: Optional[dict[str, Any]]) -> dict[str, Any]:
    accounts: list[dict[str, Any]] = []

    def _walk(rows: Optional[list]) -> None:
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            if r.get("Rows"):
                _walk(r.get("Rows"))
            if r.get("RowType") != "Row":
                continue
            cells = r.get("Cells") or []
            if len(cells) < 2:
                continue
            label = (cells[0].get("Value") or "").strip()
            low = label.lower()
            if not any(k in low for k in _DLA_KEYWORDS):
                continue
            name, code = _split_name_code(label)
            debit = _num(cells[-2].get("Value"))
            credit = _num(cells[-1].get("Value"))
            balance = round(debit - credit, 2)   # +ve = director owes company
            accounts.append({
                "account": name,
                "code": code,
                "balance": balance,
                "overdrawn": balance > 0,
                "note": (
                    "Overdrawn — director owes the company (possible s455 tax)."
                    if balance > 0 else
                    "In credit — company owes the director."
                ),
            })

    if isinstance(report, dict):
        _walk(report.get("Rows"))

    return {
        "detected": bool(accounts),
        "accounts": accounts,
        "note": (
            "Auto-detected by account name — confirm the mapping per client."
            if accounts else
            "No director's-loan account detected. Map the correct nominal account manually."
        ),
    }
