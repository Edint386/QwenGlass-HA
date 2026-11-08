"""平台这一侧：Agent 自定义接入的 WebSocket ``/agent``。

一句话直达：用户不用先说“打开某某”，直接说“关闭空调”。平台按插件描述召回到这个技能，
这句话在 ``SESSION_INIT.entryContext.rawUtterance`` 里，收到就执行、播一句，再等
:data:`FOLLOW_UP_SECONDS` 秒追加的话（“再调低一点”），没有就结束会话，麦克风跟着关。

- 认得的说法（``command.py``）自己执行，最快；认不出来的交给同一台机器上 HA 自带的 Assist 兜底，
  不接别的大模型（用户的决定：多一次请求，多一份耗时）。
- 第一句认不出来要回话（用户是冲着这个技能来的）；追加的话认不出来不吭声：麦克风开着，旁人说话也会进来。
- 握手、恢复按协议：``X-Request-Id`` 原样写回（101 和 401 都要），``connectionEpoch`` 大的换掉旧连接（4001），
  不大的关掉新连接（4002），恢复时不重复处理进来的那句话。
- 日志里每一步带 UTC 毫秒时刻和用时，和控制台的调试日志对着看。语音原文不进日志，只记字数和认出的命令。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from .command import MAX_CHARS, Ambiguous, clean, parse, plain
from .control import Controller
from .devices import Catalog, load_config
from .ha import HAError, HomeAssistant

log = logging.getLogger(__name__)

SPEC_VERSION = "1.0"
#: 连上之后等 SESSION_INIT 多久（平台要求我们 15 秒内回 READY，它自己会很快发）
INIT_TIMEOUT_SECONDS = 15.0
#: 执行完等追加的话多久
FOLLOW_UP_SECONDS = 8.0
#: 进来时没带命令（“打开家居控制”），等用户说话多久
ASK_SECONDS = 15.0
#: 平台一条 UTTERANCE 最多 64 KiB
MAX_MESSAGE_BYTES = 64 * 1024 + 4096
#: 记住多少个会话用来断线恢复
MAX_SESSIONS = 64
#: 领令牌：等令牌多久、令牌最长多少字
SETUP_TIMEOUT_SECONDS = 10.0
MAX_TOKEN_CHARS = 4096
EXIT_WORDS = frozenset({"退出", "退出吧", "结束", "没了", "没有了", "不用了", "好了", "谢谢", "可以了", "行了"})
NOT_UNDERSTOOD = "没听懂，可以说打开客厅灯、空调调到26度"


def utc(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%H:%M:%S.%f")[:-3] + "Z"


def ms(start: float, end: float) -> int:
    return int(round((end - start) * 1000))


def device_tag(device_id: str) -> str:
    """日志里只写设备 ID 的摘要。"""
    return hashlib.sha256(device_id.encode()).hexdigest()[:8] if device_id else "-"


@dataclass
class Session:
    id: str
    epoch: int
    socket: ServerConnection | None = None
    speeches: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def envelope(self, kind: str, payload: dict[str, Any]) -> str:
        message = {"specVersion": SPEC_VERSION, "sessionId": self.id, "type": kind, "payload": payload}
        return json.dumps(message, ensure_ascii=False, separators=(",", ":"))

    async def send(self, ws: ServerConnection, kind: str, payload: dict[str, Any]) -> None:
        async with self.lock:
            await ws.send(self.envelope(kind, payload))

    async def speak(self, ws: ServerConnection, text: str) -> None:
        self.speeches += 1
        piece = {"speechId": f"home_{self.speeches}", "index": 1, "hasNext": False, "text": text}
        await self.send(ws, "OUTPUT", {"actions": [{"type": "SPEAK", "payload": piece}]})


class Home:
    """一句话 → 回话。认得的自己执行，认不出来的交给 Assist。"""

    def __init__(self, ha: HomeAssistant, devices_file: Path | None = None, language: str = "zh-cn") -> None:
        self.ha = ha
        self.controller = Controller(ha)
        self.devices_file = devices_file
        self.language = language

    def catalog(self) -> Catalog:
        """每次现建：几百个实体只要一毫秒左右，设备改名、配置改了马上生效。"""
        return Catalog.build(load_config(self.devices_file), self.ha.states)

    async def respond(self, text: str, *, first: bool) -> str | None:
        """回话；``None`` 表示不吭声。"""
        started = time.perf_counter()
        parsed = parse(text, self.catalog())
        if isinstance(parsed, Ambiguous):
            return f"有好几个{parsed.name}，说得具体一点"
        if parsed:
            outcome = await self.controller.run(parsed)
            log.info(
                "命令 %s：%s，用时 %s，共 %d ms",
                [(c.action, c.device.entity_id, c.value) for c in parsed], "成功" if outcome.ok else "失败",
                outcome.timings, ms(started, time.perf_counter()),
            )
            return outcome.speech
        sentence = clean(text)
        if not sentence or (not first and len(sentence) > MAX_CHARS):
            return NOT_UNDERSTOOD if first else None
        return await self._assist(text, first=first, started=started)

    async def _assist(self, text: str, *, first: bool, started: float) -> str | None:
        if not await self.ha.ready():
            return "家里的 Home Assistant 没连上" if first else None
        try:
            response = await self.ha.converse(text, self.language)
        except (HAError, TimeoutError) as exc:
            log.warning("Assist 出错：%r", exc)
            return NOT_UNDERSTOOD if first else None
        kind = response.get("response_type")
        speech = str(((response.get("speech") or {}).get("plain") or {}).get("speech") or "").strip()
        log.info("Assist：%s，%d ms", kind, ms(started, time.perf_counter()))
        if kind in ("action_done", "query_answer") and speech:
            return speech
        return NOT_UNDERSTOOD if first else None


class Agent:
    def __init__(self, home: Home, token: str, allowed_devices: Iterable[str] = (), ha_token_file: Path | None = None) -> None:
        """``token`` 为空就不开 ``/agent``（只用 MCP 的部署），``/setup`` 和 ``/healthz`` 照常。"""
        self.home = home
        self.token = token
        self.allowed = frozenset(d for d in allowed_devices if d)
        self.sessions: dict[str, Session] = {}
        self.ha_token_file = ha_token_file

    # ---- HTTP 握手 ----

    def process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        connection.accepted_at = time.time()  # type: ignore[attr-defined]
        if request.path == "/healthz":
            return connection.respond(200, '{"ok":true}\n')
        if request.path == "/setup":  # 领令牌：不看 Bearer，令牌本身能登录 HA 才收
            return None
        if request.path != "/agent" or not self.token:
            return connection.respond(404, "not found\n")
        scheme, _, value = (request.headers.get("Authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip().encode(), self.token.encode()):
            log.warning("拒绝平台连接：Bearer Token 缺失或不匹配")
            return connection.respond(401, "unauthorized\n")
        return None

    def process_response(self, connection: ServerConnection, request: Request, response: Response) -> None:
        request_id = request.headers.get("X-Request-Id")
        if request_id:  # 101 和 401 都要原样写回
            response.headers["X-Request-Id"] = request_id

    # ---- 一条连接 ----

    async def connection(self, ws: ServerConnection) -> None:
        try:
            if ws.request is not None and ws.request.path == "/setup":
                await self._setup(ws)
                return
            await self._serve(ws)
        except ConnectionClosed:
            log.info("平台断开了 %s（%s）", utc(time.time()), ws.close_code)

    async def _serve(self, ws: ServerConnection) -> None:
        accepted = getattr(ws, "accepted_at", time.time())
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), INIT_TIMEOUT_SECONDS))
        except (TimeoutError, ValueError, TypeError):
            await ws.close(1002)
            return
        got_init = time.time()
        payload = first.get("payload") or {}
        session_id = str(first.get("sessionId") or "")
        if first.get("type") != "SESSION_INIT" or first.get("specVersion") != SPEC_VERSION or not session_id:
            await ws.close(1002)
            return
        device = str((payload.get("identity") or {}).get("deviceOpenId") or "")
        if self.allowed and device not in self.allowed:
            log.warning("会话 %s：设备 %s 不在白名单，拒绝（完整 ID 见下一行，确认是自己的眼镜再加进去）", session_id[:8], device_tag(device))
            log.warning("设备 ID：%r", device)
            stranger = Session(session_id, 0)
            await stranger.send(ws, "ERROR", {"scope": "SESSION", "code": "FORBIDDEN", "message": "device not allowed", "fatal": True})
            await ws.close(1011)
            return
        epoch = int(payload.get("connectionEpoch") or 0)
        session = self.sessions.get(session_id)
        if session is not None and epoch <= session.epoch:
            await ws.close(4002)
            return
        mode = "RESUMED" if session is not None else "CREATED"
        if session is None:
            session = self._remember(Session(session_id, epoch))
        previous, session.socket, session.epoch = session.socket, ws, epoch
        await session.send(ws, "SESSION_READY", {"mode": mode, "connectionEpoch": epoch})
        if previous is not None:
            await previous.close(4001)
        entry = payload.get("entryContext") or {}
        raw = str(entry.get("rawUtterance") or "") if mode == "CREATED" else ""  # 恢复时不重复处理
        log.info(
            "会话 %s %s：连上 %s，INIT +%d ms，%s，设备 %s，进来的话 %d 字",
            session_id[:8], mode, utc(accepted), ms(accepted, got_init), payload.get("inputMode", "?"),
            device_tag(device), len(raw),
        )
        if not self.allowed and device:
            log.info("设备 ID（写进 GLASSHOME_ALLOWED_DEVICES 就只认这副眼镜）：%r", device)
        wait = FOLLOW_UP_SECONDS
        if mode == "CREATED":
            if raw.strip():
                await self._answer(ws, session, raw, first=True, received=got_init)
            else:
                await session.speak(ws, "要控制什么")
                wait = ASK_SECONDS
        try:
            await self._listen(ws, session, wait)
        finally:
            if session.socket is ws:
                session.socket = None

    async def _setup(self, ws: ServerConnection) -> None:
        """领 HA 令牌：HA 网页里登录后由页面生成长期令牌直接送来，令牌不经过人手，也不进日志。

        只收能登录这台 HA 的令牌（当场连 HA 验一次），所以不需要别的口令：能生成这种令牌的人本来就管着 HA。
        """
        try:
            token = str(await asyncio.wait_for(ws.recv(), SETUP_TIMEOUT_SECONDS)).strip()
        except (TimeoutError, ConnectionClosed):
            return
        if not token or len(token) > MAX_TOKEN_CHARS:
            await ws.send("empty")
            await ws.close(1008)
            return
        if not await self.home.ha.check_token(token):
            log.warning("领令牌：送来的令牌登录不了 HA，没收")
            await ws.send("invalid")
            await ws.close(1008)
            return
        if self.ha_token_file is not None:
            self.ha_token_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.ha_token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token + "\n")
        self.home.ha.use_token(token)
        log.info("领令牌：收到能登录 HA 的新令牌，已保存，正在重连")
        await ws.send("ok")
        await ws.close(1000)

    def _remember(self, session: Session) -> Session:
        if len(self.sessions) >= MAX_SESSIONS:
            for key in [k for k, s in self.sessions.items() if s.socket is None][: len(self.sessions) - MAX_SESSIONS + 1]:
                del self.sessions[key]
        self.sessions[session.id] = session
        return session

    async def _answer(self, ws: ServerConnection, session: Session, text: str, *, first: bool, received: float) -> bool:
        """处理一句话，返回有没有回话。"""
        reply = await self.home.respond(text, first=first)
        if reply is None:
            log.info("会话 %s：一句 %d 字，不是命令，不回", session.id[:8], len(text))
            return False
        await session.speak(ws, reply)
        log.info("会话 %s：%s 收到一句 %d 字，+%d ms 回话", session.id[:8], utc(received), len(text), ms(received, time.time()))
        return True

    async def _listen(self, ws: ServerConnection, session: Session, wait: float) -> None:
        deadline = time.monotonic() + wait
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                log.info("会话 %s：没有追加的话，结束 %s", session.id[:8], utc(time.time()))
                await ws.close(1000)
                return
            try:
                raw = await asyncio.wait_for(ws.recv(), left)
            except TimeoutError:
                continue
            got = time.time()
            if isinstance(raw, bytes):  # 音频流的分片：这个插件用「ASR文本」，不该来
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            kind = message.get("type")
            payload = message.get("payload") or {}
            if kind != "UTTERANCE":
                log.info("会话 %s：收到 %s", session.id[:8], kind)
                continue
            text = str(payload.get("text") or "")
            if plain(text) in EXIT_WORDS:
                log.info("会话 %s：用户说结束", session.id[:8])
                await ws.close(1000)
                return
            if await self._answer(ws, session, text, first=False, received=got):
                deadline = time.monotonic() + FOLLOW_UP_SECONDS  # 说了命令，再等一轮


async def start(agent: Agent, host: str, port: int) -> Server:
    return await serve(
        agent.connection,
        host,
        port,
        process_request=agent.process_request,
        process_response=agent.process_response,
        max_size=MAX_MESSAGE_BYTES,
        ping_interval=20,
        ping_timeout=20,
    )
