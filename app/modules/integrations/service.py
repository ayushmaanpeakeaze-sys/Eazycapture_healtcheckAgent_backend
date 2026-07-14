"""Public integration interface for the rest of the application.

Architecture rule (from the Django backend):
  - Nango-facing code  → nango/service.py (NangoService)
  - App-facing code    → this file (IntegrationService)

Nothing outside this module should import NangoService directly.
IntegrationService is the only entry point for all integration
operations — whether the underlying provider is Xero, QuickBooks,
or anything else added in the future.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.modules.integrations.nango.service import NangoService

logger = logging.getLogger("eazycapture.integrations.service")


def _attachment_base(document_type: Optional[str]) -> str:
    """Xero endpoint family for attachments: bank items → BankTransactions,
    everything else (bills/invoices) → Invoices."""
    if (document_type or "").strip().upper() in {"SPEND", "RECEIVE"}:
        return "BankTransactions"
    return "Invoices"


class IntegrationService:
    """Provider-agnostic integration layer used by tasks, services, and
    routers. Delegates to NangoService for all actual API calls."""

    def __init__(self, nango: Optional[NangoService] = None) -> None:
        self._nango = nango or NangoService()

    def is_connected(
        self,
        connection_id: Optional[str],
        tenant_id: Optional[str],
    ) -> bool:
        """True when Nango is configured AND this company has a live
        connection with a known tenant."""
        return bool(
            self._nango.is_available()
            and (connection_id or "").strip()
            and (tenant_id or "").strip()
        )

    async def find_live_xero_connection(
        self, end_user_id: Optional[str] = None,
    ) -> Optional[tuple[str, str]]:
        """Newest live Xero connection (connection_id, tenant_id) in Nango, or
        None — for self-healing a stale stored connection-id. When
        ``end_user_id`` is given, the lookup is scoped to that user's
        connections only (firm isolation)."""
        return await self._nango.find_live_xero_connection(end_user_id=end_user_id)

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    async def fetch_all_invoices(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        """All invoices + bills via proxy with pagination.

        The Nango list-invoices Action returns a summary view (LineItems: [])
        which is missing TaxType and AccountCode — the audit needs those.
        Proxy returns the full invoice including line item details.
        """
        from app.core.config import settings
        use_action = settings.AUDIT_SOURCE == "action"
        documents: list[dict[str, Any]] = []
        for page in range(1, max(1, settings.MAX_NANGO_PAGES) + 1):
            if use_action:
                page_data = await self._nango.action_list_invoices_full(
                    connection_id, tenant_id=tenant_id, page=page)
            else:
                page_data = await self._nango.fetch_xero_invoices_page(connection_id, tenant_id, page)
            if not page_data:
                break
            documents.extend(page_data)
        logger.info("[Integration] fetched %d invoices via %s",
                    len(documents), "action" if use_action else "proxy")
        return documents

    async def fetch_all_credit_notes(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        """All credit notes via proxy/action with pagination (same reason as invoices)."""
        from app.core.config import settings
        use_action = settings.AUDIT_SOURCE == "action"
        documents: list[dict[str, Any]] = []
        for page in range(1, max(1, settings.MAX_NANGO_PAGES) + 1):
            if use_action:
                page_data = await self._nango.action_list_credit_notes_full(
                    connection_id, tenant_id=tenant_id, page=page)
            else:
                page_data = await self._nango.fetch_xero_credit_notes_page(connection_id, tenant_id, page)
            if not page_data:
                break
            documents.extend(page_data)
        logger.info("[Integration] fetched %d credit notes via %s",
                    len(documents), "action" if use_action else "proxy")
        return documents

    async def fetch_all_payments(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        """All payments via proxy with pagination. Each payment carries
        ``IsReconciled`` (bank-matched) + the invoice it settles, so the audit
        can mark which documents are bank-reconciled (duplicate risk signal)."""
        from app.core.config import settings
        payments: list[dict[str, Any]] = []
        for page in range(1, max(1, settings.MAX_NANGO_PAGES) + 1):
            page_data = await self._nango.fetch_xero_payments_page(connection_id, tenant_id, page)
            if not page_data:
                break
            payments.extend(page_data)
        logger.info("[Integration] fetched %d payments via proxy", len(payments))
        return payments

    async def fetch_invoices_page(
        self,
        connection_id: str,
        tenant_id: str,
        page: int,
    ) -> list[dict[str, Any]]:
        """One page of invoices via proxy (kept for compatibility)."""
        return await self._nango.fetch_xero_invoices_page(
            connection_id, tenant_id, page,
        )

    async def fetch_credit_notes_page(
        self,
        connection_id: str,
        tenant_id: str,
        page: int,
    ) -> list[dict[str, Any]]:
        """One page of credit notes via proxy (kept for compatibility)."""
        return await self._nango.fetch_xero_credit_notes_page(
            connection_id, tenant_id, page,
        )

    async def fetch_invoice(
        self,
        connection_id: str,
        tenant_id: str,
        invoice_id: str,
    ) -> Optional[dict[str, Any]]:
        """Single invoice/bill — used before writing a fix back."""
        return await self._nango.fetch_xero_invoice(
            connection_id, tenant_id, invoice_id,
        )

    async def fetch_attachable(
        self, connection_id: str, tenant_id: str, document_type: str, doc_id: str,
    ) -> Optional[dict[str, Any]]:
        """Single Invoice / BankTransaction → re-check ``HasAttachments``."""
        return await self._nango.fetch_xero_attachable(
            connection_id, tenant_id, _attachment_base(document_type), doc_id,
        )

    async def upload_attachment(
        self, connection_id: str, tenant_id: str, document_type: str, doc_id: str,
        filename: str, content: bytes, content_type: str,
    ) -> Optional[dict[str, Any]]:
        """Upload a file as an attachment on the Xero document."""
        return await self._nango.upload_xero_attachment(
            connection_id, tenant_id, _attachment_base(document_type), doc_id,
            filename, content, content_type,
        )

    async def update_invoice(
        self,
        connection_id: str,
        tenant_id: str,
        invoice_id: str,
        body: dict[str, Any],
        *,
        field_updates: Optional[dict[str, Any]] = None,
        line_item_updates: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Write a field/line-item fix back to the accounting org.

        Actions-first: header fields and line recodes both go through the
        Nango ``update-invoice`` Action. The proxy is only a fallback for
        when the action isn't enabled.
        """
        # Action-first. For a line RECODE, send the fully rebuilt LineItems from
        # `body` (LineItemID + unchanged lines + only the flagged line changed) —
        # the action does a Xero POST which REPLACES all lines, so a bare
        # {AccountCode} would drop LineItemID and wipe the other lines.
        action_fields: dict[str, Any] = dict(field_updates or {})
        if line_item_updates:
            body_lines = (body.get("Invoices") or [{}])[0].get("LineItems")
            if body_lines:
                action_fields["lineItems"] = body_lines
        if action_fields:
            result = await self._nango.action_update_invoice(
                connection_id=connection_id,
                invoice_id=invoice_id,
                fields=action_fields,
                tenant_id=tenant_id,
            )
            if result is not None:
                logger.info(
                    "[Integration] updated invoice %s via Action", invoice_id,
                )
                return result
            logger.info(
                "[Integration] update-invoice action unavailable — "
                "falling back to proxy for invoice %s", invoice_id,
            )

        # Proxy fallback: full Xero body.
        return await self._nango.update_xero_invoice(
            connection_id, tenant_id, invoice_id, body,
        )

    async def create_credit_note(
        self,
        connection_id: str,
        tenant_id: str,
        invoice_id: str,
    ) -> Optional[dict[str, Any]]:
        """Create a credit note in Xero that fully credits an invoice/bill, then
        allocate it to that document — the 'Credit Note' button on an old unpaid
        invoice (write-off / discount).

        Read-modify-create: fetch the source invoice to mirror its Contact and
        line items into a same-direction credit note (ACCREC→ACCRECCREDIT,
        ACCPAY→ACCPAYCREDIT), POST it AUTHORISED, then allocate. Returns the
        created credit note (with an ``allocation`` key) or ``None`` on any
        failure so the caller can fall back to a stub.
        """
        existing = await self.fetch_invoice(connection_id, tenant_id, invoice_id)
        invoice = None
        if isinstance(existing, dict):
            invoices = existing.get("Invoices")
            invoice = invoices[0] if isinstance(invoices, list) and invoices else existing
        if not isinstance(invoice, dict):
            logger.warning("[Integration] create_credit_note: invoice %s not found", invoice_id)
            return None

        inv_type = (invoice.get("Type") or "").strip().upper()
        credit_type = "ACCPAYCREDIT" if inv_type == "ACCPAY" else "ACCRECCREDIT"
        contact = invoice.get("Contact") or {}
        contact_id = contact.get("ContactID")
        if not contact_id:
            logger.warning("[Integration] create_credit_note: no ContactID on invoice %s", invoice_id)
            return None

        # Mirror the invoice's line items so the credit fully offsets it. Fall
        # back to a single line at the invoice total when line items are absent.
        line_items = []
        for li in (invoice.get("LineItems") or []):
            line = {
                "Description": li.get("Description") or "Credit note",
                "Quantity": li.get("Quantity") or 1,
                "UnitAmount": li.get("UnitAmount") if li.get("UnitAmount") is not None else li.get("LineAmount"),
            }
            if li.get("AccountCode"):
                line["AccountCode"] = li["AccountCode"]
            if li.get("TaxType"):
                line["TaxType"] = li["TaxType"]
            line_items.append(line)
        if not line_items:
            line_items = [{
                "Description": "Credit note",
                "Quantity": 1,
                "UnitAmount": invoice.get("Total") or invoice.get("AmountDue") or 0,
            }]

        body = {"CreditNotes": [{
            "Type": credit_type,
            "Status": "AUTHORISED",
            "Contact": {"ContactID": contact_id},
            "LineItems": line_items,
        }]}
        if invoice.get("LineAmountTypes"):
            body["CreditNotes"][0]["LineAmountTypes"] = invoice["LineAmountTypes"]
        if invoice.get("CurrencyCode"):
            body["CreditNotes"][0]["CurrencyCode"] = invoice["CurrencyCode"]

        created = await self._nango.create_xero_credit_note(connection_id, tenant_id, body)
        if not isinstance(created, dict):
            return None

        # Allocate the new credit note to the invoice (separate Xero call).
        cn_list = created.get("CreditNotes") or []
        credit_note_id = cn_list[0].get("CreditNoteID") if cn_list else None
        alloc_amount = invoice.get("AmountDue") or invoice.get("Total") or 0
        if credit_note_id and alloc_amount:
            allocation = await self._nango.allocate_xero_credit_note(
                connection_id, tenant_id, credit_note_id, invoice_id, float(alloc_amount),
            )
            created["allocation"] = allocation
        logger.info("[Integration] created credit note for invoice %s", invoice_id)
        return created

    async def update_contact_defaults(
        self,
        connection_id: str,
        tenant_id: str,
        contact_id: str,
        fields: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Write per-contact default account/tax fields back to Xero. ``fields``
        uses Xero names (SalesDefaultAccountCode, AccountsReceivableTaxType, …)."""
        return await self._nango.update_xero_contact(
            connection_id, tenant_id, contact_id, fields,
        )

    # ------------------------------------------------------------------
    # Chart of accounts + tax rates + organisation
    # ------------------------------------------------------------------

    async def fetch_chart_of_accounts(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        """COA via proxy — tenant-scoped.

        Uses the proxy (not the list-accounts Action) so the call targets
        the correct org via the tenant header. Actions resolve their org
        from connection metadata, which is wrong for a multi-org connection.
        """
        accounts = await self._nango.fetch_xero_accounts(connection_id, tenant_id)
        logger.info("[Integration] fetched %d COA accounts via proxy", len(accounts))
        return accounts

    async def fetch_tax_rates(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        """Active tax rates from the accounting org."""
        return await self._nango.fetch_xero_tax_rates(connection_id, tenant_id)

    async def fetch_organisation(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> Optional[dict[str, Any]]:
        """Org metadata: Name, BaseCurrency, ShortCode, etc."""
        return await self._nango.fetch_xero_organisation(connection_id, tenant_id)

    async def revoke_xero_org(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> dict[str, Any]:
        """Revoke ONE org's Xero grant via the ``revoke-connection`` Action so it
        returns to allow-access. Leaves other orgs on the same login connected."""
        return await self._nango.action_revoke_connection(connection_id, tenant_id)

    async def revoke_xero_org_grant(
        self,
        tenant_id: str,
        end_user_id: Optional[str] = None,
        fallback_connection_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Revoke an org's grant via whichever LIVE connection still holds it —
        the stored connection-id is often stale, so a plain revoke silently
        no-ops and the org stays greyed as ``Already connected`` in Xero.
        Firm-scoped by ``end_user_id``."""
        return await self._nango.revoke_org_grant(
            tenant_id,
            end_user_id=end_user_id,
            fallback_connection_id=fallback_connection_id,
        )

    async def fetch_profit_and_loss(
        self,
        connection_id: str,
        tenant_id: str,
        periods: int = 11,
        timeframe: str = "MONTH",
    ) -> Optional[dict[str, Any]]:
        """Xero ProfitAndLoss report. ``timeframe=MONTH`` powers Profitability +
        Sales Tracker; ``timeframe=YEAR`` powers the Corp Tax estimate."""
        return await self._nango.fetch_xero_profit_and_loss(
            connection_id, tenant_id, periods=periods, timeframe=timeframe,
        )

    async def fetch_balance_sheet(
        self,
        connection_id: str,
        tenant_id: str,
        as_at_date: Optional[str] = None,
        periods: Optional[int] = None,
        timeframe: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Xero BalanceSheet report — powers Dividend, Working Capital,
        and net-asset Valuation insights. ``as_at_date`` (YYYY-MM-DD) pins it
        to a period end for Opening Balance Differences; ``periods``+``timeframe``
        return comparative columns for the Cash Health Check's recent-movements."""
        return await self._nango.fetch_xero_balance_sheet(
            connection_id, tenant_id, as_at_date, periods=periods, timeframe=timeframe,
        )

    async def fetch_trial_balance(
        self,
        connection_id: str,
        tenant_id: str,
        as_at_date: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Xero TrialBalance report — every account's balance. Powers the
        Directors' Loan Account insight + Bank Balance Check. ``as_at_date``
        (YYYY-MM-DD) pins it to a period end.

        Prefers the ``get-trial-balance`` Nango Action (tenant-scoped, no live
        proxy); falls back to the proxy when the action isn't deployed yet."""
        report = await self._nango.action_get_trial_balance(
            connection_id, tenant_id, as_at_date,
        )
        if report is not None:
            return report
        return await self._nango.fetch_xero_trial_balance(
            connection_id, tenant_id, as_at_date,
        )

    async def fetch_bank_summary(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> Optional[dict[str, Any]]:
        """Xero BankSummary report — closing (statement) balance per bank
        account. Powers the Bank Balance check (statement vs GL gap)."""
        return await self._nango.fetch_xero_bank_summary(connection_id, tenant_id)

    async def fetch_all_bank_transactions(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        """All bank transactions (spend/receive money) via proxy, paged.
        Powers the Bank Reconciliation insight (last-reconciled + unreconciled
        count via the ``IsReconciled`` flag)."""
        from app.core.config import settings
        use_action = settings.AUDIT_SOURCE == "action"
        out: list[dict[str, Any]] = []
        for page in range(1, max(1, settings.MAX_NANGO_PAGES) + 1):
            if use_action:
                page_data = await self._nango.action_list_bank_transactions_full(
                    connection_id, tenant_id=tenant_id, page=page)
            else:
                page_data = await self._nango.fetch_xero_bank_transactions_page(
                    connection_id, tenant_id, page,
                )
            if not page_data:
                break
            out.extend(page_data)
        logger.info("[Integration] fetched %d bank transactions via %s",
                    len(out), "action" if use_action else "proxy")
        return out

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    async def fetch_contacts(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        """All contacts from the accounting org — via proxy, tenant-scoped.

        Must pass tenant_id: the proxy threads it as the org selector so
        the right org's contacts come back even on a multi-org connection.
        """
        return await self._nango.fetch_xero_contacts(connection_id, tenant_id)

    # ------------------------------------------------------------------
    # OAuth / connection management
    # ------------------------------------------------------------------

    async def list_tenants(
        self,
        connection_id: str,
    ) -> list[dict[str, Any]]:
        """Every Xero org this connection covers — one accountant's grant
        fans out to many client organisations. Each item:
        ``{"tenant_id", "tenant_name", "tenant_type"}``."""
        return await self._nango.list_xero_tenants(connection_id)

    async def create_connect_session(
        self,
        end_user_id: str,
    ) -> Optional[dict[str, Any]]:
        """Initiate a Nango Connect session for the frontend OAuth popup."""
        return await self._nango.create_xero_connect_session(end_user_id)

    async def get_connection_info(
        self,
        connection_id: str,
    ) -> Optional[dict[str, Any]]:
        """Raw Nango connection record — used after OAuth to extract tenant_id."""
        return await self._nango.get_connection_info(connection_id)
