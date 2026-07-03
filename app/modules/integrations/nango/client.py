"""Low-level async httpx wrapper around the Nango REST API.

**This is the only file allowed to call ``api.nango.dev``.** Every
other module — including the historical-audit task and the resolve
service — talks to Nango through :class:`NangoService`.

Fail-open on purpose: the client returns ``None`` on missing
credentials, transport errors, non-2xx responses, or undecodeable
bodies. Callers branch on ``is_available()`` and decide whether to
fall back to seeded data / stub responses, or surface a 503.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger("eazycapture.nango.client")

# Tag used on warnings so a grep for ``[Nango]`` shows every Nango
# call site in the logs.
_LOG_TAG = "[SuHe][Nango]"


class NangoAuthError(RuntimeError):
    """Xero rejected the connection (HTTP 401/403) — the OAuth token is expired
    or revoked. Raised (not swallowed to None) on READ calls so the audit fails
    visibly with a 'reconnect Xero' message instead of silently falling back to
    stale seed data."""

# Xero rate-limit (HTTP 429) retry policy.
_MAX_RATE_LIMIT_RETRIES = 3
_MAX_RATE_LIMIT_WAIT_S = 30.0

# Timeout for Nango control-plane / proxy calls. Kept short so a hung
# Nango call cannot block a webhook request for minutes.
_NANGO_TIMEOUT_S = 30.0


class NangoClient:
    """Thin wrapper around Nango's REST + proxy endpoints.

    Construct with an optional ``secret_key`` override (mostly for
    tests). Production callers should let the client read from
    ``settings.NANGO_SECRET_KEY`` and use ``is_available()`` to branch.
    """

    def __init__(
        self,
        *,
        secret_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> None:
        self._base_url = (base_url or settings.NANGO_BASE_URL).rstrip("/")
        self._secret_key = (
            secret_key
            if secret_key is not None
            else settings.NANGO_SECRET_KEY
        )
        timeout_s = (timeout_ms / 1000.0) if timeout_ms else _NANGO_TIMEOUT_S
        self._timeout = httpx.Timeout(max(1.0, timeout_s))

    def _is_enabled(self) -> bool:
        """Public for the rare in-module test; everyone else should
        ask :class:`NangoService` instead."""
        return bool((self._secret_key or "").strip())

    # ---------------------------------------------------------------
    # Proxy endpoints
    # ---------------------------------------------------------------

    async def proxy_get(
        self,
        connection_id: str,
        provider_config_key: str,
        endpoint: str,
        tenant_id: Optional[str] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """GET via Nango's proxy. Returns ``None`` on any failure."""
        if not self._is_enabled():
            logger.info("%s GET %s skipped — secret key not set", _LOG_TAG, endpoint)
            return None
        url = f"{self._base_url}/proxy/{endpoint.lstrip('/')}"
        return await self._send(
            "GET",
            url,
            headers=self._headers(connection_id, provider_config_key, tenant_id),
            params=params,
        )

    async def proxy_post(
        self,
        connection_id: str,
        provider_config_key: str,
        endpoint: str,
        tenant_id: Optional[str],
        json_body: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """POST via Nango's proxy. Xero treats its proxy POST as a
        PUT-like update for invoice modifications."""
        if not self._is_enabled():
            logger.info("%s POST %s skipped — secret key not set", _LOG_TAG, endpoint)
            return None
        url = f"{self._base_url}/proxy/{endpoint.lstrip('/')}"
        return await self._send(
            "POST",
            url,
            headers=self._headers(connection_id, provider_config_key, tenant_id),
            json=json_body,
        )

    async def proxy_put(
        self,
        connection_id: str,
        provider_config_key: str,
        endpoint: str,
        tenant_id: Optional[str],
        json_body: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """PUT via Nango's proxy. Needed for endpoints Xero only exposes as PUT
        (e.g. ``/CreditNotes/{id}/Allocations``). Returns ``None`` on failure."""
        if not self._is_enabled():
            logger.info("%s PUT %s skipped — secret key not set", _LOG_TAG, endpoint)
            return None
        url = f"{self._base_url}/proxy/{endpoint.lstrip('/')}"
        return await self._send(
            "PUT",
            url,
            headers=self._headers(connection_id, provider_config_key, tenant_id),
            json=json_body,
        )

    async def proxy_put_binary(
        self,
        connection_id: str,
        provider_config_key: str,
        endpoint: str,
        tenant_id: Optional[str],
        content: bytes,
        content_type: str,
    ) -> Optional[dict[str, Any]]:
        """PUT raw bytes via the proxy — for Xero attachment uploads
        (``/Invoices/{id}/Attachments/{filename}``), which take a binary body
        rather than JSON. Returns the attachment metadata, or None on failure."""
        if not self._is_enabled():
            logger.info("%s PUT(bin) %s skipped — secret key not set", _LOG_TAG, endpoint)
            return None
        url = f"{self._base_url}/proxy/{endpoint.lstrip('/')}"
        headers = self._headers(connection_id, provider_config_key, tenant_id)
        headers["Content-Type"] = content_type
        return await self._send("PUT", url, headers=headers, content=content)

    # ---------------------------------------------------------------
    # Actions  (pre-built TypeScript functions toggled on in dashboard)
    # ---------------------------------------------------------------

    async def trigger_action(
        self,
        connection_id: str,
        provider_config_key: str,
        action: str,
        input_data: Optional[dict[str, Any]] = None,
        surface_auth: bool = False,
    ) -> Optional[Any]:
        """Call a Nango Action synchronously and return whatever JSON the
        TypeScript function returns.

        Toggle the action ON in the Nango dashboard first — if the action
        is disabled, Nango returns 404 and this returns None.
        """
        if not self._is_enabled():
            logger.info("%s action %s skipped — secret key not set", _LOG_TAG, action)
            return None
        url = f"{self._base_url}/action/trigger"
        return await self._send(
            "POST",
            url,
            headers=self._headers(connection_id, provider_config_key),
            json={
                "action_name": action,
                "input": input_data or {},
            },
            surface_auth=surface_auth,
        )

    # ---------------------------------------------------------------
    # Connection + connect-session
    # ---------------------------------------------------------------

    async def get_connection(
        self,
        connection_id: str,
        provider_config_key: str,
    ) -> Optional[dict[str, Any]]:
        if not self._is_enabled():
            return None
        url = f"{self._base_url}/connection/{connection_id}"
        return await self._send(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {self._secret_key}",
                "Content-Type": "application/json",
            },
            params={"provider_config_key": provider_config_key},
        )

    async def list_xero_connections(
        self,
        connection_id: str,
        provider_config_key: str,
    ) -> Optional[list[dict[str, Any]]]:
        """Enumerate every Xero organisation (tenant) this connection covers.

        Proxies Xero's ``GET /connections`` endpoint. This is org-agnostic
        — NO ``nango-proxy-xero-tenant-id`` header — so it returns the full
        list of tenants the OAuth grant can access:
        ``[{"tenantId": "...", "tenantName": "...", "tenantType": "ORGANISATION"}, ...]``
        """
        if not self._is_enabled():
            return None
        url = f"{self._base_url}/proxy/connections"
        body = await self._send(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {self._secret_key}",
                "Content-Type": "application/json",
                "Connection-Id": connection_id,
                "Provider-Config-Key": provider_config_key,
            },
        )
        return body if isinstance(body, list) else None

    async def create_connect_session(
        self,
        end_user_id: str,
        allowed_integrations: list[str],
    ) -> Optional[dict[str, Any]]:
        """Initiate a Nango Connect session for the frontend OAuth popup.

        Returns the session payload (``session_token`` etc.) the React
        UI hands to ``@nangohq/frontend``.
        """
        if not self._is_enabled():
            return None
        url = f"{self._base_url}/connect/sessions"
        return await self._send(
            "POST",
            url,
            headers={
                "Authorization": f"Bearer {self._secret_key}",
                "Content-Type": "application/json",
            },
            json={
                "end_user": {"id": end_user_id},
                "allowed_integrations": allowed_integrations,
            },
        )

    # ---------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------

    def _headers(
        self,
        connection_id: str,
        provider_config_key: str,
        tenant_id: Optional[str] = None,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._secret_key}",
            "Content-Type": "application/json",
            "Connection-Id": connection_id,
            "Provider-Config-Key": provider_config_key,
        }
        if tenant_id:
            # Xero proxy requires the tenant id on every call.
            headers["nango-proxy-xero-tenant-id"] = tenant_id
        return headers

    async def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
        content: Optional[bytes] = None,
        surface_auth: bool = False,
    ) -> Optional[dict[str, Any]]:
        # Xero rate-limits per tenant and app-wide. On 429, honour
        # Retry-After and retry rather than fail-open to None.
        import asyncio

        attempts = 0
        while True:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(
                        method, url, headers=headers,
                        params=params,
                        json=json if content is None else None,
                        content=content,
                    )
            except httpx.HTTPError as exc:
                logger.warning("%s transport error %s %s :: %s",
                               _LOG_TAG, method, url, exc)
                return None

            if resp.status_code == 429 and attempts < _MAX_RATE_LIMIT_RETRIES:
                attempts += 1
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                wait = min(retry_after or (2 ** attempts), _MAX_RATE_LIMIT_WAIT_S)
                logger.warning(
                    "%s 429 rate-limited on %s %s — retry %d/%d in %.1fs",
                    _LOG_TAG, method, url, attempts, _MAX_RATE_LIMIT_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                continue

            if resp.status_code >= 400:
                logger.warning(
                    "%s %s %s HTTP %s :: %s",
                    _LOG_TAG, method, url, resp.status_code, resp.text[:200],
                )
                # A dead/expired connection on a READ must surface, not return
                # None, so callers don't misread it as empty data (Nango replies
                # 401/403, or 400 "Failed to get connection"). Writes return None.
                if (method == "GET" or surface_auth) and (
                    resp.status_code in (401, 403)
                    or "Failed to get connection" in (resp.text or "")
                ):
                    raise NangoAuthError(
                        f"Xero rejected the request (HTTP {resp.status_code}). "
                        f"The connection looks expired or revoked."
                    )
                return None
            try:
                return resp.json()
            except ValueError:
                logger.warning("%s %s %s body was not JSON", _LOG_TAG, method, url)
                return None


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header (seconds form). Returns None if absent/bad."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (TypeError, ValueError):
        return None
