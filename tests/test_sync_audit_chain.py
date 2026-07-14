"""A successful Xero sync must chain a fresh audit.

Without this, newly-synced data (a bill just added in Xero) sits in
``xero_document`` unchecked until someone manually re-audits — so the app
shows stale flags. Guarded on status so a failed/ skipped sync doesn't audit.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.modules.integrations.sync import tasks as sync_tasks

_CID = "11111111-1111-1111-1111-111111111111"


def test_sync_chains_audit_on_success():
    with patch.object(
        sync_tasks, "_run_company_sync",
        new=AsyncMock(return_value={"status": "ok", "total_records": 5}),
    ), patch("app.modules.healthcheck.tasks._dispatch_audit_sync") as disp:
        res = sync_tasks.sync_company_task(_CID)
    assert res["status"] == "ok"
    disp.assert_called_once_with(_CID)


def test_sync_skips_audit_on_error():
    with patch.object(
        sync_tasks, "_run_company_sync",
        new=AsyncMock(return_value={"status": "error", "error": "boom"}),
    ), patch("app.modules.healthcheck.tasks._dispatch_audit_sync") as disp:
        res = sync_tasks.sync_company_task(_CID)
    assert res["status"] == "error"
    disp.assert_not_called()


def test_sync_skips_audit_when_not_connected():
    with patch.object(
        sync_tasks, "_run_company_sync",
        new=AsyncMock(return_value={"status": "skipped", "error": "company not connected"}),
    ), patch("app.modules.healthcheck.tasks._dispatch_audit_sync") as disp:
        res = sync_tasks.sync_company_task(_CID)
    assert res["status"] == "skipped"
    disp.assert_not_called()
