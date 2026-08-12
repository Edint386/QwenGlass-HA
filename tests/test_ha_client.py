from __future__ import annotations

import asyncio

import httpx
import pytest

from qwen_ha_bridge.errors import HomeAssistantError
from qwen_ha_bridge.ha_client import HomeAssistantClient


def test_get_retries_once_after_transport_failure(settings) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("stale connection", request=request)
        return httpx.Response(200, json=[])

    async def run() -> None:
        client = HomeAssistantClient(settings, transport=httpx.MockTransport(handler))
        try:
            assert await client.states() == []
        finally:
            await client.close()

    asyncio.run(run())
    assert attempts == 2


def test_service_post_is_never_retried(settings) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadError("ambiguous write outcome", request=request)

    async def run() -> None:
        client = HomeAssistantClient(settings, transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(HomeAssistantError, match="temporarily unreachable"):
                await client.call_service("light", "turn_on", {"entity_id": "light.lamp"})
        finally:
            await client.close()

    asyncio.run(run())
    assert attempts == 1
