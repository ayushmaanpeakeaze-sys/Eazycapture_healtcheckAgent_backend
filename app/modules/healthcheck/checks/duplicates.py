"""Duplicates check group.

``_find_duplicate_documents`` detects duplicate invoices / bills / credit notes
(it covers all three document directions). ``duplicate_contact`` is detected in
``contact_checks`` but its name-similarity setting lives in the Duplicates group,
so it is included in SETTING_FIELDS / META here.

Shared helpers (_contact_key, _is_paid) are reached via lazy proxies to avoid
the package import cycle.
"""
from __future__ import annotations

from collections import defaultdict  # noqa: F401
from statistics import median  # noqa: F401
from typing import Any, Optional  # noqa: F401

from app.modules.healthcheck.checks.base import SettingField
from app.shared.transaction import BatchTransaction, FlaggedIssue  # noqa: F401
from app.modules.healthcheck.engine.audit_settings import AuditSettings, DEFAULT_SETTINGS  # noqa: F401
from app.modules.healthcheck.engine.shared import (  # noqa: F401
    _CREDIT_DOC_TYPES,
    _PURCHASE_DOC_TYPES,
    _SALES_DOC_TYPES,
)


def _contact_key(*a, **k):
    from app.modules.healthcheck.engine.deterministic import _contact_key as _f
    return _f(*a, **k)


def _is_paid(*a, **k):
    from app.modules.healthcheck.engine.deterministic import _is_paid as _f
    return _f(*a, **k)


def _name_similarity(*a, **k):
    from app.modules.healthcheck.engine.contact_checks import _name_similarity as _f
    return _f(*a, **k)


def _norm_contact_name(*a, **k):
    from app.modules.healthcheck.engine.contact_checks import _norm_contact_name as _f
    return _f(*a, **k)


_RECUR_MIN_SERIES = 3      # need >= 3 in the series to learn a cadence


_RECUR_PAIR_GAP = 14       # lone pair (no series) this far apart → recurring review


_RECUR_REVIEW = 0.45       # capped confidence for a recurring pair


def _dup_issue_type(doc_type: str) -> str:
    if doc_type in _CREDIT_DOC_TYPES:
        return "duplicate_credit_note"
    if doc_type in _SALES_DOC_TYPES:
        return "duplicate_invoice"
    return "duplicate_bill"


def _ref_match(a: BatchTransaction, b: BatchTransaction) -> str:
    """RAW (case-sensitive, no normalization) reference compare → exact | none |
    different. ``none`` when either side has no reference."""
    ra, rb = (a.reference or "").strip(), (b.reference or "").strip()
    if not ra or not rb:
        return "none"
    return "exact" if ra == rb else "different"


