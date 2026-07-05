"""Corporation-tax estimate (UK bands) + Directors'-loan auto-detect."""
from __future__ import annotations

from app.modules.insights.logic.corp_tax import estimate_corporation_tax
from app.modules.insights.logic.directors_loans import find_director_loans


# --- Corporation tax (FY2023+ UK rules) ------------------------------------

def test_small_profits_rate():
    r = estimate_corporation_tax(40000)
    assert r["tax_estimate"] == 7600.0          # 40000 * 19%
    assert "small" in r["band"]


def test_main_rate():
    r = estimate_corporation_tax(300000)
    assert r["tax_estimate"] == 75000.0         # 300000 * 25%
    assert "main" in r["band"]


def test_marginal_relief():
    r = estimate_corporation_tax(100000)
    # 100000*25% - (250000-100000)*3/200 = 25000 - 2250 = 22750
    assert r["tax_estimate"] == 22750.0
    assert "marginal" in r["band"]
    assert r["effective_rate"] == 22.75


def test_loss_no_tax():
    r = estimate_corporation_tax(-500)
    assert r["tax_estimate"] == 0.0
    assert "loss" in r["band"]


# --- Directors' loan auto-detect -------------------------------------------

def _tb_with_dla():
    def row(label, debit_bal, credit_bal):
        return {"RowType": "Row", "Cells": [
            {"Value": label}, {"Value": ""}, {"Value": ""},
            {"Value": debit_bal}, {"Value": credit_bal},
        ]}
    return {"Rows": [
        {"RowType": "Section", "Title": "Liabilities", "Rows": [
            row("Sales (200)", "", "5000.00"),                       # ignored
            row("Director's Loan Account (835)", "12000.00", ""),    # overdrawn
            row("Director Loan - J Smith (836)", "", "3000.00"),     # in credit
            row("Bank Loan (840)", "", "9000.00"),                   # NOT a director loan
        ]},
    ]}


def test_detects_director_loans():
    out = find_director_loans(_tb_with_dla())
    assert out["detected"] is True
    # Only the two "director" accounts match — Sales and the plain Bank Loan don't
    # (matching bare "loan" would be too broad; that's why manual mapping exists).
    assert len(out["accounts"]) == 2
    by_code = {a["code"]: a for a in out["accounts"]}
    assert by_code["835"]["balance"] == 12000.0
    assert by_code["835"]["overdrawn"] is True
    assert by_code["836"]["balance"] == -3000.0
    assert by_code["836"]["overdrawn"] is False


def test_no_dla_detected():
    tb = {"Rows": [{"RowType": "Section", "Title": "Assets", "Rows": [
        {"RowType": "Row", "Cells": [{"Value": "Bank (090)"}, {"Value": ""}, {"Value": ""}, {"Value": "100.00"}, {"Value": ""}]},
    ]}]}
    out = find_director_loans(tb)
    assert out["detected"] is False
    assert out["accounts"] == []
