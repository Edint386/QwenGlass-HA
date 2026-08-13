"""Request metadata logging without request bodies or credentials."""

from __future__ import annotations

import json
import logging
import secrets
import time
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger("qwen_ha_bridge.requests")
_request_id: ContextVar[str] = ContextVar("qwen_ha_bridge_request_id", default="-")


def current_request_id() -> str:
    """Return the safe correlation ID for the current HTTP request."""
    return _request_id.get()


class RequestMetadataMiddleware:
    """Log only path/status/duration; never headers, bodies, or tool arguments."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500
        request_id = secrets.token_hex(8)
        context_token = _request_id.set(request_id)

        async def send_wrapped(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 500))
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapped)
        finally:
            logger.info(
                "%s",
                json.dumps(
                    {
                        "event": "request_completed",
                        "request_id": request_id,
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            _request_id.reset(context_token)
