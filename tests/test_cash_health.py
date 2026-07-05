"""Cash Health Check — unit tests for the modular pieces (config, accounts,
outgoings, indicator, movements) + the orchestrator. Pure logic, no infra."""
from app.modules.insights.logic.cash_health import compute_cash_health
from app.modules.insights.logic.cash_health.accounts import extract_cash_and_liabilities
from app.modules.insights.logic.cash_health.config import (
    CORPORATION_TAX,
    CREDIT_CARDS,
    LOANS_OTHER,
    NET_WAGES,
    PAYE_NIC,
    PENSION,
    SUPPLIERS,
    VAT,
    category_weight,
    default_category,
    parse_config,
)
from app.modules.insights.logic.cash_health.indicator import health_indicator
from app.modules.insights.logic.cash_health.movements import compute_movements
from app.modules.insights.logic.cash_health.outgoings import build_outgoings


# --- fixtures ---------------------------------------------------------------

def _coa():
    return [
        {"Code": "090", "Name": "Business Bank Account", "Type": "BANK", "Class": "ASSET", "AccountID": "id-090"},
        {"Code": "091", "Name": "Personal Account", "Type": "BANK", "Class": "ASSET", "AccountID": "id-091"},
        {"Code": "800", "Name": "Accounts Payable", "Type": "CURRLIAB", "Class": "LIABILITY", "SystemAccount": "ACCPAY", "AccountID": "id-800"},
        {"Code": "820", "Name": "VAT", "Type": "CURRLIAB", "Class": "LIABILITY", "AccountID": "id-820"},
        {"Code": "825", "Name": "PAYE Payable", "Type": "CURRLIAB", "Class": "LIABILITY", "AccountID": "id-825"},
        {"Code": "855", "Name": "Pension Payable", "Type": "CURRLIAB", "Class": "LIABILITY", "AccountID": "id-855"},
        {"Code": "900", "Name": "Bank Loan", "Type": "TERMLIAB", "Class": "LIABILITY", "AccountID": "id-900"},
        {"Code": "200", "Name": "Sales", "Type": "REVENUE", "Class": "REVENUE", "AccountID": "id-200"},
    ]


def _tb_row(name, code, debit_bal, credit_bal, acc_id):
    return {"RowType": "Row", "Cells": [
        {"Value": f"{name} ({code})", "Attributes": [{"Id": "account", "Value": acc_id}]},
        {"Value": "0.00"}, {"Value": "0.00"},
        {"Value": str(debit_bal)}, {"Value": str(credit_bal)},
    ]}


def _tb():
    # liabilities carry a credit balance; the bank a debit balance
    return {"Rows": [{"RowType": "Section", "Rows": [
        _tb_row("Business Bank Account", "090", "5000", "0", "id-090"),
        _tb_row("Personal Account", "091", "2000", "0", "id-091"),
        _tb_row("Accounts Payable", "800", "0", "3000", "id-800"),
        _tb_row("VAT", "820", "0", "1000", "id-820"),
        _tb_row("PAYE Payable", "825", "0", "500", "id-825"),
        _tb_row("Pension Payable", "855", "0", "0", "id-855"),
        _tb_row("Bank Loan", "900", "0", "20000", "id-900"),
        _tb_row("Sales", "200", "0", "42000", "id-200"),
    ]}]}


def _bs_periods():
    return {"Rows": [
        {"RowType": "Header", "Cells": [{"Value": ""}, {"Value": "30 Jun"}, {"Value": "31 May"}, {"Value": "30 Apr"}]},
        {"RowType": "Section", "Title": "Bank", "Rows": [
            {"RowType": "Row", "Cells": [{"Value": "Bank"}, {"Value": "7000"}, {"Value": "10000"}, {"Value": "5000"}]},
            {"RowType": "SummaryRow", "Cells": [{"Value": "Total Bank"}, {"Value": "7000"}, {"Value": "10000"}, {"Value": "5000"}]},
        ]},
    ]}


