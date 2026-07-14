"""Prepayment review (``prepayment_review``) — the SOP prepayment engine.

A P&L EXPENSE line whose DESCRIPTION shows a service period extending beyond the
financial year-end: the portion after year-end should be reviewed as a prepayment.
Description-based (an explicit date range, or a period keyword like "annual" /
"subscription" / "insurance" that implies a 12-month term from the transaction
date), compared against the year-end. Review-only — never auto-posts a prepayment;
the estimated future portion is straight-line guidance only.
"""
from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal

from dateutil import parser as _dateparser
from dateutil.relativedelta import relativedelta

from app.modules.healthcheck.checks.base import SettingField
from app.shared.transaction import BatchTransaction, FlaggedIssue
from app.modules.healthcheck.engine.shared import _account_lines, _EXPENSE_ACCOUNT_TYPES

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_DATE_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}\s+" + _MONTH + r"\s+\d{4}|" + _MONTH + r"\s+\d{4})\b",
    re.IGNORECASE,
)
_MONTH_ONLY_RE = re.compile(r"^" + _MONTH + r"\s+\d{4}$", re.IGNORECASE)

# Period keywords that imply a forward-looking (typically 12-month) term.
_PERIOD_KEYWORDS = (
    "annual", "annually", "yearly", "per annum", "12 month", "12-month",
    "twelve month", "1 year", "one year", "subscription", "licence", "license",
    "insurance", "renewal",
)
_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _PERIOD_KEYWORDS) + r")\b", re.IGNORECASE)


def _settings(settings):
    if settings is None:
        from app.modules.healthcheck.engine.audit_settings import DEFAULT_SETTINGS
        return DEFAULT_SETTINGS
    return settings


def _end_of_month(d: date) -> date:
    return d.replace(day=monthrange(d.year, d.month)[1])


def _extract_period(text: str, tx_date: date):
    """(start, end) the description covers, else None. An explicit date range wins;
    otherwise a period keyword implies a 12-month term from the transaction date."""
    dates: list[tuple[date, bool]] = []
    for tok in _DATE_RE.findall(text or ""):
        try:
            d = _dateparser.parse(tok, dayfirst=True, default=datetime(tx_date.year, 1, 1))
        except (ValueError, OverflowError, TypeError):
            continue
        dates.append((d.date(), bool(_MONTH_ONLY_RE.match(tok.strip()))))
    if len(dates) >= 2:
        dates.sort(key=lambda t: t[0])
        start, end, end_month_only = dates[0][0], dates[-1][0], dates[-1][1]
        return start, (_end_of_month(end) if end_month_only else end)
    if _KEYWORD_RE.search(text or ""):
        return tx_date, tx_date + relativedelta(months=12)
    return None


def _year_end_for(tx_date: date, month: int, day: int) -> date:
    def mk(y):
        return date(y, month, min(day, monthrange(y, month)[1]))
    ye = mk(tx_date.year)
    return ye if ye >= tx_date else mk(tx_date.year + 1)


def _months_after(year_end: date, period_end: date) -> int:
    m = (period_end.year - year_end.year) * 12 + (period_end.month - year_end.month)
    if period_end.day < year_end.day:
        m -= 1
    return max(m, 0)


