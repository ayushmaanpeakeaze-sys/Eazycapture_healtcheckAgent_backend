# Frontend Spec — Payment Anomalies & Accruals

Two deterministic checks from the **Pattern & Anomaly** and **Missing-Accruals** SOPs.
Each is **one card** with several reasons inside (one reason per flagged item). Both are
**review-only** — no recode, no Save, no posting. Both are **separate** from the LLM
`anomaly` card and the vendor-based `amount_outlier` card (those stay untouched).

| Card | issue_type | Group | Level | Status |
|---|---|---|---|---|
| **Payment Anomalies** | `unusual_payment` | Payments & Anomalies | transaction | ✅ live |
| **Accruals** | `missing_accrual` | Date & Ageing | **account** (`document_type: "ACCOUNT"`) | ✅ live |

> Note: the internal `issue_type` stays `unusual_payment`; the **display name is
> "Payment Anomalies"**. Both keys appear in `GET /api/v1/health/audit-config/` and toggle
> via `disabled_rules[]`.

---

## 1. Payment Anomalies — `unusual_payment`

Fetch: `GET /api/v1/health/trapped-invoices/?company_id=<id>&issue_type=unusual_payment`.

Transaction-level — each flag is a normal trapped row. Runs over bills (ACCPAY) and
Spend Money (SPEND). The reason lives in `match_reasons.reason`.

```jsonc
{
  "issue_type": "unusual_payment",
  "severity": "medium",
  "message": "Big Co: £5000.00 to 500 is 50.0x the usual £100.00 for that account — verify.",
  "current_code": "500",
  "match_reasons": {
    "reason": "large_vs_account",   // one of the four below
    "supplier": "Big Co",
    "date": "2026-07-15",
    "amount": "5000.00",
    "account": "500",               // large_vs_account only
    "usual": "100.00",              // large_vs_account only
    "ratio": "50.0"                 // large_vs_account only
  }
}
```

### The four reasons (chip per row)
| reason | chip label | SOP rule | when |
|---|---|---|---|
| `unclear_description` | *"No / unclear description"* | R1 | recurring supplier, blank/generic description |
| `unidentified_supplier` | *"One-off supplier, no clear description"* | R8 | supplier seen ≤ 2×, unclear description — **any amount** |
| `one_off_supplier` | *"One-off supplier ({n}× this year)"* | R4 | large one-off (≥ £1,000), clear description; `n` is in the message |
| `large_vs_account` | *"{ratio}× the usual for account {account}"* | R3 | line ≥ £1,000 **and** ≥ 3× the account's median; `ratio`/`account`/`usual` in `match_reasons` |

### Render
- **Title:** "Payment Anomalies". Badge = **document count** (document-level, like the other cards).
- Per row: supplier · date · amount · account · a **reason chip** (table above).
- **Actions:** View in Xero · Dismiss. **No recode, no Save** — it is a "confirm the nature" prompt.
- Client question: *"We noticed payments that lack a clear description or don't follow the
  normal pattern — please confirm what they relate to."*

### What it flags (and doesn't)
- ✅ R1 blank/generic description (`payment`, `transfer`, `online`, …) on a bill or Spend Money.
- ✅ R8 one-off supplier (≤ 2×) with no clear description — flagged at **any** amount.
- ✅ R4 a large one-off (≥ £1,000) from a supplier seen once or twice.
- ✅ R3 a line **far above the account's usual** (≥ £1,000 **and** ≥ 3× the account median).
- One row per transaction (R1/R8/R4 take priority; R3 only if the row isn't already flagged).
- ❌ **Not** here (own cards): vague account (`misallocated_item`), large-vs-**vendor**
  (`amount_outlier`), LLM `anomaly`, missing regular payment (→ Accruals).

---

## 2. Accruals — `missing_accrual`

Fetch: `GET /api/v1/health/trapped-invoices/?company_id=<id>&issue_type=missing_accrual`.

**Account-level**: the row's `document_type` is `"ACCOUNT"` and `document_id` is the Xero
AccountID (like contacts use `"CONTACT"`). One card "Accruals"; each `result.flagged[]`
item is a missing month with a reason. Group the items by the account row.

```jsonc
{
  "issue_type": "missing_accrual",
  "account_code": "445",
  "account_name": "Light & Heat",
  "missing_month": "Mar 2026",       // or a range, e.g. "Sep 2025 – Nov 2025" for large_gap
  "reason": "final_month_missing",   // final_month_missing | post_year_cutoff | large_gap | missing_month
  "severity": "high",                // high for final/cutoff/large_gap, medium for a lone interim month
  "post_year_payment": false,        // true only when a real payment appears after year-end
  "months_present": 11,              // how many of the 12 months had a real expense
  "avg_monthly_amount": "1000.00",   // guidance only (materiality) — never a posting figure
  "message": "Light & Heat: no cost in the final month Mar 2026 — accrual likely required."
}
```

### The four reasons (chip per item)
| reason | chip label | severity | meaning |
|---|---|---|---|
| `final_month_missing` | *"Final month missing"* | high | no cost in the last month of the year — the year-end accrual |
| `post_year_cutoff` | *"Paid after year-end — accrue prior month"* | high | final month empty **and** a real payment appears in the first month after year-end |
| `large_gap` | *"Irregular timing — {range} missing"* | high | 2+ **consecutive** interior months missing (one flag for the run) |
| `missing_month` | *"Month missing"* | medium | a single interior month missing |

### Render
- **Title:** "Accruals". Per row: account · missing month (or range) · reason chip · severity · avg/month.
- Show **avg monthly amount** as guidance (materiality), not a posting figure.
- **Actions:** View in Xero (the account) · Dismiss. **No posting** (SOP: do not auto-book).
- Client question: *"This expense usually occurs every month but is missing in {month} —
  confirm whether an accrual should be recorded."*

### Rules baked in (SOP-faithful)
- Only **P&L expense** accounts (EXPENSE / OVERHEADS / DIRECTCOSTS); balance-sheet ignored.
- Only **"regular"** accounts — a real expense in **≥ 8 of 12** months.
- **Only positive expense postings (debits) count** as monthly activity — **opening reversals
  and other reversing credits are ignored** (per the SOP), so a reversal never masks a genuine
  empty month, inflates "regular", or triggers a false post-year cutoff.
- Runs on the **full financial year** (ignores the frontend Period selector) — a whole-year analysis.

---

## Notes for the API / consumer
- `unusual_payment` — transaction-level, normal trapped rows; badge is the document count.
- `missing_accrual` — keyed on the **AccountID** with `document_type: "ACCOUNT"` (mirrors
  `"CONTACT"`). Handle it like CONTACT rows: group by the account, render the items inside.
  It runs on the whole financial year and re-runs on every audit (auto-clears accounts no
  longer flagged, and preserves resolved / dismissed / marked-OK states).
