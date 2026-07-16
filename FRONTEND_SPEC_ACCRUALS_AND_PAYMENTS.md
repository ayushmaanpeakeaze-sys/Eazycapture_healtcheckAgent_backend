# Frontend Spec — Accruals & Unusual Payments (two new cards)

Two deterministic checks from the Missing-Accruals + Pattern SOPs. Each is **one card**
with several issue types inside (reason per item). Review-only — no recode, no posting.
Both are **separate** from the LLM `anomaly` card (that stays untouched).

| Card | issue_type | Group | Status |
|---|---|---|---|
| **Unusual payments** | `unusual_payment` | Payments & Anomalies | ✅ live (transaction-level) |
| **Accruals** | `missing_accrual` | Date & Ageing | ✅ live (account-level, `document_type: "ACCOUNT"`) |

Both appear in `GET /api/v1/health/audit-config/` and toggle via `disabled_rules[]`.

---

## 1. Unusual payments — `unusual_payment` (LIVE)

Fetch: `GET /api/v1/health/trapped-invoices/?company_id=<id>&issue_type=unusual_payment`.

Transaction-level, so each flag is a normal trapped row. `result.flagged[]` item:

```jsonc
{
  "issue_type": "unusual_payment",
  "severity": "medium",
  "message": "Rare Ltd: one-off £40000.00 payment (seen 1x this year) — confirm the nature.",
  "current_code": "400",
  "match_reasons": {
    "reason": "one_off_supplier",       // or "unclear_description"
    "supplier": "Rare Ltd",
    "date": "2026-07-15",
    "amount": "40000.00"
  }
}
```

### Render (one card, reason chip per row)
- **Title:** "Unusual payments". Badge = document count (document-level, like the others).
- Per row: supplier · date · amount · account · a **reason chip**:
  - `unclear_description` → *"No / unclear description"*
  - `one_off_supplier` → *"One-off supplier ({n}× this year)"* (n is in the message)
- **Actions:** View in Xero · Dismiss. **No recode, no Save.** It's a "confirm the nature" prompt.
- Client question: *"We noticed payments that lack a clear description or don't follow the
  normal pattern — please confirm what they relate to."*

### What it flags (and doesn't)
- Blank / generic description (`payment`, `transfer`, `online`, …) on a bill or Spend Money.
- A large one-off (default ≥ £1,000) from a supplier seen once or twice.
- **Not** here (already their own cards): vague account (`misallocated_item`), large-vs-vendor
  (`amount_outlier`), missing regular payment (→ Accruals).

---

## 2. Accruals — `missing_accrual` (LIVE)

Fetch: `GET /api/v1/health/trapped-invoices/?company_id=<id>&issue_type=missing_accrual`.

**Account-level**: the row's `document_type` is `"ACCOUNT"` and `document_id` is the Xero
AccountID (like contacts use `"CONTACT"`). One card "Accruals"; each `result.flagged[]`
item is a missing month with a reason. Group by the account row.

```jsonc
{
  "issue_type": "missing_accrual",
  "account_code": "445",
  "account_name": "Light & Heat",
  "missing_month": "Mar 2026",
  "reason": "final_month_missing",     // final_month_missing | post_year_cutoff | missing_month
  "severity": "high",                  // high for final/cutoff, medium for interim
  "post_year_payment": false,          // true when a payment appears after year-end
  "months_present": 11,                // how many of the 12 months had activity
  "avg_monthly_amount": "1000.00",     // guidance only
  "message": "Light & Heat: no cost in the final month Mar 2026 — accrual likely required."
}
```

### Render (one card, reason per item)
- **Title:** "Accruals". Per row: account · missing month · reason chip · severity · avg/month.
  - `final_month_missing` → *"Final month missing"* (high) — the year-end accrual
  - `post_year_cutoff` → *"Paid after year-end — accrue prior month"* (high)
  - `missing_month` → *"Month missing"* (medium)
- Show **avg monthly amount** as guidance (materiality), not a posting figure.
- **Actions:** View in Xero (the account) · Dismiss. **No posting** (SOP: do not auto-book).
- Client question: *"This expense usually occurs every month but is missing in {month} —
  confirm whether an accrual should be recorded."*

### Rules baked in
- Only P&L expense accounts; balance-sheet ignored.
- Only "regular" accounts (activity in ≥ 8 of 12 months) — irregular accounts aren't flagged.
- Runs on the **full financial year** (ignores the Period selector) — a whole-year analysis.

---

## Note for the API/consumer

- `unusual_payment` — transaction-level, normal trapped rows.
- `missing_accrual` — keyed on the **AccountID** with `document_type: "ACCOUNT"` (mirrors
  `"CONTACT"`). Handle it like CONTACT rows: group by the account, render the items inside.
  It runs on the whole financial year (ignores the Period selector) and re-runs on every
  audit (auto-clears accounts no longer flagged).
