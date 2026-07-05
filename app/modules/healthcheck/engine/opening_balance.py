"""Opening Balance Differences — compare a UK Ltd company's *filed* statutory
accounts (Net Assets at Companies House) against the same figure in Xero at
each filing's period-end date.

Two pure helpers (unit-testable, no I/O):
  * ``extract_net_assets_from_balance_sheet`` — pull the "Net Assets" figure
    out of a Xero BalanceSheet report dict.
  * ``compute_opening_balance_diffs`` — diff Filed vs Xero per period end and
    flag anything at/above the materiality threshold.

The fetch orchestration (Companies House + a BalanceSheet per period end) lives
in the engine/service layer; this module stays I/O-free so it tests cleanly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_cls
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.modules.integrations.companies_house.service import FiledNetAssets

# Default materiality — flag a difference of £1 or more.
DEFAULT_MIN_DIFFERENCE = Decimal("1")

# Row labels that carry the net-assets figure on a Xero BalanceSheet.
_NET_ASSET_LABELS = ("net assets", "net liabilities", "total equity")


def _to_decimal(raw: Any) -> Optional[Decimal]:
    if raw is None:
        return None
    text = str(raw).replace(",", "").replace("(", "-").replace(")", "").strip()
    if text in ("", "-", "."):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def extract_net_assets_from_balance_sheet(report: Optional[dict[str, Any]]) -> Optional[Decimal]:
    """Return the "Net Assets" figure from a Xero BalanceSheet report dict
    (the single report from ``Reports[0]``), or None if not found."""
    if not isinstance(report, dict):
        return None

    found: Optional[Decimal] = None

    def walk(rows: list[dict[str, Any]]) -> None:
        nonlocal found
        for row in rows or []:
            rtype = row.get("RowType")
            if rtype == "Section":
                walk(row.get("Rows", []))
                continue
            cells = row.get("Cells") or []
            if not cells:
                continue
            label = str(cells[0].get("Value", "")).strip().lower()
            if any(label == lbl or label.startswith(lbl) for lbl in _NET_ASSET_LABELS):
                for cell in cells[1:]:
                    val = _to_decimal(cell.get("Value"))
                    if val is not None:
                        # "Net Assets" wins over "Total Equity" fallback.
                        if found is None or label.startswith("net"):
                            found = val
                        break

    walk(report.get("Rows", []))
    return found


@dataclass(frozen=True)
class OpeningBalanceDiff:
    period_end: str                 # YYYY-MM-DD
    net_assets_filed: Decimal       # per Companies House (or manual)
    net_assets_xero: Decimal        # per Xero BalanceSheet at that date
    difference: Decimal             # filed - xero
    filed_source: str               # "companies_house" | "manual"

    @property
    def abs_difference(self) -> Decimal:
        return abs(self.difference)


def compute_opening_balance_diffs(
    filed: list[FiledNetAssets],
    xero_net_assets: dict[str, Optional[Decimal]],
    *,
    min_difference: Decimal = DEFAULT_MIN_DIFFERENCE,
) -> list[OpeningBalanceDiff]:
    """Diff Filed vs Xero per period end. A period is flagged only when both
    figures are present and ``|filed - xero| >= min_difference``.

    ``xero_net_assets`` maps period_end → the Xero BalanceSheet Net Assets at
    that date (None if the report couldn't be fetched for that period).
    """
    out: list[OpeningBalanceDiff] = []
    for f in filed:
        xero = xero_net_assets.get(f.period_end)
        if xero is None:
            continue
        difference = f.net_assets - xero
        if abs(difference) < min_difference:
            continue
        out.append(
            OpeningBalanceDiff(
                period_end=f.period_end,
                net_assets_filed=f.net_assets,
                net_assets_xero=xero,
                difference=difference,
                filed_source=f.source,
            )
        )
    # Newest period first (matches the "Period Ended ▼" default).
    out.sort(key=lambda d: d.period_end, reverse=True)
    return out


# --- Show Late Transactions ---------------------------------------------------
# Surface transactions dated in the closed period (accounting date <= period end)
# but posted most recently — entries booked into a filed period after the fact.

# Xero document type → the label shown to the user.
_TYPE_LABELS: dict[str, str] = {
    "ACCREC": "Invoice",
    "ACCPAY": "Bill",
    "ACCRECCREDIT": "Credit Note",
    "ACCPAYCREDIT": "Credit Note",
    "RECEIVE": "Receive Money",
    "SPEND": "Spend Money",
    "MANUALJOURNAL": "Journal",
    "JOURNAL": "Journal",
}


def _type_label(raw_type: Optional[str]) -> str:
    return _TYPE_LABELS.get((raw_type or "").strip().upper(), (raw_type or "Transaction"))


@dataclass(frozen=True)
class LateTransaction:
    transaction_id: str
    type_label: str
    amount: Decimal
    accounting_date: str        # YYYY-MM-DD — when the txn is dated
    posted_date: Optional[str]  # YYYY-MM-DD — when it was entered in Xero


def _as_date(value: Any) -> Optional[date_cls]:
    if isinstance(value, date_cls):
        return value
    if isinstance(value, str) and value:
        try:
            return date_cls.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def find_late_transactions(
    transactions: list[Any],
    period_end: str,
    *,
    limit: int = 5,
    offset: int = 0,
) -> tuple[list[LateTransaction], int]:
    """Transactions dated on/before ``period_end`` (a closed period), ordered by
    *posted* date descending — so the most-recently-entered late bookings show
    first. Returns ``(page, total)`` for the "Show More" pagination.

    ``transactions`` are engine ``BatchTransaction`` objects (need ``date``,
    ``posted_date``, ``amount``, ``type``, ``transaction_id``). ``posted_date``
    is Xero's ``UpdatedDateUTC`` — a proxy for the true ledger posted date,
    which the general-ledger ``/Journals`` endpoint (a scope we lack) would give.
    """
    cutoff = _as_date(period_end)
    if cutoff is None:
        return [], 0

    eligible = [tx for tx in transactions if (_as_date(getattr(tx, "date", None)) or cutoff) <= cutoff]
    # Most recently posted first; unknown posted dates sink to the bottom.
    eligible.sort(
        key=lambda tx: (_as_date(getattr(tx, "posted_date", None)) or date_cls.min),
        reverse=True,
    )
    total = len(eligible)

    page: list[LateTransaction] = []
    for tx in eligible[offset:offset + limit]:
        acc = _as_date(getattr(tx, "date", None))
        posted = _as_date(getattr(tx, "posted_date", None))
        page.append(
            LateTransaction(
                transaction_id=str(getattr(tx, "transaction_id", "")),
                type_label=_type_label(getattr(tx, "type", None)),
                amount=getattr(tx, "amount", Decimal("0")) or Decimal("0"),
                accounting_date=acc.isoformat() if acc else "",
                posted_date=posted.isoformat() if posted else None,
            )
        )
    return page, total
