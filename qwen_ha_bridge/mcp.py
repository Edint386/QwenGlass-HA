"""Minimal, bounded Streamable HTTP MCP transport for Qwen compatibility."""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .auth import Authenticator
from .config import Settings
from .sessions import SessionStore
from .tools import ToolRegistry, tool_error, tool_result

logger = logging.getLogger(__name__)

JSONRPC_VERSION = "2.0"
LEGACY_PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
DEFAULT_PROTOCOL_VERSION = "2025-03-26"


class MCPTransport:
    """Closed-JSON MCP responses with legacy-session and Qwen workarounds.

    The implementation deliberately does not require an initialized session
    when ``relaxed_sessions`` is enabled. Qwen's execution client has been
    observed to skip or abort that notification while still sending valid tool
    calls. This compatibility mode is bounded and carries no per-session
    authorization state.
    """

    def __init__(self, settings: Settings, tools: ToolRegistry) -> None:
        self.settings = settings
        self.tools = tools
        self.auth = Authenticator(settings)
        self.sessions = SessionStore(
            ttl_seconds=settings.session_ttl_seconds,
            max_sessions=settings.max_sessions,
        )

    def _auth_error(self, request: Request) -> Response | None:
        outcome = self.auth.authorize(request.headers)
        if outcome is None:
            return None
        if outcome == "unavailable":
            return JSONResponse(
                {"error": "bridge authentication is not configured"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={
                "WWW-Authenticate": 'Bearer realm="qwen-ha-bridge"',
                "Cache-Control": "no-store",
            },
        )

    async def post(self, request: Request) -> Response:
        auth_error = self._auth_error(request)
        if auth_error is not None:
            return auth_error
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            return JSONResponse({"error": "content-type must be application/json"}, status_code=415)
        try:
            body = await self._read_limited(request)
        except ValueError:
            return JSONResponse({"error": "request body too large"}, status_code=413)
        try:
            message = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._rpc_error(None, -32700, "parse error", http_status=400)
        if not isinstance(message, dict):
            return self._rpc_error(None, -32600, "invalid request", http_status=400)
        return await self._dispatch(request, message)

    async def get(self, request: Request) -> Response:
        auth_error = self._auth_error(request)
        if auth_error is not None:
            return auth_error
        # This bridge does not send unsolicited server messages. Streamable
        # HTTP explicitly permits 405 when a server offers no GET event stream.
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    async def delete(self, request: Request) -> Response:
        auth_error = self._auth_error(request)
        if auth_error is not None:
            return auth_error
        session_id = request.headers.get("mcp-session-id")
        if not session_id:
            return JSONResponse({"error": "missing Mcp-Session-Id"}, status_code=400)
        if not self.sessions.delete(session_id):
            return Response(status_code=404)
        return Response(status_code=204)

    async def _read_limited(self, request: Request) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > self.settings.request_body_limit:
                raise ValueError("body too large")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _dispatch(self, request: Request, message: dict[str, Any]) -> Response:
        msg_id = message.get("id")
        method = message.get("method")
        if message.get("jsonrpc") != JSONRPC_VERSION or not isinstance(method, str):
            return self._rpc_error(msg_id, -32600, "invalid request", http_status=400)
        header_method = request.headers.get("mcp-method")
        if header_method and header_method != method:
            return self._rpc_error(msg_id, -32600, "Mcp-Method header mismatch")
        params = message.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._rpc_error(msg_id, -32602, "params must be an object")
        session_id = request.headers.get("mcp-session-id")
        is_notification = "id" not in message

        if method == "initialize":
            return self._initialize(params, msg_id)

        valid_session = self.sessions.touch(session_id)
        if not self.settings.relaxed_sessions and not valid_session:
            return JSONResponse({"error": "invalid or expired session"}, status_code=404)

        if method == "notifications/initialized":
            self.sessions.mark_initialized(session_id)
            return Response(status_code=202)
        if not self.settings.relaxed_sessions and not self.sessions.is_initialized(session_id):
            return JSONResponse({"error": "session is not initialized"}, status_code=400)
        if is_notification:
            # JSON-RPC notifications never receive a JSON-RPC response.
            return Response(status_code=202)
        if not isinstance(msg_id, (str, int)) or isinstance(msg_id, bool):
            return self._rpc_error(None, -32600, "request id must be a string or integer")
        if method == "ping":
            return self._rpc_result(msg_id, {}, session_id)
        if method == "tools/list":
            return self._rpc_result(msg_id, {"tools": self.tools.definitions()}, session_id)
        if method == "tools/call":
            header_name = request.headers.get("mcp-name")
            name = params.get("name")
            if header_name and header_name != name:
                return self._rpc_error(msg_id, -32602, "Mcp-Name header mismatch")
            arguments = params.get("arguments", {})
            try:
                payload = await self.tools.execute(name if isinstance(name, str) else "", arguments)
                result = tool_result(payload)
            except Exception as exc:  # tool_error sanitizes unexpected exceptions
                result = tool_error(exc)
            return self._rpc_result(msg_id, result, session_id)
        return self._rpc_error(msg_id, -32601, "method not found", session_id=session_id)

    def _initialize(self, params: dict[str, Any], msg_id: Any) -> Response:
        if not isinstance(msg_id, (str, int)) or isinstance(msg_id, bool):
            return self._rpc_error(None, -32600, "initialize requires a request id")
        requested = params.get("protocolVersion")
        protocol = requested if requested in LEGACY_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        session_id = self.sessions.create()
        result = {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "qwen-glass-ha", "version": __version__},
            "instructions": (
                "Read tools inspect Home Assistant. Write tools, when enabled, are restricted "
                "to an explicit service allowlist."
            ),
        }
        return self._rpc_result(msg_id, result, session_id)

    @staticmethod
    def _response_headers(session_id: str | None = None) -> dict[str, str]:
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    def _rpc_result(self, msg_id: Any, result: Any, session_id: str | None = None) -> Response:
        return JSONResponse(
            {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "result": result},
            headers=self._response_headers(session_id),
        )

    def _rpc_error(
        self,
        msg_id: Any,
        code: int,
        message: str,
        *,
        http_status: int = 200,
        session_id: str | None = None,
    ) -> Response:
        return JSONResponse(
            {
                "jsonrpc": JSONRPC_VERSION,
                "id": msg_id,
                "error": {"code": code, "message": message},
            },
            status_code=http_status,
            headers=self._response_headers(session_id),
        )
