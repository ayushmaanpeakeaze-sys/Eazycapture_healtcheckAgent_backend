"""Bank reconciliation insight — derived from Xero BankTransactions.

Xero does NOT expose raw bank-statement feeds, but each BankTransaction
(spend/receive money entered in Xero) carries an ``IsReconciled`` flag and a
``Date``. From those we compute:

  * last_reconciled_date   — newest reconciled transaction (the panorama
                             "Last Bank Item Reconciled")
  * unreconciled_count     — transactions still IsReconciled=false (the
                             panorama "Unreconciled Bank Items")
  * unreconciled_value     — their total value
  * most_recent_transaction — newest transaction of any kind (panorama
                             "Most Recent Transaction")

Pure logic: no DB/HTTP.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

_MS_DATE = re.compile(r"/Date\((-?\d+)")


def _parse_date(v: Any) -> Optional[date]:
    """Xero dates come as ``/Date(1401062400000+0000)/`` (or ISO)."""
    if not v:
        return None
    s = str(v)
    m = _MS_DATE.search(s)
    if m:
        try:
            return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date()
        except (ValueError, OverflowError, OSError):
            return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _num(v: Any) -> Decimal:
    s = str(v or "").replace(",", "").strip()
    if not s:
        return Decimal("0")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _is_reconciled(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() == "true"


def compute_bank_reconciliation(
    transactions: Optional[list[dict[str, Any]]],
    extra_dates: Optional[list] = None,
) -> dict[str, Any]:
    """``extra_dates`` lets the caller fold in invoice/bill dates so
    ``most_recent_transaction`` reflects the whole ledger, not just bank rows."""
    txns = transactions or []
    unreconciled_count = 0
    unreconciled_value = Decimal("0")
    last_reconciled: Optional[date] = None
    most_recent: Optional[date] = None

    for t in txns:
        if not isinstance(t, dict):
            continue
        d = _parse_date(t.get("Date"))
        if d and (most_recent is None or d > most_recent):
            most_recent = d
        if _is_reconciled(t.get("IsReconciled")):
            if d and (last_reconciled is None or d > last_reconciled):
                last_reconciled = d
        else:
            unreconciled_count += 1
            unreconciled_value += _num(t.get("Total"))

    for d in (extra_dates or []):
        d = d if isinstance(d, date) else _parse_date(d)
        if d and (most_recent is None or d > most_recent):
            most_recent = d

    return {
        "total_transactions": len(txns),
        "unreconciled_count": unreconciled_count,
        "unreconciled_value": float(round(unreconciled_value, 2)),
        "last_reconciled_date": last_reconciled.isoformat() if last_reconciled else None,
        "most_recent_transaction": most_recent.isoformat() if most_recent else None,
    }


def compute_bank_balance_gaps(
    accounts: Optional[list[dict[str, Any]]],
    tolerance: Decimal = Decimal("0.01"),
    exclude_codes: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """Bank Balance Check (check #30) — per bank account, flag
    where the **statement balance** (Xero BankSummary closing balance) differs
    from the **GL balance** (Xero TrialBalance) by more than ``tolerance``.

    A non-zero gap = bank work unfinished (something missing or double-counted).
    Additionally, each gap carries that account's ``unreconciled_count``
    as the likely root cause.

    Pure logic — the caller supplies one dict per bank account, already joined
    from the two Xero reports::

        {"code": "090", "name": "Business Current",
         "statement_balance": 10000.00,   # BankSummary closing balance
         "gl_balance": 9400.00,           # TrialBalance for that account
         "unreconciled_count": 3}         # optional root-cause hint

    Accounts whose code is in ``exclude_codes`` (e.g. a personal/credit-card
    account) are skipped. Zero-gap accounts are omitted (hidden from the user).
    Returns one dict per flagged account, largest absolute gap first.
    """
    excluded = {str(c).strip().upper() for c in (exclude_codes or set())}
    tol = abs(tolerance)
    out: list[dict[str, Any]] = []
    for acc in (accounts or []):
        if not isinstance(acc, dict):
            continue
        code = str(acc.get("code") or "").strip()
        if code and code.upper() in excluded:
            continue
        statement = _num(acc.get("statement_balance"))
        gl = _num(acc.get("gl_balance"))
        gap = statement - gl
        if abs(gap) <= tol:
            continue
        out.append({
            "account_code": code or None,
            "account_name": (acc.get("name") or "").strip() or None,
            "statement_balance": float(round(statement, 2)),
            "gl_balance": float(round(gl, 2)),
            "gap": float(round(gap, 2)),
            "unreconciled_count": acc.get("unreconciled_count"),
        })
    out.sort(key=lambda r: abs(r["gap"]), reverse=True)
    return out


def _split_name_code(label: str) -> tuple[str, Optional[str]]:
    """'Business Current (090)' → ('Business Current', '090')."""
    label = (label or "").strip()
    if label.endswith(")") and "(" in label:
        i = label.rfind("(")
        return label[:i].strip(), label[i + 1:-1].strip()
    return label, None


def _cell_account_id(cell: Any) -> str:
    """The Xero accountID GUID from a report cell's Attributes. Both reports
    carry it on the first cell — TrialBalance as Id='account', BankSummary as
    Id='accountID' — so it's the reliable join key (BankSummary has no code in
    its label)."""
    if not isinstance(cell, dict):
        return ""
    for attr in (cell.get("Attributes") or []):
        if isinstance(attr, dict) and str(attr.get("Id") or "").lower() in ("account", "accountid"):
            v = (attr.get("Value") or "").strip()
            if v:
                return v
    return ""


def _parse_trial_balance_balances(report: Optional[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{account_id: {"code", "balance"}} from a Xero TrialBalance report. Row
    shape: Cells = [Account "Name (code)", Debit, Credit, Debit balance, Credit
    balance]; closing balance = last-two cells (debit − credit). Keyed on the
    accountID GUID in the first cell's Attributes."""
    out: dict[str, dict[str, Any]] = {}

    def _walk(rows: Optional[list]) -> None:
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            if r.get("Rows"):
                _walk(r.get("Rows"))
            if r.get("RowType") != "Row":
                continue
            cells = r.get("Cells") or []
            if len(cells) < 3:
                continue
            acc_id = _cell_account_id(cells[0])
            if not acc_id:
                continue
            _name, code = _split_name_code(cells[0].get("Value") or "")
            debit = _num(cells[-2].get("Value"))
            credit = _num(cells[-1].get("Value"))
            out[acc_id] = {"code": code, "balance": debit - credit}

    if isinstance(report, dict):
        _walk(report.get("Rows"))
    return out


