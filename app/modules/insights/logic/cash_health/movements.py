"""Recent Cash Movements — the month-over-month change in the combined bank
balance, read from a multi-period Balance Sheet (the "Total Bank" row across the
period-end columns). A positive change = cash grew that month; negative = it
fell.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional


def _num(v: Any) -> float:
    s = str(v or "").replace(",", "").strip()
    if not s:
        return 0.0
    try:
        return float(Decimal(s))
    except (InvalidOperation, ValueError):
        return 0.0


def _bank_totals(balance_sheet: Optional[dict[str, Any]]) -> list[tuple[str, float]]:
    """[(period_label, total_bank_balance)] newest-first, from a multi-period
    BalanceSheet's Bank section ``Total Bank`` summary row."""
    rows = (balance_sheet or {}).get("Rows") or []
    labels: list[str] = []
    totals: list[float] = []

    for r in rows:
        if isinstance(r, dict) and r.get("RowType") == "Header":
            labels = [(c.get("Value") or "").strip() for c in (r.get("Cells") or [])[1:]]
            break

    for r in rows:
        if not isinstance(r, dict) or r.get("RowType") != "Section":
            continue
        # the asset Bank section is titled exactly "Bank" — match it precisely so
        # a differently-titled section (e.g. "Bank Overdraft" under liabilities)
        # can't be picked instead
        if str(r.get("Title") or "").strip().lower() != "bank":
            continue
        # prefer the section's SummaryRow ("Total Bank"); else sum the rows
        summary = next(
            (s for s in (r.get("Rows") or []) if isinstance(s, dict)
             and s.get("RowType") == "SummaryRow"),
            None,
        )
        if summary is not None:
            totals = [_num(c.get("Value")) for c in (summary.get("Cells") or [])[1:]]
        else:
            cols: list[float] = []
            for sub in r.get("Rows") or []:
                vals = [_num(c.get("Value")) for c in (sub.get("Cells") or [])[1:]]
                for i, v in enumerate(vals):
                    if i < len(cols):
                        cols[i] += v
                    else:
                        cols.append(v)
            totals = cols
        break

    n = min(len(labels), len(totals))
    return [(labels[i], round(totals[i], 2) + 0.0) for i in range(n)]


def compute_movements(
    balance_sheet: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Returns::

        {
          "points":    [{period, balance}],         # newest-first, raw balances
          "movements": [{period, change, direction}] # one per month with a prior
        }

    ``direction`` is one of up | down | flat. The oldest point has no prior
    month to diff against, so it yields no movement entry.
    """
    points = _bank_totals(balance_sheet)
    movements: list[dict[str, Any]] = []
    for i in range(len(points) - 1):
        period, bal = points[i]
        prev = points[i + 1][1]
        change = round(bal - prev, 2) + 0.0
        movements.append({
            "period": period,
            "change": change,
            "direction": "up" if change > 0 else "down" if change < 0 else "flat",
        })

    return {
        "points": [{"period": p, "balance": b} for p, b in points],
        "movements": movements,
    }
