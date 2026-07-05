"""Bank Reconciliation Summary — reproduce Xero's report from the Accounting API.

Per bank account, from the standard (free) Accounting API alone:

    Balance in Xero          (Trial Balance GL balance)
  + unreconciled receipts    (RECEIVE money, IsReconciled=false)
  - unreconciled payments    (SPEND money,   IsReconciled=false)
  = Statement Balance (calculated)

This is exactly how Xero derives its "Statement balance (calculated)" when no
imported bank feed is connected. The *imported* (bank-feed) statement balance
needs Xero's gated Finance API, so it is returned as ``None`` ("Not available")
— which is what Xero itself shows in that case.

Pure logic: no DB/HTTP. The caller supplies the three Accounting-API reports.
"""
from __future__ import annotations

from typing import Any, Optional

from app.modules.insights.logic.bank import (
    _is_reconciled,
    _num,
    _parse_date,
    _parse_trial_balance_balances,
)

_DIRECTION = {"RECEIVE": "Received", "SPEND": "Spent"}
# a voided/deleted transaction is not a real outstanding item — exclude it
_VOID_STATUSES = {"VOIDED", "DELETED"}


def _bank_accounts(coa: Optional[list[dict[str, Any]]]) -> dict[str, dict[str, str]]:
    """{account_id: {code, name}} for BANK-type accounts in the chart of accounts."""
    out: dict[str, dict[str, str]] = {}
    for a in coa or []:
        if not isinstance(a, dict):
            continue
        if str(a.get("Type") or a.get("type") or "").upper() != "BANK":
            continue
        acc_id = str(a.get("AccountID") or a.get("accountID") or "").strip()
        if not acc_id:
            continue
        out[acc_id] = {
            "code": str(a.get("Code") or a.get("code") or "").strip(),
            "name": str(a.get("Name") or a.get("name") or "").strip(),
        }
    return out


def compute_bank_reconciliation_summary(
    trial_balance: Optional[dict[str, Any]],
    chart_of_accounts: Optional[list[dict[str, Any]]],
    bank_transactions: Optional[list[dict[str, Any]]],
    exclude_codes: Optional[set[str]] = None,
) -> dict[str, Any]:
    """One reconciliation summary per bank account (largest unreconciled first)::

        {
          "accounts": [{
            account_id, account_code, account_name,
            balance_in_xero,               # Trial Balance GL
            unreconciled_received,         # RECEIVE money not reconciled (£)
            unreconciled_spent,            # SPEND money not reconciled (£)
            unreconciled_lines_total,      # net = received - spent (the adjustment)
            unreconciled_count,
            statement_balance_calculated,  # balance_in_xero + unreconciled_lines_total
            imported_statement_balance,    # None — needs the gated Finance API
            needs_reconciliation,          # unreconciled_count > 0
            lines: [{date, contact, type, amount}]   # for "View Details"
          }],
          "total_unreconciled_count": int,
          "imported_statement_available": False,
        }

    ``exclude_codes`` drops bank accounts the user has chosen to ignore.
    """
    excluded = {str(c).strip().upper() for c in (exclude_codes or set())}
    banks = _bank_accounts(chart_of_accounts)
    gl = _parse_trial_balance_balances(trial_balance)   # {account_id: {code, balance}}

    # group the UNreconciled bank transactions by their bank account
    by_acc: dict[str, dict[str, Any]] = {}
    for t in bank_transactions or []:
        if not isinstance(t, dict) or _is_reconciled(t.get("IsReconciled")):
            continue
        if (t.get("Status") or "").strip().upper() in _VOID_STATUSES:
            continue   # voided/deleted → not a real outstanding item
        ba = t.get("BankAccount") or {}
        acc_id = str(ba.get("AccountID") or "").strip() if isinstance(ba, dict) else ""
        kind = (t.get("Type") or "").strip().upper()
        if not acc_id or kind not in _DIRECTION:
            continue
        amount = float(_num(t.get("Total")))
        node = by_acc.setdefault(acc_id, {"received": 0.0, "spent": 0.0, "lines": []})
        if kind == "RECEIVE":
            node["received"] += amount
            signed = amount
        else:   # SPEND
            node["spent"] += amount
            signed = -amount
        d = _parse_date(t.get("Date"))
        contact = t.get("Contact") or {}
        node["lines"].append({
            "date": d.isoformat() if d else None,
            "contact": (contact.get("Name") or "").strip() or None,
            "type": _DIRECTION[kind],
            "amount": round(signed, 2) + 0.0,
        })

    accounts: list[dict[str, Any]] = []
    for acc_id, info in banks.items():
        code = info["code"]
        if code and code.upper() in excluded:
            continue
        balance_in_xero = float((gl.get(acc_id) or {}).get("balance") or 0.0)
        node = by_acc.get(acc_id) or {"received": 0.0, "spent": 0.0, "lines": []}
        received = round(node["received"], 2)
        spent = round(node["spent"], 2)
        net = round(received - spent, 2)
        lines = sorted(node["lines"], key=lambda x: x["date"] or "")
        accounts.append({
            "account_id": acc_id,
            "account_code": code or None,
            "account_name": info["name"] or None,
            "balance_in_xero": round(balance_in_xero, 2) + 0.0,
            "unreconciled_received": received + 0.0,
            "unreconciled_spent": spent + 0.0,
            "unreconciled_lines_total": net + 0.0,
            "unreconciled_count": len(lines),
            "statement_balance_calculated": round(balance_in_xero + net, 2) + 0.0,
            "imported_statement_balance": None,   # Finance API only — see module docstring
            "needs_reconciliation": len(lines) > 0,
            "lines": lines,
        })

    accounts.sort(key=lambda a: a["unreconciled_count"], reverse=True)
    return {
        "accounts": accounts,
        "total_unreconciled_count": sum(a["unreconciled_count"] for a in accounts),
        "imported_statement_available": False,
    }
