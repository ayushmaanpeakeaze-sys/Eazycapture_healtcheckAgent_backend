"""SOP Phase 2b — manual-journal source (Step 1) + fixed-asset dedup (Step 7).

* ``_reshape_journal_to_batch`` keeps ONLY manual journals (MANJOURNAL) so we
  don't double-count invoices/bills/bank txns we already audit.
* A MANJOURNAL journal reaches ONLY the capital universe — the content check
  flags a capital keyword in it, and no other check ever sees a journal.
* ``_drop_already_capitalised`` removes a capital flag when the item is already
  on the fixed-asset register (strong value + date match only).
"""
import asyncio
from datetime import date
from decimal import Decimal

from app.shared.transaction import (
    BatchContext,
    BatchHealthCheckRequest,
    BatchLineItem,
    BatchTransaction,
    ChartOfAccount,
)
from app.modules.healthcheck.engine import run_batch_health_check
from app.modules.healthcheck.tasks import (
    _drop_already_capitalised,
    _reshape_journal_to_batch,
)


# --- reshape: manual journals only -------------------------------------------

def test_reshape_manual_journal():
    raw = {
        "JournalID": "J1", "JournalDate": "/Date(1767225600000+0000)/",
        "SourceType": "MANJOURNAL",
        "JournalLines": [
            {"AccountCode": "400", "AccountType": "EXPENSE", "Description": "Dell laptop", "NetAmount": 1200.0},
            {"AccountCode": "800", "AccountType": "CURRLIAB", "Description": "", "NetAmount": -1200.0},
        ],
    }
    out = _reshape_journal_to_batch(raw)
    assert out is not None
    assert out["type"] == "MANJOURNAL"
    assert out["transaction_id"] == "J1"
    assert out["description"] == "Dell laptop"        # first non-empty narration
    assert out["amount"] == "1200.00"                 # biggest line
    assert [li["account_code"] for li in out["line_items"]] == ["400", "800"]


def test_reshape_skips_non_manual_journal():
    # ACCPAY / CASHREC etc. mirror docs we already audit → skip (no double-count).
    raw = {"JournalID": "J2", "SourceType": "ACCPAY",
           "JournalLines": [{"AccountCode": "400", "NetAmount": 100}]}
    assert _reshape_journal_to_batch(raw) is None


def test_reshape_skips_empty_journal():
    assert _reshape_journal_to_batch(
        {"JournalID": "J3", "SourceType": "MANJOURNAL", "JournalLines": []}) is None


# --- orchestrator: a manual journal reaches ONLY the capital checks -----------

def test_manual_journal_flags_capital_item():
    tx = BatchTransaction(
        transaction_id="J1", date=date(2026, 1, 1), description="Dell laptop purchase",
        amount=Decimal("1200"), vendor_name="Manual journal", type="MANJOURNAL",
        current_account_code="400",
        line_items=[BatchLineItem(account_code="400", amount=Decimal("1200"),
                                  description="Dell laptop purchase")],
    )
    coa = [ChartOfAccount(code="400", name="Office Expenses", type="EXPENSE")]
    ctx = BatchContext(org_is_vat_registered=True, chart_of_accounts=coa)
    res = asyncio.run(run_batch_health_check(
        BatchHealthCheckRequest(transactions=[tx], context=ctx)))
    types = {f.issue_type for f in res.flagged if f.transaction_id == "J1"}
    assert "capital_item_review" in types
    # No leak: a manual journal must reach ONLY the capital check.
    assert types <= {"capital_item_review"}


# --- asset dedup (Step 7) ----------------------------------------------------

def _flag(tid, itype, amount):
    return {"transaction_id": tid, "issue_type": itype,
            "match_reasons": {"line_amount": amount}}


def _tx(tid, d):
    return {"transaction_id": tid, "date": d}


def _asset(price, d):
    return {"purchasePrice": price, "purchaseDate": d}


def test_dedup_drops_already_capitalised():
    flags = [_flag("T1", "capital_item_review", "1200.00")]
    txns = [_tx("T1", "2026-01-10")]
    assets = [_asset(1200.0, "2026-01-05")]     # same value, 5 days apart → strong
    assert _drop_already_capitalised(flags, txns, assets) == []


def test_dedup_keeps_when_amount_differs():
    flags = [_flag("T1", "capital_item_review", "1200.00")]
    txns = [_tx("T1", "2026-01-10")]
    assert len(_drop_already_capitalised(flags, txns, [_asset(999.0, "2026-01-05")])) == 1


def test_dedup_keeps_when_date_far():
    flags = [_flag("T1", "capital_item_review", "1200.00")]
    txns = [_tx("T1", "2026-01-10")]
    # same value but purchase a year earlier → not the same item → keep.
    assert len(_drop_already_capitalised(flags, txns, [_asset(1200.0, "2025-01-05")])) == 1


def test_dedup_noop_without_assets():
    flags = [_flag("T1", "capital_item_review", "1200.00")]
    txns = [_tx("T1", "2026-01-10")]
    assert len(_drop_already_capitalised(flags, txns, [])) == 1


def test_dedup_never_touches_non_capital_flags():
    # A duplicate/tax flag is never dropped even if a same-value asset exists.
    flags = [_flag("T1", "duplicate_invoice", "1200.00")]
    txns = [_tx("T1", "2026-01-10")]
    assert len(_drop_already_capitalised(flags, txns, [_asset(1200.0, "2026-01-05")])) == 1
