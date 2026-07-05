"""Unreconciled Bank Items — the buildable (ledger-side) half of the check.

Per bank account, count Xero ``BankTransactions`` that are NOT reconciled
(``IsReconciled == false``), split by direction:
  * **Unreconciled (Received)** — RECEIVE money not matched to a statement line
  * **Unreconciled (Spent)**    — SPEND money not matched to a statement line

A third figure — **Unexplained** (imported bank-feed statement lines with
no Xero transaction at all) — is NOT obtainable from the standard Accounting
API (it needs the gated Finance API / a browser extension), so it is always
returned as ``None``. Callers should render it as "—  (requires Finance API)"
rather than implying zero.
"""
from __future__ import annotations

from typing import Any, Optional

from app.modules.insights.logic.bank import _is_reconciled


def compute_unreconciled_accounts(
    bank_transactions: Optional[list[dict[str, Any]]],
    exclude_codes: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """One row per bank account that has unreconciled transactions, largest
    total first. Accounts with zero unreconciled items are omitted (hidden from
    the user). ``exclude_codes`` drops accounts the user has ignored.
    """
    excluded = {str(c).strip().upper() for c in (exclude_codes or set())}
    by_account: dict[str, dict[str, Any]] = {}

    for t in (bank_transactions or []):
        if not isinstance(t, dict) or _is_reconciled(t.get("IsReconciled")):
            continue
        bank_acc = t.get("BankAccount") or {}
        if not isinstance(bank_acc, dict):
            continue
        acc_id = str(bank_acc.get("AccountID") or "").strip()
        code = (bank_acc.get("Code") or "").strip()
        if not acc_id or code.upper() in excluded:
            continue
        acc = by_account.setdefault(acc_id, {
            "account_id": acc_id,
            "account_code": code or None,
            "account_name": (bank_acc.get("Name") or "").strip() or None,
            "received": 0,
            "spent": 0,
        })
        kind = (t.get("Type") or "").strip().upper()
        if kind == "RECEIVE":
            acc["received"] += 1
        elif kind == "SPEND":
            acc["spent"] += 1

    out: list[dict[str, Any]] = []
    for acc in by_account.values():
        total = acc["received"] + acc["spent"]
        if total == 0:
            continue
        out.append({
            "account_id": acc["account_id"],
            "account_code": acc["account_code"],
            "account_name": acc["account_name"],
            "unreconciled_received": acc["received"],
            "unreconciled_spent": acc["spent"],
            "unexplained": None,        # ← feed statement lines: Finance API only
            "total_to_reconcile": total,
        })
    out.sort(key=lambda r: r["total_to_reconcile"], reverse=True)
    return out
