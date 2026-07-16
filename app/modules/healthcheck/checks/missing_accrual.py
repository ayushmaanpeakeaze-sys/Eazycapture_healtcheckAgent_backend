"""Missing accruals (pattern-based) — a regular monthly expense with a gap.

For each P&L expense account that normally posts every month, flag a month with no
cost: the final month (highest — likely a year-end accrual), a post-year payment that
relates back, or an interim gap. Review-only; average monthly amount is guidance.
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
) -> list[dict]:
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
            key = (tx.date.year, tx.date.month)
            if key in idx:
                i = idx[key]
                present[code][i] = True
                totals[code][i] += abs(amount or Decimal("0"))
            elif key == (post.year, post.month):
                post_year[code] = True

    findings: list[dict] = []
    for code, seen in present.items():
        if sum(seen) < min_months_present:
            continue  # not a regular monthly account
        name = coa_name.get(code, code)
        vals = [totals[code][i] for i in range(months) if seen[i]]
        avg = (sum(vals) / Decimal(len(vals))).quantize(Decimal("0.01")) if vals else Decimal("0")
        for i in range(months):
            if seen[i]:
                continue
            is_final = i == months - 1
            if is_final and post_year[code]:
                reason, sev = "post_year_cutoff", "high"
                msg = (f"{name}: no cost in {cols[i]:%b %Y} (final month) but a payment "
                       f"appears after year-end — accrue the prior month.")
            elif is_final:
                reason, sev = "final_month_missing", "high"
                msg = f"{name}: no cost in the final month {cols[i]:%b %Y} — accrual likely required."
            else:
                reason, sev = "missing_month", "medium"
                msg = (f"{name}: normally posts monthly but {cols[i]:%b %Y} is missing — "
                       f"review whether an accrual is required.")
            findings.append({
                "issue_type": ISSUE_TYPE,
                "account_code": code,
                "account_name": name,
                "missing_month": f"{cols[i]:%b %Y}",
                "reason": reason,
                "severity": sev,
                "post_year_payment": is_final and post_year[code],
                "months_present": sum(seen),
                "avg_monthly_amount": str(avg),
                "message": msg[:200],
            })
    return findings


SETTING_FIELDS: tuple = ()
# built=False until the account-level persistence is wired (detection is ready).
META: tuple[tuple[str, str, bool], ...] = (("missing_accrual", "Accruals", False),)
