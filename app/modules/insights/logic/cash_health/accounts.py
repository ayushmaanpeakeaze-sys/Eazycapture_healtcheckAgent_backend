"""Parse the Xero reports into the two inputs the Cash Health Check needs:

  * current cash  — the BANK-type accounts' balances (the Trial Balance GL
                    balance, i.e. cash "as per Xero")
  * liabilities   — every LIABILITY-class account with its owed amount, each
                    sorted into an outgoing category

Classification uses the **Chart of Accounts** account Type/Class (CURRLIAB,
TERMLIAB, BANK …) — NOT a raw credit-balance heuristic, because income accounts
also carry credit balances. The COA (which always carries a code) is the master
list; each account's balance is read from the Trial Balance, joined on the
account code and falling back to the accountID GUID when a TrialBalance row's
label has no parseable code.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.modules.insights.logic.cash_health.config import (
    CashHealthConfig,
    default_category,
)

_LIABILITY_TYPES = {"CURRLIAB", "TERMLIAB", "LIABILITY", "PAYGLIABILITY"}


def _num(v: Any) -> float:
    s = str(v or "").replace(",", "").strip()
    if not s:
        return 0.0
    try:
        return float(Decimal(s))
    except (InvalidOperation, ValueError):
        return 0.0


def _split_name_code(label: str) -> tuple[str, Optional[str]]:
    """'Accounts Payable (800)' → ('Accounts Payable', '800')."""
    label = (label or "").strip()
    if label.endswith(")") and "(" in label:
        i = label.rfind("(")
        return label[:i].strip(), label[i + 1:-1].strip()
    return label, None


def _g(d: dict[str, Any], *keys: str) -> Any:
    """First present key (handles Xero's Capitalised vs lower-cased fields)."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def parse_coa(coa: Optional[list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """{code: {name, type, klass, system_account, bank_account_type, account_id}}."""
    out: dict[str, dict[str, Any]] = {}
    for a in coa or []:
        if not isinstance(a, dict):
            continue
        code = str(_g(a, "Code", "code") or "").strip()
        if not code:
            continue
        out[code] = {
            "name": str(_g(a, "Name", "name") or "").strip(),
            "type": str(_g(a, "Type", "type") or "").strip().upper(),
            "klass": str(_g(a, "Class", "_Class", "class") or "").strip().upper(),
            "system_account": str(_g(a, "SystemAccount", "systemAccount") or "").strip(),
            "bank_account_type": str(_g(a, "BankAccountType", "bankAccountType") or "").strip(),
            "account_id": str(_g(a, "AccountID", "accountID", "account_id") or "").strip(),
        }
    return out


def _cell_account_id(cell: Any) -> str:
    if not isinstance(cell, dict):
        return ""
    for attr in (cell.get("Attributes") or []):
        if isinstance(attr, dict) and str(attr.get("Id") or "").lower() in ("account", "accountid"):
            v = (attr.get("Value") or "").strip()
            if v:
                return v
    return ""


def parse_trial_balance(report: Optional[dict[str, Any]]) -> dict[str, float]:
    """{code: balance} from a Xero TrialBalance (balance = debit − credit).
    Also indexes by accountID GUID, so an account whose TrialBalance row label
    has no parseable code can still join to the COA on the GUID."""
    by_code: dict[str, float] = {}
    by_id: dict[str, float] = {}

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
            _name, code = _split_name_code(cells[0].get("Value") or "")
            balance = _num(cells[-2].get("Value")) - _num(cells[-1].get("Value"))
            if code:
                by_code[code] = balance
            acc_id = _cell_account_id(cells[0])
            if acc_id:
                by_id[acc_id] = balance

    if isinstance(report, dict):
        _walk(report.get("Rows"))
    # stash the id-index under a reserved key the joiner can pop off
    by_code["__by_id__"] = by_id  # type: ignore[assignment]
    return by_code


def _balance_for(
    code: str, account_id: str, tb_by_code: dict[str, float], tb_by_id: dict[str, float]
) -> float:
    if code in tb_by_code:
        return tb_by_code[code]
    if account_id and account_id in tb_by_id:
        return tb_by_id[account_id]
    return 0.0


def extract_cash_and_liabilities(
    coa: Optional[list[dict[str, Any]]],
    trial_balance: Optional[dict[str, Any]],
    config: CashHealthConfig,
) -> dict[str, Any]:
    """Join COA (classification) + Trial Balance (balances) into:
      {
        "bank_accounts": [{code, name, balance, disregarded}],
        "current_cash":  float,            # sum of non-disregarded bank balances
        "liabilities":   [{code, name, category, owed, type}],
      }
    Owed = −(TB balance): a liability's credit balance becomes a positive amount
    owed. Bank cash = the BANK-type accounts' GL balance (cash as per Xero).
    """
    accounts = parse_coa(coa)
    tb = parse_trial_balance(trial_balance)
    tb_by_id: dict[str, float] = tb.pop("__by_id__", {})  # type: ignore[arg-type]

    bank_accounts: list[dict[str, Any]] = []
    liabilities: list[dict[str, Any]] = []
    current_cash = 0.0

    for code, info in accounts.items():
        bal = _balance_for(code, info["account_id"], tb, tb_by_id)
        if info["type"] == "BANK":
            disregarded = code in config.disregarded_banks
            bank_accounts.append({
                "code": code,
                "name": info["name"],
                "balance": round(bal, 2) + 0.0,   # normalise -0.0 → 0.0
                "disregarded": disregarded,
            })
            if not disregarded:
                current_cash += bal
        elif info["type"] in _LIABILITY_TYPES or info["klass"] == "LIABILITY":
            category = config.account_overrides.get(code) or default_category(
                info["name"], code, info["bank_account_type"], info["system_account"],
            )
            liabilities.append({
                "code": code,
                "name": info["name"],
                "category": category,
                "owed": round(-bal, 2) + 0.0,   # credit balance → positive owed
                "type": info["type"],
            })

    bank_accounts.sort(key=lambda a: a["code"])
    return {
        "bank_accounts": bank_accounts,
        "current_cash": round(current_cash, 2),
        "liabilities": liabilities,
    }
