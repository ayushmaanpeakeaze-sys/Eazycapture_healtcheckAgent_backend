"""Duplicate invoice/bill detection — sir's ContactID + Reference rules.

Verifies detection is grouped by contact_id (the foreign key) and matches on
Reference, NOT fuzzy vendor names.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.shared.transaction import BatchTransaction
from app.modules.healthcheck.engine.audit_settings import AuditSettings
from app.modules.healthcheck.checks.duplicates import _find_duplicate_bills
from app.modules.healthcheck.engine.deterministic import (
    _build_contact_alias,
)


def _doc(tid, contact_id, ref, amount="100", d=date(2026, 1, 1),
         typ="ACCPAY", vendor="Acme"):
    return BatchTransaction(
        transaction_id=tid, date=d, description="x", amount=Decimal(amount),
        vendor_name=vendor, type=typ, contact_id=contact_id, reference=ref,
    )


# The confidence bar defaults to 90% (only precise duplicates). Tests that
# assert WEAKER matches (75% / 70% / 45%) drop the floor so the scoring itself
# is what's under test, not the default bar.
_LOOSE = AuditSettings.from_config({"duplicate_min_confidence": 0.0})


def _loose(**extra):
    return AuditSettings.from_config({"duplicate_min_confidence": 0.0, **extra})


# --- Rule 1: same contact + same Reference (case-insensitive, EXACT only) ---

def test_rule1_exact_reference():
    # Bill: same reference + amount + same day. The supplier REFERENCE is the
    # bill's number → same reference = same bill → 1.0 HIGH (symmetric with a
    # sales invoice's same-invoice-number = 1.0).
    hits = _find_duplicate_bills([_doc("B1", "C1", "INV-9"), _doc("B2", "C1", "INV-9")])
    assert len(hits) == 2                       # both sides flagged
    assert hits[0].issue_type == "duplicate_bill"
    assert hits[0].confidence == 1.0
    assert hits[0].severity == "high"


# --- No normalization: "INV-1234" vs "inv1234" are DIFFERENT refs ----------

def test_different_refs_hidden_by_default_shown_when_loose():
    # SALES (ACCREC): INV-1234 vs inv1234 = conflicting refs, no invoice number.
    # The require_exact_reference gate applies to SALES (a bill's reference IS its
    # number, so bills don't gate on it).
    pair = [_doc("B1", "C1", "INV-1234", typ="ACCREC"), _doc("B2", "C1", "inv1234", typ="ACCREC")]
    # Default (require_exact_reference ON) → conflicting refs dropped.
    assert _find_duplicate_bills(pair) == []
    # Toggle OFF → surfaces: no invoice number, conflicting ref, same amount +
    # same day → weak → 0.75 medium (review).
    loose = _loose(duplicate_require_exact_reference=False)
    hits = _find_duplicate_bills(pair, None, loose)
    assert len(hits) == 2
    assert hits[0].confidence == 0.75
    assert hits[0].match_reasons["reference_match"] == "different"
    assert hits[0].match_reasons["tier"] == "medium"


# --- Rule 3: same amount+date + missing reference --------------------------

def test_rule3_missing_reference():
    # One ref blank → reference scores 0; amount +35, contact +20, date +15 =
    # 0.70 → medium severity (high needs >= 0.80).
    hits = _find_duplicate_bills([_doc("B1", "C1", "INV-5"), _doc("B2", "C1", None)], None, _LOOSE)
    assert len(hits) == 2
    assert hits[0].severity == "medium"


# --- NOT flagged cases ------------------------------------------------------

def test_different_contacts_not_paired():
    # Two DIFFERENT customers that share a standard reference + amount are NOT a
    # duplicate (e.g. many clients on the same "Monthly Support" fee). Without a
    # duplicate-contacts merge, cross-contact pairs are never generated.
    hits = _find_duplicate_bills([_doc("B1", "C1", "INV-9"), _doc("B2", "C2", "INV-9")])
    assert hits == []


def test_genuinely_different_not_flagged():
    hits = _find_duplicate_bills([
        _doc("B1", "C1", "A", amount="100", d=date(2026, 1, 1)),
        _doc("B2", "C1", "B", amount="200", d=date(2026, 2, 2)),
    ])
    assert hits == []


def test_same_reference_different_amount_not_flagged():
    # Reused generic reference ("Training") on two genuinely different invoices.
    hits = _find_duplicate_bills([
        _doc("B1", "C1", "Training", amount="1082.50"),
        _doc("B2", "C1", "Training", amount="541.25"),
    ])
    assert hits == []


def test_far_apart_pair_dropped_by_hard_window():
    # "Date within" is a HARD filter: a same ref+amount pair ~30 days apart is
    # beyond the default 7-day window → dropped entirely (not shown at all).
    hits = _find_duplicate_bills([
        _doc("B1", "C1", "Monthly Support", amount="541.25", d=date(2026, 3, 22)),
        _doc("B2", "C1", "Monthly Support", amount="541.25", d=date(2026, 4, 21)),
    ])
    assert hits == []


def test_adjacent_day_duplicate_flagged():
    # Hamilton Smith case: same customer, same amount + reference, 1 day apart.
    # Default window is 0 (same day) → must widen to 1 to pair adjacent days.
    w1 = AuditSettings.from_config({"duplicate_days_window": 1})
    hits = _find_duplicate_bills([
        _doc("B1", "C1", "Monthly Support", amount="541.25", d=date(2026, 3, 21)),
        _doc("B2", "C1", "Monthly Support", amount="541.25", d=date(2026, 3, 22)),
    ], None, w1)
    assert len(hits) == 2
    assert hits[0].confidence == 1.0        # bill: same reference (its number) = same bill


def test_far_apart_identical_pair_is_recurring_review():
    # Window 40 → the 30-day pair is inside the window, but two IDENTICAL
    # invoices ~a month apart (even with no 3+ series) read as a recurring
    # charge, not a same-period duplicate → LOW review (0.45), never 0.97.
    hits = _find_duplicate_bills([
        _doc("B1", "C1", "Monthly Support", amount="541.25", d=date(2026, 3, 22)),
        _doc("B2", "C1", "Monthly Support", amount="541.25", d=date(2026, 4, 21)),
    ], None, _WIDE)
    assert len(hits) == 2
    assert hits[0].confidence == 0.45
    assert hits[0].match_reasons["tier"] == "low"
    assert hits[0].match_reasons["recurring"] is True


def test_same_invoice_number_is_high_not_recurring():
    # Two invoices a month apart that share the SAME invoice number → a recurring
    # charge always gets a fresh number, so this is a real duplicate → HIGH, and
    # NOT flagged recurring. (Invoice number is an extra +40 signal.)
    def _inv(tid, num, d):
        return BatchTransaction(
            transaction_id=tid, date=d, description="x", amount=Decimal("100"),
            vendor_name="Acme", type="ACCREC", contact_id="C1",
            reference="Monthly", invoice_number=num,
        )
    wide = AuditSettings.from_config({"duplicate_days_window": 40})
    hits = _find_duplicate_bills(
        [_inv("a", "INV-9", date(2026, 3, 21)), _inv("b", "INV-9", date(2026, 4, 21))],
        None, wide,
    )
    assert len(hits) == 2
    assert hits[0].match_reasons["same_invoice_number"] is True
    assert hits[0].match_reasons["recurring"] is False
    # same invoice number + amount, same period → 100% certain duplicate
    assert hits[0].confidence == 1.0


def test_posted_date_decides_original_vs_duplicate():
    # INV-A is dated EARLIER (issue) but was ENTERED later in Xero (posted_date)
    # → it's the re-entry (duplicate). The created-in-Xero date wins over the
    # issue date for picking original vs duplicate.
    def _inv(tid, issue, posted):
        return BatchTransaction(
            transaction_id=tid, date=issue, posted_date=posted, description="x",
            amount=Decimal("100"), vendor_name="Acme", type="ACCREC",
            contact_id="C1", reference="INV-9",
        )
    a = _inv("A", date(2026, 3, 21), date(2026, 4, 6))   # dated first, entered LATE
    b = _inv("B", date(2026, 3, 22), date(2026, 3, 22))  # dated 2nd, entered on time
    w1 = AuditSettings.from_config({"duplicate_days_window": 1})  # issue dates 1 day apart
    hits = _find_duplicate_bills([a, b], None, w1)
    orig = [h for h in hits if h.this_is_likely_original]
    dup = [h for h in hits if not h.this_is_likely_original]
    assert orig and orig[0].transaction_id == "B"   # entered first → original
    assert dup and dup[0].transaction_id == "A"      # entered late → duplicate


def test_close_identical_pair_still_high():
    # A 2-day-apart identical pair is a genuine same-period duplicate → HIGH.
    wide = AuditSettings.from_config({"duplicate_days_window": 40})
    hits = _find_duplicate_bills([
        _doc("B1", "C1", "Monthly Support", amount="541.25", d=date(2026, 3, 21)),
        _doc("B2", "C1", "Monthly Support", amount="541.25", d=date(2026, 3, 23)),
    ], None, wide)
    assert len(hits) == 2
    assert hits[0].confidence == 1.0        # bill: same reference, not recurring → same bill
    assert hits[0].match_reasons["recurring"] is False


def test_just_outside_window_dropped():
    # 8 days apart > 7-day window → dropped by the hard filter.
    hits = _find_duplicate_bills([
        _doc("B1", "C1", "INV-9", amount="100", d=date(2026, 1, 1)),
        _doc("B2", "C1", "INV-9", amount="100", d=date(2026, 1, 9)),
    ])
    assert hits == []


# --- Cadence-aware recurring detection -------------------------------------

# Recurring detection only matters when the window is WIDE enough that a
# recurring pair fits inside it (with a tight window the far-apart pairs are
# simply dropped by the hard filter). So these use a 40-day window.
_WIDE = AuditSettings.from_config(
    {"duplicate_days_window": 40, "duplicate_min_confidence": 0.0})


def test_recurring_monthly_series_is_review_not_high():
    # 3 monthly "Monthly Support" £541.25 invoices, window 40 → the ~30-day pairs
    # sit on the cadence → recurring (LOW review), never HIGH, even though
    # amount + reference + contact all match.
    txns = [
        _doc("A", "C1", "Monthly Support", amount="541.25", d=date(2026, 3, 21)),
        _doc("B", "C1", "Monthly Support", amount="541.25", d=date(2026, 4, 21)),
        _doc("C", "C1", "Monthly Support", amount="541.25", d=date(2026, 5, 21)),
    ]
    hits = _find_duplicate_bills(txns, None, _WIDE)
    assert hits, "recurring pairs should SHOW (as review), not be hidden"
    for h in hits:
        assert h.match_reasons["tier"] == "low"
        assert h.match_reasons["recurring"] is True


def test_double_entry_in_recurring_series_flagged_high():
    # Same monthly series (window 40), but April was entered TWICE (21st + 23rd).
    # The 2-day pair breaks the ~30-day cadence → genuine duplicate → HIGH, while
    # the on-cadence pairs stay LOW review.
    txns = [
        _doc("A", "C1", "Monthly Support", amount="541.25", d=date(2026, 3, 21)),
        _doc("B", "C1", "Monthly Support", amount="541.25", d=date(2026, 4, 21)),
        _doc("DUP", "C1", "Monthly Support", amount="541.25", d=date(2026, 4, 23)),  # double entry
        _doc("C", "C1", "Monthly Support", amount="541.25", d=date(2026, 5, 21)),
    ]
    hits = _find_duplicate_bills(txns, None, _WIDE)
    # The April 21 ↔ April 23 pair is the real duplicate → HIGH.
    dup = [h for h in hits if h.match_reasons["tier"] == "high"]
    assert dup, "the same-period double-entry must be flagged HIGH"
    pair_ids = {dup[0].transaction_id, dup[0].duplicate_of_transaction_id}
    assert pair_ids == {"B", "DUP"}
    # The genuine recurring pairs are still LOW review, not HIGH.
    assert any(h.match_reasons["tier"] == "low" and h.match_reasons["recurring"]
               for h in hits)


def test_recurring_suppresses_distinct_documents_note():
    # A recurring monthly series has DIFFERENT (sequential) invoice numbers —
    # that's expected, so the "2 distinct documents (deposit+balance)" note must
    # NOT show; it would contradict the "could be recurring" label on the card.
    def _r(tid, num, d):
        return BatchTransaction(
            transaction_id=tid, invoice_number=num, date=d, description="Monthly",
            amount=Decimal("99"), vendor_name="Sub Co", type="ACCREC",
            contact_id="C1", reference="Monthly Plan",
        )
    txns = [_r("a", "INV-1", date(2026, 1, 10)),
            _r("b", "INV-2", date(2026, 2, 10)),
            _r("c", "INV-3", date(2026, 3, 10))]
    rec = [h for h in _find_duplicate_bills(txns, None, _WIDE)
           if h.match_reasons.get("recurring")]
    assert rec, "monthly series should be recurring"
    for h in rec:
        assert h.match_reasons["distinct_documents_possible"] is False


# --- Original/duplicate tags ONLY for high-tier (confirmed) duplicates -------

def test_high_tier_keeps_original_duplicate_tags():
    # A confirmed duplicate (exact ref, same day, same amount → 90% HIGH) IS
    # directional: exactly one ORIGINAL + one DUPLICATE, and we recommend a void.
    hits = _find_duplicate_bills([_doc("A", "C1", "INV-9"), _doc("B", "C1", "INV-9")],
                                 None, _LOOSE)
    assert len(hits) == 2
    assert all(h.match_reasons["tier"] == "high" for h in hits)
    assert {h.this_is_likely_original for h in hits} == {True, False}
    assert any("void" in h.message.lower() for h in hits)


def test_review_tier_drops_original_duplicate_tag():
    # A recurring REVIEW pair is NOT a confirmed duplicate → neither side is
    # original/to-void; this_is_likely_original is None and the message only
    # asks the user to review (never "void").
    txns = [
        _doc("a", "C1", "Monthly", amount="99", d=date(2026, 1, 10)),
        _doc("b", "C1", "Monthly", amount="99", d=date(2026, 2, 10)),
        _doc("c", "C1", "Monthly", amount="99", d=date(2026, 3, 10)),
    ]
    rec = [h for h in _find_duplicate_bills(txns, None, _WIDE)
           if h.match_reasons.get("recurring")]
    assert rec
    for h in rec:
        assert h.this_is_likely_original is None
        assert "void" not in h.message.lower()
        assert "review" in h.message.lower()


def test_medium_tier_drops_original_duplicate_tag():
    # No ref + no number, same amount + same day → 75% MEDIUM review → also
    # non-directional (just a possible match with its confidence).
    hits = _find_duplicate_bills([_doc("A", "C1", None), _doc("B", "C1", None)],
                                 None, _LOOSE)
    assert len(hits) == 2
    assert all(h.match_reasons["tier"] == "medium" for h in hits)
    for h in hits:
        assert h.this_is_likely_original is None
        assert "void" not in h.message.lower()


def test_true_duplicate_same_ref_amount_date_flagged():
    hits = _find_duplicate_bills([
        _doc("B1", "C1", "INV-9", amount="541.25", d=date(2026, 3, 22)),
        _doc("B2", "C1", "INV-9", amount="541.25", d=date(2026, 3, 22)),
    ])
    assert len(hits) == 2
    assert hits[0].confidence == 1.0        # bill: same reference + amount + same day


def test_bill_diff_reference_same_day_is_distinct_docs_candidate():
    # BILL: different references, same amount + same day. The reference IS the
    # bill's number, so different references = possibly two distinct bills →
    # 0.95 HIGH, shown by default (symmetric with a sales invoice's
    # different-invoice-number same-day case).
    pair = [
        _doc("B1", "C1", "INV-100", amount="500", d=date(2026, 1, 1)),
        _doc("B2", "C1", "INV-200", amount="500", d=date(2026, 1, 1)),
    ]
    hits = _find_duplicate_bills(pair)          # default settings → still shown
    assert len(hits) == 2
    assert hits[0].confidence == 0.95
    assert hits[0].match_reasons["tier"] == "high"
    assert hits[0].match_reasons["reference_match"] == "different"


def test_different_direction_not_paired():
    # One ACCREC, one ACCPAY — never a duplicate pair.
    hits = _find_duplicate_bills([
        _doc("B1", "C1", "INV-9", typ="ACCREC"),
        _doc("B2", "C1", "INV-9", typ="ACCPAY"),
    ])
    assert hits == []


# --- Credit notes: type-aware (credit-to-credit only) ------------

def test_duplicate_sales_credit_notes_flagged():
    # Two identical sales credit notes for the same customer → duplicate.
    hits = _find_duplicate_bills([
        _doc("CN1", "C1", "CR-9", typ="ACCRECCREDIT"),
        _doc("CN2", "C1", "CR-9", typ="ACCRECCREDIT"),
    ])
    assert len(hits) == 2
    assert hits[0].issue_type == "duplicate_credit_note"
    assert hits[0].confidence == 0.90       # exact ref + amount, same day, no number


def test_duplicate_purchase_credit_notes_flagged():
    hits = _find_duplicate_bills([
        _doc("CN1", "C1", "CR-9", typ="ACCPAYCREDIT"),
        _doc("CN2", "C1", "CR-9", typ="ACCPAYCREDIT"),
    ])
    assert len(hits) == 2
    assert hits[0].issue_type == "duplicate_credit_note"


def test_invoice_and_credit_note_never_paired():
    # The critical false-positive guard: an invoice (ACCREC) and a credit note
    # (ACCRECCREDIT) for the same contact/ref/amount are NEVER a duplicate pair.
    hits = _find_duplicate_bills([
        _doc("INV", "C1", "DOC-9", typ="ACCREC"),
        _doc("CN", "C1", "DOC-9", typ="ACCRECCREDIT"),
    ])
    assert hits == []


def test_applied_credit_notes_still_flagged_despite_paid_gate():
    # also_check_paid is OFF by default → two PAID invoices are suppressed. But
    # "paid" is an invoice concept; a fully-applied credit note must still flag
    # (credit pairs bypass the paid gate).
    def _credit(tid):
        return BatchTransaction(
            transaction_id=tid, date=date(2026, 1, 1), description="x",
            amount=Decimal("100"), vendor_name="Acme", type="ACCRECCREDIT",
            contact_id="C1", reference="CR-9", status="PAID",
            amount_paid=Decimal("100"), amount_due=Decimal("0"),
        )
    hits = _find_duplicate_bills([_credit("CN1"), _credit("CN2")])
    assert len(hits) == 2
    assert hits[0].issue_type == "duplicate_credit_note"


# --- Legacy fallback + direction -------------------------------------------

def test_legacy_fallback_to_vendor_name():
    # No contact_id → falls back to vendor name grouping.
    hits = _find_duplicate_bills([
        _doc("B1", None, "INV-9", vendor="Acme"),
        _doc("B2", None, "INV-9", vendor="Acme"),
    ])
    assert len(hits) == 2


def test_accrec_is_duplicate_invoice():
    hits = _find_duplicate_bills([
        _doc("B1", "C1", "INV-9", typ="ACCREC"),
        _doc("B2", "C1", "INV-9", typ="ACCREC"),
    ])
    assert hits[0].issue_type == "duplicate_invoice"


# --- Contact identity: duplicate invoices key on the REAL ContactID ---------
# The duplicate-contacts alias is used by OTHER checks, but NOT by the duplicate
# invoice engine — distinct ContactIDs are always separate parties here.

def test_contact_alias_union_find():
    alias = _build_contact_alias([["A", "B"], ["B", "C"]])
    # A, B, C all collapse to one canonical id (still used by other checks)
    assert alias["A"] == alias["B"] == alias["C"]


def test_cross_contact_not_paired_without_alias():
    # Two different ContactIDs are NEVER paired — duplicate invoices key on the
    # real ContactID only, so distinct contacts stay separate.
    hits = _find_duplicate_bills([_doc("1", "A", "INV-9"), _doc("2", "B", "INV-9")], None)
    assert hits == []


# --- Settings per the spec --------------------------------------------------

def _paid_doc(tid, ref, *, paid, amount="100", d=date(2026, 1, 1)):
    return BatchTransaction(
        transaction_id=tid, date=d, description="x", amount=Decimal(amount),
        vendor_name="Acme", type="ACCPAY", contact_id="C1", reference=ref,
        status="PAID" if paid else "AUTHORISED",
        amount_paid=Decimal(amount) if paid else Decimal("0"),
        amount_due=Decimal("0") if paid else Decimal(amount),
    )


def test_require_same_amount_off_flags_different_values():
    pair = [
        _doc("B1", "C1", "INV-1", amount="100", d=date(2026, 1, 1)),
        _doc("B2", "C1", "INV-9", amount="500", d=date(2026, 1, 1)),  # diff amount+ref
    ]
    assert _find_duplicate_bills(pair) == []                       # default: amount required
    # loose mode: allow differing values AND lower the waterfall floor so the
    # weaker (diff-value, diff-ref) match surfaces.
    loose = AuditSettings.from_config({
        "duplicate_require_same_amount": False, "duplicate_min_confidence": 0.3,
        "duplicate_require_exact_reference": False,   # allow the conflicting refs through
    })
    hits = _find_duplicate_bills(pair, None, loose)
    assert len(hits) == 2                                          # loose mode flags it
    assert hits[0].match_reasons["same_amount"] is False
    assert hits[0].match_reasons["reference_match"] == "different"


def test_require_exact_reference_suppresses_normalized():
    s = AuditSettings.from_config({"duplicate_require_exact_reference": True})
    # SALES: conflicting refs (case differs, no invoice number) → suppressed by
    # the sales reference gate when exact required.
    assert _find_duplicate_bills(
        [_doc("B1", "C1", "INV-1234", typ="ACCREC"), _doc("B2", "C1", "inv1234", typ="ACCREC")], None, s,
    ) == []
    # exact-reference sales pair (no invoice number) still flagged at 0.90.
    assert len(_find_duplicate_bills(
        [_doc("B1", "C1", "INV-9", typ="ACCREC"), _doc("B2", "C1", "INV-9", typ="ACCREC")], None, s,
    )) == 2


def test_also_check_paid_off_requires_one_unpaid():
    off = AuditSettings.from_config({"duplicate_also_check_paid": False})
    # both paid → suppressed
    assert _find_duplicate_bills(
        [_paid_doc("B1", "INV-9", paid=True), _paid_doc("B2", "INV-9", paid=True)], None, off,
    ) == []
    # one unpaid → flagged
    assert len(_find_duplicate_bills(
        [_paid_doc("B1", "INV-9", paid=True), _paid_doc("B2", "INV-9", paid=False)], None, off,
    )) == 2
    # default (also_check_paid=False now) → both-paid suppressed too
    assert _find_duplicate_bills(
        [_paid_doc("B1", "INV-9", paid=True), _paid_doc("B2", "INV-9", paid=True)],
    ) == []


def test_output_ranks_by_confidence():
    # same-ref bill pair (1.0) + no-ref pair (0.75) → output sorted strongest first.
    txns = [
        _doc("A1", "C1", "INV-9", amount="100"), _doc("A2", "C1", "INV-9", amount="100"),
        _doc("B1", "C2", None, amount="200"), _doc("B2", "C2", None, amount="200"),
    ]
    confs = [h.confidence for h in _find_duplicate_bills(txns, None, _LOOSE)]
    assert confs == sorted(confs, reverse=True)
    assert confs[0] == 1.0 and 0.75 in confs


def test_min_confidence_floor_drops_weak_matches():
    pair = [_doc("A1", "C1", None, amount="100"), _doc("A2", "C1", None, amount="100")]  # no ref → 0.75
    assert _find_duplicate_bills(pair, None, _LOOSE) != []         # low floor → kept
    assert _find_duplicate_bills(pair) == []                       # default bar 0.90 → 0.75 dropped


# --- match_reasons ("what matched" chips) ----------------------------------

def test_match_reasons_exact_reference():
    w1 = AuditSettings.from_config({"duplicate_days_window": 1})
    hits = _find_duplicate_bills([
        _doc("B1", "C1", "INV-9", amount="541.25", d=date(2026, 3, 21)),
        _doc("B2", "C1", "INV-9", amount="541.25", d=date(2026, 3, 22)),
    ], None, w1)
    mr = hits[0].match_reasons
    assert mr["same_contact"] is True
    assert mr["same_amount"] is True and mr["amount"] == "541.25"
    assert mr["days_apart"] == 1
    assert mr["reference_match"] == "exact"
    assert mr["cross_contact"] is False
    assert mr["confidence"] == 1.0 and mr["tier"] == "high"   # bill: reference = its number


def test_match_reasons_no_reference_medium():
    hits = _find_duplicate_bills([
        _doc("B1", "C1", None, amount="100", d=date(2026, 1, 1)),
        _doc("B2", "C1", None, amount="100", d=date(2026, 1, 1)),
    ], None, _LOOSE)
    mr = hits[0].match_reasons
    assert mr["reference_match"] == "none"
    assert mr["days_apart"] == 0
    assert mr["tier"] == "medium" and mr["confidence"] == 0.75


def test_alias_never_merges_distinct_contacts():
    # Even when the duplicate-contacts alias claims A and B are the same party,
    # the duplicate-invoice engine IGNORES it and keys on the real ContactID, so
    # the two records stay separate and no duplicate is raised.
    alias = _build_contact_alias([["A", "B"]])
    hits = _find_duplicate_bills([_doc("1", "A", "INV-9"), _doc("2", "B", "INV-9")], alias)
    assert hits == []


def test_alias_never_merges_transitive_contacts():
    # Transitive alias (A↔B↔C) is likewise ignored: A and C are different
    # ContactIDs, so they are never paired.
    alias = _build_contact_alias([["A", "B"], ["B", "C"]])
    hits = _find_duplicate_bills([_doc("1", "A", "INV-9"), _doc("2", "C", "INV-9")], alias)
    assert hits == []


def test_cross_contact_flag_always_false():
    # Same ContactID pair → flagged, and cross_contact is always False now.
    hits = _find_duplicate_bills([_doc("1", "C1", "INV-9"), _doc("2", "C1", "INV-9")])
    assert hits and hits[0].match_reasons["cross_contact"] is False


# --- Scale: sorted sliding-window blocking (near-linear, same results) ------

def test_scale_sliding_window_only_compares_within_window():
    # 50 monthly invoices (same contact, same amount + ref) — a recurring
    # series, NONE a same-period duplicate (30-day gaps > 7-day window). Plus
    # one genuine same-day re-entry. Sliding-window blocking must: flag only the
    # same-day pair, skip the 30-day-apart pairs, and finish fast (no O(k^2)).
    base = date(2026, 1, 1)
    txns = [_doc(f"M{i}", "C1", "Sub", amount="100", d=base + timedelta(days=30 * i))
            for i in range(50)]
    txns.append(_doc("DUP", "C1", "Sub", amount="100", d=base + timedelta(days=30 * 10)))  # same day as M10
    hits = _find_duplicate_bills(txns)
    pairs = {frozenset([h.transaction_id, h.duplicate_of_transaction_id]) for h in hits}
    # Only the same-day re-entry pairs; every 30-day-apart monthly pair is
    # outside the window → never compared. (Proves sliding-window equivalence.)
    assert pairs == {frozenset(["M10", "DUP"])}


# --- "Distinct documents?" hint (different numbers AND a date gap) ----------

def test_distinct_documents_hint_on_different_numbers():
    def _inv(tid, num, d=date(2026, 1, 1)):
        return BatchTransaction(
            transaction_id=tid, date=d, description="x",
            amount=Decimal("500"), vendor_name="Acme", type="ACCREC",
            contact_id="C1", reference="Project X", invoice_number=num,
        )
    w = AuditSettings.from_config({"duplicate_days_window": 7, "duplicate_min_confidence": 0.0})
    # DIFFERENT invoice numbers + a DATE GAP → could be 2 distinct documents → hint.
    hits = _find_duplicate_bills([_inv("A", "DEP-1"), _inv("B", "BAL-1", date(2026, 1, 3))], None, w)
    assert len(hits) == 2
    assert hits[0].match_reasons["distinct_documents_possible"] is True
    assert "distinct documents" in hits[0].message
    # SAME DAY, different numbers → confident re-entry (Xero auto-renumber) → NO hint.
    same_day = _find_duplicate_bills([_inv("A", "DEP-1"), _inv("B", "BAL-1")], None, w)
    assert same_day and same_day[0].match_reasons["distinct_documents_possible"] is False
    assert "distinct documents" not in same_day[0].message
    # SAME invoice number → no hint regardless of date.
    hits2 = _find_duplicate_bills([_inv("A", "INV-9"), _inv("B", "INV-9", date(2026, 1, 3))], None, w)
    assert hits2 and hits2[0].match_reasons["distinct_documents_possible"] is False
