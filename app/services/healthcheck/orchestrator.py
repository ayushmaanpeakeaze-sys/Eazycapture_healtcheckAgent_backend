"""Health-check orchestrator — ties deterministic + LLM passes together.

``run_batch_health_check`` applies the per-client audit config (disabled
rules + ignore-before date), runs every enabled check, and returns the
flagged issues.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Awaitable, Callable, Optional

from app.schemas.transaction import (
    BatchHealthCheckRequest,
    BatchHealthCheckResponse,
    FlaggedIssue,
)
from app.services.healthcheck.audit_settings import AuditSettings
from app.services.healthcheck.shared import (
    _allowed_tax_codes,
    _BANK_TXN_TYPES,
    _coa_lookup,
    _coa_summary,
    _coa_type_lookup,
    _format_tax_codes_hint,
    _noop_issues,
    _tax_direction_map,
)
from app.modules.healthcheck.checks.documents import _find_undocumented_bills
from app.modules.healthcheck.checks.fixed_assets import (
    _find_capital_items,
    _find_low_cost_fixed_assets,
)
from app.modules.healthcheck.checks.coding import (
    _find_direction_mismatches,
    _find_misallocated_items,
    _find_multi_account_suppliers,
    _find_unexpected_accounts,
    amount_outlier_flag,
    find_amount_outlier_candidates,
)
from app.modules.healthcheck.checks.tax import (
    _find_multi_tax_code_suppliers,
    _find_purchase_tax_missing,
    _find_purchase_tax_on_invoices,
    _find_sales_tax_missing,
    _find_sales_tax_on_bills,
    _find_unexpected_tax_codes,
)
from app.modules.healthcheck.checks.bank import (
    _find_bill_direct_payments,
    _find_invoice_direct_deposits,
    _find_opening_balance_differences,
)
from app.modules.healthcheck.checks.duplicates import _find_duplicate_bills
from app.services.healthcheck.deterministic import (
    _inspect_transaction,
)
from app.modules.ai.checks_llm import (
    _batched_category_audit,
    _llm_anomaly_review,
)

ProgressCallback = Callable[[dict], Awaitable[None]]

logger = logging.getLogger("eazycapture.healthcheck.orchestrator")


async def run_batch_health_check(
    req: BatchHealthCheckRequest,
    progress_callback: Optional[ProgressCallback] = None,
) -> BatchHealthCheckResponse:
    """Concurrently audit a batch of transactions and return all flagged issues."""
    context = req.context
    allowed_tax_codes = _allowed_tax_codes(context)
    tax_dir = _tax_direction_map(context)
    # Every check keys on the real Xero ContactID; duplicate contacts are only
    # flagged, never merged. Pass None so checks fall back to the raw ContactID.
    contact_alias = None
    tax_codes_hint = _format_tax_codes_hint(context)
    coa_summary = _coa_summary(context)
    coa_lookup = _coa_lookup(context)
    coa_type_lookup = _coa_type_lookup(context)
    # Per-contact default accounts enable default-based Unexpected-Account
    # (empty map falls back to the frequency heuristic).
    contact_defaults_map = {
        cd.contact_id.strip(): {
            "sales": cd.sales_account, "purchase": cd.purchase_account,
            "sales_tax": cd.sales_tax, "purchase_tax": cd.purchase_tax,
        }
        for cd in (context.contact_defaults if context else [])
        if cd.contact_id and cd.contact_id.strip()
    }

    today = date.today()

    # --- Per-client audit config ---------------------------------------
    disabled = set(req.disabled_rules or [])
    # Per-client tunable thresholds; defaults match the historical constants.
    settings = AuditSettings.from_config(req.settings)

    # Drop transactions dated before the ignore-before date so no check sees them.
    transactions = req.transactions
    if req.ignore_before is not None:
        transactions = [t for t in transactions if t.date >= req.ignore_before]
        if not transactions:
            return BatchHealthCheckResponse(flagged=[])

    # Split out bank transactions (Money In / Money Out); most checks keep
    # seeing only invoices/bills/credits so they never flag a bank transaction.
    bank_transactions = [
        t for t in transactions if (t.type or "").strip().upper() in _BANK_TXN_TYPES
    ]
    transactions = [
        t for t in transactions if (t.type or "").strip().upper() not in _BANK_TXN_TYPES
    ]

    # Per-transaction deterministic rules (no LLM): tax/vendor/required-field checks.
    per_tx_results = [
        _inspect_transaction(tx, allowed_tax_codes, tax_codes_hint, today, settings)
        for tx in transactions
    ]

    # Skip a pass entirely when all the rules it produces are disabled,
    # saving LLM tokens and latency.
    run_category = "wrong_category" not in disabled
    run_anomaly = "anomaly" not in disabled
    run_amount_outlier = "amount_outlier" not in disabled
    # Global kill-switch: when LLM checks are disabled, skip every LLM pass so
    # the audit stays fully deterministic. Amount-outlier still runs as a flag.
    from app.core.config import settings as _app_settings
    if not _app_settings.LLM_CHECKS_ENABLED:
        run_category = run_anomaly = False

    # Amount-outlier candidates feed the LLM anomaly review when enabled;
    # otherwise they are emitted as raw amount_outlier flags.
    outlier_candidates = find_amount_outlier_candidates(transactions, contact_alias, settings)
    do_anomaly_llm = run_anomaly and bool(outlier_candidates)

    # All LLM passes run in parallel — total latency = max, not sum.
    category_task = (
        _batched_category_audit(
            transactions, coa_summary, coa_lookup, coa_type_lookup,
            progress_callback=progress_callback,
            audit_settings=settings,
        )
        if (run_category and coa_summary is not None)
        else _noop_issues()
    )
    # Capital review is fully deterministic (low_cost_fixed_asset +
    # capital_item_review, both below); no LLM capital pass.
    capital_task = _noop_issues()
    anomaly_task = (
        _llm_anomaly_review(outlier_candidates, settings)
        if do_anomaly_llm
        else _noop_issues()
    )
    # return_exceptions=True so a single failing LLM pass degrades to its
    # deterministic fallback instead of sinking the whole audit.
    category_issues, capital_issues, anomaly_issues = await asyncio.gather(
        category_task, capital_task, anomaly_task,
        return_exceptions=True,
    )

    def _llm_pass(issues, label):
        if isinstance(issues, BaseException):
            logger.warning(
                "[Audit] LLM %s pass failed (%s); continuing deterministic-only",
                label, type(issues).__name__,
            )
            return None
        return issues

    category_issues = _llm_pass(category_issues, "category")
    anomaly_result = _llm_pass(anomaly_issues, "anomaly")
    if isinstance(capital_issues, BaseException):
        capital_issues = []
    # If the anomaly LLM was attempted but failed, fall back to the deterministic
    # amount_outlier flags below so outliers are still surfaced.
    anomaly_llm_failed = anomaly_result is None
    category_issues = category_issues or []
    anomaly_issues = anomaly_result or []

    flagged: list[FlaggedIssue] = []
    for issues in per_tx_results:
        flagged.extend(issues)
    flagged.extend(category_issues)
    flagged.extend(capital_issues)  # always empty — capital is deterministic now
    flagged.extend(anomaly_issues)
    # Capital checks (deterministic) run over invoices, bills and bank items:
    # low_cost_fixed_asset flags fixed-asset lines too cheap to capitalise;
    # capital_item_review flags expense lines too big to expense.
    capital_universe = transactions + bank_transactions
    flagged.extend(_find_low_cost_fixed_assets(capital_universe, coa_type_lookup, coa_lookup, settings))
    flagged.extend(_find_capital_items(capital_universe, coa_lookup, coa_type_lookup, settings))
    # If the LLM anomaly pass didn't run, fall back to the deterministic
    # amount_outlier flags so outliers are still surfaced.
    if (not do_anomaly_llm or anomaly_llm_failed) and run_amount_outlier:
        flagged.extend(amount_outlier_flag(c) for c in outlier_candidates)
    # Duplicate invoices/bills key on the real ContactID, so two distinct
    # ContactIDs are always treated as separate parties.
    flagged.extend(_find_duplicate_bills(transactions, None, settings))
    flagged.extend(_find_opening_balance_differences(transactions, coa_lookup))
    flagged.extend(_find_direction_mismatches(transactions, coa_type_lookup))
    # Multi-Account Suppliers: checks bill line items and Money-Out bank
    # payments, so it gets invoices/bills plus bank transactions.
    flagged.extend(_find_multi_account_suppliers(transactions + bank_transactions, coa_lookup, contact_alias, settings))
    flagged.extend(_find_multi_tax_code_suppliers(transactions + bank_transactions, contact_alias, settings))
    # Unexpected-Account / Unexpected-Tax also check Money In/Out against the
    # contact's default, so they get invoices/bills plus bank transactions.
    flagged.extend(_find_unexpected_accounts(transactions + bank_transactions, coa_lookup, contact_defaults_map))
    flagged.extend(_find_unexpected_tax_codes(transactions + bank_transactions, contact_defaults_map))
    # Bill-or-Direct-Payment: unpaid bill matched to a direct SPEND payment.
    flagged.extend(_find_bill_direct_payments(transactions, bank_transactions, settings))
    # Invoice-or-Direct-Deposit: unpaid invoice matched to a direct RECEIVE deposit.
    flagged.extend(_find_invoice_direct_deposits(transactions, bank_transactions, settings))
    # Misallocated Items: vague-account line over the threshold; checks
    # bills/invoices and Money In/Out, so it gets bank transactions too.
    flagged.extend(_find_misallocated_items(transactions + bank_transactions, coa_lookup, settings))
    # Undocumented Bills: supplier bill (or Money Out) with no Xero attachment.
    # Money Out is flagged too; the frontend hides it by default (exclude_bank_items).
    flagged.extend(_find_undocumented_bills(transactions + bank_transactions, settings))
    # Tax-missing checks only apply to a VAT-registered org. Skip when explicitly
    # flagged non-VAT; run when unknown (None).
    org_vat = context.org_is_vat_registered if context else None
    if org_vat is not False:
        # Tax-missing checks are account-type driven, so they get invoices/bills
        # plus Money In/Out.
        tax_universe = transactions + bank_transactions
        flagged.extend(_find_purchase_tax_missing(tax_universe, coa_lookup, coa_type_lookup, settings))
        flagged.extend(_find_sales_tax_missing(tax_universe, coa_lookup, coa_type_lookup, settings))
    # Wrong-direction VAT (sales code on a bill / purchase code on an invoice).
    # Include bank items so the "Show Bank payments too" toggle can reveal them.
    flagged.extend(_find_sales_tax_on_bills(transactions + bank_transactions, tax_dir))
    flagged.extend(_find_purchase_tax_on_invoices(transactions + bank_transactions, tax_dir))

    # Drop any flag whose rule the client disabled on the Audit Configuration
    # screen; also covers the deterministic rules in one place.
    if disabled:
        flagged = [f for f in flagged if f.issue_type not in disabled]

    return BatchHealthCheckResponse(flagged=flagged)