# --- config -----------------------------------------------------------------

def test_config_defaults():
    c = parse_config(None)
    assert c.is_included(SUPPLIERS) is True       # default: all included
    assert c.override_for(VAT) is None
    assert c.disregarded_banks == frozenset()


def test_config_parses_overrides_and_disregards():
    c = parse_config({
        "included": {"loans_other": False},
        "overrides": {"net_wages": "8000", "vat": 1500.0},
        "account_overrides": {"912": "loans_other", "999": "not_a_category"},
        "disregarded_banks": ["091", "  "],
    })
    assert c.is_included(LOANS_OTHER) is False
    assert c.is_included(SUPPLIERS) is True
    assert c.override_for("net_wages") == 8000.0      # coerced from str
    assert c.override_for(VAT) == 1500.0
    assert c.account_overrides == {"912": "loans_other"}   # bad category dropped
    assert c.disregarded_banks == frozenset({"091"})       # blank dropped


def test_default_category_by_name_then_code():
    assert default_category("VAT", "820") == VAT
    assert default_category("PAYE Payable", None) == PAYE_NIC
    assert default_category("Superannuation Payable", None) == PENSION
    assert default_category("Income Tax Payable", "830") == CORPORATION_TAX
    assert default_category("Accounts Payable", "800", system_account="ACCPAY") == SUPPLIERS
    assert default_category("Company Credit Card", None, bank_account_type="CREDITCARD") == CREDIT_CARDS
    # name beats a stale code: 855 is "Clearing Account" here, not pension
    assert default_category("Clearing Account", "855") == LOANS_OTHER
    assert default_category("Some Mystery Provision", None) == LOANS_OTHER


def test_default_category_word_boundary_no_false_substring():
    # short tokens must not match inside longer words
    assert default_category("Technician Wages Payable", None) == NET_WAGES   # not paye (techNIC)
    assert default_category("Mechanic Wages Payable", None) == NET_WAGES     # mechaNIC
    assert default_category("Innovation Grant Payable", None) == LOANS_OTHER  # innoVATion
    assert default_category("Private Loan", None) == LOANS_OTHER             # priVATe
    assert default_category("Municipal Tax Payable", None) == LOANS_OTHER    # muNICipal
    # genuine matches still work
    assert default_category("NIC Payable", None) == PAYE_NIC
    assert default_category("VAT Control Account", None) == VAT


def test_short_term_weighted_more_than_long_term():
    assert category_weight(SUPPLIERS) > category_weight(LOANS_OTHER)
    assert category_weight(VAT) > category_weight(CORPORATION_TAX)


# --- accounts ---------------------------------------------------------------

def test_extract_cash_and_liabilities():
    res = extract_cash_and_liabilities(_coa(), _tb(), parse_config(None))
    assert res["current_cash"] == 7000.0          # 5000 + 2000
    cats = {l["code"]: l["category"] for l in res["liabilities"]}
    assert cats["800"] == SUPPLIERS
    assert cats["820"] == VAT
    assert cats["825"] == PAYE_NIC
    assert cats["855"] == PENSION
    assert cats["900"] == LOANS_OTHER
    assert "200" not in cats                        # revenue is NOT a liability
    owed = {l["code"]: l["owed"] for l in res["liabilities"]}
    assert owed["800"] == 3000.0                    # credit balance → positive owed


def test_disregarded_bank_excluded_from_cash():
    res = extract_cash_and_liabilities(
        _coa(), _tb(), parse_config({"disregarded_banks": ["091"]}))
    assert res["current_cash"] == 5000.0            # personal account dropped
    flags = {b["code"]: b["disregarded"] for b in res["bank_accounts"]}
    assert flags["091"] is True and flags["090"] is False


# --- outgoings --------------------------------------------------------------

def _liabilities():
    return extract_cash_and_liabilities(_coa(), _tb(), parse_config(None))["liabilities"]


