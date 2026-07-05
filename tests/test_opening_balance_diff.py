"""Opening Balance Differences — iXBRL parsing + Net Assets comparison.

Covers the two pure cores of the check (no I/O):
  * Companies House iXBRL → filed Net Assets (sign / scale / multi-period)
  * Xero BalanceSheet → Net Assets extraction
  * Filed-vs-Xero diff engine (reproduces the published numbers)
"""
from decimal import Decimal

from app.modules.integrations.companies_house.ixbrl import extract_net_assets
from app.modules.integrations.companies_house.service import FiledNetAssets
from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.modules.healthcheck.engine.opening_balance import (
    compute_opening_balance_diffs,
    extract_net_assets_from_balance_sheet,
    find_late_transactions,
)


@dataclass
class _Tx:
    transaction_id: str
    date: date
    posted_date: Optional[date]
    amount: Decimal
    type: str


def _ixbrl(rows: str) -> str:
    return f"""<?xml version="1.0"?>
<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:core="http://xbrl.frc.org.uk/fr/2021-01-01/core">
<head><ix:header><ix:resources>
  <xbrli:context id="c1"><xbrli:period><xbrli:instant>2023-09-30</xbrli:instant></xbrli:period></xbrli:context>
  <xbrli:context id="c2"><xbrli:period><xbrli:instant>2022-09-30</xbrli:instant></xbrli:period></xbrli:context>
</ix:resources></ix:header></head>
<body>{rows}</body></html>"""


# ---------------- iXBRL parsing ----------------

def test_ixbrl_basic_net_assets():
    doc = _ixbrl(
        '<ix:nonFraction name="core:NetAssetsLiabilities" contextRef="c1" decimals="0">324</ix:nonFraction>'
        '<ix:nonFraction name="core:NetAssetsLiabilities" contextRef="c2" decimals="0">528</ix:nonFraction>'
    )
    out = extract_net_assets(doc)
    assert out == {"2023-09-30": Decimal("324"), "2022-09-30": Decimal("528")}


def test_ixbrl_negative_sign():
    doc = _ixbrl(
        '<ix:nonFraction name="core:NetAssetsLiabilities" contextRef="c1" sign="-" decimals="0">12968</ix:nonFraction>'
    )
    assert extract_net_assets(doc)["2023-09-30"] == Decimal("-12968")


def test_ixbrl_scale_thousands():
    doc = _ixbrl(
        '<ix:nonFraction name="core:NetAssetsLiabilities" contextRef="c1" scale="3" decimals="0">2</ix:nonFraction>'
    )
    assert extract_net_assets(doc)["2023-09-30"] == Decimal("2000")


def test_ixbrl_comma_and_parentheses():
    doc = _ixbrl(
        '<ix:nonFraction name="core:NetAssetsLiabilities" contextRef="c1" decimals="0">(1,234)</ix:nonFraction>'
    )
    assert extract_net_assets(doc)["2023-09-30"] == Decimal("-1234")


def test_ixbrl_canonical_concept_wins_over_equity():
    # Same date tagged both as Equity and NetAssetsLiabilities — canonical wins.
    doc = _ixbrl(
        '<ix:nonFraction name="core:Equity" contextRef="c1" decimals="0">999</ix:nonFraction>'
        '<ix:nonFraction name="core:NetAssetsLiabilities" contextRef="c1" decimals="0">324</ix:nonFraction>'
    )
    assert extract_net_assets(doc)["2023-09-30"] == Decimal("324")


def test_ixbrl_equity_fallback_when_no_net_assets():
    doc = _ixbrl(
        '<ix:nonFraction name="core:ShareholdersFunds" contextRef="c1" decimals="0">700</ix:nonFraction>'
    )
    assert extract_net_assets(doc)["2023-09-30"] == Decimal("700")


def test_ixbrl_garbage_returns_empty():
    assert extract_net_assets(b"not xml at all <<<") == {}
    assert extract_net_assets("<html><body>no facts</body></html>") == {}


# ---------------- Xero BalanceSheet extraction ----------------

def _bs(net_assets: str) -> dict:
    return {
        "Rows": [
            {"RowType": "Section", "Title": "Liabilities", "Rows": [
                {"RowType": "SummaryRow", "Cells": [
                    {"Value": "Total Liabilities"}, {"Value": "100.00"}]},
            ]},
            {"RowType": "SummaryRow", "Cells": [
                {"Value": "Net Assets"}, {"Value": net_assets}]},
            {"RowType": "Section", "Title": "Equity", "Rows": [
                {"RowType": "SummaryRow", "Cells": [
                    {"Value": "Total Equity"}, {"Value": net_assets}]},
            ]},
        ]
    }