def _parse_bank_summary_balances(
    report: Optional[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """{account_id: {"name", "closing"}} from a Xero BankSummary report. Row
    shape: Cells = [Account Name, Opening, Cash Received, Cash Spent, Closing];
    statement balance = last (Closing) cell. The bank account's NAME is in the
    label (no code), and its accountID GUID is in the cell Attributes — so we
    key on accountID. The trailing 'Total' SummaryRow is skipped (RowType)."""
    out: dict[str, dict[str, Any]] = {}

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
            acc_id = _cell_account_id(cells[0])
            if not acc_id:
                continue
            out[acc_id] = {
                "name": (cells[0].get("Value") or "").strip(),
                "closing": _num(cells[-1].get("Value")),
            }

    if isinstance(report, dict):
        _walk(report.get("Rows"))
    return out


def _unreconciled_by_account(transactions: Optional[list[dict[str, Any]]]) -> dict[str, int]:
    """{account_id: unreconciled_count} grouped from BankTransactions, for the
    Bank Balance check's root-cause hint. Keyed on BankAccount.AccountID to join
    with the reports."""
    out: dict[str, int] = {}
    for t in (transactions or []):
        if not isinstance(t, dict) or _is_reconciled(t.get("IsReconciled")):
            continue
        bank_acc = t.get("BankAccount") or {}
        acc_id = str(bank_acc.get("AccountID") or "").strip() if isinstance(bank_acc, dict) else ""
        if not acc_id:
            continue
        out[acc_id] = out.get(acc_id, 0) + 1
    return out


def compute_bank_balance(
    trial_balance: Optional[dict[str, Any]],
    bank_summary: Optional[dict[str, Any]],
    bank_transactions: Optional[list[dict[str, Any]]] = None,
    tolerance: Decimal = Decimal("0.01"),
    exclude_codes: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Bank Balance Check — join the BankSummary (statement) + TrialBalance (GL)
    reports per bank account (on the accountID GUID) and flag gaps. Fail-open:
    any unexpected report shape yields no gaps rather than an error.

    Returns ``{accounts_checked, gap_count, gaps: [...]}``.
    """
    statement = _parse_bank_summary_balances(bank_summary)  # by account_id
    gl = _parse_trial_balance_balances(trial_balance)       # by account_id
    unrec = _unreconciled_by_account(bank_transactions)     # by account_id

    accounts = [
        {
            "code": (gl.get(acc_id) or {}).get("code") or "",
            "name": info.get("name"),
            "statement_balance": info.get("closing"),
            "gl_balance": (gl.get(acc_id) or {}).get("balance", Decimal("0")),
            "unreconciled_count": unrec.get(acc_id),
        }
        for acc_id, info in statement.items()
    ]
    gaps = compute_bank_balance_gaps(accounts, tolerance, exclude_codes)
    return {
        "accounts_checked": len(accounts),
        "gap_count": len(gaps),
        "gaps": gaps,
    }
