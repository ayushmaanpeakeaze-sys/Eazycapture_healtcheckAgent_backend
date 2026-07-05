"""Contact Defaults (check #23).

Detection now covers all four Xero contact defaults (sales/purchase × account/tax),
emits current values + which are missing, and the Confirm write-back maps our
keys to Xero field names. Live list/confirm fail open when not connected.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from app.core.db import SyncSessionLocal
from app.main import app
from app.modules.healthcheck.models import Company
from app.modules.healthcheck.services.contact_defaults_service import (
    to_xero_default_fields,
)
from app.modules.healthcheck.engine.contact_checks import (
    _contact_defaults,
    extract_contact_defaults,
    missing_contact_defaults,
)


# ---------------------------------------------------------------- pure detection

def _c(**kw):
    base = {"ContactID": "C1", "Name": "Acme", "IsCustomer": False, "IsSupplier": False}
    base.update(kw)
    return base


def test_extract_all_four_defaults():
    c = _c(SalesDefaultAccountCode="200", AccountsReceivableTaxType="OUTPUT2",
           PurchasesDefaultAccountCode="400", AccountsPayableTaxType="INPUT2")
    assert extract_contact_defaults(c) == {
        "sales_account": "200", "sales_tax": "OUTPUT2",
        "purchases_account": "400", "purchases_tax": "INPUT2",
    }


def test_customer_missing_sales_tax():
    c = _c(IsCustomer=True, SalesDefaultAccountCode="200")  # has account, no tax
    assert missing_contact_defaults(c) == ["sales_tax"]


def test_supplier_missing_both_purchase_defaults():
    c = _c(IsSupplier=True)
    assert missing_contact_defaults(c) == ["purchases_account", "purchases_tax"]


def test_both_roles_all_set_not_missing():
    c = _c(IsCustomer=True, IsSupplier=True,
           SalesDefaultAccountCode="200", AccountsReceivableTaxType="OUTPUT2",
           PurchasesDefaultAccountCode="400", AccountsPayableTaxType="INPUT2")
    assert missing_contact_defaults(c) == []


def test_contact_defaults_flag_shape():
    contacts = [_c(IsCustomer=True, Name="Beta Ltd", ContactID="C9")]  # missing both sales
    flags = _contact_defaults(contacts)
    assert len(flags) == 1
    f = flags[0]
    assert f["issue_type"] == "contact_defaults"
    assert set(f["missing_defaults"]) == {"sales_account", "sales_tax"}
    assert f["current_defaults"]["sales_account"] == ""
    assert f["is_customer"] is True and f["is_supplier"] is False


def test_neither_role_and_archived_skipped():
    contacts = [
        _c(ContactID="N1", Name="No role"),                       # neither role
        _c(ContactID="A1", Name="Archived", IsCustomer=True, IsArchived=True),
    ]
    assert _contact_defaults(contacts) == []


# ----------------------------------------------------------- write-back mapping

def test_to_xero_fields_maps_and_drops_blanks():
    out = to_xero_default_fields({
        "sales_account": "200", "sales_tax": "", "purchases_account": None,
        "purchases_tax": "INPUT2",
    })
    assert out == {"SalesDefaultAccountCode": "200", "AccountsPayableTaxType": "INPUT2"}


def test_to_xero_fields_empty_when_nothing_valid():
    assert to_xero_default_fields({"sales_account": "", "sales_tax": None}) == {}


# ----------------------------------------------------- endpoints (not-connected)

@pytest.fixture
async def async_client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _insert_company(name: str) -> uuid.UUID:
    cid = uuid.uuid4()
    with SyncSessionLocal() as db:
        db.add(Company(id=cid, name=name, is_active=True))
        db.commit()
    return cid


def _delete_company(cid: uuid.UUID) -> None:
    with SyncSessionLocal() as db:
        c = db.get(Company, cid)
        if c is not None:
            db.delete(c)
            db.commit()


async def test_list_returns_not_connected_without_nango(async_client):
    co = _insert_company("CD list test")
    try:
        resp = await async_client.get(f"/api/v1/health/contact-defaults/?company_id={co}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["connected"] is False
        assert body["contacts"] == [] and body["total"] == 0
    finally:
        _delete_company(co)


async def test_confirm_not_connected(async_client):
    co = _insert_company("CD confirm test")
    try:
        resp = await async_client.post(
            f"/api/v1/health/contact-defaults/{uuid.uuid4()}/confirm/?company_id={co}",
            json={"sales_account": "200"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "not connected"
    finally:
        _delete_company(co)


def _read_contact_row(company_id, contact_id):
    from app.modules.healthcheck.models import HealthCheckResult
    from sqlalchemy import select as _select
    with SyncSessionLocal() as db:
        return db.execute(
            _select(HealthCheckResult).where(
                HealthCheckResult.company_id == company_id,
                HealthCheckResult.document_id == contact_id,
                HealthCheckResult.document_type == "CONTACT",
            )
        ).scalars().first()


async def test_dismiss_then_reinstate_persists(async_client):
    co = _insert_company("CD dismiss test")
    contact_id = uuid.uuid4()
    try:
        # dismiss → creates a dismissed contact_defaults trapped row
        resp = await async_client.post(
            f"/api/v1/health/contact-defaults/{contact_id}/dismiss/?company_id={co}",
            json={"dismissal_reason": "no defaults needed"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dismissed"] is True
        assert body["trapped_row_id"]                  # row id returned
        row = _read_contact_row(co, contact_id)
        assert row is not None and row.result["dismissed"] is True
        assert row.result["dismissal_reason"] == "no defaults needed"
        assert row.result["rule_ids"] == ["contact_defaults"]

        # reinstate → flips dismissed back off
        resp = await async_client.post(
            f"/api/v1/health/contact-defaults/{contact_id}/reinstate/?company_id={co}",
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["dismissed"] is False
        assert _read_contact_row(co, contact_id).result["dismissed"] is False
    finally:
        # clean child rows then the company
        with SyncSessionLocal() as db:
            from app.modules.healthcheck.models import HealthCheckResult
            from sqlalchemy import delete as _delete
            db.execute(_delete(HealthCheckResult).where(HealthCheckResult.company_id == co))
            db.commit()
        _delete_company(co)
