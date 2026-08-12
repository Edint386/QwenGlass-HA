"""Starlette application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import Settings
from .ha_client import HomeAssistantClient
from .mcp import MCPTransport
from .middleware import RequestMetadataMiddleware
from .tools import ToolRegistry


def create_app(
    settings: Settings | None = None,
    *,
    ha_transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    settings = settings or Settings.from_env()
    ha = HomeAssistantClient(settings, transport=ha_transport)
    tools = ToolRegistry(settings, ha)
    mcp = MCPTransport(settings, tools)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        await ha.start()
        try:
            yield
        finally:
            await ha.close()

    async def mcp_post(request: Request) -> Response:
        return await mcp.post(request)

    async def mcp_get(request: Request) -> Response:
        return await mcp.get(request)

    async def mcp_delete(request: Request) -> Response:
        return await mcp.delete(request)

    async def health(_request: Request) -> Response:
        return JSONResponse(
            {"status": "ok", "service": "qwen-glass-ha"},
            headers={"Cache-Control": "no-store"},
        )

    routes = [
        Route("/mcp", mcp_post, methods=["POST"]),
        Route("/mcp", mcp_get, methods=["GET"]),
        Route("/mcp", mcp_delete, methods=["DELETE"]),
        Route("/healthz", health, methods=["GET"]),
    ]
    middleware = [
        Middleware(RequestMetadataMiddleware),
        Middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts)),
    ]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.settings = settings
    app.state.ha = ha
    app.state.mcp = mcp
    return app
