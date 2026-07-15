# Frontend Spec — Capital Item Review & Prepayment Review

Both checks are **live and deployed**, and both are verified against their written SOPs
(91/91 rules). Every payload below is copied from the real check output — nothing here
is aspirational.

They look similar in the feed but behave **differently**. The one thing to get right:

> **Capital gets a recode dropdown. Prepayment does not.**

---

## 0. The two rules in one line each

| Check | Question it answers | SOP one-liner |
|-------|--------------------|---------------|
| `capital_item_review` | **What** is it? (an asset?) | P&L expense over £500 whose description / supplier looks like an asset → maybe capitalise |
| `prepayment_review` | **How long** is the period? (past year-end?) | P&L expense over £250 whose description shows a period running past the year-end → the future portion may be a prepayment |

Both are **review-only**: never auto-post, never auto-create, never auto-capitalise.

---

## 1. Where they live (Audit Configuration)

`GET /api/v1/health/audit-config/?company_id=<id>` → `groups[]`

| group | key | label |
|-------|-----|-------|
| Fixed Assets | `capital_item_review` | Capital item review |
| Fixed Assets | `low_cost_fixed_asset` | Low-cost fixed asset |
| Date & Ageing | `prepayment_review` | Prepayment review |

Toggle → add/remove the key in `disabled_rules[]` → `PUT /api/v1/health/audit-config/`.

### Settings (gear) — from the same response's `settings_schema[]`

| check | key | label | type | default | min | max | step |
|-------|-----|-------|------|---------|-----|-----|------|
| capital_item_review | `capital_item_threshold` | Flag expense over … | amount | **500** | 0 | — | 100 |
| capital_item_review | `capital_monitored_accounts` | Monitored expense accounts | list | — | — | — | — |
| low_cost_fixed_asset | `low_cost_asset_max` | (low-cost asset ceiling) | amount | 10000 | 0 | — | 100 |
| prepayment_review | `prepayment_min_amount` | Review expense over … | amount | **250** | 0 | — | 50 |
| prepayment_review | `financial_year_end_month` | Financial year-end month | int | 12 | 1 | 12 | 1 |
| prepayment_review | `financial_year_end_day` | Financial year-end day | int | 31 | 1 | 31 | 1 |

- Thresholds are **fully configurable** — `min=0` and **no max**, so the user can go below
  or above the default.
- **Year-end hint to show:** *"Taken from your Xero organisation automatically. Only set
  these to override it."* The audit reads `FinancialYearEndMonth` / `FinancialYearEndDay`
  from the connected org — the two fields are an override, not a setup step.
- The label says **"Flag expense over …"** and the rule is **strictly above** the amount —
  keep the wording consistent if you change the copy.

---

## 2. Fetching

```
GET /api/v1/health/trapped-invoices/?company_id=<id>&issue_type=capital_item_review
GET /api/v1/health/trapped-invoices/?company_id=<id>&issue_type=prepayment_review
```
Response: `{ results: [...], total, limit, offset }`.

> Use the **server-side `issue_type` filter**. Don't fetch page 1 and filter client-side —
> medium-severity items beyond the first page silently disappear.

**Badge counts are document-level** (`/stats/` → `by_issue_type[].count`), matching the
row count in the list. A bill with two flagged lines is **one** document in both.

---

## 3. Payload — Capital item review

`message` (ready to display):
```
ford: £10000.00 on expense account 449 (Motor Vehicle Expenses) — description mentions
'motor vehicle'; supplier 'ford' commonly sells assets. Review whether to capitalise as
a fixed asset instead of expensing.
```

`severity`: `medium` · `suggested_code`: **`null`**

```jsonc
{
  "line_no": 1,
  "transaction_date": "2026-07-14",
  "supplier": "ford",
  "description": "motor vehicle",
  "account_code": "449",
  "account_name": "Motor Vehicle Expenses",
  "current_account_type": "EXPENSE",
  "line_amount": "10000.00",
  "threshold": "500.00",
  "currency": "GBP",
  "matched_keyword": "motor vehicle",     // why it fired (may be null)
  "matched_supplier": "ford",             // why it fired (may be null)
  "monitored_account": false,             // why it fired
  "recommended_action": "capitalise",
  "recode_to_account_type": "FIXED"       // ← drives the dropdown, see §5
}
```

At least one of `matched_keyword` / `matched_supplier` / `monitored_account` is always
truthy — that's the "why". Show them as chips.

---

## 4. Payload — Prepayment review

`message` (ready to display):
```
Rare Insurance Ltd: £12000.00 on 433 (Insurance) covers a period to 31 Jul 2027, beyond
the 31 Mar 2027 year-end — ~4 month(s) (~£4000.00) may be a prepayment.
```

`severity`: `medium` · `suggested_code`: **`null`** · **no `recode_to_account_type`**

```jsonc
{
  "line_no": 1,
  "transaction_date": "2026-07-15",
  "description": "Annual Office Insurance 01 Aug 2026 - 31 Jul 2027",
  "account_code": "433",
  "account_name": "Insurance",
  "current_account_type": "EXPENSE",
  "line_amount": "12000.00",
  "currency": "GBP",
  "period_start": "2026-08-01",
  "period_end": "2027-07-31",
  "year_end": "2027-03-31",
  "months_after_year_end": 4,
  "total_months": 12,
  "prepaid_estimate": "4000.00",      // carry forward to the balance sheet
  "expense_this_year": "8000.00",     // stays in the P&L this year
  "monthly_amount": "1000.00",
  "release_schedule": [               // month-by-month release — render as a table
    { "month": "Apr 2027", "release": "1000.00", "remaining": "3000.00" },
    { "month": "May 2027", "release": "1000.00", "remaining": "2000.00" },
    { "month": "Jun 2027", "release": "1000.00", "remaining": "1000.00" },
    { "month": "Jul 2027", "release": "1000.00", "remaining": "0.00"    }
  ],
  "recommended_action": "prepay_future_portion"
}
```

