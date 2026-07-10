"""Structured context for each health-check issue type.

Every issue surfaces three pieces of information to the user:

  issue    — what was specifically found (row-specific, built from data)
  so_what  — why it matters financially / for compliance (per type, static)
  solution — how to fix it (per type, static)

The ``so_what`` and ``solution`` texts are deterministic — they never
change for a given ``issue_type``. The ``issue`` text is assembled from
the row's title + flagged item messages.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Per-type context: (so_what, solution)
# ---------------------------------------------------------------------------

_CONTEXT: dict[str, tuple[str, str]] = {
    "duplicate_invoice": (
        "Duplicate sales invoices artificially overstate revenue and the "
        "amount owed by your customers. This leads to overstated taxable "
        "profits, potential overpayment of tax, incorrect financial reports, "
        "and can confuse or frustrate customers who receive two invoices for "
        "the same work.",
        "Review each flagged invoice pair. Void or delete the duplicate — "
        "keep the original and void the later entry. If both are genuine "
        "separate transactions, dismiss the flag.",
    ),
    "duplicate_bill": (
        "Duplicate supplier bills overstate your costs and the balance you owe "
        "to the supplier. This understates your taxable profits, which means "
        "HMRC may later find you have underpaid tax. It also risks paying the "
        "supplier twice, and produces incorrect creditor balances and financial "
        "reports.",
        "Review each flagged bill pair. Void or delete the duplicate — keep "
        "the original and void the later entry. If you have already paid both, "
        "request a refund from the supplier for the overpayment. Dismiss if "
        "both are genuinely separate transactions.",
    ),
    "duplicate_credit_note": (
        "Duplicate credit notes double-count a refund or write-off. A repeated "
        "sales credit understates the amount owed to you (and your revenue); a "
        "repeated purchase credit understates your costs. Either way your "
        "profit, tax and contact balances are wrong, and you risk refunding or "
        "being refunded twice for the same thing.",
        "Review each flagged credit-note pair. Void or delete the duplicate — "
        "keep the original and void the later entry. If both are genuinely "
        "separate credits, dismiss the flag.",
    ),
    "old_unpaid_invoice": (
        "Customer invoices more than 60 days old and still unpaid artificially "
        "inflate your profits — you are recognising revenue you have not yet "
        "collected. This means you may be paying more tax than necessary on "
        "income that has not actually arrived. Uncollected debts also distort "
        "your debtors balance and cash-flow picture.",
        "Chase the customer for payment. If the invoice was raised in error, "
        "void or delete it. If the customer has been given a discount or the "
        "debt is irrecoverable, raise a Credit Note to write it off and "
        "reduce your taxable income. Dismiss any items you are satisfied "
        "are correct to leave open.",
    ),
    "old_unpaid_bill": (
        "Supplier bills more than 60 days old and still unpaid can artificially "
        "reduce your profits if they are no longer genuine liabilities — for "
        "example if the bill was entered in error or the amount has been written "
        "off. Understating profits means paying too little tax, which creates a "
        "risk of HMRC enquiry. Unpaid bills also overstate your creditors "
        "balance, distorting your financial position.",
        "Review each bill. Pay the supplier if the amount is genuinely owed. "
        "If the bill was raised in error, void or delete it. If a discount was "
        "agreed or the balance no longer needs to be paid, raise a Credit Note "
        "to clear it. Dismiss any bills that are correct to leave open.",
    ),
    "old_unsettled_sales_credit": (
        "Unsettled sales credit notes leave an open credit on the customer "
        "account. This overstates the amount owed to you as a debtor offset "
        "and may indicate a refund or allocation was never processed.",
        "Allocate the credit note against an outstanding invoice, process a "
        "refund to the customer, or void the credit note if it was raised in "
        "error.",
    ),
    "old_unsettled_purchase_credit": (
        "Unsettled purchase credit notes leave an open credit sitting on the "
        "supplier account. If unused, this understates the net amount you owe "
        "and can distort your creditor balance.",
        "Allocate the credit note against an outstanding bill, request a "
        "refund from the supplier, or void it if it was raised in error.",
    ),
    "opening_balance_difference": (
        "Opening balance discrepancies mean your Xero ledger does not agree "
        "with your prior accounting system or trial balance. Any difference "
        "flows through to every subsequent report and financial statement, "
        "making them unreliable.",
        "Run the Balance Sheet report as at the opening date and compare it "
        "to your prior system or audited accounts. Post a correcting journal "
        "to reconcile any difference.",
    ),
    "invoice_or_direct_booking": (
        "An authorised sale with no invoice number suggests the income was "
        "posted as a direct bank entry rather than through a formal invoice. "
        "This bypasses your invoice trail, making VAT reporting and customer "
        "statements unreliable.",
        "Obtain the original invoice from your records. Create a proper sales "
        "invoice in Xero and reconcile it to the bank entry. Void the direct "
        "coding if a separate bank transaction was created.",
    ),
    "bill_or_direct_booking": (
        "An authorised purchase with no bill reference suggests the expense "
        "was coded directly from the bank rather than through a proper bill. "
        "This means you have no purchase document to support the VAT claim "
        "and no supplier statement to reconcile against.",
        "Obtain the original bill or receipt from the supplier. Create a "
        "proper bill in Xero and reconcile it to the bank entry. Void the "
        "direct coding if a separate bank transaction was created.",
    ),
    "low_cost_fixed_asset": (
        "Small amounts posted to fixed asset accounts may not meet your "
        "capitalisation threshold. Capitalising items below the threshold "
        "overstates fixed assets, understates expenses, and inflates profit. "
        "It also creates unnecessary depreciation entries.",
        "Recode the line to the appropriate expense account. If the item "
        "genuinely qualifies as a fixed asset (e.g. part of a larger asset "
        "purchase), dismiss the flag with a note.",
    ),
    "capital_item_review": (
        "A high-value purchase on an expense account may need capitalising "
        "under FRS 102. Expensing a capital item understates your fixed assets, "
        "overstates expenses, reduces profit, and means you are not claiming "
        "the correct capital allowances.",
        "Review whether the purchase has a useful life beyond one year. If so, "
        "recode it to the appropriate fixed asset account and set up a "
        "depreciation schedule. If it is a genuine repair or consumable, "
        "dismiss the flag.",
    ),
    "wrong_category": (
        "A transaction posted to the wrong expense or income account will "
        "misstate your profit and loss reporting. Incorrect categorisation "
        "can also affect VAT recovery if the wrong tax treatment is applied "
        "to the account.",
        "Recode the line item to the correct account. Use the Chart of "
        "Accounts to find the most appropriate code for the vendor and "
        "transaction type.",
    ),
    "amount_outlier": (
        "This transaction's amount is far larger than what this vendor "
        "normally charges. An unusually large amount can indicate a typo "
        "(an extra zero), a duplicate, a one-off captured under the wrong "
        "vendor, or — at worst — a fraudulent entry. Left unchecked it "
        "distorts spend reporting and your P&L.",
        "Open the transaction and confirm the amount against the source "
        "document. If it's a typo, correct it. If it's a genuine one-off "
        "(annual renewal, capital purchase), dismiss the flag.",
    ),
    "anomaly": (
        "This transaction is unusual across more than one dimension — its "
        "amount is far off this vendor's norm and it deviates in account "
        "code, tax code, or description too. Combined oddities like this are "
        "the classic shape of a posting error, a misattributed payment, or "
        "a fraudulent entry, and warrant a closer look.",
        "Review the transaction against its source document. Verify the "
        "amount, account, and tax code are all correct for this vendor. "
        "Correct any miscoding; dismiss if it is a legitimate one-off.",
    ),
    "wrong_direction_account": (
        "A purchase bill (money you owe) posted to a fixed asset or "
        "balance-sheet account, or a sales invoice posted to an expense "
        "account, means the transaction is on the wrong side of the ledger. "
        "This will misstate both your P&L and your balance sheet.",
        "Recode the line item to an account on the correct side of the "
        "ledger — expense accounts for purchase bills, revenue accounts "
        "for sales invoices. Use the suggested account code where provided.",
    ),
    "multi_account_supplier": (
        "A supplier being posted to several different accounts inconsistently "
        "makes it hard to track spend accurately and can obscure VAT recovery "
        "patterns. It often indicates the account was chosen at random rather "
        "than according to a consistent policy.",
        "Review the postings for this supplier and align them to the most "
        "appropriate account. Update the supplier's default account in Xero "
        "to prevent future inconsistencies.",
    ),
    "multi_tax_code_supplier": (
        "A supplier using different tax codes across transactions suggests "
        "inconsistent VAT treatment. This can lead to incorrect VAT returns "
        "and exposure on HMRC review.",
        "Review the tax codes applied to this supplier. Correct any incorrect "
        "codes and update the supplier's default tax setting in Xero.",
    ),
    "unexpected_account": (
        "A transaction posted to an account that is unusual for its type or "
        "vendor may indicate a miscoding. Unexpected account usage can distort "
        "category-level reporting and budget comparisons.",
        "Verify the account code is correct for this transaction. Recode if "
        "necessary and update the supplier default to prevent recurrence.",
    ),
    "unexpected_tax_code": (
        "An unexpected tax code on a transaction suggests the VAT treatment "
        "may be wrong. Incorrect tax codes lead to errors in your VAT return "
        "and can result in underpayment or overclaiming of VAT.",
        "Check the correct VAT treatment for this transaction. Update the "
        "tax code and, if your VAT return has already been filed, consider "
        "whether an adjustment is needed.",
    ),
    "purchase_tax_missing": (
        "A purchase bill with no tax code means VAT has not been recorded "
        "on the transaction. If the purchase carries VAT you are entitled to "
        "recover, leaving the tax code blank means you are forfeiting that "
        "claim.",
        "Set the correct VAT tax code on the line item. For standard-rated "
        "UK purchases this is typically INPUT. Check the supplier invoice to "
        "confirm the VAT amount before saving.",
    ),
    "unapproved_invoice": (
        "A sales invoice left in Draft or Submitted status is not posted to "
        "the ledger and will not appear on your debtors report or VAT return. "
        "Revenue is understated until the invoice is approved.",
        "Review the invoice and approve it if the sale is genuine. If it was "
        "raised in error, void or delete it.",
    ),
    "unapproved_bill": (
        "A purchase bill left in Draft or Submitted status is not posted to "
        "the ledger and will not appear on your creditors report or VAT return. "
        "Your liabilities and VAT reclaim are understated until it is approved.",
        "Review the bill and approve it if the purchase is genuine and you "
        "have the supplier invoice. If it was raised in error, void or delete it.",
    ),
    "sales_tax_on_bills": (
        "A purchase bill coded with a sales (OUTPUT) tax code means VAT is "
        "being treated as collected from a customer rather than paid to a "
        "supplier. This means you cannot reclaim the VAT on the purchase, "
        "resulting in an overstatement of costs and a VAT return error.",
        "Change the tax code on the bill to the correct purchase tax code "
        "(typically INPUT). Check whether previous VAT returns included this "
        "error and consider whether an amendment is needed.",
    ),
    "purchase_tax_on_invoices": (
        "A sales invoice coded with a purchase (INPUT) tax code means VAT is "
        "being treated as paid to a supplier rather than collected from a "
        "customer. This understates the VAT you owe to HMRC and could result "
        "in an underpayment of VAT.",
        "Change the tax code on the invoice to the correct sales tax code "
        "(typically OUTPUT). Check whether previous VAT returns were affected "
        "and consider whether an amendment is needed.",
    ),
    "sales_tax_missing": (
        "A sales invoice with no tax code means VAT has not been recorded "
        "on the transaction. If the sale is VAT-able, leaving the tax code "
        "blank means you are not collecting VAT that HMRC expects you to "
        "account for.",
        "Set the correct VAT tax code on the invoice. For standard-rated UK "
        "sales this is typically OUTPUT. Check the customer's VAT status and "
        "the nature of the supply before saving.",
    ),
    "duplicate_contact": (
        "Two contacts share a strong identity signal — the same email, phone, "
        "bank account, tax number, or a near-identical name — suggesting the "
        "same supplier or customer was entered twice. Duplicate contacts split "
        "transaction history, make statements unreliable, and mean invoices or "
        "bills may be assigned to the wrong contact (and reconciliation can't "
        "see the full picture).",
        "Review the two contacts. Merge them in Xero if they are the same "
        "entity — keep the one with the most complete information and transfer "
        "any transactions from the duplicate before archiving it.",
    ),
    "contact_defaults": (
        "A supplier or customer with no default account code set means every "
        "new transaction requires manual coding. This leads to inconsistent "
        "posting and increases the risk of coding errors over time.",
        "Open the contact in Xero and set the default purchase account "
        "(for suppliers) or default sales account (for customers). Also set "
        "the default tax type to match the contact's typical VAT treatment.",
    ),
    "inactive_contact": (
        "A contact marked as active in Xero but with no transactions in the "
        "last 180 days clutters your contact list and makes finding active "
        "suppliers and customers harder. It may also indicate a contact that "
        "was set up in error and never used.",
        "Review the contact. If it is no longer used, archive it in Xero to "
        "keep your contact list clean. If it is still active but just hasn't "
        "had recent transactions, dismiss the flag.",
    ),
}

_DEFAULT_SO_WHAT = (
    "This issue may affect the accuracy of your financial reports, "
    "VAT returns, or ledger balances."
)
_DEFAULT_SOLUTION = (
    "Review the flagged transaction in Xero and apply the appropriate "
    "correction. Dismiss the flag if you are satisfied the entry is correct."
)


def get_context(issue_type: str) -> tuple[str, str]:
    """Return (so_what, solution) for the given issue_type.
    Falls back to generic text for unknown types.
    """
    return _CONTEXT.get(issue_type, (_DEFAULT_SO_WHAT, _DEFAULT_SOLUTION))
