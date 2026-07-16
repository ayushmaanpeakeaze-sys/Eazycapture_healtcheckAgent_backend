"""Missing accruals (pattern-based) — a regular monthly expense with a gap.

Regular expense accounts are identified two ways per the SOP: a PREDEFINED list of
names (light & heat, electricity, rates, accountancy, software subscriptions,
telephone/internet, rent, insurance …), OR auto-detected (activity in most months).
For each regular account: flag an empty final month (year-end accrual — alone enough),
a post-year payment that relates back, and — when the account is mostly present — a
lone interim gap or a run of 2+ consecutive missing months. Only positive expense
postings (debits) count as activity; reversing credits (opening reversals) are
ignored per the SOP. Review-only; average is guidance.
"""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from app.modules.healthcheck.checks.prepayment_schedule import _fy_month_ends, _end_of_month
from app.modules.healthcheck.engine.shared import _account_lines, _PURE_EXPENSE_ACCOUNT_TYPES

ISSUE_TYPE = "missing_accrual"

# SOP Part 1: predefined "regular expense accounts" — costs that normally recur every
# month. Matched as whole words against the account name (case-insensitive).
_REGULAR_ACCOUNT_KEYWORDS: tuple[str, ...] = (
    "light & heat", "light and heat", "heat", "heating", "electricity", "electric",
    "gas", "rates", "water", "accountancy", "accounting", "audit", "software",
    "subscription", "telephone", "internet", "broadband", "phone", "mobile",
    "rent", "insurance",
)
_REGULAR_NAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _REGULAR_ACCOUNT_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def find_missing_accruals(
    transactions,
    coa_name: dict[str, str],
    coa_type: dict[str, str],
    year_end,
    months: int = 12,
    min_months_present: int = 8,
    coa_id: dict[str, str] | None = None,
    regular_pattern: re.Pattern | None = None,
) -> list[dict]:
    coa_id = coa_id or {}
    rx = regular_pattern or _REGULAR_NAME_RE
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
                present[code][idx[key]] = True
                totals[code][idx[key]] += amt
            elif key == (post.year, post.month):
                post_year[code] = True

    expense_codes = {
        c for c, t in coa_type.items()
        if (t or "").strip().upper() in _PURE_EXPENSE_ACCOUNT_TYPES
    }
    predefined = {c for c in expense_codes if rx.search(coa_name.get(c, c) or "")}
    candidates = set(present.keys()) | predefined

    findings: list[dict] = []
    for code in sorted(candidates):
        seen = present.get(code) or [False] * months
        tot = totals.get(code) or [Decimal("0")] * months
        n_present = sum(seen)
        is_auto = n_present >= min_months_present
        if not (is_auto or code in predefined):
            continue  # neither predefined nor auto-detected as regular
        name = coa_name.get(code, code)
        vals = [tot[i] for i in range(months) if seen[i]]
        avg = (sum(vals) / Decimal(len(vals))).quantize(Decimal("0.01")) if vals else Decimal("0")

        def _add(month_label, reason, sev, msg, is_post=False, _code=code, _name=name,
                 _seen=seen, _avg=avg):
            findings.append({
                "issue_type": ISSUE_TYPE,
                "account_id": coa_id.get(_code),
                "account_code": _code,
                "account_name": _name,
                "missing_month": month_label,
                "reason": reason,
                "severity": sev,
                "post_year_payment": is_post,
                "months_present": sum(_seen),
                "avg_monthly_amount": str(_avg),
                "message": msg[:200],
            })

        # Part 2 (interim gaps) only when the account is mostly present (SOP: 8–11
        # months). A predefined-but-sparse account is judged on its final month only.
        if is_auto:
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

        # Part 3 (final month) + Part 4 (post-year cut-off) — for every regular account.
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
