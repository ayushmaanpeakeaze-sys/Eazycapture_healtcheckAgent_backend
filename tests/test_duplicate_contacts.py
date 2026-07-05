"""Duplicate-contacts check: name similarity + VAT-aware confidence.

The MATCH is driven purely by the normalised contact NAME (≥70% similarity).
``confidence`` then adjusts for tax/VAT — boosted when both VATs agree, drastically
reduced (but still shown) when they differ. tax/bank/email/phone/address/person are
ENRICHMENT columns only, never part of the score. Nothing is ever auto-merged.
"""
from app.modules.healthcheck.engine.audit_settings import AuditSettings
from app.modules.healthcheck.engine.contact_checks import _duplicate_contacts


def _c(cid, name, *, customer=True, supplier=False, tax=None, email=None,
       phone=None, bank=None, archived=False, first=None, last=None, address=None):
    return {
        "ContactID": cid,
        "Name": name,
        "IsCustomer": customer,
        "IsSupplier": supplier,
        "IsArchived": archived,
        "TaxNumber": tax,
        "EmailAddress": email,
        "BankAccountDetails": bank,
        "Phones": [{"PhoneNumber": phone}] if phone else [],
        "Addresses": ([{"AddressLine1": address}] if address else []),
        "FirstName": first,
        "LastName": last,
    }


def _pairs(flags):
    """Set of frozenset({a,b}) for the flagged (subject, partner) records."""
    return {frozenset((f["contact_id"], f["partner_id"])) for f in flags}


# --- Worked examples --------------------------------------------------------

def test_abc_furniture_suffix_match_100():
    flags = _duplicate_contacts([
        _c("A", "ABC Furniture"),
        _c("B", "ABC Furniture Limited"),
    ])
    assert _pairs(flags) == {frozenset(("A", "B"))}
    # 'Limited' is stripped → identical names → 100%.
    assert flags[0]["name_similarity"] == 1.0
    assert all(f["severity"] == "high" for f in flags)
    # emitted for BOTH contacts (subject + partner swap)
    assert {f["contact_id"] for f in flags} == {"A", "B"}


def test_basket_case_fuzzy_96():
    flags = _duplicate_contacts([_c("A", "Basket Case"), _c("B", "Basket Cased")])
    assert _pairs(flags) == {frozenset(("A", "B"))}
    assert flags[0]["name_similarity"] >= 0.90


def test_espresso_typo_caught_across_first_letter():
    # 'Espresso'/'Expresso' differ at the 2nd char — first-token blocking would
    # MISS this; trigram blocking catches it (shared 'pre','res','ess','sso').
    flags = _duplicate_contacts([_c("A", "Espresso 31"), _c("B", "Expresso 31 Ltd")])
    assert _pairs(flags) == {frozenset(("A", "B"))}
    assert flags[0]["name_similarity"] >= 0.85


def test_dissimilar_names_not_flagged():
    flags = _duplicate_contacts([_c("A", "Microsoft Ltd"), _c("B", "ABC Furniture")])
    assert flags == []


# --- generic business words must NOT inflate the score ----------------------

def test_generic_word_does_not_inflate_score():
    # 'RITE Agency' vs 'City Agency' share " agency" → ~82% on the whole name,
    # but only 'rite' vs 'city' is distinctive (~50%). Dropping the generic word
    # means these are NOT flagged. Same for '…Club', '…Group', etc.
    flags = _duplicate_contacts([
        _c("A", "RITE Agency"), _c("B", "City Agency"),
        _c("C", "SMART Agency"),
        _c("D", "Eastside Club"), _c("E", "Bayside Club"),
    ])
    assert flags == []


def test_real_duplicate_with_generic_word_still_flagged():
    # Distinctive part is identical → still a duplicate, generic word or not.
    flags = _duplicate_contacts([_c("A", "Acme Agency"), _c("B", "Acme Agency Ltd")])
    assert {f["contact_id"] for f in flags} == {"A", "B"}
    assert flags[0]["name_similarity"] == 1.0


def test_all_generic_name_is_not_emptied():
    # Guard: a contact literally called "The Agency" keeps 'agency' rather than
    # normalising to "" — so true duplicates of it still match.
    flags = _duplicate_contacts([_c("A", "The Agency"), _c("B", "The Agency Limited")])
    assert {f["contact_id"] for f in flags} == {"A", "B"}


# --- VAT-aware confidence ---------------------------------------------------

