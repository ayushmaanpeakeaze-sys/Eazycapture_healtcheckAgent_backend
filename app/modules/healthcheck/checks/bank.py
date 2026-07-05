"""Bank & Reconciliation check group.

Standalone detection here: bill-paid-directly / invoice-paid-directly (matching
an unpaid document to a direct bank settlement) and the opening-balance-difference
heuristic. The richer Bank Balance / Opening Balance Differences / Unreconciled /
Unprocessed checks have their own service modules; their registry entries + the
bank settings are collected here.

_outstanding_amount stays in deterministic (shared) and is reached via a lazy proxy.
"""
from __future__ import annotations

from collections import defaultdict  # noqa: F401
from decimal import Decimal

from app.modules.healthcheck.checks.base import SettingField
from app.shared.transaction import BatchTransaction, FlaggedIssue  # noqa: F401
from app.modules.healthcheck.engine.audit_settings import AuditSettings, DEFAULT_SETTINGS  # noqa: F401
from app.modules.healthcheck.engine.shared import _OPEN_BILL_STATUSES  # noqa: F401


def _outstanding_amount(*a, **k):
    from app.modules.healthcheck.engine.deterministic import _outstanding_amount as _f
    return _f(*a, **k)


_BILL_DIRECT_AMOUNT_TOL = Decimal("0.01")


def _find_direct_settlement_mismatches(
    transactions: list[BatchTransaction],
    bank_transactions: list[BatchTransaction],
    *,
    doc_type: str,        # "ACCPAY" (bill) or "ACCREC" (invoice)
    bank_type: str,       # "SPEND" (money out) or "RECEIVE" (money in)
    issue_type: str,      # "bill_direct_payment" or "invoice_direct_deposit"
    window: int,
    doc_key: str,         # "bill" / "invoice" — match_reasons key prefix + wording
    bank_key: str,        # "payment" / "deposit"
    bank_phrase: str,     # "direct bank payment" / "direct bank deposit"
    settle_hint: str,     # "the bill may need to be marked paid." / "…invoice…"
) -> list[FlaggedIssue]:
    """Generic core for the two 'settled directly via the bank instead of against
    the open document' checks:
      • Bill or Direct Payment    — unpaid ACCPAY bill   ↔ SPEND (money out)
      • Invoice or Direct Deposit — unpaid ACCREC invoice ↔ RECEIVE (money in)

    Same contact + same amount, bank txn dated within ``window`` days AFTER the
    document. POSSIBLE mismatch (not confirmed). O(D + B): index bank txns by
    contact, one lookup per document.
    """
    bank_by_contact: dict[str, list[BatchTransaction]] = defaultdict(list)
    for bt in bank_transactions:
        if (bt.type or "").strip().upper() == bank_type:
            cid = (bt.contact_id or "").strip()
            if cid:
                bank_by_contact[cid].append(bt)
    if not bank_by_contact:
        return []

    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        if (tx.type or "").strip().upper() != doc_type:
            continue
        status = (tx.status or "").strip().upper()
        if status and status not in _OPEN_BILL_STATUSES:
            continue
        due = _outstanding_amount(tx)
        if due <= 0:
            continue
        cid = (tx.contact_id or "").strip()
        if not cid:
            continue
        for bank in bank_by_contact.get(cid, []):
            delta = (bank.date - tx.date).days
            if delta < 0 or delta > window:
                continue
            if abs(due - bank.amount) > _BILL_DIRECT_AMOUNT_TOL:
                continue
            currency = (tx.currency_code or "GBP").strip().upper()
            symbol = "£" if currency == "GBP" else f"{currency} "
            flagged.append(FlaggedIssue(
                transaction_id=tx.transaction_id,
                issue_type=issue_type,
                severity="medium",
                message=(
                    f"{tx.vendor_name}: unpaid {doc_key} {symbol}{due:.2f} ({tx.date.isoformat()}) "
                    f"has a matching {bank_phrase} {symbol}{bank.amount:.2f} on "
                    f"{bank.date.isoformat()} ({delta}d later) — possible direct "
                    f"settlement; {settle_hint}"
                )[:200],
                match_reasons={
                    # --- the open DOCUMENT row (bill / invoice) ---
                    f"{doc_key}_transaction_id": tx.transaction_id,
                    f"{doc_key}_date": tx.date.isoformat(),
                    f"{doc_key}_amount": f"{tx.amount:.2f}",     # Total Value
                    "amount_due": f"{due:.2f}",                   # still-outstanding
                    f"{doc_key}_description": (tx.description or "").strip()[:200] or None,
                    # --- the matching BANK row (payment / deposit) ---
                    f"{bank_key}_transaction_id": bank.transaction_id,
                    f"{bank_key}_date": bank.date.isoformat(),
                    f"{bank_key}_amount": f"{bank.amount:.2f}",
                    f"{bank_key}_description": (bank.description or "").strip()[:200] or None,
                    # --- match meta ---
                    "days_apart": delta,
                    "currency": currency,
                },
            ))
            break   # one matching bank txn is enough to raise the flag
    return flagged


