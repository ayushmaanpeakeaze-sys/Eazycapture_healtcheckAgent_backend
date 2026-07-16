"""Missing accruals (pattern-based) — a regular monthly expense with a gap.

For each P&L expense account that normally posts every month, flag a month with no
cost: the final month (likely a year-end accrual), a post-year payment that relates
back, a lone interim gap, or a run of 2+ consecutive missing months (irregular
timing). Only positive expense postings (debits) count as activity — reversing
credits (opening reversals) are ignored per the SOP. Review-only; average is guidance.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from app.modules.healthcheck.checks.prepayment_schedule import _fy_month_ends, _end_of_month
from app.modules.healthcheck.engine.shared import _account_lines, _PURE_EXPENSE_ACCOUNT_TYPES

ISSUE_TYPE = "missing_accrual"


def find_missing_accruals(
    transactions,
    coa_name: dict[str, str],
    coa_type: dict[str, str],
    year_end,
    months: int = 12,
    min_months_present: int = 8,
    coa_id: dict[str, str] | None = None,
) -> list[dict]:
    coa_id = coa_id or {}
    cols = _fy_month_ends(year_end, months)
    idx = {(c.year, c.month): i for i, c in enumerate(cols)}
    post = _end_of_month(year_end + relativedelta(months=1))

    present: dict[str, list[bool]] = defaultdict(lambda: [False] * months)
    totals: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal("0")] * months)
    post_year: dict[str, bool] = defaultdict(bool)

    for tx in transactions:
        for _line_no, code, amount in _account_lines(tx):
            code = (code or "").strip()
            if not code or (coa_type.get(code) or "").strip().upper() not in _PURE_EXPENSE_ACCOUNT_TYPES:
                continue
            amt = amount or Decimal("0")
            if amt <= 0:
                continue
            key = (tx.date.year, tx.date.month)
            if key in idx:
                i = idx[key]
                present[code][i] = True
                totals[code][i] += amt
            elif key == (post.year, post.month):
                post_year[code] = True

    findings: list[dict] = []
    for code, seen in present.items():
        if sum(seen) < min_months_present:
            continue  # not a regular monthly account
        name = coa_name.get(code, code)
        vals = [totals[code][i] for i in range(months) if seen[i]]
        avg = (sum(vals) / Decimal(len(vals))).quantize(Decimal("0.01")) if vals else Decimal("0")

        def _add(month_label, reason, sev, msg, is_post=False):
            findings.append({
                "issue_type": ISSUE_TYPE,
                "account_id": coa_id.get(code),
                "account_code": code,
                "account_name": name,
                "missing_month": month_label,
                "reason": reason,
                "severity": sev,
                "post_year_payment": is_post,
                "months_present": sum(seen),
                "avg_monthly_amount": str(avg),
                "message": msg[:200],
            })

        i = 0
        while i < months - 1:
            if seen[i]:
                i += 1
                continue
            j = i
            while j < months - 1 and not seen[j]:
                j += 1
            run = list(range(i, j))
            if len(run) >= 2:
                first, last = cols[run[0]], cols[run[-1]]
                _add(f"{first:%b %Y} – {last:%b %Y}", "large_gap", "high",
                     f"{name}: no cost for {len(run)} consecutive months "
                     f"({first:%b %Y} – {last:%b %Y}) — irregular timing; review for "
                     f"missing entries or an accrual.")
            else:
                _add(f"{cols[run[0]]:%b %Y}", "missing_month", "medium",
                     f"{name}: normally posts monthly but {cols[run[0]]:%b %Y} is missing — "
                     f"review whether an accrual is required.")
            i = j

        if not seen[months - 1]:
            last = cols[months - 1]
            if post_year[code]:
                _add(f"{last:%b %Y}", "post_year_cutoff", "high",
                     f"{name}: no cost in {last:%b %Y} (final month) but a payment "
                     f"appears after year-end — accrue the prior month.", is_post=True)
            else:
                _add(f"{last:%b %Y}", "final_month_missing", "high",
                     f"{name}: no cost in the final month {last:%b %Y} — accrual likely required.")
    return findings


SETTING_FIELDS: tuple = ()
META: tuple[tuple[str, str, bool], ...] = (("missing_accrual", "Accruals", True),)
