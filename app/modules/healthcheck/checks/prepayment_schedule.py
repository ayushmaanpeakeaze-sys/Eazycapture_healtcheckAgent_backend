"""Prepayment schedule — the amortisation working paper: month-by-month grid,
carry-forward balance per line, and reconciliation to the ledger. Review-only."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from dateutil.relativedelta import relativedelta

from app.modules.healthcheck.checks.prepayments import _extract_period, _end_of_month

_Q = Decimal("0.001")


_PREPAYMENT_ACCOUNT_TYPES = {"PREPAYMENT"}
_PREPAYMENT_NAME_HINTS = ("prepay", "prepaid")


def is_prepayment_account(name: str | None, acc_type: str | None) -> bool:
    """A Prepayments account — Xero's PREPAYMENT type, or a name that says so
    (many orgs park prepayments in a Current-Asset account literally named
    'Prepayments')."""
    if (acc_type or "").strip().upper() in _PREPAYMENT_ACCOUNT_TYPES:
        return True
    low = (name or "").lower()
    return any(h in low for h in _PREPAYMENT_NAME_HINTS)


# Debit side only: bills + Spend Money post into Prepayments; sales/receipts/credits net out.
_PREPAYMENT_DEBIT_DOC_TYPES = {"ACCPAY", "SPEND"}


def collect_prepayment_items(
    documents: list[dict],
    coa_name: dict[str, str],
    coa_type: dict[str, str],
) -> list[dict]:
    """Bill / Spend-Money lines posted to a Prepayments account, shaped for the builder."""
    items: list[dict] = []
    for doc in documents:
        if (doc.get("Type") or "").strip().upper() not in _PREPAYMENT_DEBIT_DOC_TYPES:
            continue
        contact = (doc.get("Contact") or {}).get("Name")
        for li in (doc.get("LineItems") or []):
            code = str(li.get("AccountCode") or "").strip()
            if not code or not is_prepayment_account(coa_name.get(code), coa_type.get(code)):
                continue
            items.append({
                "date": _xero_date(doc.get("Date")),
                "invoice_no": doc.get("InvoiceNumber") or doc.get("Reference") or doc.get("CreditNoteNumber"),
                "supplier": contact,
                "description": li.get("Description") or doc.get("Reference"),
                "account_code": code,
                "account_name": coa_name.get(code, code),
                "amount": li.get("LineAmount") or 0,
            })
    return items


def prepayment_balance_from_trial_balance(
    parsed_tb: dict, account_codes: set[str],
) -> Decimal:
    """Sum the balances of the Prepayments account(s) from a parsed Xero Trial
    Balance (``{account_id: {"code", "balance"}}``) — the real ledger figure the
    schedule reconciles against."""
    total = Decimal("0")
    for row in (parsed_tb or {}).values():
        if str((row or {}).get("code") or "").strip() in account_codes:
            total += Decimal(str(row.get("balance") or 0))
    return total


def _xero_date(raw) -> date | None:
    """Xero '/Date(ms+0000)/' or ISO → date."""
    if not raw:
        return None
    s = str(raw)
    if s.startswith("/Date("):
        try:
            ms = int(s[6:].split("+")[0].split("-")[0])
            return date.fromtimestamp(ms / 1000)
        except (ValueError, OverflowError, OSError):
            return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _fy_month_ends(year_end: date, months: int = 12) -> list[date]:
    """The month-ends of the financial year that ENDS on ``year_end`` — oldest
    first (year_end 31 Mar 2027 → Apr 2026 … Mar 2027)."""
    first = year_end + relativedelta(months=-(months - 1))
    return [_end_of_month(first + relativedelta(months=i)) for i in range(months)]


def _released_months(period_start: date, cutoff: date, total_months: int) -> int:
    """Whole months released from the period start up to and including ``cutoff``,
    capped at the term length (never release more than the whole prepayment)."""
    m = (cutoff.year - period_start.year) * 12 + (cutoff.month - period_start.month) + 1
    return max(0, min(m, total_months))


def build_prepayment_schedule(
    items: list[dict],
    year_end: date,
    months: int = 12,
    ledger_balance: str | Decimal | None = None,
) -> dict:
    """Build the amortisation grid for a list of prepaid items.

    Each ``item``: ``{date, invoice_no, supplier, description, account_code,
    account_name, amount, ledger_amount?}``. ``amount`` is the prepaid cost.

    ``ledger_balance`` is the ACTUAL Prepayments-account balance read from Xero
    (the Trial Balance action). Pass it to reconcile the schedule against the real
    ledger; omit it and the ledger side falls back to the sum of posted amounts.

    Returns the columns, one row per line (monthly cells + carry-forward
    balance), the per-column + balance totals, and the schedule-vs-ledger check.
    """
    cols = _fy_month_ends(year_end, months)
    col_labels = [f"{c:%b-%y}" for c in cols]

    rows: list[dict] = []
    col_totals = [Decimal("0") for _ in cols]
    schedule_balance = Decimal("0")
    ledger_balance_sum = Decimal("0")

    for it in items:
        amount = Decimal(str(it["amount"]))
        ledger_balance_sum += Decimal(str(it.get("ledger_amount", it["amount"])))
        tx_date = it["date"]
        period = _extract_period(str(it.get("description") or ""), tx_date)

        if period is None:
            # No period signal — can't amortise. Surface it so it isn't silently
            # dropped from a reconciliation (it still sits in the account).
            rows.append({
                **_row_meta(it, amount),
                "period_start": None, "period_end": None,
                "total_months": None, "monthly": None,
                "cells": [None] * len(cols),
                "balance": str(amount.quantize(_Q, ROUND_HALF_UP)),
                "unscheduled": True,
            })
            schedule_balance += amount
            continue

        p_start, p_end = period
        total_months = max(round((p_end - p_start).days / 30.44), 1)
        monthly = (amount / Decimal(total_months)).quantize(_Q, ROUND_HALF_UP)

        s_eom, e_eom = _end_of_month(p_start), _end_of_month(p_end)
        cells: list[Decimal | None] = []
        for i, col in enumerate(cols):
            if s_eom <= col <= e_eom:
                cells.append(monthly)
                col_totals[i] += monthly
            else:
                cells.append(None)

        released = _released_months(p_start, year_end, total_months)
        balance = (amount - monthly * released).quantize(_Q, ROUND_HALF_UP)
        schedule_balance += balance

        rows.append({
            **_row_meta(it, amount),
            "period_start": p_start.isoformat(), "period_end": p_end.isoformat(),
            "total_months": total_months, "monthly": str(monthly),
            "cells": [str(v) if v is not None else None for v in cells],
            "balance": str(balance),
            "unscheduled": False,
        })

    schedule_balance = schedule_balance.quantize(_Q, ROUND_HALF_UP)
    # Real Xero account balance when provided (from the Trial Balance action),
    # else the sum of posted amounts.
    ledger = (Decimal(str(ledger_balance)) if ledger_balance is not None
              else ledger_balance_sum).quantize(_Q, ROUND_HALF_UP)
    difference = (ledger - schedule_balance).quantize(_Q, ROUND_HALF_UP)

    return {
        "year_end": year_end.isoformat(),
        "columns": col_labels,
        "rows": rows,
        "column_totals": [str(t.quantize(_Q, ROUND_HALF_UP)) for t in col_totals],
        "total_balance": str(schedule_balance),
        "validation": {
            "schedule_balance": str(schedule_balance),
            "ledger_balance": str(ledger),
            "ledger_source": "xero_trial_balance" if ledger_balance is not None else "posted_amounts",
            "difference": str(difference),
            # A few units of rounding is fine; a material gap means a release
            # was missed or something is mis-posted in the account.
            "reconciled": abs(difference) <= Decimal("1"),
        },
    }


def _row_meta(it: dict, amount: Decimal) -> dict:
    return {
        "date": it["date"].isoformat() if isinstance(it.get("date"), date) else it.get("date"),
        "invoice_no": it.get("invoice_no"),
        "supplier": it.get("supplier"),
        "description": it.get("description"),
        "account_code": it.get("account_code"),
        "account_name": it.get("account_name"),
        "amount": str(amount.quantize(_Q, ROUND_HALF_UP)),
    }
