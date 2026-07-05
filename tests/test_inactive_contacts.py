"""Inactive Contacts check: last-activity DATE + age threshold.

A contact is flagged when its MOST RECENT transaction is older than
``inactive_days`` (default 180), or when it has never transacted. Output carries
``last_activity_date`` + ``age_days`` for the table columns.
"""
from datetime import date

from app.modules.healthcheck.engine.audit_settings import AuditSettings
from app.modules.healthcheck.engine.contact_checks import (
    _build_last_activity,
    _inactive_contacts,
    run_contact_checks,
)

TODAY = date(2026, 6, 24)


def _c(cid, name, *, customer=True, supplier=False, archived=False):
    return {
        "ContactID": cid, "Name": name,
        "IsCustomer": customer, "IsSupplier": supplier, "IsArchived": archived,
    }


def _tx(cid, d):
    return {"contact_id": cid, "date": d}   # d is an ISO 'YYYY-MM-DD' string


# --- last-activity map ------------------------------------------------------

def test_build_last_activity_takes_most_recent():
    la = _build_last_activity([
        _tx("A", "2026-01-01"), _tx("A", "2026-03-01"), _tx("B", "2026-02-01"),
    ])
    assert la["A"] == date(2026, 3, 1)   # max for A
    assert la["B"] == date(2026, 2, 1)


def test_build_last_activity_handles_objects_and_bad_dates():
    class _T:
        def __init__(self, c, d): self.contact_id, self.date = c, d
    la = _build_last_activity([_T("A", date(2026, 5, 1)), _T("A", "bad"), _T("", "2026-01-01")])
    assert la == {"A": date(2026, 5, 1)}   # bad date ignored, blank contact ignored


# --- flagging ---------------------------------------------------------------

def test_recent_activity_not_flagged():
    la = {"A": date(2026, 6, 1)}   # 23 days ago < 180
    assert _inactive_contacts([_c("A", "Acme")], la, TODAY) == []


def test_old_activity_flagged_with_date_and_age():
    last = date(2024, 1, 10)
    hits = _inactive_contacts([_c("A", "ABC Furniture")], {"A": last}, TODAY)
    assert len(hits) == 1
    h = hits[0]
    assert h["issue_type"] == "inactive_contact"
    assert h["last_activity_date"] == "2024-01-10"
    assert h["age_days"] == (TODAY - last).days >= 180
    assert "days ago" in h["message"]


def test_never_used_flagged():
    hits = _inactive_contacts([_c("A", "Ghost Co")], {}, TODAY)
    assert len(hits) == 1
    assert hits[0]["last_activity_date"] is None
    assert hits[0]["age_days"] is None
    assert "never" in hits[0]["message"].lower()


def test_archived_skipped():
    assert _inactive_contacts([_c("A", "Acme", archived=True)], {}, TODAY) == []


def test_no_role_contact_skipped():
    assert _inactive_contacts([_c("A", "Acme", customer=False, supplier=False)], {}, TODAY) == []


def test_threshold_respected():
    la = {"A": date(2026, 3, 1)}   # ~115 days ago
    assert _inactive_contacts([_c("A", "Acme")], la, TODAY) == []           # default 180 → ok
    strict = AuditSettings.from_config({"inactive_days": 90})
    assert len(_inactive_contacts([_c("A", "Acme")], la, TODAY, strict)) == 1


def test_end_to_end_via_run_contact_checks():
    contacts = [_c("A", "Dormant Traders"), _c("B", "Busy Builders")]
    txns = [_tx("B", "2026-06-20")]   # only B has recent activity
    flags = run_contact_checks(contacts, txns, today=TODAY)
    inactive = {f["contact_name"] for f in flags if f["issue_type"] == "inactive_contact"}
    assert "Dormant Traders" in inactive       # never used → flagged
    assert "Busy Builders" not in inactive      # recent → not flagged
