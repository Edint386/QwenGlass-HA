"""MCP 服务：平台的 MCP 插件调这里（``POST /mcp``，Bearer，无状态 + JSON 响应）。

三个工具（逻辑在 ``tools.py``）：

- ``control``：开关、调温度、切模式、调风速、调亮度。同一台设备的几件事一次传完，一轮工具调用就够。
- ``status``：查状态、室温湿度。状态在内存里（HA 的 WebSocket 一直订阅着），不用往返。
- ``call_service``：前两个做不到的（扫地机、场景、音箱音量），直接调 HA 的服务。

和小说阅读器（QwenGlass-Reader）一样的约定：

- 平台只把 ``content[0].text`` 交给模型，所以返回扁平 JSON，``message`` 是能直接念的一句话；
- 插件选「无状态协议」，每次调用一个 POST，不先 ``initialize``；
- 工具描述就是大模型选工具的依据、平台上改不了，用中文写清什么时候用、参数怎么填。
  设备名单写进描述（从 ``devices.toml`` 生成），大模型不用先查一轮；改了名单要在平台上刷新插件的工具。
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, Required, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import WithJsonSchema
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .tools import HomeTools

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
MAX_BODY_BYTES = 64 * 1024

INSTRUCTIONS = (
    "控制家里接进 Home Assistant 的米家设备。用户直接说命令（开空调、关灯、空调调到26度），不需要先打开什么。"
    "控制用 control，问状态用 status，一句话里同一台设备的几件事一次 control 传完。"
    "工具返回的 message 原样念给用户，不要加别的话。"
)

CONTROL_DESCRIPTION = (
    "控制家里的设备：开关、调温度、切空调模式、调风速、调亮度、扫地机扫地（power=on）和回充（power=off）。"
    "用户说“开空调”“把灯关了”“空调调到26度”"
    "“空调制冷24度，风力调到最大”“热死了”“灯亮一点”“净化器开到最大”这类话时调用。"
    "同一台设备的几件事一次传完（制冷 + 24 度 + 风速最大 = 一次调用）；几台设备做同样的事，device 用顿号隔开；"
    "几台设备做不同的事，分别调用。"
    "device：设备名，“所有灯”= 全部灯。{devices}。"
    "其余参数只填用户说到的：power（on 开 / off 关）；temperature（目标温度，度）；"
    "temperature_change（升几度为正、降几度为负，“高一点”= 1）；mode（制冷/制热/除湿/送风/自动）；"
    "fan（风速：最大/高/中/低/最小/自动）；brightness（亮度 1～100）；brightness_change（亮为正、暗为负，“一点”= 20）。"
    "调温度、切模式时空调关着会自动打开，不用另外传 power。返回的 message 原样念给用户。"
)
STATUS_DESCRIPTION = (
    "查家里设备的状态和屋里的温度、湿度：开没开、空调设的几度、什么模式、风速、室温。用户问“空调开着吗”"
    "“空调现在多少度”“屋里多少度”“湿度多少”“灯关了没”“家里设备都什么状态”时调用。"
    "device：设备名，几台用顿号隔开；留空 = 全部常用设备。返回的 message 原样念给用户。"
)
CALL_SERVICE_DESCRIPTION = (
    "control 做不到的操作才用：直接调 Home Assistant 的服务。比如执行场景（service=scene.turn_on）、音箱音量调到三成"
    "（media_player.volume_set，data={\"volume_level\": 0.3}）。"
    "device：设备名（按米家里的名字说）；service：“域.服务”；data：服务参数，JSON 对象，可以不填。"
    "开关、温度、模式、风速、亮度、扫地机扫地和回充一律用 control，不要用这个。返回的 message 是执行结果，用一句话告诉用户。"
)


class Result(TypedDict, total=False):
    success: Required[bool]
    #: 念给用户的一句话
    message: Required[str]
    #: 失败时给模型看的原因
    error: str


# 参数都按 Any 收、schema 照常声明类型：大模型常把温度传成 "26度"、把 power 传成 "开"，tools.py 再宽松解析
DeviceParam = Annotated[Any, WithJsonSchema({"type": "string", "description": "设备名，几台用顿号隔开"})]
StatusDeviceParam = Annotated[Any, WithJsonSchema({"type": "string", "description": "设备名，几台用顿号隔开；留空 = 全部常用设备"})]
PowerParam = Annotated[Any, WithJsonSchema({"type": "string", "enum": ["on", "off"], "description": "on 开 / off 关"})]
TemperatureParam = Annotated[Any, WithJsonSchema({"type": "number", "description": "目标温度（摄氏度）"})]
TemperatureChangeParam = Annotated[Any, WithJsonSchema({"type": "number", "description": "升几度为正，降几度为负"})]
ModeParam = Annotated[Any, WithJsonSchema(
    {"type": "string", "enum": ["制冷", "制热", "除湿", "送风", "自动"], "description": "空调模式"})]
FanParam = Annotated[Any, WithJsonSchema(
    {"type": "string", "description": "风速：最大、高、中、低、最小、自动"})]
BrightnessParam = Annotated[Any, WithJsonSchema(
    {"type": "integer", "minimum": 1, "maximum": 100, "description": "亮度百分比"})]
BrightnessChangeParam = Annotated[Any, WithJsonSchema({"type": "integer", "description": "调亮为正，调暗为负；“一点”= 20"})]
ServiceDeviceParam = Annotated[Any, WithJsonSchema({"type": "string", "description": "设备名"})]
ServiceParam = Annotated[Any, WithJsonSchema({"type": "string", "description": "域.服务，比如 vacuum.start"})]
DataParam = Annotated[Any, WithJsonSchema({"type": "object", "description": "服务参数，可以不填"})]


def build_server(tools: HomeTools) -> MCPServer:
    """注册好三个工具的 MCPServer。每个应用新建一个（SDK 的 session manager 只能 run 一次）。"""
    server = MCPServer(
        "glasshome",
        title="米家控制",
        description="用千问 AI 眼镜控制家里的米家设备",
        instructions=INSTRUCTIONS,
        version="0.2.0",
    )
    devices = tools.device_list() or "设备按米家里的名字说"

    def annotations(title: str, read_only: bool) -> ToolAnnotations:
        return ToolAnnotations(title=title, readOnlyHint=read_only, destructiveHint=False, idempotentHint=True,
                               openWorldHint=False)

    def failure(tool: str, exc: BaseException) -> Result:
        log.exception("工具 %s 出错", tool)
        return {"success": False, "message": "家里的控制服务出错了", "error": f"{type(exc).__name__}: {exc}"}

    @server.tool(name="control", title="控制设备", description=CONTROL_DESCRIPTION.format(devices=devices),
                 annotations=annotations("控制设备", False))
    async def control(
        device: DeviceParam = "",
        power: PowerParam = None,
        temperature: TemperatureParam = None,
        temperature_change: TemperatureChangeParam = None,
        mode: ModeParam = None,
        fan: FanParam = None,
        brightness: BrightnessParam = None,
        brightness_change: BrightnessChangeParam = None,
    ) -> Result:
        try:
            return await tools.control(  # type: ignore[return-value]
                device, power=power, temperature=temperature, temperature_change=temperature_change, mode=mode,
                fan=fan, brightness=brightness, brightness_change=brightness_change,
            )
        except Exception as exc:  # noqa: BLE001
            return failure("control", exc)

    @server.tool(name="status", title="查状态", description=STATUS_DESCRIPTION, annotations=annotations("查状态", True))
    async def status(device: StatusDeviceParam = "") -> Result:
        try:
            return await tools.status(device)  # type: ignore[return-value]
        except Exception as exc:  # noqa: BLE001
            return failure("status", exc)

    @server.tool(name="call_service", title="调 HA 服务", description=CALL_SERVICE_DESCRIPTION,
                 annotations=annotations("调 HA 服务", False))
    async def call_service(device: ServiceDeviceParam = "", service: ServiceParam = "", data: DataParam = None) -> Result:
        try:
            return await tools.call_service(device, service, data)  # type: ignore[return-value]
        except Exception as exc:  # noqa: BLE001
            return failure("call_service", exc)

    return server


class Guard:
    """``/mcp``：Bearer 校验（常量时间比较；Token 栏里多写了 “Bearer ” 也认），无状态模式下 GET 回 405，记一行访问日志。"""

    def __init__(self, app: ASGIApp, *, token: str) -> None:
        if not token:
            raise ValueError("MCP 的 token 不能为空")
        self.app = app
        self.token = token.encode()
        self._last_reject = 0.0

    @staticmethod
    def extract(header: str | None) -> str:
        value = (header or "").strip()
        for _ in range(2):  # “Bearer Bearer <token>”
            parts = value.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                value = parts[1].strip()
        return value

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == MCP_PATH or path.startswith(MCP_PATH + "/")):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        token = self.extract(headers.get("authorization"))
        if not token or not hmac.compare_digest(token.encode(), self.token):
            now = time.monotonic()
            if now - self._last_reject > 10:
                self._last_reject = now
                log.warning("拒绝 MCP 请求（401）：Bearer Token 缺失或不对，来自 %s", headers.get("x-real-ip") or "-")
            response: Response = JSONResponse({"error": "unauthorized"}, status_code=401,
                                              headers={"WWW-Authenticate": 'Bearer realm="glasshome"'})
            await response(scope, receive, send)
            return
        if scope.get("method") == "GET":
            await PlainTextResponse("stateless MCP endpoint, use POST", status_code=405, headers={"Allow": "POST"})(
                scope, receive, send)
            return
        started = time.perf_counter()
        status = 0

        async def counting(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, counting)
        finally:
            log.info("MCP %s %s %.0f ms，来自 %s %s", scope.get("method"), status or "-",
                     (time.perf_counter() - started) * 1000, headers.get("x-real-ip") or "-",
                     headers.get("user-agent", "-")[:40])


def create_app(tools: HomeTools, token: str) -> Starlette:
    server = build_server(tools)
    # 只听回环、外面隔着 nginx，Host 是公网域名带端口：不做 DNS 重绑定检查，靠 Bearer
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp_app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        transport_security=security,
        max_request_body_size=MAX_BODY_BYTES,
    )
    manager = server.session_manager

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True, "ha": tools.ha.connected.is_set()})

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(
        routes=[Route("/healthz", healthz, methods=["GET"]), *mcp_app.routes],
        middleware=[Middleware(Guard, token=token)],
        lifespan=lifespan,
    )
    app.state.mcp_server = server
    return app
