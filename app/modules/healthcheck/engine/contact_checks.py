"""Contact-based health checks (pure rule logic — no DB/Celery/HTTP).

Lives in the rule-engine package alongside ``deterministic.py`` and
``llm_rules.py`` so all pure health-check logic is in one place. The
application layer (``app/modules/healthcheck/tasks.py``) calls
``run_contact_checks`` during an audit.

Three rules run against the full contacts list fetched from Xero:

  duplicate_contact   — name-similarity match (≥70%), VAT-aware confidence
  contact_defaults    — supplier/customer with no default account or tax code
  inactive_contact    — contact with no transactions in the last 180 days

All three return plain dicts suitable for storing as HealthCheckResult rows,
keyed on ContactID as the document_id.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from rapidfuzz import fuzz

from app.modules.healthcheck.engine.audit_settings import AuditSettings, DEFAULT_SETTINGS

logger = logging.getLogger("eazycapture.contact_checks")

# (inactive window is per-client: ``settings.inactive_days``, default 180)

# Company suffixes stripped before fuzzy name matching so "ABC Ltd" and
# "ABC Limited" normalize to the same token.
_COMPANY_SUFFIXES = {
    "pvt", "ltd", "limited", "private", "inc", "incorporated",
    "llc", "llp", "plc", "co", "company", "corp", "corporation", "the",
}

# Generic business-type words kept in the name but treated as stop words in the
# similarity score, so distinctive tokens drive the match and shared filler does not.
_BUSINESS_STOPWORDS = {
    "agency", "club", "group", "holdings", "services", "service",
    "solutions", "solution", "consulting", "consultancy", "consultants",
    "partners", "associates", "enterprises", "enterprise", "trading",
    "ventures", "industries",
}
# Weight of the distinctive part vs the full name; generic stop-words retain 15% influence.
_DISTINCTIVE_WEIGHT = 0.85


def _norm_contact_name(name: str) -> str:
    """Lowercase, strip punctuation, drop ONLY legal suffixes (Ltd/Limited/…).
    Generic business words are KEPT — they're down-weighted in ``_name_similarity``,
    not removed — so the original name is preserved. 'ABC Ltd' → 'abc';
    'RITE Agency' → 'rite agency'."""
    words = [
        w for w in "".join(c if c.isalnum() else " " for c in (name or "").lower()).split()
        if w and w not in _COMPANY_SUFFIXES
    ]
    return " ".join(words)


def _distinctive_name(norm: str) -> str:
    """The distinctive part of a normalised name — generic business words dropped.
    Falls back to the full name if that would leave nothing (e.g. 'The Agency')."""
    toks = [w for w in norm.split() if w not in _BUSINESS_STOPWORDS]
    return " ".join(toks) or norm


def _name_similarity(norm_a: str, norm_b: str) -> float:
    """0..1 name similarity. The DISTINCTIVE tokens drive the score; generic
    business words ('Agency'/'Club'/…) contribute only a small residual, so a
    shared filler word can't make two different names look like duplicates."""
    if not norm_a or not norm_b:
        return 0.0

    def _fz(x: str, y: str) -> float:
        return max(fuzz.ratio(x, y), fuzz.token_sort_ratio(x, y)) / 100.0

    core = _fz(_distinctive_name(norm_a), _distinctive_name(norm_b))
    full = _fz(norm_a, norm_b)
    return _DISTINCTIVE_WEIGHT * core + (1 - _DISTINCTIVE_WEIGHT) * full


# ---------------------------------------------------------------------------
# Duplicate-contact scoring (name similarity, VAT-aware confidence)
# ---------------------------------------------------------------------------
# The match is driven purely by fuzzy name similarity; a pair is a candidate at
# >= dup_contact_name_sim (default 70%). ``confidence`` then adjusts that by VAT:
# same VAT boosts, different VAT reduces, but the pair is still shown. All other
# fields are enrichment for the user, not part of the score.
_VAT_MATCH_BOOST = 0.10        # same VAT → nudge confidence up
_VAT_MISMATCH_FACTOR = 0.40    # different VAT → 95% name match ⇒ ~38% confidence


def _norm_val(v: Any) -> str:
    return str(v or "").strip().lower()


def _norm_phone(raw: str) -> str:
    """Digits only, with UK country code / leading zero normalised so
    '+44 7911 123456' and '07911 123456' compare equal (edge case 9)."""
    d = "".join(ch for ch in (raw or "") if ch.isdigit())
    if d.startswith("44") and len(d) > 10:
        d = d[2:]
    return d.lstrip("0")


