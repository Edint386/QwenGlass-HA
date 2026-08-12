#!/usr/bin/env python3
"""Non-destructive MCP handshake and read-tool verification."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx


def bridge_key() -> str:
    path = Path(os.environ.get("BRIDGE_API_KEY_FILE", "secrets/bridge_api_key"))
    return path.read_text(encoding="utf-8").strip()


async def rpc(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    method: str,
    *,
    msg_id: int | None = None,
    params: dict | None = None,
) -> httpx.Response:
    body: dict = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    return await client.post(url, headers=headers, json=body)


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/mcp"
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {bridge_key()}",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        initialized = await rpc(
            client,
            url,
            headers,
            "initialize",
            msg_id=1,
            params={
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "verify", "version": "1.0"},
            },
        )
        initialized.raise_for_status()
        session_id = initialized.headers.get("mcp-session-id")
        if not session_id:
            raise RuntimeError("initialize did not return Mcp-Session-Id")
        headers["Mcp-Session-Id"] = session_id
        notification = await rpc(client, url, headers, "notifications/initialized")
        if notification.status_code != 202:
            raise RuntimeError(f"initialized notification returned {notification.status_code}")
        tools = await rpc(client, url, headers, "tools/list", msg_id=2, params={})
        tools.raise_for_status()
        catalog = tools.json()["result"]["tools"]
        status = await rpc(
            client,
            url,
            headers,
            "tools/call",
            msg_id=3,
            params={"name": "bridge_status", "arguments": {}},
        )
        status.raise_for_status()
        result = status.json()["result"]
        print("tools:", ", ".join(tool["name"] for tool in catalog))
        print("bridge_status:", json.dumps(result["structuredContent"], ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