def test_vat_mismatch_drops_confidence_but_still_shows():
    flags = _duplicate_contacts([
        _c("A", "ABC Furniture", tax="GB123456"),
        _c("B", "ABC Furniture", tax="GB999999"),
    ])
    # Still SHOWN (user decides) — but flagged as a likely different entity.
    assert _pairs(flags) == {frozenset(("A", "B"))}
    f = flags[0]
    assert f["name_similarity"] == 1.0
    assert f["vat_status"] == "mismatch"
    assert f["confidence"] < 0.5            # drastically reduced from 1.0
    assert f["severity"] == "low"
    assert "VAT numbers differ" in f["message"]


def test_vat_match_boosts_confidence():
    flags = _duplicate_contacts([
        _c("A", "Basket Case", tax="GB123456"),
        _c("B", "Basket Cased", tax="GB123456"),
    ])
    f = flags[0]
    assert f["vat_status"] == "match"
    assert f["confidence"] >= f["name_similarity"]   # boosted
    assert f["severity"] == "high"
    assert "Same VAT number" in f["message"]


def test_no_vat_uses_name_similarity_as_confidence():
    flags = _duplicate_contacts([_c("A", "ABC Furniture"), _c("B", "ABC Furniture Ltd")])
    f = flags[0]
    assert f["vat_status"] == "unknown"
    assert f["confidence"] == f["name_similarity"] == 1.0


# --- customer/supplier split -------------------------------------------------

def test_customer_supplier_split_is_low_review_note():
    flags = _duplicate_contacts([
        _c("A", "Hamilton Smith", customer=True, supplier=False),
        _c("B", "Hamilton Smith", customer=False, supplier=True),
    ])
    assert _pairs(flags) == {frozenset(("A", "B"))}
    assert all(f.get("is_split") for f in flags)
    assert all(f["severity"] == "low" for f in flags)
    assert all("do not merge" in f["message"] for f in flags)


# --- threshold + enrichment + housekeeping ----------------------------------

def test_name_similarity_threshold_respected():
    strict = AuditSettings.from_config({"dup_contact_name_sim": 0.95})
    # 91% typo pair drops out when the floor is raised to 95%.
    flags = _duplicate_contacts([_c("A", "Espresso 31"), _c("B", "Expresso 31")], strict)
    assert flags == []


def test_archived_contacts_ignored():
    flags = _duplicate_contacts([
        _c("A", "ABC Furniture"),
        _c("B", "ABC Furniture Limited", archived=True),
    ])
    assert flags == []


def test_enrichment_helper_columns_present():
    flags = _duplicate_contacts([
        _c("A", "ABC Furniture", supplier=True, email="a@abc.com",
           phone="07911 123456", first="Jane", address="1 High St"),
        _c("B", "ABC Furniture Limited"),
    ])
    a = next(f for f in flags if f["contact_id"] == "A")
    h = a["helper"]
    for col in ("has_invoices", "has_bills", "has_person", "has_email",
                "has_address", "has_phone"):
        assert col in h
    assert h["has_invoices"] and h["has_bills"] and h["has_person"]
    assert h["has_email"] and h["has_address"] and h["has_phone"]
    assert h["email"] == "a@abc.com"


def test_row_carries_partner_helper_too():
    # Each row is self-contained: it carries the partner's enrichment as well,
    # so the UI renders BOTH lines of the match from one row (no "not in feed").
    flags = _duplicate_contacts([
        _c("A", "Ronny", customer=True, supplier=False),
        _c("B", "Ronny Agency", customer=True, supplier=True, email="b@x.com"),
    ])
    a = next(f for f in flags if f["contact_id"] == "A")
    assert a["partner_helper"]["has_bills"] is True       # B is a supplier
    assert a["partner_helper"]["has_email"] is True        # B has an email
    assert a["helper"]["has_bills"] is False               # A is not a supplier
    # and the mirror row carries A's helper as ITS partner
    b = next(f for f in flags if f["contact_id"] == "B")
    assert b["partner_helper"]["has_bills"] is False


def test_results_sorted_by_confidence_desc():
    flags = _duplicate_contacts([
        _c("A", "ABC Furniture"), _c("B", "ABC Furniture Ltd"),   # 100%
        _c("C", "Basket Case"), _c("D", "Basket Cased"),          # ~96%
    ])
    confs = [f["confidence"] for f in flags]
    assert confs == sorted(confs, reverse=True)