def _find_prepayments(
    transactions: list[BatchTransaction],
    coa_lookup: dict[str, str],
    coa_type_lookup: dict[str, str],
    settings=None,
) -> list[FlaggedIssue]:
    """Flag a P&L EXPENSE line whose description covers a period ending after the
    financial year-end — the future portion may be a prepayment. Review-only."""
    settings = _settings(settings)
    threshold = settings.prepayment_min_amount
    # int() — from_config may hand these back as float/Decimal.
    ye_month, ye_day = int(settings.financial_year_end_month), int(settings.financial_year_end_day)
    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        doc_text = " ".join(p for p in (tx.description, tx.reference) if p)
        line_descs = {i + 1: (li.description or "") for i, li in enumerate(tx.line_items)}
        year_end = _year_end_for(tx.date, ye_month, ye_day)
        currency = (tx.currency_code or "GBP").strip().upper()
        symbol = "£" if currency == "GBP" else f"{currency} "
        for line_no, code, amount in _account_lines(tx):
            code = (code or "").strip()
            if not code or amount is None:
                continue
            if (coa_type_lookup.get(code) or "").strip().upper() not in _EXPENSE_ACCOUNT_TYPES:
                continue  # P&L expense accounts only
            amt = abs(amount)
            if amt <= threshold:
                continue
            # scan the doc text + THIS line's own description — the service period is
            # usually written on the line item, not the document.
            period = _extract_period(f"{doc_text} {line_descs.get(line_no, '')}".strip(), tx.date)
            if period is None:
                continue
            p_start, p_end = period
            if p_end <= year_end:
                continue  # whole period within the year — no prepayment
            name = coa_lookup.get(code) or code
            months_after = _months_after(year_end, p_end)
            if months_after < 1:
                continue  # spills past year-end by less than a month — immaterial
            total_months = max(round((p_end - p_start).days / 30.44), 1)
            prepaid = (Decimal(months_after) / Decimal(total_months) * amt).quantize(Decimal("0.01"))
            flagged.append(FlaggedIssue(
                transaction_id=tx.transaction_id,
                issue_type="prepayment_review",
                severity="medium",
                message=(
                    f"{tx.vendor_name}: {symbol}{amt:.2f} on {code} ({name}) covers a "
                    f"period to {p_end:%d %b %Y}, beyond the {year_end:%d %b %Y} "
                    f"year-end — ~{months_after} month(s) (~{symbol}{prepaid}) may be a "
                    f"prepayment."
                )[:200],
                current_code=code,
                reasoning=(
                    f"The description shows a service period ending {p_end:%d %b %Y}, "
                    f"after the {year_end:%d %b %Y} financial year-end. The portion "
                    f"after year-end (~{months_after} of {total_months} months, "
                    f"~{symbol}{prepaid} straight-line) should be reviewed for treatment "
                    f"as a prepayment so the year-end cut-off stays consistent. Guidance "
                    f"only — confirm with the client, no auto-posting."
                ),
                match_reasons={
                    "line_no": line_no,
                    "account_code": code,
                    "account_name": name,
                    "current_account_type": "EXPENSE",
                    "line_amount": f"{amt:.2f}",
                    "currency": currency,
                    "period_start": p_start.isoformat(),
                    "period_end": p_end.isoformat(),
                    "year_end": year_end.isoformat(),
                    "months_after_year_end": months_after,
                    "total_months": total_months,
                    "prepaid_estimate": str(prepaid),
                    "recommended_action": "prepay_future_portion",
                },
            ))
    return flagged


# --- settings (gear) ---------------------------------------------------------
SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField("prepayment_min_amount", "Date & Ageing", "prepayment_review",
                 "Review expense over …", "amount",
                 "Only review P&L expenses above this amount for prepayments (cuts "
                 "noise on small items). Default 250.",
                 unit="currency", min=0, step=50),
    SettingField("financial_year_end_month", "Date & Ageing", "prepayment_review",
                 "Financial year-end month", "int",
                 "Month (1-12) of the organisation's financial year-end. Used to "
                 "decide which part of an expense's period falls after year-end. "
                 "Default 12 (December).",
                 min=1, max=12, step=1),
    SettingField("financial_year_end_day", "Date & Ageing", "prepayment_review",
                 "Financial year-end day", "int",
                 "Day (1-31) of the organisation's financial year-end. Default 31.",
                 min=1, max=31, step=1),
)

# --- registry metadata (key, label, built) -----------------------------------
META: tuple[tuple[str, str, bool], ...] = (
    ("prepayment_review", "Prepayment review", True),
)