def test_balance_sheet_net_assets_extraction():
    assert extract_net_assets_from_balance_sheet(_bs("19407.25")) == Decimal("19407.25")


def test_balance_sheet_negative_net_assets():
    assert extract_net_assets_from_balance_sheet(_bs("-12968.00")) == Decimal("-12968.00")


def test_balance_sheet_missing_returns_none():
    assert extract_net_assets_from_balance_sheet({"Rows": []}) is None
    assert extract_net_assets_from_balance_sheet(None) is None


# ---------------- Diff engine (reproduction) ----------------

def test_diff_engine_reproduces_totals():
    filed = [
        FiledNetAssets("2023-09-30", Decimal("324")),
        FiledNetAssets("2022-09-30", Decimal("528")),
        FiledNetAssets("2021-09-30", Decimal("427")),
    ]
    xero = {
        "2023-09-30": Decimal("21368"),
        "2022-09-30": Decimal("-12968"),
        "2021-09-30": Decimal("-2592"),
    }
    diffs = compute_opening_balance_diffs(filed, xero)
    assert [d.difference for d in diffs] == [
        Decimal("-21044"), Decimal("13496"), Decimal("3019")]
    assert sum(d.abs_difference for d in diffs) == Decimal("37559")
    # newest period first
    assert diffs[0].period_end == "2023-09-30"


def test_diff_engine_skips_below_threshold():
    filed = [FiledNetAssets("2023-09-30", Decimal("100.50"))]
    xero = {"2023-09-30": Decimal("100.00")}  # 50p difference < £1
    assert compute_opening_balance_diffs(filed, xero) == []


def test_diff_engine_custom_threshold_flags():
    filed = [FiledNetAssets("2023-09-30", Decimal("100.50"))]
    xero = {"2023-09-30": Decimal("100.00")}
    diffs = compute_opening_balance_diffs(filed, xero, min_difference=Decimal("0.10"))
    assert diffs and diffs[0].difference == Decimal("0.50")


def test_diff_engine_skips_when_xero_missing():
    filed = [FiledNetAssets("2023-09-30", Decimal("324"))]
    assert compute_opening_balance_diffs(filed, {"2023-09-30": None}) == []


# ---------------- Show Late Transactions ----------------

def _late_fixture() -> list[_Tx]:
    return [
        _Tx("a", date(2023, 5, 14), date(2024, 8, 8), Decimal("84"), "ACCREC"),
        _Tx("b", date(2023, 5, 10), date(2024, 7, 19), Decimal("87"), "ACCPAY"),
        _Tx("c", date(2023, 3, 31), date(2024, 7, 15), Decimal("0"), "MANUALJOURNAL"),
        _Tx("d", date(2023, 9, 24), date(2024, 3, 5), Decimal("210"), "ACCREC"),
        # dated AFTER the period end — must be excluded
        _Tx("z", date(2023, 12, 1), date(2024, 9, 9), Decimal("999"), "ACCREC"),
    ]


def test_late_txns_excludes_after_period_and_orders_by_posted():
    page, total = find_late_transactions(_late_fixture(), "2023-09-30", limit=10)
    assert total == 4  # 'z' (dated after 30/09/2023) excluded
    # ordered by posted date desc
    assert [t.transaction_id for t in page] == ["a", "b", "c", "d"]
    assert page[0].posted_date == "2024-08-08"


def test_late_txns_type_labels():
    page, _ = find_late_transactions(_late_fixture(), "2023-09-30", limit=10)
    labels = {t.transaction_id: t.type_label for t in page}
    assert labels == {"a": "Invoice", "b": "Bill", "c": "Journal", "d": "Invoice"}


def test_late_txns_pagination():
    page1, total = find_late_transactions(_late_fixture(), "2023-09-30", limit=2, offset=0)
    page2, _ = find_late_transactions(_late_fixture(), "2023-09-30", limit=2, offset=2)
    assert total == 4
    assert [t.transaction_id for t in page1] == ["a", "b"]
    assert [t.transaction_id for t in page2] == ["c", "d"]


def test_late_txns_bad_period_returns_empty():
    assert find_late_transactions(_late_fixture(), "not-a-date") == ([], 0)
