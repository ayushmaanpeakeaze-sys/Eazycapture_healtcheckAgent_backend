"""/stats issue-type + severity counts are DOCUMENT-level.

A per-check badge must match that check's document list: a bill with two
capital lines is ONE "Capital item review" document, not two.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from app.core.db import SyncSessionLocal
from app.main import app
from app.modules.healthcheck.models import Company, HealthCheckResult


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
        co = db.get(Company, cid)
        if co is not None:
            db.delete(co)
            db.commit()


def _insert_doc(company_id: uuid.UUID, flagged: list[dict]) -> None:
    with SyncSessionLocal() as db:
        db.add(HealthCheckResult(
            id=uuid.uuid4(), company_id=company_id, document_id=uuid.uuid4(),
            document_type="ACCPAY", kind="post_ledger", status="blocked",
            error_msgs="x", result={"flagged": flagged},
        ))
        db.commit()


async def test_stats_counts_are_document_level(async_client: httpx.AsyncClient):
    co = _insert_company("Stats doc-level")
    try:
        # ONE bill with TWO capital line-flags + one duplicate flag.
        _insert_doc(co, [
            {"issue_type": "capital_item_review", "severity": "medium", "message": "l1"},
            {"issue_type": "capital_item_review", "severity": "medium", "message": "l2"},
            {"issue_type": "duplicate_bill", "severity": "high", "message": "d1"},
        ])
        resp = await async_client.get(f"/api/v1/health/stats/?company_id={co}")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        by_type = {t["issue_type"]: t["count"] for t in body["by_issue_type"]}
        # 1 document, not 2 line-flags:
        assert by_type["capital_item_review"] == 1
        assert by_type["duplicate_bill"] == 1

        by_sev = {s["severity"]: s["count"] for s in body["by_severity"]}
        # the doc has both a medium and a high issue → one each (not two mediums).
        assert by_sev["medium"] == 1
        assert by_sev["high"] == 1
    finally:
        _delete_company(co)