> All money fields in both payloads are **strings** (exact decimals). Don't parse to float
> and re-round. Format using the `currency` field (`GBP` → `£`).

---

## 5. The ONE rule that separates them: `recode_to_account_type`

```
match_reasons.recode_to_account_type present  →  show the recode dropdown + "Save changes"
                              (capital: "FIXED" → offer the FIXED-asset accounts)
match_reasons.recode_to_account_type absent   →  NO dropdown, review-only
                              (prepayment)
```

`suggested_code` is `null` on **both** — do **not** use it to decide. Capital has no single
"correct" target account, so the dropdown is populated from the org's accounts filtered to
`recode_to_account_type`, and the user picks. That selection is what "Save changes" posts.

---

## 6. Rendering — Capital item review

**Row:** Type · Item (`supplier` + `transaction_date` + `description`) · Net (`line_amount`) ·
Account used (`account_code` `account_name` / `current_account_type`) · **Change to (fixed asset)** dropdown.

**Why-chips** (only render the ones that are truthy):
- `matched_keyword` → *Description mentions "motor vehicle"*
- `matched_supplier` → *Supplier "ford" commonly sells assets*
- `monitored_account` → *Account often hides capital items*

**Actions:** recode dropdown + **Save changes** · View (Xero deep link) · Dismiss.

---

## 7. Rendering — Prepayment review (**the table**)

**Row:** Type · Item (`transaction_date` + `description`) · Net · Account used ·
Period (`period_start` → `period_end`). **No dropdown.**

**Expanded — summary strip (the split):**
```
Total £12,000.00   ·   This year (P&L) £8,000.00   ·   Prepayment (carry forward) £4,000.00
Period 01 Aug 2026 → 31 Jul 2027   ·   Year-end 31 Mar 2027   ·   4 of 12 months after year-end
```
- Total → `line_amount` · This year → `expense_this_year` · Prepayment → `prepaid_estimate`
  (emphasise this one) · "4 of 12" → `months_after_year_end` / `total_months`

**Expanded — release schedule table** (render `release_schedule[]` as-is, one row each):

| Month | Release to P&L | Remaining prepaid |
|-------|---------------:|------------------:|
| Apr 2027 | £1,000.00 | £3,000.00 |
| May 2027 | £1,000.00 | £2,000.00 |
| Jun 2027 | £1,000.00 | £1,000.00 |
| Jul 2027 | £1,000.00 | £0.00 |

- Right-align money; tabular numerals so the columns line up
- The last row's `remaining` is always `0.00`
- Caption: *"Straight-line estimate — guidance only, nothing is posted."*
- Always present and non-empty on a flag, but code defensively (`?? []`)

**Actions:** **Edit in Xero** · **Dismiss**. No dropdown, no "Save changes".

> ❌ **Never add a "post journal" button.** Both SOPs forbid auto-posting. A prepayment is a
> **partial reclass** (move X% to Prepayments and release it over time) — a single line
> recode cannot express it, which is why there's no dropdown.

---

## 8. Client questions (optional prompts, from the SOPs)

- **Capital:** "This expense contains asset-related keywords. Please confirm whether it
  should be capitalised as a fixed asset instead of expensed."
- **Prepayment:** "This expense appears to cover a period beyond the year-end. Please
  confirm whether the future portion should be treated as a prepayment."

---

## 9. Zero flags is often **CORRECT** — don't read it as broken

Both SOPs deliberately narrow the scope. An item is **correctly** not flagged when:

**Both**
- it sits on a **balance-sheet account** (Prepayments / asset / liability) — the SOPs say
  ignore those;
- it's a **credit note** (a reversal);
- it's a **sales invoice or Receive Money** (capital reads the purchase side only);
- it's **under the threshold**.

**Capital only**
- the description is obvious revenue spend — *repair, servicing, maintenance, AMC, fuel,
  insurance, spare part, consumable* — even above the threshold;
- it's already in the **fixed-asset register** (validated against Xero `/Assets`).

**Prepayment only**
- the period **matches the financial year exactly** (01 Apr 2026 → 31 Mar 2027 against a
  31 Mar year-end — nothing falls after year-end);
- the period is **in the past** relative to the bill (a late/accrual bill);
- the spill past year-end is **under 1 month**.

To see a prepayment flag you need an **expense account** line whose period **crosses** the
year-end — e.g. "Annual insurance 01 Aug 2026 – 31 Jul 2027" on Insurance, year-end 31 Mar.

---

## 10. CSV report

`GET /api/v1/health/report/csv/?company_id=<id>` groups every check into sections — both
appear automatically. No frontend change.

---

## Quick reference

|  | Capital item review | Prepayment review |
|--|---------------------|-------------------|
| Group | Fixed Assets | Date & Ageing |
| Threshold | £500 (configurable, min 0) | £250 (configurable) |
| Fires on | keyword / supplier / monitored account | date range or period keyword |
| `recode_to_account_type` | `"FIXED"` → **dropdown + Save** | absent → **no dropdown** |
| Extra data | matched_keyword, matched_supplier, monitored_account | period, year-end, split, **release_schedule** |
| Actions | Recode · View · Dismiss | Edit in Xero · Dismiss |
