"""命令行：``serve`` 起服务，``devices`` 看名录，``say`` 不用眼镜直接说一句（规则版），``tool`` 直接调一个 MCP 工具。

配置都从环境变量读：

| 变量 | 说明 |
|---|---|
| ``GLASSHOME_MCP_TOKEN`` | 平台 MCP 插件的 Bearer Token；不填就不起 MCP |
| ``GLASSHOME_AGENT_TOKEN`` | 平台 Agent 插件的 Bearer Token（老的一句话直达版）；不填就不开 ``/agent`` |
| ``GLASSHOME_ALLOWED_DEVICES`` | 只认这几副眼镜的 ``deviceOpenId``，逗号分隔，不填不限 |
| ``GLASSHOME_HA_URL`` | HA 的 WebSocket 地址，默认 ``ws://127.0.0.1:8123/api/websocket`` |
| ``GLASSHOME_HA_TOKEN`` | HA 的长期访问令牌；不填就读 ``$GLASSHOME_DATA/ha_token``（在 HA 网页里领的，见 ``agent.Agent._setup``） |
| ``GLASSHOME_DATA`` | 存领到的令牌，默认 ``/data`` |
| ``GLASSHOME_DEVICES`` | 设备说法 ``devices.toml`` 的路径，默认 ``devices.toml`` |
| ``GLASSHOME_LANGUAGE`` | 交给 Assist 时的语言，默认 ``zh-cn`` |
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .agent import Agent, Home, start
from .ha import HomeAssistant
from .tools import HomeTools

log = logging.getLogger("glasshome")


def _token_file() -> Path:
    """领令牌（/setup）存到这里；环境变量 GLASSHOME_HA_TOKEN 优先。"""
    return Path(os.environ.get("GLASSHOME_DATA", "/data")) / "ha_token"


def _ha(required: bool = True) -> HomeAssistant:
    token = os.environ.get("GLASSHOME_HA_TOKEN", "").strip()
    if not token:
        try:
            token = _token_file().read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    if not token and required:
        sys.exit("要设 GLASSHOME_HA_TOKEN（HA 个人资料 → 安全 → 长期访问令牌）")
    return HomeAssistant(os.environ.get("GLASSHOME_HA_URL", "ws://127.0.0.1:8123/api/websocket"), token)


def _home(ha: HomeAssistant) -> Home:
    return Home(ha, _devices_file(), os.environ.get("GLASSHOME_LANGUAGE", "zh-cn"))


def _devices_file() -> Path:
    return Path(os.environ.get("GLASSHOME_DEVICES", "devices.toml"))


async def _serve(host: str, port: int, mcp_port: int) -> None:
    import uvicorn

    from .mcp_server import create_app

    ha = _ha(required=False)
    allowed = [d.strip() for d in os.environ.get("GLASSHOME_ALLOWED_DEVICES", "").split(",")]
    agent = Agent(_home(ha), os.environ.get("GLASSHOME_AGENT_TOKEN", "").strip(), allowed, _token_file())
    # 没有 HA 令牌也照常起：平台那头能先连上、能试召回，每句话都回“没连上”；领到令牌（/setup）就连
    runner = asyncio.ensure_future(ha.run())
    if not ha.token:
        log.error("还没有 HA 令牌：每句话都会回“家里的 Home Assistant 没连上”，在 .env 里填 GLASSHOME_HA_TOKEN")
    server = await start(agent, host, port)
    log.info("在 %s:%d 上听 %s，HA %s", host, port, "/agent 和 /setup" if agent.token else "/setup（没有 GLASSHOME_AGENT_TOKEN，不开 /agent）", ha.url)
    jobs = [asyncio.ensure_future(server.serve_forever())]
    token = os.environ.get("GLASSHOME_MCP_TOKEN", "").strip()
    if token:
        app = create_app(HomeTools(ha, _devices_file()), token)
        config = uvicorn.Config(app, host=host, port=mcp_port, log_config=None, access_log=False, server_header=False,
                                lifespan="on", timeout_keep_alive=75, timeout_graceful_shutdown=3)
        jobs.append(asyncio.ensure_future(uvicorn.Server(config).serve()))
        log.info("在 %s:%d 上听 /mcp", host, mcp_port)
    else:
        log.error("没有 GLASSHOME_MCP_TOKEN：MCP 不起")
    try:
        await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)  # uvicorn 收到 SIGTERM 会先退出
    finally:
        for job in jobs:
            job.cancel()
        runner.cancel()


async def _connected() -> HomeAssistant:
    ha = _ha()
    asyncio.ensure_future(ha.run())
    if not await ha.ready(15):
        sys.exit(f"15 秒内没连上 HA：{ha.url}")
    return ha


async def _devices() -> None:
    home = _home(await _connected())
    for device in sorted(home.catalog().devices, key=lambda d: d.entity_id):
        state = home.ha.state(device.entity_id) or {}
        print(f"{device.entity_id}\t{state.get('state', '?')}\t{' / '.join(device.names)}")


async def _say(text: str) -> None:
    home = _home(await _connected())
    reply = await home.respond(text, first=True)
    print(reply)


async def _tool(name: str, arguments: str) -> None:
    tools = HomeTools(await _connected(), _devices_file())
    if name not in ("control", "status", "call_service"):
        sys.exit("工具只有 control、status、call_service")
    result = await getattr(tools, name)(**json.loads(arguments or "{}"))
    print(json.dumps(result, ensure_ascii=False))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="glasshome", description="千问 AI 眼镜一句话控制 Home Assistant")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="起服务：MCP（--mcp-port）和 Agent 的 WebSocket（--port）")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=19530)
    serve.add_argument("--mcp-port", type=int, default=19531)
    commands.add_parser("devices", help="列出名录：实体、状态、能叫的名字")
    say = commands.add_parser("say", help="不用眼镜，直接说一句（会真的控制设备）")
    say.add_argument("text")
    tool = commands.add_parser("tool", help="直接调一个 MCP 工具（会真的控制设备），参数是 JSON")
    tool.add_argument("name")
    tool.add_argument("arguments", nargs="?", default="{}")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    for noisy in ("mcp", "httpx", "uvicorn.error"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.command == "serve":
        asyncio.run(_serve(args.host, args.port, args.mcp_port))
    elif args.command == "devices":
        asyncio.run(_devices())
    elif args.command == "tool":
        asyncio.run(_tool(args.name, args.arguments))
    else:
        asyncio.run(_say(args.text))


if __name__ == "__main__":
    main()
