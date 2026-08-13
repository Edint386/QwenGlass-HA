"""Bounded and retry-safe Home Assistant HTTP client."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from typing import Any

import httpx

from .config import Settings
from .errors import CredentialError, HomeAssistantError
from .secrets import SecretFile

logger = logging.getLogger(__name__)


class HomeAssistantClient:
    """A concurrency-bounded HA client with safe retry semantics.

    GET requests may be retried once after a transport failure. POST requests
    are never retried because Home Assistant service calls are not guaranteed
    to be idempotent; retrying could operate a device twice.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._token = SecretFile(settings.ha_token_file, label="HA token")
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_ha_requests)
        self._keepalive_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._ensure_client()
        await self._warmup()
        if self.settings.keepalive_interval_seconds > 0:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="ha-keepalive")

    async def close(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive_task
            self._keepalive_task = None
        async with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None and not self._client.is_closed:
            return self._client
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                timeout = httpx.Timeout(self.settings.ha_timeout_seconds)
                limits = httpx.Limits(
                    max_connections=self.settings.max_concurrent_ha_requests,
                    max_keepalive_connections=self.settings.max_concurrent_ha_requests,
                    keepalive_expiry=max(20.0, self.settings.keepalive_interval_seconds * 2.5),
                )
                self._client = httpx.AsyncClient(
                    base_url=self.settings.ha_base_url,
                    timeout=timeout,
                    limits=limits,
                    transport=self._transport,
                    follow_redirects=False,
                )
            return self._client

    def token_configured(self) -> bool:
        try:
            return self._token.read_optional() is not None
        except CredentialError:
            return False

    def _headers(self, *, require_token: bool) -> dict[str, str]:
        token = self._token.read_required() if require_token else self._token.read_optional()
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        require_token: bool = True,
        timeout_seconds: float | None = None,
        retry_transport: bool | None = None,
    ) -> httpx.Response:
        safe_to_retry = method == "GET" and retry_transport is not False
        attempts = 2 if safe_to_retry else 1
        last_error: httpx.TransportError | None = None
        for attempt in range(attempts):
            try:
                client = await self._ensure_client()
                async with self._semaphore:
                    request_options: dict[str, Any] = {}
                    if timeout_seconds is not None:
                        request_options["timeout"] = httpx.Timeout(timeout_seconds)
                    response = await client.request(
                        method,
                        path,
                        headers=self._headers(require_token=require_token),
                        json=dict(payload) if payload is not None else None,
                        **request_options,
                    )
                if len(response.content) > self.settings.max_ha_response_bytes:
                    raise HomeAssistantError(
                        "Home Assistant response exceeded the configured limit",
                        code="ha_response_too_large",
                    )
                return response
            except CredentialError:
                raise
            except httpx.TransportError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0)
                    continue
                break
        logger.warning(
            "Home Assistant transport failure type=%s",
            type(last_error).__name__ if last_error is not None else "unknown",
        )
        raise HomeAssistantError(
            "Home Assistant is temporarily unreachable", code="ha_unreachable"
        ) from last_error

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise HomeAssistantError(
                "Home Assistant returned invalid JSON", code="ha_invalid_response"
            ) from exc

    @staticmethod
    def _require_success(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise HomeAssistantError(
                "Home Assistant rejected the configured token", code="ha_unauthorized"
            )
        if not 200 <= response.status_code < 300:
            raise HomeAssistantError(
                f"Home Assistant returned HTTP {response.status_code}",
                code="ha_http_error",
            )

    async def health(self) -> dict[str, Any]:
        configured = self.token_configured()
        result: dict[str, Any] = {"token_configured": configured}
        try:
            response = await self._request("GET", "/api/", require_token=False)
            result["ha_api_http_status"] = response.status_code
            result["ha_api_reachable"] = True
            if configured and response.status_code in {401, 403}:
                result["authentication_valid"] = False
            elif configured:
                result["authentication_valid"] = 200 <= response.status_code < 300
        except (CredentialError, HomeAssistantError) as exc:
            result["ha_api_reachable"] = False
            result["error_code"] = exc.code
        return result

    async def states(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/api/states")
        self._require_success(response)
        data = self._json(response)
        if not isinstance(data, list):
            raise HomeAssistantError(
                "Home Assistant returned an invalid states payload",
                code="ha_invalid_response",
            )
        return [item for item in data if isinstance(item, dict)]

    async def state(
        self,
        entity_id: str,
        *,
        timeout_seconds: float | None = None,
        retry_transport: bool | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/api/states/{entity_id}",
            timeout_seconds=timeout_seconds,
            retry_transport=retry_transport,
        )
        self._require_success(response)
        data = self._json(response)
        if not isinstance(data, dict):
            raise HomeAssistantError(
                "Home Assistant returned an invalid state payload",
                code="ha_invalid_response",
            )
        return data

    async def call_service(
        self,
        domain: str,
        service: str,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/api/services/{domain}/{service}",
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
        self._require_success(response)
        if not response.content:
            return {"http_status": response.status_code, "body": None}
        return {"http_status": response.status_code, "body": self._json(response)}

    async def _warmup(self) -> None:
        try:
            await self._request("GET", "/api/", require_token=False)
        except (CredentialError, HomeAssistantError):
            logger.info("Home Assistant warmup did not complete")

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.keepalive_interval_seconds)
            try:
                await self._request("GET", "/api/", require_token=False)
            except (CredentialError, HomeAssistantError):
                logger.warning("Home Assistant keepalive failed")