def _find_bill_direct_payments(
    transactions: list[BatchTransaction],
    bank_transactions: list[BatchTransaction],
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[FlaggedIssue]:
    """Unpaid supplier BILL (ACCPAY) settled by a direct SPEND payment instead of
    against the bill → bill stays falsely unpaid / supplier risk of double-pay."""
    return _find_direct_settlement_mismatches(
        transactions, bank_transactions,
        doc_type="ACCPAY", bank_type="SPEND", issue_type="bill_direct_payment",
        window=settings.bill_direct_window_days, doc_key="bill", bank_key="payment",
        bank_phrase="direct bank payment",
        settle_hint="the bill may need to be marked paid.",
    )


def _find_invoice_direct_deposits(
    transactions: list[BatchTransaction],
    bank_transactions: list[BatchTransaction],
    settings: AuditSettings = DEFAULT_SETTINGS,
) -> list[FlaggedIssue]:
    """Unpaid customer INVOICE (ACCREC) settled by a direct RECEIVE deposit
    instead of against the invoice → invoice stays falsely unpaid, Accounts
    Receivable / profit overstated, customer chased for money already paid."""
    return _find_direct_settlement_mismatches(
        transactions, bank_transactions,
        doc_type="ACCREC", bank_type="RECEIVE", issue_type="invoice_direct_deposit",
        window=settings.invoice_direct_window_days, doc_key="invoice", bank_key="deposit",
        bank_phrase="direct bank deposit",
        settle_hint="the invoice may need to be marked paid.",
    )


_HISTORICAL_ADJUSTMENT_CODE = "840"


_HISTORICAL_ADJUSTMENT_NAME_KEYWORDS = ("historical adjustment", "opening balance")


def _find_opening_balance_differences(
    transactions: list[BatchTransaction],
    coa_lookup: dict[str, str],
    historical_code: str = _HISTORICAL_ADJUSTMENT_CODE,
) -> list[FlaggedIssue]:
    codes = {historical_code} if historical_code else set()
    codes.update(
        code.strip()
        for code, name in coa_lookup.items()
        if any(keyword in (name or "").lower() for keyword in _HISTORICAL_ADJUSTMENT_NAME_KEYWORDS)
    )
    return [
        FlaggedIssue(
            transaction_id=tx.transaction_id,
            issue_type="opening_balance_difference",
            severity="high",
            message=f"Posted to {tx.current_account_code} ({coa_lookup.get((tx.current_account_code or '').strip(), 'Historical Adjustment')}) - review.",
            current_code=(tx.current_account_code or "").strip(),
        )
        for tx in transactions
        if (tx.current_account_code or "").strip() in codes
    ]


# --- settings + registry -----------------------------------------------------
SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField("bill_direct_window_days", "Bank & Reconciliation", "bill_direct_payment",
                 "Direct payment within … of bill", "int",
                 "Match an unpaid bill with a direct bank payment to the same "
                 "supplier dated at most this many days after the bill "
                 "(default 30).",
                 unit="days", min=1, max=365, step=1),
    SettingField("invoice_direct_window_days", "Bank & Reconciliation", "invoice_direct_deposit",
                 "Direct deposit within … of invoice", "int",
                 "Match an unpaid invoice with a direct bank deposit from the "
                 "same customer dated at most this many days after the invoice "
                 "(default 30).",
                 unit="days", min=1, max=365, step=1),
    SettingField("opening_balance_min_difference", "Bank & Reconciliation", "opening_balance_difference",
                 "Minimum difference to flag", "amount",
                 "Smallest |Net Assets filed at Companies House − Net Assets in "
                 "Xero| (at the same period end) that raises an issue. Default £1 "
                 "ignores negligible rounding differences.",
                 unit="£", min=0, step=1),
    # NOTE: Bank Balance Check has NO user-facing tolerance setting (none is
    # exposed). The check uses a fixed £0.01 floor (any real gap
    # flags) via AuditSettings.bank_balance_tolerance's default.
)

META: tuple[tuple[str, str, bool], ...] = (
    ("unprocessed_bank", "Unprocessed bank", False),
    ("unreconciled_bank", "Unreconciled bank (Received/Spent)", True),
    ("bank_balance_check", "Bank balance check", True),
    ("opening_balance_difference", "Opening balance differences", True),
    ("bill_direct_payment", "Bill paid directly (vs unpaid bill)", True),
    ("invoice_direct_deposit", "Invoice paid directly (vs unpaid invoice)", True),
)