def _contact_phones(c: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for p in (c.get("Phones") or []):
        d = _norm_phone(p.get("PhoneNumber") or "")
        if len(d) >= 6:
            out.add(d)
    return out


def _customer_only(c: dict[str, Any]) -> bool:
    return bool(c.get("IsCustomer")) and not c.get("IsSupplier")


def _supplier_only(c: dict[str, Any]) -> bool:
    return bool(c.get("IsSupplier")) and not c.get("IsCustomer")


def _opposite_roles(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (_customer_only(a) and _supplier_only(b)) or (
        _supplier_only(a) and _customer_only(b)
    )


def _has_address(c: dict[str, Any]) -> bool:
    """True if any Xero address on the contact has a non-empty line/city/postcode
    (Xero returns empty POBOX/STREET objects even when nothing is filled in)."""
    for a in (c.get("Addresses") or []):
        if any((a.get(k) or "").strip() for k in (
            "AddressLine1", "AddressLine2", "City", "Region", "PostalCode", "Country",
        )):
            return True
    return False


def _has_person(c: dict[str, Any]) -> bool:
    """True if a person's name is attached to the contact (the 'Person' column)."""
    return bool(
        (c.get("FirstName") or "").strip()
        or (c.get("LastName") or "").strip()
        or c.get("ContactPersons")
    )


def _name_trigrams(norm_compact: str) -> set[str]:
    """Character trigrams of the space-stripped normalised name, used as blocking
    keys. Two contacts are compared only if they share a trigram, which keeps
    candidate generation near-linear yet still catches typos — e.g.
    'espresso'/'expresso' share 'pre', 'res', 'ess', 'sso'."""
    s = norm_compact
    if len(s) < 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _contact_helper(c: dict[str, Any]) -> dict[str, Any]:
    """Enrichment columns (Invoices / Bills / Person / Email / Address /
    Telephone) so a human can pick which record to keep — we NEVER auto-merge."""
    phones = sorted(_contact_phones(c))
    return {
        "has_invoices": bool(c.get("IsCustomer")),
        "has_bills": bool(c.get("IsSupplier")),
        "has_person": _has_person(c),
        "has_email": bool((c.get("EmailAddress") or "").strip()),
        "has_address": _has_address(c),
        "has_phone": bool(phones),
        # raw values too (handy for a side panel / drill-down)
        "email": (c.get("EmailAddress") or "").strip() or None,
        "phone": next(iter(phones), None),
        "tax_number": (c.get("TaxNumber") or "").strip() or None,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_contact_checks(
    contacts: list[dict[str, Any]],
    transactions: Any,
    today: date | None = None,
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[dict[str, Any]]:
    """Return a list of flagged-contact dicts.

    Each dict has:
        contact_id   — Xero ContactID (used as document_id)
        contact_name — display name
        issue_type   — one of the three above
        severity     — "high" / "medium"
        message      — short description
        partner_id   — ContactID of the duplicate (duplicate_contact only)

    ``transactions`` is the audited transaction list (invoices/bills/credit
    notes/bank txns) — used to build the ContactID → last-activity-date map for
    the inactive check. ``settings`` carries the per-client thresholds
    (name-similarity floor, inactive window).
    """
    if not contacts:
        return []
    today = today or date.today()
    last_activity = _build_last_activity(transactions)
    flagged: list[dict[str, Any]] = []
    flagged.extend(_duplicate_contacts(contacts, settings))
    flagged.extend(_contact_defaults(contacts))
    flagged.extend(_inactive_contacts(contacts, last_activity, today, settings))
    return flagged


# ---------------------------------------------------------------------------
# Rule: duplicate contacts
# ---------------------------------------------------------------------------

def _duplicate_contacts(
    contacts: list[dict[str, Any]],
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[dict[str, Any]]:
    """Check 1 — duplicate contacts (name similarity + VAT-aware confidence).
    Runs FIRST in the audit.

    Stage 1 (blocking): bucket contacts by the trigrams of their normalised name
        so only name-similar contacts are ever compared (near-linear; still
        catches typos like 'Espresso'/'Expresso').
    Stage 2 (similarity): score each candidate pair on the fuzzy ratio of the two
        normalised names; keep pairs at or above ``dup_contact_name_sim`` (70%).
    Stage 3 (VAT-aware confidence): start from the name similarity, then BOOST it
        when both VATs match and DRASTICALLY REDUCE it when they differ — but
        still surface the pair so the user can decide. A same-name
        customer-only/supplier-only pair is shown as a low-severity 'split' note.

    We never auto-merge — each record carries `helper` enrichment columns
    (Invoices/Bills/Person/Email/Address/Telephone) so a human picks which to keep.
    """
    from collections import defaultdict

    active = [
        c for c in contacts
        if not c.get("IsArchived") and (c.get("Name") or "").strip()
    ]
    by_id: dict[str, dict[str, Any]] = {}
    norm: dict[str, str] = {}
    for c in active:
        cid = (c.get("ContactID") or "").strip()
        if not cid:
            continue
        by_id[cid] = c
        norm[cid] = _norm_contact_name(c.get("Name") or "")

    # --- Stage 1: trigram blocking → candidate pairs (no all-pairs sweep) ---
    buckets: dict[str, list[str]] = defaultdict(list)
    for cid, n in norm.items():
        for tg in _name_trigrams(n.replace(" ", "")):
            buckets[tg].append(cid)
    candidate: set[tuple[str, str]] = set()
    for ids in buckets.values():
        uids = sorted(set(ids))
        for i in range(len(uids)):
            for j in range(i + 1, len(uids)):
                candidate.add((uids[i], uids[j]))

    flagged: list[dict[str, Any]] = []

    def _emit(ca, cb, sim, conf, vat_status, is_split=False) -> None:
        pct = round(sim * 100)
        for subject, partner in ((ca, cb), (cb, ca)):
            sname = (subject.get("Name") or "").strip()
            pname = (partner.get("Name") or "").strip()
            if is_split:
                msg = (
                    f"'{sname}' has a {pct}% name match with '{pname}' but one is "
                    f"customer-only and the other supplier-only — review, do not merge."
                )
                severity = "low"
            elif vat_status == "mismatch":
                ta = (subject.get("TaxNumber") or "").strip()
                tb = (partner.get("TaxNumber") or "").strip()
                msg = (
                    f"'{sname}' looks similar to '{pname}' ({pct}% name match) but their "
                    f"VAT numbers differ ({ta} vs {tb}) — likely different businesses. Review."
                )
                severity = "low"
            else:
                tail = " Same VAT number — very likely the same contact." if vat_status == "match" else ""
                msg = f"'{sname}' may be a duplicate of '{pname}' — {pct}% name match.{tail}"
                severity = "high" if conf >= 0.85 else "medium"
            rec: dict[str, Any] = {
                "contact_id": (subject.get("ContactID") or "").strip(),
                "contact_name": sname,
                "issue_type": "duplicate_contact",
                "severity": severity,
                "message": msg[:220],
                "partner_id": (partner.get("ContactID") or "").strip(),
                "partner_name": pname,
                "confidence": round(min(1.0, conf), 2),
                "name_similarity": round(sim, 2),
                "vat_status": vat_status,
                # Enrichment for both sides so each row is self-contained and
                # renders both lines of the match without a cross-row lookup.
                "helper": _contact_helper(subject),
                "partner_helper": _contact_helper(partner),
            }
            if is_split:
                rec["is_split"] = True
            flagged.append(rec)

    # --- Stage 2 + 3: name similarity, then VAT-aware confidence ---
    name_floor = settings.dup_contact_name_sim
    for ida, idb in candidate:
        na, nb = norm[ida], norm[idb]
        if not na or not nb:
            continue
        sim = _name_similarity(na, nb)
        if sim < name_floor:
            continue
        a, b = by_id[ida], by_id[idb]
        ta, tb = _norm_val(a.get("TaxNumber")), _norm_val(b.get("TaxNumber"))
        if ta and tb and ta == tb:
            vat_status, conf = "match", min(1.0, sim + _VAT_MATCH_BOOST)
        elif ta and tb and ta != tb:
            vat_status, conf = "mismatch", sim * _VAT_MISMATCH_FACTOR
        else:
            vat_status, conf = "unknown", sim
        # Same name but intentional customer-only/supplier-only split — show as a
        # low-severity review note, never a merge candidate.
        if _opposite_roles(a, b):
            _emit(a, b, sim, conf, vat_status, is_split=True)
            continue
        _emit(a, b, sim, conf, vat_status)

    flagged.sort(key=lambda r: r.get("confidence") or 0.0, reverse=True)
    return flagged


# ---------------------------------------------------------------------------
# Rule: contact defaults missing
# ---------------------------------------------------------------------------

# The four per-contact defaults Xero exposes (read + write).
_DEFAULT_FIELD_LABELS = {
    "sales_account": "sales default account",
    "sales_tax": "sales default tax",
    "purchases_account": "purchase default account",
    "purchases_tax": "purchase default tax",
}
# Maps our snake_case keys ↔ the Xero Contact field names (for read + write).
_DEFAULT_FIELD_TO_XERO = {
    "sales_account": "SalesDefaultAccountCode",
    "sales_tax": "AccountsReceivableTaxType",
    "purchases_account": "PurchasesDefaultAccountCode",
    "purchases_tax": "AccountsPayableTaxType",
}


def extract_contact_defaults(c: dict[str, Any]) -> dict[str, str]:
    """The four Xero per-contact defaults, normalized to '' when unset."""
    return {
        key: (c.get(xero) or "").strip()
        for key, xero in _DEFAULT_FIELD_TO_XERO.items()
    }


def missing_contact_defaults(c: dict[str, Any]) -> list[str]:
    """Which of the four defaults are missing, given the contact's role(s).
    Customers need the two sales defaults; suppliers need the two purchase
    defaults (a contact can be both)."""
    defaults = extract_contact_defaults(c)
    missing: list[str] = []
    if c.get("IsCustomer"):
        missing += [k for k in ("sales_account", "sales_tax") if not defaults[k]]
    if c.get("IsSupplier"):
        missing += [k for k in ("purchases_account", "purchases_tax") if not defaults[k]]
    return missing


def _contact_defaults(contacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag contacts missing any of their applicable defaults — the four Xero
    fields: sales/purchase default **account** and default **tax**. Missing
    defaults let an incorrect account/tax slip in when invoices/bills are
    created. This is the enabler for the Unexpected-Account/Tax checks."""
    flagged: list[dict[str, Any]] = []
    for c in contacts:
        if c.get("IsArchived"):
            continue
        name = (c.get("Name") or "").strip()
        cid = c.get("ContactID", "")
        if not name or not cid:
            continue
        if not (c.get("IsCustomer") or c.get("IsSupplier")):
            continue  # neither role → no defaults apply
        missing = missing_contact_defaults(c)
        if not missing:
            continue
        labels = [_DEFAULT_FIELD_LABELS[m] for m in missing]
        flagged.append({
            "contact_id": cid,
            "contact_name": name,
            "issue_type": "contact_defaults",
            "severity": "medium",
            "message": (
                f"{name} is missing: {', '.join(labels)}. "
                f"Set defaults to ensure consistent coding."
            )[:140],
            "partner_id": None,
            # extras for the Contact Defaults screen (pre-fill + which to set)
            "missing_defaults": missing,
            "current_defaults": extract_contact_defaults(c),
            "is_customer": bool(c.get("IsCustomer")),
            "is_supplier": bool(c.get("IsSupplier")),
        })
    return flagged


# ---------------------------------------------------------------------------
# Rule: inactive contacts
# ---------------------------------------------------------------------------

def _coerce_date(raw: Any) -> date | None:
    """Parse a transaction date (ISO string 'YYYY-MM-DD' or a date) → date."""
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def _build_last_activity(transactions: Any) -> dict[str, date]:
    """ContactID → MOST RECENT transaction date across the audited transactions
    (invoices, bills, credit notes, bank txns). Accepts dicts or objects with a
    ``contact_id`` and ``date``. O(transactions), single pass."""
    out: dict[str, date] = {}
    for tx in transactions or []:
        cid = tx.get("contact_id") if isinstance(tx, dict) else getattr(tx, "contact_id", None)
        cid = (cid or "").strip()
        if not cid:
            continue
        raw = tx.get("date") if isinstance(tx, dict) else getattr(tx, "date", None)
        d = _coerce_date(raw)
        if d is None:
            continue
        if cid not in out or d > out[cid]:
            out[cid] = d
    return out


def _inactive_contacts(
    contacts: list[dict[str, Any]],
    last_activity: dict[str, date],
    today: date,
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[dict[str, Any]]:
    """Flag a contact whose MOST RECENT transaction is older than
    ``settings.inactive_days``, OR that has never transacted.

    ``last_activity`` maps ContactID → most recent transaction date. Output
    carries ``last_activity_date`` + ``age_days`` so the UI can show the
    "Most Recent Transaction Date" and "Age (Days)" columns.
    """
    threshold = settings.inactive_days
    flagged: list[dict[str, Any]] = []
    for c in contacts:
        if c.get("IsArchived"):
            continue
        name = (c.get("Name") or "").strip()
        cid = (c.get("ContactID") or "").strip()
        if not name or not cid:
            continue
        if not (c.get("IsSupplier") or c.get("IsCustomer")):
            continue
        last = last_activity.get(cid)
        if last is None:
            age_days: int | None = None
            message = f"{name} has never had a transaction — consider archiving if not needed."
        else:
            age_days = (today - last).days
            if age_days < threshold:
                continue   # used recently → not inactive
            message = (
                f"{name}'s most recent transaction was {age_days} days ago "
                f"({last.isoformat()}) — consider archiving if no longer active."
            )
        flagged.append({
            "contact_id": cid,
            "contact_name": name,
            "issue_type": "inactive_contact",
            "severity": "medium",
            "message": message[:200],
            "partner_id": None,
            # Columns: Most Recent Transaction Date + Age (Days)
            "last_activity_date": last.isoformat() if last else None,
            "age_days": age_days,
        })
    return flagged
