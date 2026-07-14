"""_build_xero_body line-targeting — the read-modify-write that a "Save
changes" recode relies on.

A multi-line bill must only recode the flagged line(s) (those on a
``current_code`` account), preserving every other line — a Xero invoice POST
REPLACES all lines, so dropping/altering the untouched lines corrupts the bill.
Pure unit tests: fetch_invoice is mocked, no DB / Xero needed.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.modules.healthcheck.services.resolve_service import ResolveService
from app.modules.integrations.service import IntegrationService


def _svc() -> ResolveService:
    return ResolveService(db=MagicMock())


def _integration(lines: list[dict]) -> MagicMock:
    integration = MagicMock()
    integration.fetch_invoice = AsyncMock(
        return_value={"Invoices": [{"InvoiceID": "inv-1", "LineItems": lines}]},
    )
    return integration


async def test_recodes_only_the_flagged_line():
    """Two-line bill: line on flagged capital account 429 + a legit expense 200.
    Recode to fixed-assets 710 must touch ONLY the 429 line."""
    svc = _svc()
    doc_id = uuid.uuid4()
    integration = _integration([
        {"LineItemID": "L1", "AccountCode": "429", "Description": "Laptop",
         "Quantity": 1, "UnitAmount": 1200},
        {"LineItemID": "L2", "AccountCode": "200", "Description": "Sales support",
         "Quantity": 1, "UnitAmount": 300},
    ])

    body = await svc._build_xero_body(
        integration=integration,
        connection_id="conn-1",
        tenant_id="tenant-1",
        document_id=doc_id,
        header_updates={},
        line_item_updates={"AccountCode": "710"},
        target_codes=frozenset({"429"}),
    )

    lines = {l["LineItemID"]: l for l in body["Invoices"][0]["LineItems"]}
    assert lines["L1"]["AccountCode"] == "710"   # flagged line recoded
    assert lines["L2"]["AccountCode"] == "200"   # other line untouched
    # unchanged fields survive on both (Xero POST replaces the whole line set)
    assert lines["L1"]["Description"] == "Laptop"
    assert lines["L2"]["Description"] == "Sales support"
    assert body["Invoices"][0]["InvoiceID"] == str(doc_id)


async def test_empty_target_codes_recodes_every_line():
    """No target_codes (single-line docs / legacy) → every line recoded."""
    svc = _svc()
    integration = _integration([
        {"LineItemID": "L1", "AccountCode": "429", "Quantity": 1, "UnitAmount": 1200},
        {"LineItemID": "L2", "AccountCode": "429", "Quantity": 1, "UnitAmount": 50},
    ])

    body = await svc._build_xero_body(
        integration=integration,
        connection_id="c", tenant_id="t", document_id=uuid.uuid4(),
        header_updates={}, line_item_updates={"AccountCode": "710"},
        target_codes=frozenset(),
    )

    codes = [l["AccountCode"] for l in body["Invoices"][0]["LineItems"]]
    assert codes == ["710", "710"]


async def test_header_only_update_skips_the_fetch():
    """No line_item_updates → no read-modify-write, just the header body."""
    svc = _svc()
    integration = _integration([])
    doc_id = uuid.uuid4()

    body = await svc._build_xero_body(
        integration=integration,
        connection_id="c", tenant_id="t", document_id=doc_id,
        header_updates={"Status": "AUTHORISED"}, line_item_updates={},
    )

    integration.fetch_invoice.assert_not_awaited()
    assert body == {"Invoices": [{"InvoiceID": str(doc_id), "Status": "AUTHORISED"}]}


# =====================================================================
# IntegrationService.update_invoice — action-first, full LineItems
# =====================================================================

def _integration_service(action_result):
    nango = MagicMock()
    nango.action_update_invoice = AsyncMock(return_value=action_result)
    nango.update_xero_invoice = AsyncMock(return_value={"proxy": True})
    return IntegrationService(nango=nango), nango


async def test_recode_goes_through_action_with_full_lineitems():
    """A line recode must hit the Nango Action (actions-first) and pass the
    FULL rebuilt LineItems (LineItemID + unchanged lines), never the proxy."""
    svc, nango = _integration_service({"invoiceId": "inv-1", "updated": True})
    body = {"Invoices": [{
        "InvoiceID": "inv-1",
        "LineItems": [
            {"LineItemID": "L1", "AccountCode": "710", "Description": "Laptop"},
            {"LineItemID": "L2", "AccountCode": "200", "Description": "Support"},
        ],
    }]}

    result = await svc.update_invoice(
        "conn-1", "tenant-1", "inv-1", body,
        line_item_updates={"AccountCode": "710"},
    )

    assert result == {"invoiceId": "inv-1", "updated": True}
    nango.update_xero_invoice.assert_not_awaited()          # proxy NOT used
    _, kwargs = nango.action_update_invoice.await_args
    sent = kwargs["fields"]["lineItems"]
    assert sent == body["Invoices"][0]["LineItems"]         # full set, with LineItemID
    assert all("LineItemID" in l for l in sent)


async def test_recode_falls_back_to_proxy_when_action_unavailable():
    """Action returns None (not enabled) → proxy fallback with the same body."""
    svc, nango = _integration_service(None)
    body = {"Invoices": [{"InvoiceID": "inv-1",
                          "LineItems": [{"LineItemID": "L1", "AccountCode": "710"}]}]}

    result = await svc.update_invoice(
        "c", "t", "inv-1", body, line_item_updates={"AccountCode": "710"},
    )

    nango.action_update_invoice.assert_awaited_once()
    nango.update_xero_invoice.assert_awaited_once_with("c", "t", "inv-1", body)
    assert result == {"proxy": True}
