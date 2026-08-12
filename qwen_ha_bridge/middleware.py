"""Request metadata logging without request bodies or credentials."""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

logger = logging.getLogger("qwen_ha_bridge.requests")


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
                "request id=%s method=%s path=%s status=%s duration_ms=%.1f",
                request_id,
                scope.get("method"),
                scope.get("path"),
                status,
                (time.perf_counter() - started) * 1000,
            )
