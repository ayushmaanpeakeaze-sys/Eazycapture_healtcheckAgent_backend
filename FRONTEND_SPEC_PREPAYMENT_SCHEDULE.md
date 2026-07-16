# Frontend Spec — Prepayment Schedule (working paper)

The amortisation working paper for the Prepayments account: every prepaid line laid
out month-by-month, its carry-forward balance at the year-end, a Total row, and a
reconciliation to the **real Xero account balance**. Backend is live and deployed.

This is **separate** from the `prepayment_review` check (which flags *expenses that
should be prepaid*). This shows the items **already in Prepayments** and reconciles
the account. Review-only — nothing is posted.

---

## 1. Endpoint

```
GET /api/v1/health/prepayment-schedule/?company_id=<id>[&year_end=YYYY-MM-DD][&months=12]
```

- `year_end` — optional; the schedule "as at" date. Omitted → derived from the org's
  financial year-end (Xero `FinancialYearEndMonth/Day`).
- `months` — optional column count (default 12, 1–24).

Auth like the other health endpoints (company scoped).

---

## 2. Response (exact shape)

```jsonc
{
  "year_end": "2027-03-31",
  "columns": ["Apr-26","May-26","Jun-26","Jul-26","Aug-26","Sep-26",
              "Oct-26","Nov-26","Dec-26","Jan-27","Feb-27","Mar-27"],
  "rows": [
    {
      "date": "2026-07-15",
      "invoice_no": "INS-1",
      "supplier": "Aviva",
      "description": "Annual Office Insurance 01 Aug 2026 - 31 Jul 2027",
      "account_code": "620",
      "account_name": "Prepayments",
      "amount": "12000.000",
      "period_start": "2026-08-01",
      "period_end": "2027-07-31",
      "total_months": 12,
      "monthly": "1000.000",
      "cells": [null,null,null,null,"1000.000","1000.000","1000.000",
                "1000.000","1000.000","1000.000","1000.000","1000.000"],
      "balance": "4000.000",          // carry-forward at year-end
      "unscheduled": false
    }
    // … one row per prepaid line
  ],
  "column_totals": ["0.000","0.000","0.000","1500.000", … ],  // per-column sum, aligns 1:1 with columns[]
  "total_balance": "…",                                        // sum of row balances
  "validation": {
    "schedule_balance": "…",           // = total_balance
    "ledger_balance": "…",             // real Prepayments balance in Xero
    "ledger_source": "xero_trial_balance" | "posted_amounts",
    "difference": "…",                 // ledger − schedule
    "reconciled": true                 // |difference| ≤ 1
  },
  "prepayment_accounts": ["620"],
  "item_count": 2
}
```

- **All money fields are strings** (exact decimals, 3 dp). Don't parse to float and
  re-round. Show 2 dp; the 3rd is precision for the arithmetic.
- `cells[]` aligns 1:1 with `columns[]`. `null` = no release that month (before the
  period starts, or after it ends). A value = that month's release.
- Values are the org's currency (single-currency assumption for the account).

---

## 3. Render — one wide table (the working paper)

Columns, left→right:

| Fixed columns | then the month grid | last |
|---|---|---|
| Date · Invoice no. · Supplier · Description · Amount · Months · Per month · Account | `columns[]` (one per month) | **Balance** |

- Map each row: `date`, `invoice_no`, `supplier`, `description`, `amount`, `total_months`,
  `monthly`, `account_code`+`account_name`, then `cells[]` (blank when `null`), then `balance`.
- **Total row** at the bottom: `column_totals[]` under each month column, `total_balance`
  under Balance.
- Right-align money; use `font-variant-numeric: tabular-nums` so columns line up.
- The whole table is wide → put it in a container with `overflow-x: auto` (the page body
  must not scroll sideways).

### Unscheduled rows (`unscheduled: true`)
No period could be read from the description → `monthly` and all `cells` are `null`, but
`balance` = the full amount (it still sits in the account). Render the row with an
"Unscheduled — add a period" hint instead of month figures. Don't hide it.

---

## 4. The validation block (below the table)

Mirror the accountant's check:

```
Schedule balance      <schedule_balance>
Xero balance          <ledger_balance>      (Prepayments account)
Difference            <difference>          ✓ if reconciled
```

- Green/✓ when `reconciled: true` (difference within rounding).
- Amber/red when `false` — a large gap means a monthly release was never booked, or a
  posting (journal / opening balance) sits in the account that isn't a bill.
- If `ledger_source === "posted_amounts"`, the org isn't connected (or the report scope
  isn't granted) — label it *"estimated from postings"* and soften the reconciled state.
  `"xero_trial_balance"` = the real balance from Xero.

---

## 5. Actions

Review-only. **No recode dropdown, no "Save changes", no "post journal".** The schedule
shows what the month-end release *would* be; the accountant confirms and posts it in Xero.
A "View in Xero" deep-link per row is fine; "Dismiss" is optional.

---

## 6. Empty / not-connected states

- `item_count: 0` → "No items in the Prepayments account for this year." (Correct, not an error.)
- No `prepayment_accounts` → the org has no account typed/named Prepayments; show a short note.

---

## Reference: a working example

`£12,000` annual insurance, period 01 Aug 2026 → 31 Jul 2027, year-end 31 Mar 2027:
`monthly £1,000`, releases Apr–Jul 2027 fall after year-end, so `balance £4,000` carries
forward. The demo of exactly this layout: the schedule grid + Total + validation block.