def test_outgoings_cumulative_can_pay():
    out = build_outgoings(_liabilities(), current_cash=7000.0,
                          config=parse_config(None), corp_tax_estimate=2000.0)
    by_cat = {c["category"]: c for c in out["categories"]}
    assert by_cat[SUPPLIERS]["amount"] == 3000.0
    assert by_cat[CORPORATION_TAX]["amount"] == 2000.0   # estimate folded in
    # cash 7000 covers suppliers(3000)+paye(500)+vat(1000)+corp(2000)=6500, not loans
    assert by_cat[SUPPLIERS]["can_pay"] is True
    assert by_cat[VAT]["can_pay"] is True
    assert by_cat[CORPORATION_TAX]["can_pay"] is True
    assert by_cat[LOANS_OTHER]["can_pay"] is False
    assert out["total_expected_outgoings"] == 26500.0
    assert out["all_covered"] is False


def test_outgoings_override_and_exclude():
    cfg = parse_config({"overrides": {"net_wages": 4000.0}, "included": {"loans_other": False}})
    out = build_outgoings(_liabilities(), current_cash=7000.0, config=cfg, corp_tax_estimate=0.0)
    by_cat = {c["category"]: c for c in out["categories"]}
    assert by_cat["net_wages"]["amount"] == 4000.0 and by_cat["net_wages"]["overridden"] is True
    assert by_cat[LOANS_OTHER]["included"] is False
    assert by_cat[LOANS_OTHER]["can_pay"] is None         # excluded → not assessed
    # excluded loans (20000) drop out of the total
    assert out["total_expected_outgoings"] == 3000.0 + 4000.0 + 500.0 + 1000.0


# --- indicator --------------------------------------------------------------

def test_indicator_no_outgoings_is_full():
    cats = [{"category": SUPPLIERS, "included": True, "amount": 0.0}]
    assert health_indicator(cats, 5000.0)["score"] == 100.0


def test_indicator_weighted_partial_coverage():
    out = build_outgoings(_liabilities(), current_cash=7000.0,
                          config=parse_config(None), corp_tax_estimate=2000.0)
    ind = health_indicator(out["categories"], 7000.0)
    # short-term fully covered, only the big long-term loan is short → high score
    assert 88.0 <= ind["score"] <= 94.0
    assert ind["rating"] == "strong"


def test_indicator_low_when_cash_short():
    out = build_outgoings(_liabilities(), current_cash=500.0,
                          config=parse_config(None), corp_tax_estimate=0.0)
    ind = health_indicator(out["categories"], 500.0)
    assert ind["score"] < 40.0


# --- movements --------------------------------------------------------------

def test_movements_month_over_month():
    m = compute_movements(_bs_periods())
    assert [p["balance"] for p in m["points"]] == [7000.0, 10000.0, 5000.0]
    moves = m["movements"]
    assert len(moves) == 2                            # oldest has no prior month
    assert moves[0] == {"period": "30 Jun", "change": -3000.0, "direction": "down"}
    assert moves[1] == {"period": "31 May", "change": 5000.0, "direction": "up"}


def test_movements_empty_balance_sheet():
    assert compute_movements(None) == {"points": [], "movements": []}


# --- orchestrator -----------------------------------------------------------

def test_compute_cash_health_full_payload():
    p = compute_cash_health(_coa(), _tb(), _bs_periods(),
                            corp_tax_estimate=2000.0, config_raw=None)
    assert p["current_cash"] == 7000.0
    assert 88.0 <= p["health_score"] <= 94.0
    assert p["rating"] == "strong"
    assert {c["category"] for c in p["outgoings"]["categories"]} >= {
        SUPPLIERS, VAT, PAYE_NIC, CORPORATION_TAX, LOANS_OTHER}
    assert len(p["recent_movements"]) == 2
    assert len(p["bank_accounts"]) == 2