def _find_duplicate_documents(
    transactions: list[BatchTransaction],
    contact_alias: Optional[dict[str, str]] = None,
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[FlaggedIssue]:
    """Per-contact, additive/rule-based duplicate detection.

    Blocks by the REAL Xero ContactID AND document family, scores each in-window
    pair on a rule waterfall (same invoice number → 1.0; recurring → 0.45;
    different amount → 0.65; different numbers same/other day → 0.95/0.70; exact
    reference → 0.90; else 0.75), and only marks ORIGINAL vs DUPLICATE for
    HIGH-tier (confirmed) pairs — weaker review pairs get just a confidence score.

    NOTE: duplicate invoices key on the actual ContactID only. Two distinct
    ContactIDs are ALWAYS treated as separate parties — the duplicate-contacts
    alias is deliberately NOT applied here, so a fuzzy contact guess can never
    manufacture a cross-contact "duplicate". ``contact_alias`` is accepted for
    call-site compatibility but intentionally ignored.
    """
    # Block by (real ContactID, document family); the contact alias is never
    # applied here, so only Xero's own ContactID groups invoices together.
    by_group: dict[tuple[str, str], list[BatchTransaction]] = defaultdict(list)
    for tx in transactions:
        doc_type = (tx.type or "").strip().upper()
        if not doc_type:
            continue
        by_group[(_contact_key(tx), doc_type)].append(tx)

    flagged: list[FlaggedIssue] = []
    for (contact_key, doc_type), group in by_group.items():
        if len(group) < 2:
            continue

        # Learn the cadence of each same-amount series (>= 3) so a recurring
        # monthly charge is told apart from a same-period double entry.
        cadence: dict[Any, float] = {}
        amount_groups: dict[Any, list[BatchTransaction]] = defaultdict(list)
        for tx in group:
            amount_groups[tx.amount].append(tx)
        for amt, txs in amount_groups.items():
            if len(txs) >= _RECUR_MIN_SERIES:
                ds = sorted(t.date for t in txs)
                gaps = [g for g in ((ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)) if g > 0]
                if gaps:
                    cadence[amt] = median(gaps)

        is_credit_pair = doc_type in _CREDIT_DOC_TYPES
        is_purchase = doc_type in _PURCHASE_DOC_TYPES
        issue_type = _dup_issue_type(doc_type)

        # Sorted sliding-window blocking: only compare pairs within the date
        # window (near-linear; identical results to all-pairs).
        group.sort(key=lambda t: t.date)
        for idx, a in enumerate(group):
            for b in group[idx + 1:]:
                days_apart = (b.date - a.date).days
                if days_apart > settings.duplicate_days_window:
                    break  # sorted → every later b is further still

                # --- gates (candidate selection) --------------------------
                same_amount = a.amount == b.amount
                if settings.duplicate_require_same_amount and not same_amount:
                    continue
                num_a, num_b = (a.invoice_number or "").strip(), (b.invoice_number or "").strip()
                same_invoice_number = bool(num_a and num_b and num_a == num_b)
                ref_match = _ref_match(a, b)
                ra, rb = (a.reference or "").strip(), (b.reference or "").strip()
                same_reference = bool(ra and rb and ra == rb)
                # Identifying number: the invoice number for sales, the supplier
                # reference for purchases. ``diff_id`` means two different documents.
                if is_purchase:
                    diff_id = bool(ra and rb and ra != rb)
                else:
                    diff_id = bool(num_a and num_b and num_a != num_b)
                # Only sales drop on a conflicting secondary reference; bills use
                # the reference as their number and are scored in the waterfall.
                if (settings.duplicate_require_exact_reference and not is_purchase
                        and ref_match == "different" and not same_invoice_number):
                    continue
                if (not settings.duplicate_also_check_paid
                        and _is_paid(a) and _is_paid(b) and not is_credit_pair):
                    continue

                # --- recurring re-prove -----------------------------------
                recurring = False
                if same_amount and not same_invoice_number:
                    cad = cadence.get(a.amount)
                    recurring = (days_apart >= cad * 0.75) if cad is not None \
                        else (days_apart >= _RECUR_PAIR_GAP)

                # --- confidence + tier (rule waterfall) -------------------
                # Strongest signal is the same identifying number: the invoice
                # number for sales, or the supplier reference for bills.
                if same_invoice_number:
                    confidence, tier = 1.0, "high"
                elif recurring:
                    confidence, tier = _RECUR_REVIEW, "low"
                elif not same_amount:
                    confidence, tier = 0.65, "medium"
                elif is_purchase and same_reference:
                    confidence, tier = 1.0, "high"      # bill: same supplier reference = same bill
                elif diff_id:
                    confidence, tier = (0.95, "high") if days_apart == 0 else (0.70, "medium")
                elif ref_match == "exact":
                    confidence, tier = 0.90, "high"     # sales: same PO reference, no invoice numbers
                else:
                    confidence, tier = 0.75, "medium"

                if confidence < settings.duplicate_min_confidence:
                    continue

                # "Could be 2 distinct documents" only applies when the identifying
                # numbers differ and there is a date gap; suppressed for recurring.
                distinct_docs_possible = diff_id and days_apart > 0 and not recurring
                # Grouped by the real ContactID, so a pair always shares one
                # contact — cross-contact pairing is no longer possible by design.
                cross_contact = False
                one_paid_one_out = _is_paid(a) != _is_paid(b)
                risk = "high" if (tier in ("high", "medium") and one_paid_one_out) else "normal"

                # --- original vs duplicate: paid → posted_date → issue date
                a_paid, b_paid = _is_paid(a), _is_paid(b)
                if a_paid != b_paid:
                    original, duplicate = (a, b) if a_paid else (b, a)
                elif a.posted_date and b.posted_date and a.posted_date != b.posted_date:
                    original, duplicate = (a, b) if a.posted_date <= b.posted_date else (b, a)
                else:
                    original, duplicate = (a, b) if a.date <= b.date else (b, a)

                currency = (a.currency_code or "GBP").strip().upper()
                symbol = "£" if currency == "GBP" else f"{currency} "

                # "why it matched" basis for the message
                parts: list[str] = []
                if same_invoice_number:
                    parts.append(f"same invoice no. {num_a}")
                elif ref_match == "exact":
                    parts.append(f"same reference '{(a.reference or '').strip()}'")
                parts.append("same amount" if same_amount else "different amount")
                parts.append("same day" if days_apart == 0 else f"{days_apart} day(s) apart")
                basis = ", ".join(parts)

                match_reasons = {
                    "same_contact": True,
                    "same_amount": same_amount,
                    "amount": f"{a.amount:.2f}",
                    "other_amount": None if same_amount else f"{b.amount:.2f}",
                    "currency": currency,
                    "days_apart": days_apart,
                    "reference_match": ref_match,
                    "same_invoice_number": same_invoice_number,
                    "distinct_documents_possible": distinct_docs_possible,
                    "cross_contact": cross_contact,
                    "confidence": confidence,
                    "review": tier != "high",
                    "tier": tier,
                    "recurring": recurring,
                    "one_paid_one_outstanding": one_paid_one_out,
                    "risk": risk,
                }

                is_confirmed_dup = tier == "high"
                pct = int(round(confidence * 100))
                severity = "high" if tier == "high" else "medium"

                for subject, partner, is_orig in (
                    (original, duplicate, True),
                    (duplicate, original, False),
                ):
                    partner_label = (
                        (partner.reference or partner.invoice_number or "").strip()
                        or partner.transaction_id
                    )
                    partner_at = f"{partner.date.isoformat()}, {symbol}{partner.amount:.2f}"
                    if is_confirmed_dup and is_orig:
                        message = (
                            f"Likely has duplicate {partner_label} ({partner_at}; "
                            f"{basis}). Likely the original — keep this; void {partner_label}."
                        )
                    elif is_confirmed_dup:
                        message = (
                            f"Likely duplicate of {partner_label} ({partner_at}; "
                            f"{basis}). Recommended: void this one, keep {partner_label}."
                        )
                    elif recurring:
                        message = (
                            f"Possible recurring charge — also {partner_label} "
                            f"({partner_at}; {basis}). Looks like a subscription "
                            f"({pct}% match); please review — likely a legitimate "
                            f"recurring charge, not a duplicate."
                        )
                    else:
                        message = (
                            f"Possible match ({pct}%) with {partner_label} "
                            f"({partner_at}; {basis}). Please review — not a confirmed "
                            f"duplicate; keep both unless you confirm a true copy."
                        )
                    if distinct_docs_possible:
                        message += " Different invoice numbers — could be 2 distinct documents. Please check."
                    flagged.append(FlaggedIssue(
                        transaction_id=subject.transaction_id,
                        issue_type=issue_type,
                        severity=severity,
                        message=message[:280],
                        confidence=confidence,
                        duplicate_of_transaction_id=partner.transaction_id,
                        duplicate_of_invoice_number=(partner.invoice_number or "").strip() or None,
                        duplicate_of_date=partner.date,
                        this_is_likely_original=(is_orig if is_confirmed_dup else None),
                        match_reasons=match_reasons,
                    ))

    flagged.sort(key=lambda f: f.confidence or 0.0, reverse=True)
    return flagged


# Cross-contact confidence is additive and HONEST: the weights sum to 100 at a
# perfect match, so the score reflects exactly which signals lined up — no cap
# that would fake a 100%. A sales invoice's identifier (its invoice number) is
# auto-numbered fresh by Xero, so it ALWAYS differs across contacts; a
# cross-contact SALES duplicate therefore tops out ~80%. A BILL's identifier is
# the reused supplier reference, so a bill can reach ~95%. That inherent ceiling
# is why the flag bar below is review-tier, not the same-contact 90%.
_CROSS_AMOUNT_POINTS = 20    # same amount — the blocking anchor, always present
_CROSS_NAME_WEIGHT = 30      # max points from the party signal (VAT or name similarity)
_CROSS_SAMEDAY_POINTS = 15
_CROSS_WINDOW_POINTS = 8      # within the date window but not same day
# The document's identifying number is the invoice number for a sales invoice
# but the supplier REFERENCE for a bill (bills carry no number of their own).
# Matching it is the strong "same document" signal; the other field is weak.
_CROSS_IDENTIFIER_POINTS = 20
_CROSS_DESCRIPTION_POINTS = 10
_CROSS_SECONDARY_POINTS = 5
# Party gate: with no VAT to compare, the two contacts must be at least this
# name-similar to be plausibly the SAME supplier under two records. Below it they
# are treated as different parties and skipped OUTRIGHT — the clean cut that keeps
# a low-name pair (Hamilton Smith Ltd vs Rex Media Group, 26%) out no matter how
# much of their content coincides. A touch below the duplicate-contacts bar (70%)
# because a cross-contact pair must ALSO clear the confidence bar below.
_CROSS_NAME_MIN = 0.60
# Review-tier confidence bar. Cross-contact can't use the unique invoice-number
# match that lifts a same-contact duplicate to 90%+, so it flags lower: enough to
# surface a name-similar + content-matching pair (e.g. ~74%) while a bare
# same-amount coincidence stays under.
_CROSS_MIN_CONFIDENCE = 0.60
# Shown verbatim on every cross-contact flag (also in match_reasons.advisory) so a
# match always reads as "check this", never a confirmed duplicate.
_CROSS_ADVISORY = (
    "These may be the same supplier entered twice under two contact records — "
    "verify in Xero before treating either as a duplicate."
)


def _find_cross_contact_duplicates(
    transactions: list[BatchTransaction],
    contact_vat: Optional[dict[str, str]] = None,
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[FlaggedIssue]:
    """Content-based duplicate detection ACROSS different contacts.

    Complements ``_find_duplicate_documents`` (per-ContactID, left untouched). The same
    document recorded under two contact records — a supplier saved twice as
    "Peakvisory" and "Peakvisory Limited" — never shares a ContactID, so the
    per-contact pass can't see it.

    Blocks by (document type, amount) so only same-total documents are compared
    (near-linear; contact count is irrelevant). Amount is the anchor here, so the
    per-contact ``duplicate_require_same_amount`` toggle does not apply — without a
    contact anchor there is nothing else to bound the comparison.

    Two gates, both at 60% (review tier — a cross-contact match can never use the
    unique invoice-number match that confirms a same-contact duplicate):

    1. PARTY gate — are the two contacts the same supplier? VAT decides when BOTH
       have one (same VAT confirms; different VAT means different companies →
       skip). With no VAT it falls back to name similarity, which must clear
       ``_CROSS_NAME_MIN`` (0.60) or the pair is treated as different parties and
       skipped OUTRIGHT — content never overrides this (so Hamilton Smith Ltd vs
       Rex Media Group at 26% stays out however much lines up).
    2. CONFIDENCE gate — among same-party pairs, an honest additive score (party
       strength + same date + reference + invoice number + description; weights
       sum to 100, no fake cap) must clear ``_CROSS_MIN_CONFIDENCE`` (0.60), so a
       bare same-amount coincidence stays under.

    Always review tier with ``cross_contact`` set and an ``advisory`` telling the
    user to verify in Xero.
    """
    vat_map = contact_vat or {}
    by_amount: dict[tuple[str, Any], list[BatchTransaction]] = defaultdict(list)
    for tx in transactions:
        doc_type = (tx.type or "").strip().upper()
        if not doc_type or tx.amount is None:
            continue
        by_amount[(doc_type, tx.amount)].append(tx)

    flagged: list[FlaggedIssue] = []
    for (doc_type, amount), group in by_amount.items():
        if len(group) < 2:
            continue
        is_credit_pair = doc_type in _CREDIT_DOC_TYPES
        is_purchase = doc_type in _PURCHASE_DOC_TYPES
        issue_type = _dup_issue_type(doc_type)
        group.sort(key=lambda t: t.date)
        for idx, a in enumerate(group):
            for b in group[idx + 1:]:
                days_apart = (b.date - a.date).days
                if days_apart > settings.duplicate_days_window:
                    break  # sorted → every later b is further still
                # Same-contact pairs belong to _find_duplicate_documents.
                if _contact_key(a) == _contact_key(b):
                    continue
                if (not settings.duplicate_also_check_paid
                        and _is_paid(a) and _is_paid(b) and not is_credit_pair):
                    continue

                # --- party signal: VAT decides when both have it, else name ---
                vat_a = (vat_map.get((a.contact_id or "").strip()) or "").strip()
                vat_b = (vat_map.get((b.contact_id or "").strip()) or "").strip()
                if vat_a and vat_b:
                    if vat_a != vat_b:
                        continue  # different VAT → different companies → skip
                    name_sim = 1.0            # same VAT → certainly the same party
                    party_by = "vat"
                else:
                    name_sim = _name_similarity(
                        _norm_contact_name(a.vendor_name or ""),
                        _norm_contact_name(b.vendor_name or ""),
                    )
                    party_by = "name"
                    # Party gate: names too different to be the same supplier under
                    # two records → skip outright, no matter how much content lines
                    # up. VAT-confirmed pairs above bypass this by design.
                    if name_sim < _CROSS_NAME_MIN:
                        continue

                # --- multi-signal score: no single signal decides ---
                ra, rb = (a.reference or "").strip(), (b.reference or "").strip()
                same_reference = bool(ra and rb and ra == rb)
                na, nb = (a.invoice_number or "").strip(), (b.invoice_number or "").strip()
                same_invoice_number = bool(na and nb and na == nb)
                da, db = (a.description or "").strip().lower(), (b.description or "").strip().lower()
                same_description = bool(da and db and da == db)
                # A bill's identifying number IS its reference; a sales invoice's is
                # its invoice number. Matching that is the strong "same document"
                # signal; the other field is weak corroboration.
                if is_purchase:
                    same_identifier, same_secondary = same_reference, same_invoice_number
                else:
                    same_identifier, same_secondary = same_invoice_number, same_reference

                # Name is scored at full float precision (not per-signal rounded),
                # so 91% vs 92% name shows in the final number; only the total is
                # rounded, for display. The weights sum to 100, so no fake cap.
                points = float(_CROSS_AMOUNT_POINTS)
                points += name_sim * _CROSS_NAME_WEIGHT
                points += _CROSS_SAMEDAY_POINTS if days_apart == 0 else _CROSS_WINDOW_POINTS
                if same_identifier:
                    points += _CROSS_IDENTIFIER_POINTS
                if same_secondary:
                    points += _CROSS_SECONDARY_POINTS
                if same_description:
                    points += _CROSS_DESCRIPTION_POINTS
                confidence = round(min(points, 100.0) / 100.0, 2)
                if confidence < _CROSS_MIN_CONFIDENCE:
                    continue

                one_paid_one_out = _is_paid(a) != _is_paid(b)
                currency = (a.currency_code or "GBP").strip().upper()
                symbol = "£" if currency == "GBP" else f"{currency} "
                match_reasons = {
                    "same_contact": False,
                    "cross_contact": True,
                    "same_amount": True,
                    "amount": f"{amount:.2f}",
                    "currency": currency,
                    "days_apart": days_apart,
                    "party_by": party_by,
                    "name_similarity": round(name_sim, 2),
                    "same_reference": same_reference,
                    "reference_match": _ref_match(a, b),
                    "same_invoice_number": same_invoice_number,
                    "same_description": same_description,
                    "confidence": confidence,
                    "review": True,
                    "tier": "review",
                    "one_paid_one_outstanding": one_paid_one_out,
                    "risk": "high" if one_paid_one_out else "normal",
                    "advisory": _CROSS_ADVISORY,
                }
                # Spell out what lined up and what didn't, plus how the two contacts
                # were matched — so a weak match reads as "check this", not a
                # confirmed duplicate. The granular flags also live in match_reasons.
                matched = ["amount"]
                if days_apart == 0:
                    matched.append("date")
                if same_reference:
                    matched.append("reference")
                if same_invoice_number:
                    matched.append("invoice number")
                if same_description:
                    matched.append("description")
                missed = []
                if days_apart != 0:
                    missed.append("date")
                if not same_reference:
                    missed.append("reference")
                if not same_invoice_number:
                    missed.append("invoice number")
                if not same_description:
                    missed.append("description")
                party_note = (
                    "Matched by VAT."
                    if party_by == "vat"
                    else f"Names {int(round(name_sim * 100))}% similar."
                )
                breakdown = f"Matched: {', '.join(matched)}."
                if missed:
                    breakdown += f" Not matched: {', '.join(missed)}."
                pct_conf = int(round(confidence * 100))
                # Which is the likely original: a PAID copy is already actioned, so
                # the UNPAID one is the risky duplicate; else the earlier-dated one.
                # Mirrors the per-contact pass so the UI orders/labels them the same.
                a_paid, b_paid = _is_paid(a), _is_paid(b)
                if a_paid != b_paid:
                    original, duplicate = (a, b) if a_paid else (b, a)
                elif a.posted_date and b.posted_date and a.posted_date != b.posted_date:
                    original, duplicate = (a, b) if a.posted_date <= b.posted_date else (b, a)
                else:
                    original, duplicate = (a, b) if a.date <= b.date else (b, a)
                for subject, partner, is_orig in (
                    (original, duplicate, True),
                    (duplicate, original, False),
                ):
                    this_c = (subject.vendor_name or "").strip() or "this contact"
                    other_c = (partner.vendor_name or "").strip() or "another contact"
                    message = (
                        f"Possible duplicate ({pct_conf}%) — '{this_c}' and "
                        f"'{other_c}' ({symbol}{amount:.2f}). {breakdown} {party_note} "
                        f"{_CROSS_ADVISORY}"
                    )
                    flagged.append(FlaggedIssue(
                        transaction_id=subject.transaction_id,
                        issue_type=issue_type,
                        severity="medium",
                        message=message[:340],
                        confidence=confidence,
                        duplicate_of_transaction_id=partner.transaction_id,
                        duplicate_of_invoice_number=(partner.invoice_number or "").strip() or None,
                        duplicate_of_date=partner.date,
                        this_is_likely_original=is_orig,
                        match_reasons=match_reasons,
                    ))

    flagged.sort(key=lambda f: f.confidence or 0.0, reverse=True)
    return flagged


# --- settings + registry -----------------------------------------------------
SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField("duplicate_days_window", "Duplicates", "duplicate_invoice",
                 "Date within", "int",
                 "How many days apart the two documents may be dated. 0 (default) "
                 "= same issue date only; increase to 1, 2, … to allow that many "
                 "days apart.",
                 unit="days", min=0, max=365, step=1),
    SettingField("duplicate_require_exact_reference", "Duplicates", "duplicate_invoice",
                 "Require exact reference", "bool",
                 "Both must share the same invoice reference (RAW exact, case-sensitive)."),
    SettingField("duplicate_require_same_amount", "Duplicates", "duplicate_invoice",
                 "Require same amount", "bool",
                 "Both must have the same total value."),
    SettingField("duplicate_also_check_paid", "Duplicates", "duplicate_invoice",
                 "Also check paid invoices", "bool",
                 "Include already-paid invoices in matching."),
    SettingField("duplicate_min_confidence", "Duplicates", "duplicate_invoice",
                 "Confidence", "percent",
                 "How sure we are it's a duplicate. Keep at 90% — it shows "
                 "duplicates precisely (only near-certain matches). Lower it only "
                 "to review weaker possible matches.",
                 min=0, max=1, step=0.05),
    SettingField("dup_contact_name_sim", "Duplicates", "duplicate_contact",
                 "Minimum name similarity", "percent",
                 "How similar two contact NAMES must be to flag a possible "
                 "duplicate (default 70%). Raise it to surface only very "
                 "close matches; lower it to catch looser ones. Matching is "
                 "name-only — VAT / email / phone are shown to help you decide, "
                 "not used to match.",
                 min=0, max=1, step=0.05),
)

META: tuple[tuple[str, str, bool], ...] = (
    ("duplicate_invoice", "Duplicate invoices", True),
    ("duplicate_bill", "Duplicate bills", True),
    ("duplicate_credit_note", "Duplicate credit notes", True),
    ("duplicate_contact", "Duplicate contacts", True),
)
