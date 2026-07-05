"""Unreconciled Bank Items — pure count logic + service orchestration."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.modules.healthcheck.services.unreconciled_bank_service import UnreconciledBankService
from app.modules.healthcheck.engine.unreconciled_bank import compute_unreconciled_accounts


def _txn(acc_id, code, name, kind, reconciled):
    return {
        "BankAccount": {"AccountID": acc_id, "Code": code, "Name": name},
        "Type": kind, "IsReconciled": reconciled,
    }


def test_counts_split_by_type_and_skip_reconciled():
    txns = [
        _txn("a", "090", "Business", "SPEND", False),
        _txn("a", "090", "Business", "SPEND", False),
        _txn("a", "090", "Business", "RECEIVE", False),
        _txn("a", "090", "Business", "SPEND", True),    # reconciled → ignored
    ]
    rows = compute_unreconciled_accounts(txns)
    assert len(rows) == 1
    r = rows[0]
    assert r["unreconciled_spent"] == 2
    assert r["unreconciled_received"] == 1
    assert r["total_to_reconcile"] == 3
    assert r["unexplained"] is None       # feed-side: Finance API only


def test_zero_unreconciled_account_hidden():
    txns = [_txn("a", "090", "Business", "SPEND", True)]   # all reconciled
    assert compute_unreconciled_accounts(txns) == []


def test_multiple_accounts_sorted_by_total():
    txns = [
        _txn("a", "090", "Business", "SPEND", False),
        _txn("b", "091", "Savings", "SPEND", False),
        _txn("b", "091", "Savings", "RECEIVE", False),
    ]
    rows = compute_unreconciled_accounts(txns)
    assert [r["account_code"] for r in rows] == ["091", "090"]  # 2 before 1


def test_exclude_codes_drops_account():
    txns = [_txn("a", "090", "Business", "SPEND", False)]
    assert compute_unreconciled_accounts(txns, exclude_codes={"090"}) == []


# ---------------- service ----------------

def _company():
    return SimpleNamespace(
        audit_config={}, nango_connection_id="conn",
        xero_tenant_id="tenant", xero_shortcode="SHORT")


def test_service_attaches_process_url_and_total():
    company = _company()
    db = MagicMock(); db.get = AsyncMock(return_value=company); db.commit = AsyncMock()
    # Under AUDIT_SOURCE=db the service first reads synced bank txns; make that
    # read return nothing so it falls through to the (mocked) live fetch this
    # test exercises.
    _empty = MagicMock(); _empty.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=_empty)
    integ = MagicMock()
    integ.is_connected = MagicMock(return_value=True)
    integ.fetch_all_bank_transactions = AsyncMock(return_value=[
        _txn("acc090", "090", "Business", "SPEND", False),
        _txn("acc090", "090", "Business", "RECEIVE", False),
    ])
    svc = UnreconciledBankService(db, integration=integ)
    out = asyncio.run(svc.list_accounts(uuid4()))
    assert out["total_to_reconcile"] == 2
    assert out["unexplained_available"] is False
    assert out["items"][0]["process_url"]


def test_service_exclude_persists():
    company = _company()
    db = MagicMock(); db.get = AsyncMock(return_value=company); db.commit = AsyncMock()
    svc = UnreconciledBankService(db, integration=MagicMock())
    asyncio.run(svc.exclude_account(uuid4(), "091", excluded=True))
    assert "091" in company.audit_config["unreconciled"]["excluded"]
    db.commit.assert_awaited()
