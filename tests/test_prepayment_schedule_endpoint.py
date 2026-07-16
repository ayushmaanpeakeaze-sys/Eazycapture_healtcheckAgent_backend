"""GET /prepayment-schedule/ — the working-paper endpoint over synced data."""
from __future__ import annotations

import uuid

import httpx
import pytest

from app.core.db import SyncSessionLocal
from app.main import app
from app.modules.healthcheck.models import Company
from app.modules.integrations.sync.models import XeroDocument


@pytest.fixture
async def async_client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _seed(company_id: uuid.UUID) -> None:
    with SyncSessionLocal() as db:
        db.add(Company(id=company_id, name="Schedule Co", is_active=True))
        db.add(XeroDocument(
            company_id=company_id, entity="account", xero_id="620",
            raw_json={"Code": "620", "Name": "Prepayments", "Type": "CURRENT"},
        ))
        db.add(XeroDocument(
            company_id=company_id, entity="invoice", xero_id="inv-1",
            raw_json={
                "Type": "ACCPAY", "Date": "2026-07-15", "Reference": "INS-1",
                "Contact": {"Name": "Aviva"},
                "LineItems": [{"AccountCode": "620", "LineAmount": 12000,
                               "Description": "Annual insurance"}],
            },
        ))
        db.commit()


def _cleanup(company_id: uuid.UUID) -> None:
    with SyncSessionLocal() as db:
        co = db.get(Company, company_id)
        if co is not None:
            db.delete(co)      # XeroDocument cascades on company delete
            db.commit()


async def test_prepayment_schedule_endpoint(async_client: httpx.AsyncClient):
    cid = uuid.uuid4()
    _seed(cid)
    try:
        resp = await async_client.get(
            f"/api/v1/health/prepayment-schedule/?company_id={cid}&year_end=2027-03-31",
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["columns"] == [
            "Apr-26", "May-26", "Jun-26", "Jul-26", "Aug-26", "Sep-26",
            "Oct-26", "Nov-26", "Dec-26", "Jan-27", "Feb-27", "Mar-27",
        ]
        assert body["item_count"] == 1
        assert body["prepayment_accounts"] == ["620"]
        row = body["rows"][0]
        assert row["supplier"] == "Aviva"
        assert row["monthly"] == "1000.000"          # 12000 / 12
        # No live connection → ledger falls back to the posted amount.
        assert body["validation"]["ledger_source"] == "posted_amounts"
    finally:
        _cleanup(cid)
