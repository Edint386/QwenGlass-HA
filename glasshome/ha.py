"""和 Home Assistant 一直连着的 WebSocket 客户端。

- 连上就订阅 ``state_changed``，再取一次全部状态，之后状态都在内存里：问“空调开着吗”不用往返，
  控制前也能先看一眼，已经是这个状态就不发命令。
- ``call_service`` 走这条连接，一个往返，不用每次握手。
- 断了马上重连，间隔从 0.5 秒翻倍到 10 秒。断着的时候来的命令最多等 :data:`CONNECT_WAIT_SECONDS`。
- 发出去之后连接断了，不知道 HA 执行了没有，抛 :class:`Lost`。命令都是“设成某状态”，调用方可以放心重试一次。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

#: 断着的时候，命令最多等多久重连
CONNECT_WAIT_SECONDS = 2.0
#: 一次调用最多等多久结果
CALL_TIMEOUT_SECONDS = 4.0
RETRY_MIN_SECONDS, RETRY_MAX_SECONDS = 0.5, 10.0
#: 全部状态可能有几百 KB
MAX_MESSAGE_BYTES = 32 * 1024 * 1024


class HAError(Exception):
    """HA 回了失败，或者连不上。"""


class NotConnected(HAError):
    pass


class Lost(HAError):
    """发出去之后连接断了：不知道执行了没有。"""


class Refused(HAError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class HomeAssistant:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.states: dict[str, dict[str, Any]] = {}
        self.connected = asyncio.Event()
        self._ws: ClientConnection | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._watchers: list[Callable[[str, dict[str, Any] | None], None]] = []
        self._send_lock = asyncio.Lock()
        self._token_changed = asyncio.Event()
        self.version = ""

    # ---- 令牌 ----

    async def check_token(self, token: str) -> bool:
        """这个令牌能不能登录这台 HA：连一次、认证、断开。"""
        try:
            async with websockets.connect(self.url, open_timeout=10, max_size=1 << 16) as ws:
                first = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if first.get("type") != "auth_required":
                    return False
                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                return reply.get("type") == "auth_ok"
        except (OSError, ConnectionClosed, asyncio.TimeoutError, ValueError):
            return False

    def use_token(self, token: str) -> None:
        """换令牌：正连着的断开，用新令牌重连。"""
        self.token = token
        self._token_changed.set()
        if self._ws is not None:
            asyncio.ensure_future(self._ws.close())

    # ---- 连接 ----

    async def run(self) -> None:
        """一直跑：连上、同步、读消息；断了就重连。没有令牌就等，有了再连。"""
        delay = RETRY_MIN_SECONDS
        while True:
            while not self.token:
                self._token_changed.clear()
                await self._token_changed.wait()
            self._token_changed.clear()
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Refused as exc:
                log.error("HA 拒绝了令牌（%s），%d 秒后再试", exc, RETRY_MAX_SECONDS)
                delay = RETRY_MAX_SECONDS
            except (OSError, ConnectionClosed, HAError, asyncio.TimeoutError, ValueError) as exc:
                log.warning("HA 连接断了：%r", exc)
            finally:
                self._drop()
            if time.monotonic() - started > 30 or self._token_changed.is_set():  # 连了很久才断的、换了令牌的，马上重连
                delay = RETRY_MIN_SECONDS
            try:  # 等的时候换了令牌就不等了
                await asyncio.wait_for(self._token_changed.wait(), delay)
                delay = RETRY_MIN_SECONDS
            except asyncio.TimeoutError:
                delay = min(delay * 2, RETRY_MAX_SECONDS)

    async def _session(self) -> None:
        async with websockets.connect(self.url, max_size=MAX_MESSAGE_BYTES, ping_interval=20, ping_timeout=20, open_timeout=10) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if first.get("type") != "auth_required":
                raise HAError(f"握手的第一条不是 auth_required：{first.get('type')}")
            self.version = str(first.get("ha_version", ""))
            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if reply.get("type") != "auth_ok":
                raise Refused("auth_invalid", str(reply.get("message", "")))
            self._ws = ws
            reader = asyncio.ensure_future(self._read(ws))
            try:
                await self.call({"type": "subscribe_events", "event_type": "state_changed"}, timeout=10)
                states = await self.call({"type": "get_states"}, timeout=20)
                self.states = {s["entity_id"]: s for s in states if isinstance(s, dict) and "entity_id" in s}
                self.connected.set()
                log.info("连上 HA %s：%d 个实体", self.version, len(self.states))
                await reader
            finally:
                reader.cancel()

    async def _read(self, ws: ClientConnection) -> None:
        async for raw in ws:
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "event":
                data = (message.get("event") or {}).get("data") or {}
                entity = data.get("entity_id")
                if entity:
                    new = data.get("new_state")
                    if new is None:
                        self.states.pop(entity, None)
                    else:
                        self.states[entity] = new
                    for watcher in list(self._watchers):
                        watcher(entity, new)
            elif kind == "result":
                future = self._pending.pop(int(message.get("id", 0)), None)
                if future is not None and not future.done():
                    if message.get("success"):
                        future.set_result(message.get("result"))
                    else:
                        error = message.get("error") or {}
                        future.set_exception(Refused(str(error.get("code", "")), str(error.get("message", ""))))

    def _drop(self) -> None:
        self.connected.clear()
        self._ws = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(Lost("连接断了"))
        self._pending.clear()

    # ---- 调用 ----

    async def call(self, message: dict[str, Any], timeout: float = CALL_TIMEOUT_SECONDS) -> Any:
        ws = self._ws
        if ws is None:
            raise NotConnected("没连上 HA")
        message_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            async with self._send_lock:
                await ws.send(json.dumps({"id": message_id, **message}, ensure_ascii=False))
        except ConnectionClosed as exc:
            self._pending.pop(message_id, None)
            raise NotConnected("没连上 HA") from exc  # 没发出去，可以放心重试
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(message_id, None)

    async def ready(self, timeout: float = CONNECT_WAIT_SECONDS) -> bool:
        try:
            await asyncio.wait_for(self.connected.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def call_service(self, domain: str, service: str, entity_id: str, data: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"type": "call_service", "domain": domain, "service": service, "target": {"entity_id": entity_id}}
        if data:
            message["service_data"] = data
        await self.call(message)

    async def converse(self, text: str, language: str) -> dict[str, Any]:
        """交给 HA 的 Assist 理解并执行，返回它的 response。"""
        result = await self.call({"type": "conversation/process", "text": text, "language": language})
        return (result or {}).get("response") or {}

    def state(self, entity_id: str) -> dict[str, Any] | None:
        return self.states.get(entity_id)

    async def wait_state(self, entity_id: str, check: Callable[[dict[str, Any]], bool], timeout: float) -> bool:
        """等到这个实体的状态满足 ``check``，现在已经满足就马上返回。"""
        current = self.states.get(entity_id)
        if current is not None and check(current):
            return True
        loop = asyncio.get_running_loop()
        done: asyncio.Future[bool] = loop.create_future()

        def watcher(entity: str, new: dict[str, Any] | None) -> None:
            if entity == entity_id and new is not None and check(new) and not done.done():
                done.set_result(True)

        self._watchers.append(watcher)
        try:
            return await asyncio.wait_for(done, timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._watchers.remove(watcher)
