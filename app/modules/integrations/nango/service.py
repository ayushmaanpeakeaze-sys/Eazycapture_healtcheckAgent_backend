"""The ONLY allowed interface for all Nango operations in the monolith.

Use cases and services MUST call ``NangoService``, never
``NangoClient`` directly. The client lives intentionally narrowly
scoped to this package: future swaps (different proxy, different
provider, mocks for tests) only need to touch :class:`NangoService`.

Every public method below is fail-open: ``None`` / empty list on
disabled-or-error so the caller can branch on ``is_available()``.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.config import settings
from app.modules.integrations.nango.client import NangoClient

logger = logging.getLogger("eazycapture.nango.service")

_LOG_TAG = "[SuHe][Nango]"


def _connection_end_user_id(cn: dict) -> str:
    """The end-user id stamped on a Nango connection at connect-session time.
    Checks the ``end_user.id`` object and the ``tags.end_user_id`` mirror."""
    eu = cn.get("end_user")
    if isinstance(eu, dict) and eu.get("id"):
        return str(eu["id"]).strip()
    tags = cn.get("tags")
    if isinstance(tags, dict) and tags.get("end_user_id"):
        return str(tags["end_user_id"]).strip()
    return ""


class NangoService:
    """High-level Nango operations used by the rest of the app."""

    def __init__(self, client: Optional[NangoClient] = None) -> None:
        self._client = client or NangoClient()
        self._provider_config_key = settings.NANGO_XERO_INTEGRATION_ID

    def is_available(self) -> bool:
        """``True`` when Nango is configured. Callers branch on this
        to decide between the real path and the seed/stub fallback."""
        return self._client._is_enabled()

    async def _live_xero_connection_ids(
        self, end_user_id: Optional[str] = None,
    ) -> list[str]:
        """All live Xero connection-ids in Nango, newest first. Scoped to
        ``end_user_id`` (firm isolation) when given."""
        c = self._client
        if not c._is_enabled():
            return []
        body = await c._send(
            "GET", f"{c._base_url}/connection",
            headers={"Authorization": f"Bearer {c._secret_key}"},
        )
        conns = (body or {}).get("connections", body if isinstance(body, list) else [])
        xero = [
            cn for cn in conns
            if (cn.get("provider_config_key") or cn.get("provider")) == self._provider_config_key
        ]
        if end_user_id is not None:
            want = str(end_user_id).strip()
            xero = [cn for cn in xero if _connection_end_user_id(cn) == want]
        xero.sort(key=lambda cn: cn.get("created") or cn.get("created_at") or "", reverse=True)
        ids: list[str] = []
        for cn in xero:
            cid = (cn.get("connection_id") or cn.get("id") or "").strip()
            if cid and cid not in ids:
                ids.append(cid)
        return ids

    async def find_live_xero_connection(
        self, end_user_id: Optional[str] = None,
    ) -> Optional[tuple[str, str]]:
        """Newest live Xero connection ``(connection_id, tenant_id)`` in Nango, or
        None. Lets an audit SELF-HEAL when a company's stored connection-id has
        gone stale — the Nango free plan mints a brand-new connection-id on every
        reconnect, leaving the company row pointing at a dead one.

        When ``end_user_id`` is given, ONLY that end-user's connections are
        considered. The connect-session stamps every connection with the
        authenticated user's id, so this scopes the lookup to the caller — one
        firm can never pick up another firm's connection."""
        ids = await self._live_xero_connection_ids(end_user_id)
        if not ids:
            return None
        cid = ids[0]
        full = await self._client.get_connection(cid, self._provider_config_key)
        tenant = ((full or {}).get("connection_config") or {}).get("tenant_id")
        if not tenant:
            return None
        return cid, tenant

    async def revoke_org_grant(
        self,
        tenant_id: str,
        end_user_id: Optional[str] = None,
        fallback_connection_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Revoke ONE org's Xero grant. A company's stored connection-id often
        goes stale (Nango mints a new one per reconnect), so this scans the
        firm's LIVE Xero connections and revokes via whichever still holds the
        tenant — the stored id is only a last-resort fallback. Firm-scoped via
        ``end_user_id``."""
        candidates = await self._live_xero_connection_ids(end_user_id)
        if fallback_connection_id and fallback_connection_id not in candidates:
            candidates.append(fallback_connection_id)
        for cid in candidates:
            result = await self.action_revoke_connection(cid, tenant_id)
            if result.get("revoked"):
                return result
        return {"revoked": False, "connectionId": None}

    # ---------------------------------------------------------------
    # Xero reads
    # ---------------------------------------------------------------

    async def fetch_xero_invoices_page(
        self,
        connection_id: str,
        tenant_id: str,
        page: int,
    ) -> list[dict[str, Any]]:
        """Return one page of Xero Invoices for a tenant. Empty list
        on disabled-or-error so the caller's pagination loop just
        stops without special-casing."""
        body = await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="api.xro/2.0/Invoices",
            tenant_id=tenant_id,
            params={"page": page},
        )
        if not isinstance(body, dict):
            return []
        invoices = body.get("Invoices") or []
        return invoices if isinstance(invoices, list) else []

    async def fetch_xero_credit_notes_page(
        self,
        connection_id: str,
        tenant_id: str,
        page: int,
    ) -> list[dict[str, Any]]:
        body = await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="api.xro/2.0/CreditNotes",
            tenant_id=tenant_id,
            params={"page": page},
        )
        if not isinstance(body, dict):
            return []
        credit_notes = body.get("CreditNotes") or []
        return credit_notes if isinstance(credit_notes, list) else []

    async def fetch_xero_payments_page(
        self,
        connection_id: str,
        tenant_id: str,
        page: int,
    ) -> list[dict[str, Any]]:
        """One page of Xero Payments. Each payment carries ``IsReconciled`` (the
        bank-matched flag) and the ``Invoice`` it settles — used to mark which
        invoices/bills are bank-reconciled for the duplicate risk signal."""
        body = await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="api.xro/2.0/Payments",
            tenant_id=tenant_id,
            params={"page": page},
        )
        if not isinstance(body, dict):
            return []
        payments = body.get("Payments") or []
        return payments if isinstance(payments, list) else []

    async def fetch_xero_journals_page(
        self,
        connection_id: str,
        tenant_id: str,
        page: int,
    ) -> list[dict[str, Any]]:
        """One page of raw Xero Journals — the GL postings (incl. manual journals).
        Xero paginates journals by JournalNumber offset → offset = (page-1)*100.
        Proxy fallback for the list-journals action; needs accounting.journals.read."""
        body = await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="api.xro/2.0/Journals",
            tenant_id=tenant_id,
            params={"offset": (page - 1) * 100},
        )
        if not isinstance(body, dict):
            return []
        journals = body.get("Journals") or []
        return journals if isinstance(journals, list) else []

    async def fetch_xero_assets_page(
        self,
        connection_id: str,
        tenant_id: str,
        page: int,
    ) -> list[dict[str, Any]]:
        """One page of the Xero fixed-asset register (assets.xro/1.0, REGISTERED).
        Proxy fallback for the list-assets action; needs the assets.read scope."""
        body = await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="assets.xro/1.0/Assets",
            tenant_id=tenant_id,
            params={"status": "REGISTERED", "page": page, "pageSize": 100},
        )
        if not isinstance(body, dict):
            return []
        assets = body.get("items") or []
        return assets if isinstance(assets, list) else []

    async def fetch_xero_invoice(
        self,
        connection_id: str,
        tenant_id: str,
        invoice_id: str,
    ) -> Optional[dict[str, Any]]:
        """Single invoice — needed for line-item resolves so we can
        preserve ``LineItemID`` + the unchanged fields on the
        round-trip back to Xero."""
        return await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint=f"api.xro/2.0/Invoices/{invoice_id}",
            tenant_id=tenant_id,
        )

    # ---------------------------------------------------------------
    # Actions  (toggle ON in Nango dashboard to enable)
    # ---------------------------------------------------------------

    async def action_list_accounts(
        self,
        connection_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch COA via the pre-built ``list-accounts`` Nango Action.

        The Action must be toggled ON in the Nango dashboard.
        Returns [] if the action is disabled or errors.
        """
        result = await self._client.trigger_action(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            action="list-accounts",
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("accounts", "Accounts", "items", "data"):
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    async def action_list_invoices(
        self,
        connection_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch all invoices/bills via the ``list-invoices`` Nango Action."""
        result = await self._client.trigger_action(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            action="list-invoices",
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("invoices", "Invoices", "items", "data"):
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    async def action_list_credit_notes(
        self,
        connection_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch credit notes via the ``list-credit-notes`` Nango Action."""
        result = await self._client.trigger_action(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            action="list-credit-notes",
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("creditNotes", "CreditNotes", "items", "data"):
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    async def action_list_contacts(
        self,
        connection_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch contacts via the ``list-contacts`` Nango Action."""
        result = await self._client.trigger_action(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            action="list-contacts",
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("contacts", "Contacts", "items", "data"):
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    async def action_update_invoice(
        self,
        connection_id: str,
        invoice_id: str,
        fields: dict[str, Any],
        tenant_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Update an invoice via the ``update-invoice`` Nango Action.
        ``tenant_id`` passed per-call (one connection → many Xero orgs)."""
        input_data: dict[str, Any] = {"invoiceId": invoice_id, **fields}
        if tenant_id:
            input_data["tenantId"] = tenant_id
        result = await self._client.trigger_action(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            action="update-invoice",
            input_data=input_data,
        )
        return result if isinstance(result, dict) else None

    # --- custom FULL-data list actions (line items intact; pre-built strip them) ---
    async def _action_list_full(
        self,
        connection_id: str,
        action: str,
        result_key: str,
        tenant_id: Optional[str] = None,
        page: int = 1,
        where: Optional[str] = None,
        modified_since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """One page of a custom ``list-*-full`` action. ``tenant_id`` is passed
        PER-CALL (one connection can cover many Xero orgs, so the org can't live
        in connection metadata). Empty list when the action isn't enabled
        (404 → None) so the caller's page loop just stops."""
        input_data: dict[str, Any] = {"page": page}
        if tenant_id:
            input_data["tenantId"] = tenant_id
        if where:
            input_data["where"] = where
        if modified_since:
            input_data["modifiedSince"] = modified_since
        result = await self._client.trigger_action(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            action=action,
            input_data=input_data,
            surface_auth=True,
        )
        if isinstance(result, dict) and isinstance(result.get(result_key), list):
            return result[result_key]
        return []

    async def action_list_invoices_full(
        self, connection_id: str, tenant_id: Optional[str] = None, page: int = 1,
        where: Optional[str] = None, modified_since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """``list-invoices-full`` — invoices/bills WITH line items, one page."""
        return await self._action_list_full(
            connection_id, "list-invoices-full", "invoices", tenant_id, page, where, modified_since)

    async def action_list_bank_transactions_full(
        self, connection_id: str, tenant_id: Optional[str] = None, page: int = 1,
        where: Optional[str] = None, modified_since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """``list-bank-transactions-full`` — Money In/Out WITH lines + IsReconciled."""
        return await self._action_list_full(
            connection_id, "list-bank-transactions-full", "bankTransactions", tenant_id, page, where, modified_since)

    async def action_list_contacts_full(
        self, connection_id: str, tenant_id: Optional[str] = None, page: int = 1,
        where: Optional[str] = None, modified_since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """``list-contacts-full`` — contacts WITH defaults (sales/purchase
        account + tax), email, status. One page."""
        return await self._action_list_full(
            connection_id, "list-contacts-full", "contacts", tenant_id, page, where, modified_since)

    async def action_list_accounts_full(
        self, connection_id: str, tenant_id: Optional[str] = None, page: int = 1,
        where: Optional[str] = None, modified_since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """``list-accounts-full`` — full chart of accounts (code, name, Type,
        TaxType, Class, status). Xero doesn't paginate accounts → single page."""
        return await self._action_list_full(
            connection_id, "list-accounts-full", "accounts", tenant_id, page, where, modified_since)

    async def action_list_credit_notes_full(
        self, connection_id: str, tenant_id: Optional[str] = None, page: int = 1,
        where: Optional[str] = None, modified_since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """``list-credit-notes-full`` — credit notes WITH lines + RemainingCredit."""
        return await self._action_list_full(
            connection_id, "list-credit-notes-full", "creditNotes", tenant_id, page, where, modified_since)

    async def action_list_tax_rates(
        self, connection_id: str, tenant_id: Optional[str] = None, page: int = 1,
        where: Optional[str] = None, modified_since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """``list-tax-rates`` — every Xero tax rate (single page, Xero doesn't paginate)."""
        return await self._action_list_full(
            connection_id, "list-tax-rates", "taxRates", tenant_id, page, where, modified_since)

    async def action_list_payments(
        self, connection_id: str, tenant_id: Optional[str] = None, page: int = 1,
        where: Optional[str] = None, modified_since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """``list-payments`` — Xero payments, one page (IsReconciled + settled invoice)."""
        return await self._action_list_full(
            connection_id, "list-payments", "payments", tenant_id, page, where, modified_since)

    async def action_list_organisation(
        self, connection_id: str, tenant_id: Optional[str] = None, page: int = 1,
        where: Optional[str] = None, modified_since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """``list-organisation`` — the Xero organisation record (single page)."""
        return await self._action_list_full(
            connection_id, "list-organisation", "organisations", tenant_id, page, where, modified_since)

    async def action_list_journals(
        self, connection_id: str, tenant_id: Optional[str] = None, page: int = 1,
        where: Optional[str] = None, modified_since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """``list-journals`` — one page of raw GL journals WITH lines (incl. manual
        journals). Needs the ``accounting.journals.read`` scope on the connection."""
        return await self._action_list_full(
            connection_id, "list-journals", "journals", tenant_id, page, where, modified_since)

    async def action_list_assets(
        self, connection_id: str, tenant_id: Optional[str] = None, page: int = 1,
        where: Optional[str] = None, modified_since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """``list-assets`` — one page of the fixed-asset register (REGISTERED).
        Needs the ``assets.read`` scope on the connection."""
        return await self._action_list_full(
            connection_id, "list-assets", "assets", tenant_id, page, where, modified_since)

    async def action_list_payroll_employees(
        self, connection_id: str, tenant_id: Optional[str] = None, page: int = 1,
        where: Optional[str] = None, modified_since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """``list-payroll-employees`` — one page of Xero Payroll employees (approved
        staff). Needs the ``payroll.employees.read`` scope on the connection."""
        return await self._action_list_full(
            connection_id, "list-payroll-employees", "employees", tenant_id, page, where, modified_since)

    async def action_get_trial_balance(
        self,
        connection_id: str,
        tenant_id: Optional[str] = None,
        date: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """``get-trial-balance`` — the Xero TrialBalance report via the Nango
        Action (tenant-scoped). Returns the single report dict, or None when the
        action isn't enabled yet / errors — so the caller can fall back to the
        proxy."""
        input_data: dict[str, Any] = {}
        if tenant_id:
            input_data["tenantId"] = tenant_id
        if date:
            input_data["date"] = date
        try:
            result = await self._client.trigger_action(
                connection_id=connection_id,
                provider_config_key=self._provider_config_key,
                action="get-trial-balance",
                input_data=input_data,
            )
        except Exception as exc:   # action not deployed / transient — let caller fall back
            logger.info("[Nango] get-trial-balance action unavailable: %s", exc)
            return None
        if isinstance(result, dict) and isinstance(result.get("report"), dict):
            return result["report"]
        return None

    async def action_revoke_connection(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> dict[str, Any]:
        """``revoke-connection`` — revoke ONE org's Xero grant (DELETE /connections/{id})."""
        try:
            result = await self._client.trigger_action(
                connection_id=connection_id,
                provider_config_key=self._provider_config_key,
                action="revoke-connection",
                input_data={"tenantId": tenant_id},
            )
        except Exception as exc:
            logger.warning("[Nango] revoke-connection failed for %s: %s", tenant_id, exc)
            return {"revoked": False, "connectionId": None}
        if isinstance(result, dict):
            return {
                "revoked": bool(result.get("revoked")),
                "connectionId": result.get("connectionId"),
            }
        return {"revoked": False, "connectionId": None}

    # ---------------------------------------------------------------
    # Proxy fallbacks (used when the Action is not yet toggled on)
    # ---------------------------------------------------------------

    async def fetch_xero_accounts(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch COA via proxy (fallback when list-accounts action is off)."""
        body = await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="api.xro/2.0/Accounts",
            tenant_id=tenant_id,
            params={"where": 'Status=="ACTIVE"'},
        )
        if not isinstance(body, dict):
            return []
        accounts = body.get("Accounts") or []
        return accounts if isinstance(accounts, list) else []

    async def fetch_xero_tax_rates(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch tax rates via proxy (no Action equivalent currently)."""
        body = await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="api.xro/2.0/TaxRates",
            tenant_id=tenant_id,
            params={"where": 'Status=="ACTIVE"'},
        )
        if not isinstance(body, dict):
            return []
        rates = body.get("TaxRates") or []
        return rates if isinstance(rates, list) else []

    async def fetch_xero_contacts(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch contacts via proxy — tenant-scoped, PAGED.

        IMPORTANT 1: contacts MUST go through the proxy (which passes the
        nango-proxy-xero-tenant-id header per call), NOT the list-contacts
        Action. The Action resolves its org from connection metadata, which
        is last-write-wins and wrong for a multi-org connection.

        IMPORTANT 2: we must PAGE (``?page=N``). Per Xero's docs, a non-paged
        GET /Contacts returns only a SUBSET of elements — it omits
        ``SalesDefaultAccountCode`` / ``PurchasesDefaultAccountCode`` (and other
        detail fields). Paging returns the full element set per contact, which
        the Contact-Defaults + Unexpected-Account checks rely on. 100/page.
        """
        from app.core.config import settings

        out: list[dict[str, Any]] = []
        for page in range(1, max(1, settings.MAX_NANGO_PAGES) + 1):
            body = await self._client.proxy_get(
                connection_id=connection_id,
                provider_config_key=self._provider_config_key,
                endpoint="api.xro/2.0/Contacts",
                tenant_id=tenant_id,
                params={"page": page},
            )
            if not isinstance(body, dict):
                break
            contacts = body.get("Contacts") or []
            if not isinstance(contacts, list) or not contacts:
                break
            out.extend(contacts)
            if len(contacts) < 100:  # Xero returns 100/page → fewer = last page
                break
        return out

    async def fetch_xero_organisation(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> Optional[dict[str, Any]]:
        """Org metadata (Name, ShortCode, BaseCurrency, …). Called by
        the webhook handler so we can persist the shortcode for
        tenant-scoped deep-links."""
        body = await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="api.xro/2.0/Organisation",
            tenant_id=tenant_id,
        )
        if not isinstance(body, dict):
            return None
        orgs = body.get("Organisations") or []
        if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict):
            return orgs[0]
        return None

    async def fetch_xero_profit_and_loss(
        self,
        connection_id: str,
        tenant_id: str,
        periods: int = 11,
        timeframe: str = "MONTH",
    ) -> Optional[dict[str, Any]]:
        """Xero ProfitAndLoss report. ``periods``+``timeframe`` make Xero
        return a multi-column comparison (e.g. 12 monthly columns). Returns
        the single report dict from the ``Reports`` array, or None on failure."""
        body = await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="api.xro/2.0/Reports/ProfitAndLoss",
            tenant_id=tenant_id,
            params={"periods": str(periods), "timeframe": timeframe},
        )
        if not isinstance(body, dict):
            return None
        reports = body.get("Reports") or []
        if isinstance(reports, list) and reports and isinstance(reports[0], dict):
            return reports[0]
        return None

    async def fetch_xero_balance_sheet(
        self,
        connection_id: str,
        tenant_id: str,
        as_at_date: Optional[str] = None,
        periods: Optional[int] = None,
        timeframe: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Xero BalanceSheet report. ``as_at_date`` (YYYY-MM-DD) pins the report
        to a period end — used by Opening Balance Differences to read Net Assets
        at each Companies-House filing date. ``periods``+``timeframe`` (e.g. 4 +
        ``MONTH``) make Xero return comparative period-end columns — used by the
        Cash Health Check's recent-movements. Returns the single report dict from
        the ``Reports`` array, or None on failure."""
        params: dict[str, str] = {}
        if as_at_date:
            params["date"] = as_at_date
        if periods is not None:
            params["periods"] = str(periods)
        if timeframe:
            params["timeframe"] = timeframe
        body = await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="api.xro/2.0/Reports/BalanceSheet",
            tenant_id=tenant_id,
            params=params or None,
        )
        if not isinstance(body, dict):
            return None
        reports = body.get("Reports") or []
        if isinstance(reports, list) and reports and isinstance(reports[0], dict):
            return reports[0]
        return None

    async def fetch_xero_bank_transactions_page(
        self,
        connection_id: str,
        tenant_id: str,
        page: int,
    ) -> list[dict[str, Any]]:
        """One page of Xero BankTransactions (spend/receive money, with the
        ``IsReconciled`` flag). Empty list on disabled-or-error so the caller's
        pagination loop just stops. Does NOT expose raw bank-statement feeds."""
        body = await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="api.xro/2.0/BankTransactions",
            tenant_id=tenant_id,
            params={"page": page},
        )
        if not isinstance(body, dict):
            return []
        txns = body.get("BankTransactions") or []
        return txns if isinstance(txns, list) else []

    async def fetch_xero_trial_balance(
        self,
        connection_id: str,
        tenant_id: str,
        as_at_date: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Xero TrialBalance report (every account's balance). ``as_at_date``
        (YYYY-MM-DD) pins it to a period end — used by the Bank Balance Check to
        read each bank account's GL balance at the selected closing date.
        Returns the single report dict from the ``Reports`` array."""
        params = {"date": as_at_date} if as_at_date else None
        body = await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="api.xro/2.0/Reports/TrialBalance",
            tenant_id=tenant_id,
            params=params,
        )
        if not isinstance(body, dict):
            return None
        reports = body.get("Reports") or []
        if isinstance(reports, list) and reports and isinstance(reports[0], dict):
            return reports[0]
        return None

    async def fetch_xero_bank_summary(
        self,
        connection_id: str,
        tenant_id: str,
    ) -> Optional[dict[str, Any]]:
        """Xero BankSummary report — opening/closing (statement) balance per
        bank account. Powers the Bank Balance check (statement vs GL gap).
        Returns the single report dict from the ``Reports`` array."""
        body = await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="api.xro/2.0/Reports/BankSummary",
            tenant_id=tenant_id,
        )
        if not isinstance(body, dict):
            return None
        reports = body.get("Reports") or []
        if isinstance(reports, list) and reports and isinstance(reports[0], dict):
            return reports[0]
        return None

    # ---------------------------------------------------------------
    # Xero writes
    # ---------------------------------------------------------------

    async def update_xero_invoice(
        self,
        connection_id: str,
        tenant_id: str,
        invoice_id: str,
        body: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """POST the invoice update. Xero's API treats proxy POST as a
        modify-in-place when ``InvoiceID`` is in the body."""
        result = await self._client.proxy_post(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint=f"api.xro/2.0/Invoices/{invoice_id}",
            tenant_id=tenant_id,
            json_body=body,
        )
        if result is None:
            logger.warning(
                "%s update_xero_invoice failed invoice_id=%s", _LOG_TAG, invoice_id,
            )
        return result

    async def fetch_xero_attachable(
        self,
        connection_id: str,
        tenant_id: str,
        endpoint_base: str,
        doc_id: str,
    ) -> Optional[dict[str, Any]]:
        """Single Invoice / BankTransaction (endpoint_base) — used to RE-CHECK
        ``HasAttachments`` after the user uploads a document in Xero."""
        return await self._client.proxy_get(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint=f"api.xro/2.0/{endpoint_base}/{doc_id}",
            tenant_id=tenant_id,
        )

    async def upload_xero_attachment(
        self,
        connection_id: str,
        tenant_id: str,
        endpoint_base: str,
        doc_id: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> Optional[dict[str, Any]]:
        """Upload a file as an attachment on a Xero Invoice / BankTransaction:
        ``PUT /{base}/{id}/Attachments/{filename}`` with the raw file body."""
        result = await self._client.proxy_put_binary(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint=f"api.xro/2.0/{endpoint_base}/{doc_id}/Attachments/{filename}",
            tenant_id=tenant_id,
            content=content,
            content_type=content_type,
        )
        if result is None:
            logger.warning(
                "%s upload_xero_attachment failed doc=%s/%s", _LOG_TAG, endpoint_base, doc_id,
            )
        return result

    async def update_xero_contact(
        self,
        connection_id: str,
        tenant_id: str,
        contact_id: str,
        fields: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """POST a contact update (modify-in-place when ``ContactID`` is in the
        body). Used to write per-contact defaults — SalesDefaultAccountCode,
        PurchasesDefaultAccountCode, AccountsReceivableTaxType,
        AccountsPayableTaxType."""
        body = {"Contacts": [{"ContactID": contact_id, **fields}]}
        result = await self._client.proxy_post(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint=f"api.xro/2.0/Contacts/{contact_id}",
            tenant_id=tenant_id,
            json_body=body,
        )
        if result is None:
            logger.warning(
                "%s update_xero_contact failed contact_id=%s", _LOG_TAG, contact_id,
            )
        return result

    async def create_xero_credit_note(
        self,
        connection_id: str,
        tenant_id: str,
        body: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """POST a credit note to Xero (``POST /CreditNotes``). ``body`` is the
        full Xero payload, e.g. ``{"CreditNotes": [{"Type": "ACCRECCREDIT",
        "Status": "AUTHORISED", "Contact": {...}, "LineItems": [...]}]}``."""
        result = await self._client.proxy_post(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint="api.xro/2.0/CreditNotes",
            tenant_id=tenant_id,
            json_body=body,
        )
        if result is None:
            logger.warning("%s create_xero_credit_note failed", _LOG_TAG)
        return result

    async def allocate_xero_credit_note(
        self,
        connection_id: str,
        tenant_id: str,
        credit_note_id: str,
        invoice_id: str,
        amount: float,
    ) -> Optional[dict[str, Any]]:
        """Allocate an AUTHORISED credit note to an invoice. Xero exposes this as
        ``PUT /CreditNotes/{id}/Allocations`` with the allocation wrapped in an
        ``Allocations`` array — a POST or a bare (unwrapped) body 404s. Separate
        call from creation."""
        body = {"Allocations": [{"Amount": amount, "Invoice": {"InvoiceID": invoice_id}}]}
        result = await self._client.proxy_put(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
            endpoint=f"api.xro/2.0/CreditNotes/{credit_note_id}/Allocations",
            tenant_id=tenant_id,
            json_body=body,
        )
        if result is None:
            logger.warning(
                "%s allocate_xero_credit_note failed credit_note_id=%s",
                _LOG_TAG, credit_note_id,
            )
        return result

    # ---------------------------------------------------------------
    # Connect-session + connection metadata
    # ---------------------------------------------------------------

    async def create_xero_connect_session(
        self,
        end_user_id: str,
    ) -> Optional[dict[str, Any]]:
        return await self._client.create_connect_session(
            end_user_id=end_user_id,
            allowed_integrations=[self._provider_config_key],
        )

    async def get_connection_info(
        self,
        connection_id: str,
    ) -> Optional[dict[str, Any]]:
        return await self._client.get_connection(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
        )

    async def list_xero_tenants(
        self,
        connection_id: str,
    ) -> list[dict[str, Any]]:
        """Every Xero org (tenant) this connection can access.

        Returns a list of ``{"tenantId", "tenantName", "tenantType"}`` dicts.
        Empty list on disabled-or-error. This is how one accountant's single
        OAuth grant fans out to many client organisations.
        """
        body = await self._client.list_xero_connections(
            connection_id=connection_id,
            provider_config_key=self._provider_config_key,
        )
        if not isinstance(body, list):
            return []
        # Keep only ORGANISATION tenants (Xero may also list PRACTICE manager).
        out: list[dict[str, Any]] = []
        for t in body:
            if not isinstance(t, dict):
                continue
            tenant_id = (t.get("tenantId") or "").strip()
            if not tenant_id:
                continue
            ttype = (t.get("tenantType") or "ORGANISATION").strip().upper()
            if ttype and ttype != "ORGANISATION":
                continue
            out.append({
                "tenant_id": tenant_id,
                "tenant_name": (t.get("tenantName") or "").strip() or "Untitled org",
                "tenant_type": ttype,
            })
        return out
