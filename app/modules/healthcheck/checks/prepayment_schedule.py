"""Prepayment schedule — the amortisation working paper.

Lays out the items sitting in a Prepayments account month-by-month (the grid),
with each line's carry-forward balance at the year-end, a per-month total, and a
reconciliation of the schedule balance to the account's ledger balance.

Guidance only: the month-end release journals are for the accountant to post —
nothing is auto-posted (SOP: "Do not auto-book").
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from dateutil.relativedelta import relativedelta

from app.modules.healthcheck.checks.prepayments import _extract_period, _end_of_month

_Q = Decimal("0.001")


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
) -> dict:
    """Build the amortisation grid for a list of prepaid items.

    Each ``item``: ``{date, invoice_no, supplier, description, account_code,
    account_name, amount, ledger_amount?}``. ``amount`` is the prepaid cost;
    ``ledger_amount`` (optional) is what currently sits in the account for that
    line — defaults to ``amount`` when the release journals aren't synced.

    Returns the columns, one row per line (monthly cells + carry-forward
    balance), the per-column + balance totals, and the schedule-vs-ledger check.
    """
    cols = _fy_month_ends(year_end, months)
    col_labels = [f"{c:%b-%y}" for c in cols]

    rows: list[dict] = []
    col_totals = [Decimal("0") for _ in cols]
    schedule_balance = Decimal("0")
    ledger_balance = Decimal("0")

    for it in items:
        amount = Decimal(str(it["amount"]))
        ledger_balance += Decimal(str(it.get("ledger_amount", it["amount"])))
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
    ledger_balance = ledger_balance.quantize(_Q, ROUND_HALF_UP)
    difference = (ledger_balance - schedule_balance).quantize(_Q, ROUND_HALF_UP)

    return {
        "year_end": year_end.isoformat(),
        "columns": col_labels,
        "rows": rows,
        "column_totals": [str(t.quantize(_Q, ROUND_HALF_UP)) for t in col_totals],
        "total_balance": str(schedule_balance),
        "validation": {
            "schedule_balance": str(schedule_balance),
            "ledger_balance": str(ledger_balance),
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
