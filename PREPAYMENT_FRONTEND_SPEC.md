# Frontend Spec — Prepayment Review (`prepayment_review`)

Backend is **live and deployed**. Every payload below is what the API already returns —
nothing here is aspirational.

---

## 1. What the check flags (one line)

> A **P&L expense** (over £250) whose **description** shows a service period running
> **past the financial year-end** → the future portion may be a prepayment.

**Review-only.** It never posts a journal and never creates a prepayment — it flags the
item and shows the numbers so the bookkeeper decides.

**Out of scope** (per the SOP): balance-sheet postings (anything already in Prepayments /
an asset account), credit notes, opening journals.

---

## 2. Where it lives

`GET /api/v1/health/audit-config/?company_id=<id>` → `groups[]` → **"Date & Ageing"**

| key | label | built |
|-----|-------|-------|
| `prepayment_review` | Prepayment review | true |

Toggle → add/remove the key in `disabled_rules[]` → `PUT /api/v1/health/audit-config/`.

### Settings (gear)

Same response's `settings_schema[]` (check = `prepayment_review`):

| key | label | type | default |
|-----|-------|------|---------|
| `prepayment_min_amount` | Review expense over … | amount | 250 |
| `financial_year_end_month` | Financial year-end month | int (1–12) | 12 |
| `financial_year_end_day` | Financial year-end day | int (1–31) | 31 |

> **Year-end hint to show:** *"Taken from your Xero organisation automatically. Only set
> these to override it."* The audit reads `FinancialYearEndMonth` / `FinancialYearEndDay`
> from the connected org — these two fields are an override, not a setup step.

---

## 3. Fetching the flags

```
GET /api/v1/health/trapped-invoices/?company_id=<id>&issue_type=prepayment_review
```
Response: `{ results: [...], total, limit, offset }`.

> Use the **server-side `issue_type` filter**. Don't fetch page 1 and filter client-side —
> medium-severity items beyond the first page will silently disappear.

---

## 4. The flag payload (exact, from the live check)

`message` — already human-readable, safe as the row summary:

```
Rare Insurance Ltd: £12000.00 on 433 (Insurance) covers a period to 31 Jul 2027,
beyond the 31 Mar 2027 year-end — ~4 month(s) (~£4000.00) may be a prepayment.
```

`suggested_code` is **`null`** — there is no account to recode to (see §6).

`match_reasons`:

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
  "release_schedule": [               // month-by-month release of the prepaid portion
    { "month": "Apr 2027", "release": "1000.00", "remaining": "3000.00" },
    { "month": "May 2027", "release": "1000.00", "remaining": "2000.00" },
    { "month": "Jun 2027", "release": "1000.00", "remaining": "1000.00" },
    { "month": "Jul 2027", "release": "1000.00", "remaining": "0.00"    }
  ],
  "recommended_action": "prepay_future_portion"
}
```

Money fields are **strings** (exact decimals) — don't parse to float and re-round.
Format using the `currency` field (`GBP` → `£`).

---

## 5. Rendering — row + the table

### 5a. Collapsed row (list)

| Column | Source |
|--------|--------|
| Type | `document_type` (BILL / SPEND) |
| Item | vendor + `transaction_date` + `description` |
| Net | `line_amount` (+ `currency`) |
| Account used | `account_code` + `account_name` + `current_account_type` |
| Period | `period_start` → `period_end` |

### 5b. Expanded — **the table (this is the ask)**

**Summary strip — the split:**

```
Total £12,000.00   ·   This year (P&L) £8,000.00   ·   Prepayment (carry forward) £4,000.00
Period 01 Aug 2026 → 31 Jul 2027   ·   Year-end 31 Mar 2027   ·   4 of 12 months fall after year-end
```
- Total → `line_amount`
- This year → `expense_this_year`
- Prepayment → `prepaid_estimate` ← emphasise this one
- "4 of 12" → `months_after_year_end` / `total_months`

**Release schedule table** — render `release_schedule[]` directly, one row per entry:

| Month | Release to P&L | Remaining prepaid |
|-------|---------------:|------------------:|
| Apr 2027 | £1,000.00 | £3,000.00 |
| May 2027 | £1,000.00 | £2,000.00 |
| Jun 2027 | £1,000.00 | £1,000.00 |
| Jul 2027 | £1,000.00 | £0.00 |

- `month` → Month · `release` → Release to P&L · `remaining` → Remaining prepaid
- Right-align money; tabular numerals so columns line up
- Last row's `remaining` is always `0.00` (the prepayment fully releases)
- Caption: *"Straight-line estimate — guidance only, nothing is posted."*

`release_schedule` is always present and non-empty on a flag
(`months_after_year_end ≥ 1`), but code defensively (`?? []`) anyway.

---

## 6. Actions — review-only (differs from Capital)

- ❌ **No recode dropdown / no "Save changes."** `suggested_code` is `null` by design. A
  prepayment is a **partial reclass** (move X% to Prepayments, release it over time) — a
  single line recode cannot express that.
- ✅ **"Edit in Xero"** (deep link) and **"Dismiss."**
- ❌ **No "post journal" button.** The SOP forbids auto-posting. The schedule shows what
  *would* be released; the accountant decides.

---

## 7. Client question (optional prompt, from the SOP)

> "This expense appears to cover a period beyond the year-end. Please confirm whether the
> future portion should be treated as a prepayment."

---

## 8. Zero flags can be CORRECT — don't read it as broken

An item is **correctly** not flagged when:

- it already sits in **Prepayments / any balance-sheet account** (SOP: ignore those);
- the period **matches the financial year exactly** (01 Apr 2026 → 31 Mar 2027 against a
  31 Mar year-end — nothing falls after year-end);
- the period is **in the past** relative to the bill date (a late/accrual bill);
- it's a **credit note** (a reversal);
- the amount is **≤ £250**, or the spill past year-end is **under 1 month**.

To see a flag, the bill must be on an **expense account** with a period that **crosses**
the year-end — e.g. "Annual insurance 01 Aug 2026 – 31 Jul 2027" on Insurance, year-end
31 Mar.

---

## 9. CSV report

`GET /api/v1/health/report/csv/?company_id=<id>` groups every check into sections —
`prepayment_review` appears automatically. No frontend change.

---

## Capital vs Prepayment — keep the distinction visible

|  | Capital item review | Prepayment review |
|--|---------------------|-------------------|
| Question | **What** is it? (an asset?) | **How long** is the period? (past year-end?) |
| In-app fix | fixed-asset dropdown + Save (recode) | **none** — Edit in Xero |
| Extra data | matched_keyword / matched_supplier | period, year-end, split, **release_schedule** |
