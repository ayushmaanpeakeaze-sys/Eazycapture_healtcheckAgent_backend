"""needs_reconnect flag — set on a dead Xero grant, cleared on a successful
fetch, and surfaced in the panorama so a broken connection can't look connected.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.db import AsyncSessionLocal, SyncSessionLocal
from app.modules.healthcheck.models import Company
from app.modules.integrations.nango.client import NangoAuthError
from app.modules.integrations.nango.service import NangoService
from app.modules.integrations.service import IntegrationService
from app.modules.integrations.sync.engine import SyncEngine, SyncResult


def _make_company(needs_reconnect: bool = False) -> uuid.UUID:
    cid = uuid.uuid4()
    with SyncSessionLocal() as db:
        db.add(Company(
            id=cid, name="Reconnect test", is_active=True,
            nango_connection_id="conn-x", xero_tenant_id="tenant-x",
            needs_reconnect=needs_reconnect,
        ))
        db.commit()
    return cid


def _drop_company(cid: uuid.UUID) -> None:
    with SyncSessionLocal() as db:
        c = db.get(Company, cid)
        if c is not None:
            db.delete(c)
            db.commit()


def _flag(cid: uuid.UUID) -> bool:
    with SyncSessionLocal() as db:
        return db.get(Company, cid).needs_reconnect


def test_audit_sets_needs_reconnect_on_dead_grant(monkeypatch):
    from app.modules.healthcheck import tasks

    monkeypatch.setattr(NangoService, "is_available", lambda self: True)
    monkeypatch.setattr(tasks.db_read, "has_synced_documents", lambda *a, **k: False)

    async def _raise(*a, **k):
        raise NangoAuthError("Xero rejected the request (HTTP 403).")
    monkeypatch.setattr(IntegrationService, "fetch_all_invoices", _raise)

    async def _empty(*a, **k):
        return []
    for name in ("fetch_all_credit_notes", "fetch_all_payments",
                 "fetch_all_bank_transactions"):
        monkeypatch.setattr(IntegrationService, name, _empty)

    async def _no_live(*a, **k):
        return None
    monkeypatch.setattr(IntegrationService, "find_live_xero_connection", _no_live)

    cid = _make_company(needs_reconnect=False)
    try:
        with SyncSessionLocal() as db:
            company = db.get(Company, cid)
            with pytest.raises(RuntimeError, match="(?i)reconnect xero"):
                tasks._fetch_audit_transactions(db, company)
        assert _flag(cid) is True
    finally:
        _drop_company(cid)


def test_audit_clears_needs_reconnect_on_success(monkeypatch):
    from app.modules.healthcheck import tasks

    monkeypatch.setattr(NangoService, "is_available", lambda self: True)
    monkeypatch.setattr(tasks.db_read, "has_synced_documents", lambda *a, **k: False)

    async def _empty(*a, **k):
        return []
    for name in ("fetch_all_invoices", "fetch_all_credit_notes",
                 "fetch_all_payments", "fetch_all_bank_transactions"):
        monkeypatch.setattr(IntegrationService, name, _empty)

    cid = _make_company(needs_reconnect=True)
    try:
        with SyncSessionLocal() as db:
            company = db.get(Company, cid)
            _txns, source = tasks._fetch_audit_transactions(db, company)
        assert source == "nango"
        assert _flag(cid) is False
    finally:
        _drop_company(cid)


async def test_panorama_surfaces_needs_reconnect():
    from app.modules.healthcheck.services.panorama_service import (
        CompaniesPanoramaService,
    )

    cid = _make_company(needs_reconnect=True)
    try:
        async with AsyncSessionLocal() as db:
            resp = await CompaniesPanoramaService(db).get_panorama(
                days=30, allowed_company_ids=[cid],
            )
        row = next((r for r in resp.results if r.company_id == cid), None)
        assert row is not None
        assert row.needs_reconnect is True
    finally:
        _drop_company(cid)


async def test_sync_health_sets_on_auth_error_clears_on_ok():
    engine = SyncEngine()
    cid = _make_company(needs_reconnect=False)
    try:
        await engine._update_connection_health(cid, {
            "invoices": SyncResult("invoices", status="error",
                                   error="403", auth_error=True),
        })
        assert _flag(cid) is True

        await engine._update_connection_health(cid, {
            "invoices": SyncResult("invoices", status="ok", records=3),
        })
        assert _flag(cid) is False

        await engine._update_connection_health(cid, {
            "invoices": SyncResult("invoices", status="error",
                                   error="timeout", auth_error=False),
        })
        assert _flag(cid) is False

        await engine._update_connection_health(cid, {
            "invoices": SyncResult("invoices", status="error",
                                   error="403", auth_error=True),
            "payments": SyncResult("payments", status="ok", records=0),
        })
        assert _flag(cid) is True
    finally:
        _drop_company(cid)
