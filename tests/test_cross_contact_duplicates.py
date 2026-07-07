"""Cross-contact duplicate detection — same document under two contact records.

The per-contact pass (``_find_duplicate_documents``) keys strictly on ContactID, so a
supplier saved twice (e.g. "Peakvisory" and "Peakvisory Limited") hides a real
duplicate. ``_find_cross_contact_duplicates`` blocks by amount, then scores each
cross-contact pair on several signals — party (VAT when both present, else name
similarity), date, reference, invoice number, description — none decides alone.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.shared.transaction import BatchTransaction
from app.modules.healthcheck.checks.duplicates import _find_cross_contact_duplicates


def _doc(tid, contact_id, *, number=None, ref=None, amount="500", desc="x",
         d=date(2026, 1, 12), typ="ACCPAY", vendor="Peakvisory"):
    return BatchTransaction(
        transaction_id=tid, date=d, description=desc, amount=Decimal(amount),
        vendor_name=vendor, type=typ, contact_id=contact_id,
        invoice_number=number, reference=ref,
    )


# --- party by NAME (no VAT) --------------------------------------------------

def test_name_match_same_supplier_flagged():
    # Peakvisory (C1) and Peakvisory Limited (C2): 100% name after suffix strip.
    hits = _find_cross_contact_duplicates([
        _doc("B1", "C1", vendor="Peakvisory"),
        _doc("B2", "C2", vendor="Peakvisory Limited"),
    ])
    assert len(hits) == 2
    assert hits[0].issue_type == "duplicate_bill"
    assert hits[0].match_reasons["cross_contact"] is True
    assert hits[0].match_reasons["tier"] == "review"
    assert hits[0].match_reasons["party_by"] == "name"
    # Honest score: 100% name + amount + same-day + same-description, no reference
    # (a bill's identifier) → ~0.75, not a fake 100%.
    assert hits[0].confidence >= 0.70
    assert hits[0].match_reasons["advisory"]


def test_case_only_difference_flagged():
    # "ABC" vs "abc" — case is normalised away → 100% name.
    hits = _find_cross_contact_duplicates([
        _doc("B1", "C1", vendor="ABC"),
        _doc("B2", "C2", vendor="abc"),
    ])
    assert len(hits) == 2
    assert hits[0].match_reasons["party_by"] == "name"


def test_coincidental_amount_not_flagged():
    # ₹400 both spent: different names, different ref/description → below the bar.
    hits = _find_cross_contact_duplicates([
        _doc("B1", "C1", amount="400", ref="R-1", desc="alpha", vendor="Acme"),
        _doc("B2", "C2", amount="400", ref="R-2", desc="beta", vendor="Globex"),
    ])
    assert hits == []


def test_different_party_names_skipped_despite_matching_content():
    # Acme vs Zenith (~20% name, no VAT) is below the 60% party gate, so this is
    # not the "same supplier under two records" case — skipped even though the
    # reference, number, amount and day all match. Content never overrides party.
    hits = _find_cross_contact_duplicates([
        _doc("B1", "C1", ref="R-9", number="N-9", vendor="Acme"),
        _doc("B2", "C2", ref="R-9", number="N-9", vendor="Zenith"),
    ])
    assert hits == []


# --- identifier is doc-type aware (bill=reference, sales=invoice number) -----

def test_bill_reference_is_the_identifier():
    # A bill carries no invoice number of its own — the supplier REFERENCE is its
    # number, so a matching reference is the strong "same document" signal. Same
    # party (100% name after suffix strip), so it clears the party gate and the
    # matching reference lifts a bill near its ~95% ceiling.
    hits = _find_cross_contact_duplicates([
        _doc("B1", "C1", ref="SUP-9", vendor="Acme Traders", typ="ACCPAY"),
        _doc("B2", "C2", ref="SUP-9", vendor="Acme Traders Ltd", typ="ACCPAY"),
    ])
    assert len(hits) == 2
    assert hits[0].confidence >= 0.90       # bill reference = identifier → high


def test_hamilton_rex_false_positive_excluded_by_party_gate():
    # The real demo false-positive: Hamilton Smith Ltd vs Rex Media Group share a
    # reference (and amount/day) but are different companies (26% name, no VAT).
    # The 60% party gate cuts them out before content is even scored — content
    # matching can never resurrect a below-gate name pair.
    hits = _find_cross_contact_duplicates([
        _doc("S1", "C1", ref="R-9", number="INV-1", desc="a",
             vendor="Hamilton Smith Ltd", typ="ACCREC"),
        _doc("S2", "C2", ref="R-9", number="INV-2", desc="a",
             vendor="Rex Media Group", typ="ACCREC"),
    ])
    assert hits == []


# --- party by VAT ------------------------------------------------------------

def test_different_vat_skipped_even_if_name_and_content_match():
    # sir's case: two DIFFERENT companies both named "ABC", different VAT → skip.
    vat = {"C1": "GB111", "C2": "GB222"}
    hits = _find_cross_contact_duplicates(
        [
            _doc("B1", "C1", ref="R-9", vendor="ABC"),
            _doc("B2", "C2", ref="R-9", vendor="ABC"),
        ],
        contact_vat=vat,
    )
    assert hits == []


def test_same_vat_flags_even_with_different_names():
    # Same VAT = same company, so name is irrelevant.
    vat = {"C1": "GB999", "C2": "GB999"}
    hits = _find_cross_contact_duplicates(
        [
            _doc("B1", "C1", vendor="Acme"),
            _doc("B2", "C2", vendor="Globex"),
        ],
        contact_vat=vat,
    )
    assert len(hits) == 2
    assert hits[0].match_reasons["party_by"] == "vat"


def test_one_vat_missing_falls_back_to_name():
    # Only C1 has a VAT → cannot compare VAT → name similarity decides.
    vat = {"C1": "GB999"}
    hits = _find_cross_contact_duplicates(
        [
            _doc("B1", "C1", vendor="Peakvisory"),
            _doc("B2", "C2", vendor="Peakvisory Limited"),
        ],
        contact_vat=vat,
    )
    assert len(hits) == 2
    assert hits[0].match_reasons["party_by"] == "name"


# --- original vs duplicate (paid copy = keep) --------------------------------

def test_paid_copy_is_the_likely_original():
    # One paid, one unpaid copy across two contacts → the PAID one is already
    # actioned (keep it); the UNPAID one is the risky duplicate. High risk.
    def inv(tid, cid, vendor, status, due):
        return BatchTransaction(
            transaction_id=tid, date=date(2026, 7, 7), description="Chairs and tables",
            amount=Decimal("300"), vendor_name=vendor, type="ACCREC", contact_id=cid,
            reference="nvn", invoice_number=None, status=status, amount_due=Decimal(due),
        )
    hits = _find_cross_contact_duplicates([
        inv("unpaid-one", "C1", "Ayushmaan singh rajput", "AUTHORISED", "300"),
        inv("paid-one", "C2", "Ayushmaan Singh", "PAID", "0"),
    ])
    assert len(hits) == 2
    original = [h for h in hits if h.this_is_likely_original]
    assert len(original) == 1
    assert original[0].transaction_id == "paid-one"      # PAID copy = keep
    assert hits[0].match_reasons["risk"] == "high"
    assert hits[0].match_reasons["reference_match"] == "exact"
    # Honest confidence: sales, 81% name, invoice# differs → ~0.74, NOT a fake 1.0.
    assert 0.70 <= hits[0].confidence < 0.85


# --- boundaries --------------------------------------------------------------

def test_same_contact_ignored():
    hits = _find_cross_contact_duplicates([
        _doc("B1", "C1", vendor="Peakvisory"),
        _doc("B2", "C1", vendor="Peakvisory"),
    ])
    assert hits == []


def test_different_amount_never_compared():
    hits = _find_cross_contact_duplicates([
        _doc("B1", "C1", amount="500", vendor="Peakvisory"),
        _doc("B2", "C2", amount="501", vendor="Peakvisory Limited"),
    ])
    assert hits == []


def test_beyond_date_window_not_flagged_by_default():
    hits = _find_cross_contact_duplicates([
        _doc("B1", "C1", d=date(2026, 1, 12), vendor="Peakvisory"),
        _doc("B2", "C2", d=date(2026, 1, 15), vendor="Peakvisory Limited"),
    ])
    assert hits == []
