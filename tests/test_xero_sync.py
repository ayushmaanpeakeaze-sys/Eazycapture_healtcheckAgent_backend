"""Unit tests for the DB-backed Xero sync — the pure, network-free logic:
date parsing, the If-Modified-Since watermark format, the entity-spec config,
and the reconciled-id derivation. The live full/incremental flow is covered by
manual end-to-end runs against a real org; these guard the bits that silently
break (date formats, watermark round-trips, spec drift).
"""
from datetime import datetime, timezone

from app.modules.integrations.sync.db_read import _reconciled_invoice_ids
from app.modules.integrations.sync.engine import (
    ENTITY_SPECS,
    WATERMARK_OVERLAP,
    format_if_modified_since,
    parse_xero_datetime,
)
from app.modules.integrations.sync.models import SYNC_ENTITIES


# --- parse_xero_datetime -------------------------------------------------

def test_parse_ms_ajax_date():
    # Xero's Accounting API format, as seen live on UpdatedDateUTC.
    dt = parse_xero_datetime("/Date(1229650679057+0000)/")
    assert dt is not None
    assert dt.tzinfo is not None
    # 1229650679057 ms → 2008-12-19 (sanity on the epoch conversion)
    assert dt.year == 2008 and dt.month == 12


def test_parse_iso_date():
    dt = parse_xero_datetime("2026-06-24T10:13:38Z")
    assert dt == datetime(2026, 6, 24, 10, 13, 38, tzinfo=timezone.utc)


def test_parse_iso_naive_assumes_utc():
    dt = parse_xero_datetime("2026-06-24T10:13:38")
    assert dt is not None and dt.tzinfo is not None
    assert dt.hour == 10


def test_parse_passthrough_datetime():
    src = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert parse_xero_datetime(src) == src


def test_parse_bad_values_are_none():
    for bad in (None, "", "not-a-date", "/Date()/", "garbage"):
        assert parse_xero_datetime(bad) is None


# --- format_if_modified_since -------------------------------------------

def test_format_if_modified_since_shape():
    dt = datetime(2027, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert format_if_modified_since(dt) == "2027-01-02T03:04:05"


def test_format_round_trips_with_parse():
    dt = datetime(2026, 6, 24, 10, 13, 38, tzinfo=timezone.utc)
    again = parse_xero_datetime(format_if_modified_since(dt))
    assert again == dt


def test_watermark_overlap_is_sixty_seconds():
    # The safety window that re-asks just before the watermark so a same-second
    # update isn't missed. Upsert is idempotent so re-seeing a row is harmless.
    assert WATERMARK_OVERLAP.total_seconds() == 60


# --- ENTITY_SPECS --------------------------------------------------------

def test_specs_cover_every_sync_entity():
    assert set(ENTITY_SPECS) == set(SYNC_ENTITIES)


def test_incremental_vs_full_modes():
    incremental = {"invoice", "bank_transaction", "credit_note", "contact", "account"}
    # journal + asset (SOP) are full-refresh, watermark-less like tax_rate/payment/org.
    full = {"tax_rate", "payment", "organisation", "journal", "asset"}
    assert {e for e, s in ENTITY_SPECS.items() if s.mode == "incremental"} == incremental
    assert {e for e, s in ENTITY_SPECS.items() if s.mode == "full"} == full


def test_id_fields_match_xero():
    expected = {
        "invoice": "InvoiceID",
        "bank_transaction": "BankTransactionID",
        "credit_note": "CreditNoteID",
        "contact": "ContactID",
        "account": "AccountID",
        "tax_rate": "TaxType",
        "payment": "PaymentID",
        "organisation": "OrganisationID",
        "journal": "JournalID",
        "asset": "assetId",
    }
    assert {e: s.id_field for e, s in ENTITY_SPECS.items()} == expected


def test_single_call_entities_do_not_paginate():
    # Accounts / tax rates / organisation come back in one Xero call.
    for e in ("account", "tax_rate", "organisation"):
        assert ENTITY_SPECS[e].paginates is False
    for e in ("invoice", "credit_note", "contact", "payment"):
        assert ENTITY_SPECS[e].paginates is True


# --- reconciled-id derivation (db_read mirrors tasks exactly) -------------

def test_reconciled_ids_only_bank_matched():
    payments = [
        {"IsReconciled": True, "Invoice": {"InvoiceID": "inv-1"}},
        {"IsReconciled": False, "Invoice": {"InvoiceID": "inv-2"}},   # not matched
        {"IsReconciled": True, "Invoice": {"InvoiceID": "inv-3"}},
        {"IsReconciled": True, "Invoice": {}},                         # no id
        "garbage",                                                      # ignored
    ]
    assert _reconciled_invoice_ids(payments) == {"inv-1", "inv-3"}


def test_reconciled_ids_empty():
    assert _reconciled_invoice_ids([]) == set()
    assert _reconciled_invoice_ids(None) == set()
