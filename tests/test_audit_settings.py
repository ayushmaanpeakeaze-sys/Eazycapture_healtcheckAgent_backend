"""Per-client configurable settings (AuditSettings).

Phase 1 of "build everything from the check docs": every check that used to
read a hardcoded constant now reads it from an ``AuditSettings`` built from the
company's ``audit_config['settings']``. Defaults match the old constants, so
behaviour is unchanged unless a client overrides a value — these tests prove
both halves: defaults are stable, and an override actually moves a result.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.shared.transaction import BatchHealthCheckRequest, BatchTransaction
from app.modules.healthcheck.engine import run_batch_health_check
from app.modules.healthcheck.engine.audit_settings import (
    DEFAULT_SETTINGS,
    AuditSettings,
)
from app.modules.healthcheck.engine.contact_checks import _duplicate_contacts
from app.modules.healthcheck.checks.coding import (
    _find_multi_account_suppliers,
    find_amount_outlier_candidates,
)
from app.modules.healthcheck.checks.duplicates import _find_duplicate_documents
from app.modules.healthcheck.checks.tax import _find_purchase_tax_missing
from app.modules.healthcheck.engine.deterministic import (
    _check_old_unpaid,
)

_TODAY = date.today()


# --------------------------------------------------------------------------
# AuditSettings.from_config — the coercion layer
# --------------------------------------------------------------------------

def test_from_config_none_returns_defaults():
    assert AuditSettings.from_config(None) == DEFAULT_SETTINGS


def test_from_config_ignores_unknown_keys():
    s = AuditSettings.from_config({"not_a_setting": 999, "overdue_days": 30})
    assert s.overdue_days == 30
    assert not hasattr(s, "not_a_setting")


def test_from_config_coerces_decimal_and_int_and_bool():
    s = AuditSettings.from_config({
        "overdue_days": "30",            # str → int
        "outlier_multiple": "2.5",       # str → Decimal
        "supplier_dominance": "0.6",     # str → float
        "ignore_generic_contact": 0,     # falsy → bool False
    })
    assert s.overdue_days == 30 and isinstance(s.overdue_days, int)
    assert s.outlier_multiple == Decimal("2.5")
    assert s.supplier_dominance == 0.6
    assert s.ignore_generic_contact is False


def test_from_config_bad_value_keeps_default():
    s = AuditSettings.from_config({"overdue_days": "not-a-number"})
    assert s.overdue_days == DEFAULT_SETTINGS.overdue_days


# --------------------------------------------------------------------------
# Deterministic checks honour overrides (unit level)
# --------------------------------------------------------------------------

def _bill(tid, days_ago, ref="R1", amount="100.00", cid="C1", vendor="Acme"):
    d = _TODAY - timedelta(days=days_ago)
    return BatchTransaction(
        transaction_id=tid, date=d, description="x", amount=Decimal(amount),
        vendor_name=vendor, contact_id=cid, reference=ref, type="ACCPAY",
        current_account_code="400", tax_code="20I",
    )


def test_duplicate_window_override_widens_detection():
    # Two identical bills 10 days apart, same reference "DUP". "Date within" is a
    # hard filter: the default 0-day window drops them; widening to 14 days brings
    # them inside. Not recurring (10 < 14 lone-pair gap) and same reference (the
    # bill's number) → 1.0 HIGH.
    txns = [_bill("1", 30, ref="DUP"), _bill("2", 20, ref="DUP")]
    assert _find_duplicate_documents(txns) == []                   # default 0 → dropped
    wide = _find_duplicate_documents(
        txns, None, AuditSettings.from_config({"duplicate_days_window": 14}),
    )
    assert wide and wide[0].confidence == 1.0


def test_multi_account_pure_two_distinct():
    coa = {"400": "Rent", "401": "Travel"}
    # Per the spec: a supplier with 2+ DISTINCT accounts is flagged — no 3-txn or
    # dominance minimum. Just two bills on two accounts is enough.
    txns = [_bill("1", 5),
            _bill("2", 6, ref="R2").model_copy(update={"current_account_code": "401"})]
    hits = _find_multi_account_suppliers(txns, coa)
    assert len(hits) == 1 and hits[0].current_code in {"400", "401"}
    assert hits[0].accounts_used == ["400", "401"]
    # A supplier that only ever uses ONE account → never flagged.
    same = [_bill("1", 5), _bill("2", 6), _bill("3", 7)]
    assert _find_multi_account_suppliers(same, coa) == []


def test_outlier_multiple_override():
    # Vendor's typical amount ~100; one bill at 300 (3x). Default multiple 4x
    # → not an outlier; lowering to 2x → flagged.
    txns = [_bill("1", 5, amount="100"), _bill("2", 6, amount="100"),
            _bill("3", 7, amount="100"), _bill("4", 8, amount="300")]
    assert find_amount_outlier_candidates(txns) == []             # default 4x
    loud = find_amount_outlier_candidates(
        txns, None, AuditSettings.from_config({"outlier_multiple": "2.0"}),
    )
    assert any(c["tx"].transaction_id == "4" for c in loud)


# --------------------------------------------------------------------------
# Contact checks honour overrides
# --------------------------------------------------------------------------

def _contact(cid, name, email=None, tax=None):
    return {
        "ContactID": cid, "Name": name, "EmailAddress": email,
        "TaxNumber": tax, "IsSupplier": True, "IsCustomer": False,
    }


def test_shared_email_alone_does_not_match_different_names():
    # Matching is NAME-only: two differently-named contacts that
    # happen to share an email are NOT a duplicate — email is enrichment, not a
    # match signal. ('Alpha Roofing' vs 'Beta Plumbing' → ~0% name similarity.)
    contacts = [
        _contact("C1", "Alpha Roofing", email="bob@shared.co.uk"),
        _contact("C2", "Beta Plumbing", email="bob@shared.co.uk"),
    ]
    assert _duplicate_contacts(contacts) == []


def test_similar_names_flagged_regardless_of_email():
    # 'Acme Ltd' / 'Acme Limited' → identical after suffix-strip → flagged by the
    # NAME. The shared email is just shown as enrichment.
    contacts = [
        _contact("C1", "Acme Ltd", email="bob@acme.com"),
        _contact("C2", "Acme Limited", email="bob@acme.com"),
    ]
    hits = _duplicate_contacts(contacts)
    assert any(f["issue_type"] == "duplicate_contact" for f in hits)
    assert hits[0]["name_similarity"] == 1.0


# --------------------------------------------------------------------------
# Polish: tax-missing ignore-lists + multi-account whitelist
# --------------------------------------------------------------------------

def _vat_bill(tid, code, cid="C1", vendor="Acme", acct="400"):
    return BatchTransaction(
        transaction_id=tid, date=_TODAY, description="x", amount=Decimal("100"),
        vendor_name=vendor, contact_id=cid, type="ACCPAY",
        current_account_code=acct, tax_code=code,
    )


# 400 = expense (in scope for purchase tax-missing); 200 = sales income.
_TAX_COA = {"400": "Office Expenses", "200": "Sales"}
_TAX_COA_TYPES = {"400": "EXPENSE", "200": "REVENUE"}


def test_purchase_tax_missing_flags_then_ignore_account_suppresses():
    # Bill line on an EXPENSE account coded No VAT → missing input tax.
    txns = [_vat_bill("1", "NONE")]
    assert _find_purchase_tax_missing(txns, _TAX_COA, _TAX_COA_TYPES) != []
    # A bill that DOES charge VAT (20I) → fine.
    assert _find_purchase_tax_missing([_vat_bill("2", "20I")], _TAX_COA, _TAX_COA_TYPES) == []
    # Ignoring the account suppresses it.
    quiet = _find_purchase_tax_missing(
        txns, _TAX_COA, _TAX_COA_TYPES,
        AuditSettings.from_config({"tax_missing_ignore_accounts": ["400"]}),
    )
    assert quiet == []


def test_purchase_tax_missing_ignore_contact_suppresses():
    quiet = _find_purchase_tax_missing(
        [_vat_bill("1", "NONE")], _TAX_COA, _TAX_COA_TYPES,
        AuditSettings.from_config({"tax_missing_ignore_contacts": ["C1"]}),
    )
    assert quiet == []


def test_multi_account_whitelist_suppresses():
    coa = {"400": "Rent", "401": "Travel"}
    txns = [_vat_bill("1", "20I"), _vat_bill("2", "20I"), _vat_bill("3", "20I"),
            _vat_bill("4", "20I", acct="401")]
    assert _find_multi_account_suppliers(txns, coa) != []
    quiet = _find_multi_account_suppliers(
        txns, coa, None,
        AuditSettings.from_config({"multi_account_whitelist_contacts": ["C1"]}),
    )
    assert quiet == []


async def test_org_vat_gate_skips_tax_missing_end_to_end():
    from app.shared.transaction import BatchContext, ChartOfAccount

    txns = [_vat_bill("1", "NONE")]   # expense account 400, No VAT
    coa = [ChartOfAccount(code="400", name="Office Expenses", type="EXPENSE")]

    async def _run(vat_registered):
        ctx = BatchContext(org_is_vat_registered=vat_registered, chart_of_accounts=coa)
        req = BatchHealthCheckRequest(
            transactions=txns, context=ctx,
            disabled_rules=["wrong_category", "capital_item_review",
                            "low_cost_fixed_asset", "anomaly", "amount_outlier"],
        )
        res = await run_batch_health_check(req)
        return [f for f in res.flagged if f.issue_type == "purchase_tax_missing"]

    assert await _run(None)        # unknown → runs
    assert await _run(True)        # VAT-registered → runs
    assert not await _run(False)   # non-VAT → skipped


# --------------------------------------------------------------------------
# End-to-end: request.settings flows through the orchestrator
# --------------------------------------------------------------------------

async def test_overdue_days_override_end_to_end():
    """The per-client knob reaches the check via BatchHealthCheckRequest.

    Default basis is now due_date with a 1-day grace → a bill 20 days past due
    flags by default; raising the grace past 20 suppresses it.
    """
    tx = BatchTransaction(
        transaction_id="t1", date=_TODAY - timedelta(days=25),
        description="Office rent", amount=Decimal("1000.00"),
        vendor_name="Acme Ltd", tax_code="20I", current_account_code="400",
        invoice_number="INV-1", due_date=_TODAY - timedelta(days=20),
        status="AUTHORISED", amount_due=Decimal("1000.00"), type="ACCPAY",
    )

    def _overdue(settings):
        req = BatchHealthCheckRequest(transactions=[tx], settings=settings)
        return req

    # Default (1-day grace) → 20 days overdue → flagged.
    default = await run_batch_health_check(_overdue(None))
    assert [f for f in default.flagged if f.issue_type == "old_unpaid_bill"]

    # Grace raised to 30 days → 20 days overdue is within grace → not flagged.
    override = await run_batch_health_check(_overdue({"overdue_days": 30}))
    assert not [f for f in override.flagged if f.issue_type == "old_unpaid_bill"]


# --------------------------------------------------------------------------
# settings_schema — per-check field metadata for the config screen
# --------------------------------------------------------------------------

def test_settings_schema_keys_are_real_fields_no_dupes():
    """Every key in the schema is a real AuditSettings field and appears once —
    nothing stale, nothing duplicated. (Scoped to Duplicate Invoices for now, so
    it's a subset, not the full field set.)"""
    import dataclasses

    from app.modules.healthcheck.engine.audit_settings import settings_schema

    schema_keys = [f["key"] for g in settings_schema() for f in g["fields"]]
    assert len(schema_keys) == len(set(schema_keys)), "a field is mapped twice"
    field_names = {f.name for f in dataclasses.fields(AuditSettings)}
    assert set(schema_keys) <= field_names, "schema references an unknown field"


def test_settings_schema_exposes_duplicate_invoice_tunables():
    """The Duplicate Invoices check exposes its toggles + the Confidence bar
    (in render order). The bar defaults to a high value so only precise
    duplicates show; lowering it reviews weaker matches."""
    from app.modules.healthcheck.engine.audit_settings import settings_schema

    dup = next(e for e in settings_schema() if e["check"] == "duplicate_invoice")
    assert dup["group"] == "Duplicates"
    assert [f["key"] for f in dup["fields"]] == [
        "duplicate_days_window",
        "duplicate_require_exact_reference",
        "duplicate_require_same_amount",
        "duplicate_also_check_paid",
        "duplicate_min_confidence",
    ]
    conf = next(f for f in dup["fields"] if f["key"] == "duplicate_min_confidence")
    assert conf["type"] == "percent"


def test_settings_schema_exposes_duplicate_contact_similarity():
    """The Duplicate Contacts check exposes a single name-similarity threshold
    (the 'minimum similarity %'), rendered as a percent slider."""
    from app.modules.healthcheck.engine.audit_settings import settings_schema

    dc = next(e for e in settings_schema() if e["check"] == "duplicate_contact")
    assert dc["group"] == "Duplicates"
    f = next(f for f in dc["fields"] if f["key"] == "dup_contact_name_sim")
    assert f["type"] == "percent"
    assert f["default"] == 0.70
    assert f["min"] == 0 and f["max"] == 1


# --------------------------------------------------------------------------
# Old unpaid invoices/bills — configurable age basis + separate thresholds
# --------------------------------------------------------------------------

def _sales_invoice(days_old, due_days_ago, amount="1000.00"):
    """An open, unpaid ACCREC invoice raised `days_old` ago, due `due_days_ago`."""
    return BatchTransaction(
        transaction_id="t1", date=_TODAY - timedelta(days=days_old),
        description="Consulting", amount=Decimal(amount), vendor_name="Acme Ltd",
        tax_code="20I", current_account_code="200", invoice_number="INV-1",
        due_date=_TODAY - timedelta(days=due_days_ago), status="AUTHORISED",
        amount_due=Decimal(amount), type="ACCREC",
    )


def test_old_unpaid_age_basis_due_date_is_default():
    # Raised 90 days ago, only just became due (5 days ago). Default basis is now
    # due_date → 5 days overdue → flagged at the 1-day grace. The age column
    # reflects days OVERDUE, not days since raised.
    inv = _sales_invoice(days_old=90, due_days_ago=5)
    hit = _check_old_unpaid(inv, _TODAY)
    assert hit is not None and hit.issue_type == "old_unpaid_invoice"
    assert hit.match_reasons["age_days"] == 5
    assert hit.match_reasons["age_basis"] == "due_date"
    assert hit.match_reasons["outstanding"]  # outstanding amount string


def test_old_unpaid_not_yet_due_is_not_flagged():
    # Raised 90 days ago but not due until 10 days from now → not overdue → no flag
    # under the default due_date basis.
    inv = _sales_invoice(days_old=90, due_days_ago=-10)
    assert _check_old_unpaid(inv, _TODAY) is None


def test_old_unpaid_age_basis_invoice_date_override():
    # Same invoice, counting from the INVOICE date → 90 days old → flagged, and
    # the age column reflects invoice_date.
    inv = _sales_invoice(days_old=90, due_days_ago=5)
    s = AuditSettings.from_config({"old_unpaid_age_basis": "invoice_date"})
    hit = _check_old_unpaid(inv, _TODAY, s)
    assert hit is not None and hit.issue_type == "old_unpaid_invoice"
    assert hit.match_reasons["age_days"] == 90
    assert hit.match_reasons["age_basis"] == "invoice_date"


def test_old_unpaid_separate_invoice_and_bill_thresholds():
    # A 45-day-old customer invoice + a 45-day-old supplier bill.
    inv = _sales_invoice(days_old=45, due_days_ago=45)
    bill = BatchTransaction(
        transaction_id="b1", date=_TODAY - timedelta(days=45), description="x",
        amount=Decimal("500"), vendor_name="Supplier Co", tax_code="20I",
        current_account_code="400", invoice_number="BILL-1",
        due_date=_TODAY - timedelta(days=45), status="AUTHORISED",
        amount_due=Decimal("500"), type="ACCPAY",
    )
    # Invoices flag at 30 days; bills only at 90 → invoice flags, bill doesn't.
    s = AuditSettings.from_config(
        {"old_unpaid_invoice_days": 30, "old_unpaid_bill_days": 90})
    inv_hit = _check_old_unpaid(inv, _TODAY, s)
    bill_hit = _check_old_unpaid(bill, _TODAY, s)
    assert inv_hit is not None and inv_hit.issue_type == "old_unpaid_invoice"
    assert bill_hit is None


def test_legacy_overdue_days_still_seeds_both_thresholds():
    # Back-compat: the old shared `overdue_days` knob must still move both checks.
    s = AuditSettings.from_config({"overdue_days": 30})
    assert s.old_unpaid_invoice_days == 30
    assert s.old_unpaid_bill_days == 30


def test_old_unpaid_age_basis_bad_value_keeps_default():
    s = AuditSettings.from_config({"old_unpaid_age_basis": "nonsense"})
    assert s.old_unpaid_age_basis == DEFAULT_SETTINGS.old_unpaid_age_basis  # "invoice_date"


def test_settings_schema_exposes_old_unpaid_invoice_section():
    from app.modules.healthcheck.engine.audit_settings import settings_schema

    inv = next(e for e in settings_schema() if e["check"] == "old_unpaid_invoice")
    assert inv["group"] == "Date & Ageing"
    by_key = {f["key"]: f for f in inv["fields"]}
    assert by_key["old_unpaid_invoice_days"]["type"] == "int"
    basis = by_key["old_unpaid_age_basis"]
    assert basis["type"] == "select"
    assert basis["options"] == ["due_date", "invoice_date"]
    assert basis["default"] == "due_date"


def test_settings_schema_groups_and_checks_are_valid():
    """Each entry pairs with a real rules_registry group + rule key so the UI
    can attach the field block to that check's on/off toggle. Field types and
    defaults are well-formed."""
    from app.modules.healthcheck.rules_registry import ALL_RULE_KEYS, _GROUPS
    from app.modules.healthcheck.engine.audit_settings import settings_schema

    defaults = AuditSettings().as_json_dict()
    valid_types = {"bool", "int", "amount", "multiple", "percent", "list", "select"}
    for entry in settings_schema():
        assert entry["group"] in _GROUPS, entry["group"]
        assert entry["check"] in ALL_RULE_KEYS, entry["check"]
        for fld in entry["fields"]:
            assert fld["type"] in valid_types, fld
            assert fld["default"] == defaults[fld["key"]]
            # "select" fields must carry their allowed options, and the default
            # must be one of them.
            if fld["type"] == "select":
                assert fld["options"], fld
                assert fld["default"] in fld["options"], fld
