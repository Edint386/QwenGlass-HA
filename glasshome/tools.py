"""MCP 工具背后的逻辑：平台的大模型听懂话、填好参数，这里认设备、执行、回一句能直接念的话。

和 Agent 版（``agent.py``）的分工不同：说法五花八门由大模型去懂，这里只管三件事——

- **认设备**：大模型传的设备名不一定和配置一字不差（“卧室空调”“净化”“所有灯”），
  按“完全相同 > 名字在里面 > 名字的一部分”认，认不出来就把能叫的名字列回去。
- **一次做完**：同一台设备的几件事（制冷、24 度、风速最大）一次调用传完，执行层合成最少的 HA 调用；
  大模型只要一轮工具调用，用户少等一轮。
- **3 秒内回话**：平台等工具 3 秒，这里 :data:`DEADLINE_SECONDS` 到了还没回音就先回“没有回应，可能离线了”，命令照样在后台跑完。

参数都宽松收（“26度”“+2”“开”“true”），大模型常这样传。返回扁平的 JSON：``success``、``message``（念给用户的一句话），
出错时多一个 ``error`` 给大模型看。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .command import (
    BRIGHTER,
    DIMMER,
    FAN_LEVELS,
    MODES,
    OFF,
    ON,
    QUERY,
    SET_BRIGHTNESS,
    SET_FAN,
    SET_MODE,
    SET_TEMPERATURE,
    TEMPERATURE_DOWN,
    TEMPERATURE_UP,
    Command,
    allowed,
    parse_number,
)
from .control import MODE_WORDS, Controller, Outcome
from .devices import Catalog, Device, load_config, normalize
from .ha import HAError, HomeAssistant, Lost, NotConnected, Refused

log = logging.getLogger(__name__)

#: 平台等工具 3 秒，扣掉网络往返
DEADLINE_SECONDS = 2.5
#: 一次最多控制几台设备（“所有灯”也算在内）
MAX_DEVICES = 12
#: 不说设备名时，查状态最多报几台
MAX_STATUS = 10

#: “所有灯”“全部的灯”“家里的灯”
_ALL_LIGHTS = re.compile(r"^(?:所有|全部|全屋|家里|屋里|整个屋子)?的?(?:灯|灯光|电灯|照明)(?:都|全部)?$")
#: 几台设备一起说：“挂灯、灯泡”“挂灯和灯泡”
_SEPARATORS = re.compile(r"[、,，;；/+]+|和|跟|与|还有|以及")
_POWER = {
    "on": True, "true": True, "1": True, "开": True, "打开": True, "开启": True, "开着": True, "启动": True,
    "off": False, "false": False, "0": False, "关": False, "关闭": False, "关掉": False, "关上": False, "关机": False,
}
#: 动作 → 说“不能……”时的说法
_ACTION_WORDS = {
    ON: "开", OFF: "关", SET_TEMPERATURE: "调温度", TEMPERATURE_UP: "调温度", TEMPERATURE_DOWN: "调温度",
    SET_MODE: "切模式", SET_FAN: "调风速", SET_BRIGHTNESS: "调亮度", BRIGHTER: "调亮度", DIMMER: "调亮度",
}


class Unknown(ValueError):
    """认不出设备或参数：消息能直接念。"""


def _number(value: Any, what: str) -> float | None:
    """“26”“26.5”“26度”“二十六”“+2”“-1” → 数；空 → None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"(摄氏度|度|℃|°c|°|%|％|百分之)", "", str(value).strip().lower())
    if not text:
        return None
    sign = -1.0 if text[0] in "-－" else 1.0
    text = text.lstrip("+＋-－")
    try:
        return sign * float(text)
    except ValueError:
        parsed = parse_number(text)
        if parsed is None:
            raise Unknown(f"{what}要是一个数") from None
        return sign * parsed


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


class HomeTools:
    def __init__(self, ha: HomeAssistant, devices_file: Path | None = None) -> None:
        self.ha = ha
        self.controller = Controller(ha)
        self.devices_file = devices_file

    def config(self) -> list[dict[str, Any]]:
        try:
            return load_config(self.devices_file)
        except (OSError, ValueError) as exc:
            log.warning("devices.toml 读不了：%s", exc)
            return []

    def catalog(self) -> Catalog:
        """每次现建（一毫秒左右）：设备改名、配置改了马上生效。"""
        return Catalog.build(self.config(), self.ha.states)

    # ---- 给工具描述用 ----

    def device_list(self) -> str:
        """配置里的设备和它们的说法，写进工具描述，大模型不用先查一轮。"""
        controls: list[str] = []
        sensors: list[str] = []
        for entry in self.config():
            names = [str(n) for n in entry.get("names", []) if str(n).strip()]
            if not names:
                continue
            text = names[0] + (f"（也叫{'、'.join(names[1:])}）" if names[1:] else "")
            (sensors if str(entry.get("entity", "")).startswith("sensor.") else controls).append(text)
        parts = []
        if controls:
            parts.append("能控制的：" + "；".join(controls))
        if sensors:
            parts.append("只能查的：" + "；".join(sensors))
        return "。".join(parts)

    # ---- 认设备 ----

    def _configured_lights(self, catalog: Catalog) -> list[Device]:
        configured = {str(e.get("entity", "")) for e in self.config()}
        lights = [d for d in catalog.by_domain("light") if d.entity_id in configured]
        return lights or catalog.by_domain("light")

    def resolve(self, text: Any, catalog: Catalog) -> list[tuple[Device, str]]:
        """设备名 → [(设备, 回话里叫它什么)]。认不出抛 :class:`Unknown`。"""
        raw = _text(text)
        if not raw:
            raise Unknown("要说是哪台设备")
        whole = normalize(raw)
        pieces = [whole] if catalog.named(whole) else [normalize(p) for p in _SEPARATORS.split(raw)]
        found: list[tuple[Device, str]] = []
        for piece in filter(None, pieces):
            for device, name in self._resolve_one(piece, catalog):
                if all(device != d for d, _ in found):
                    found.append((device, name))
        if not found:
            raise Unknown("要说是哪台设备")
        if len(found) > MAX_DEVICES:
            raise Unknown(f"一次最多控制{MAX_DEVICES}台设备")
        return found

    def _resolve_one(self, piece: str, catalog: Catalog) -> list[tuple[Device, str]]:
        exact = catalog.named(piece)
        if len(exact) == 1:
            return [(exact[0], piece)]
        if len(exact) > 1:
            raise Unknown(f"有好几个{piece}，说得具体一点")
        if _ALL_LIGHTS.match(piece):
            return [(d, d.label) for d in self._configured_lights(catalog)]
        match = catalog.find(piece)  # “卧室的空调”里有“空调”
        if match is not None and match.device is not None:
            return [(match.device, match.device.label)]
        partial = catalog.containing(piece)  # “净化”是“空气净化器”的一部分
        if len(partial) == 1:
            return [(partial[0], partial[0].label)]
        if piece.endswith("灯") and len(piece) <= 3:  # “台灯”“灯”：配置里只有一盏灯时就是它
            lights = self._configured_lights(catalog)
            if len(lights) == 1:
                return [(lights[0], lights[0].label)]
        names = "、".join(e["names"][0] for e in self.config()
                         if e.get("names") and not str(e.get("entity", "")).startswith("sensor.")) or "米家里的名字"
        if len(partial) > 1:
            raise Unknown(f"{piece}对上好几台设备：{'、'.join(d.label for d in partial[:5])}，说具体一点")
        raise Unknown(f"家里没有叫{piece}的设备，能控制的有：{names}")

    # ---- 参数 → 命令 ----

    def commands(self, device: Device, name: str, args: dict[str, Any]) -> list[Command]:
        """一台设备要做的几件事。没给任何参数、参数不认识、这台设备不支持，都抛 :class:`Unknown`。"""
        actions: list[tuple[str, float | str | None]] = []
        power = _text(args.get("power")).lower()
        if isinstance(args.get("power"), bool):
            power = "on" if args["power"] else "off"
        if power:
            if power not in _POWER:
                raise Unknown("power 只能是 on 或 off")
            actions.append((ON if _POWER[power] else OFF, None))
        temperature = _number(args.get("temperature"), "温度")
        if temperature is not None:
            actions.append((SET_TEMPERATURE, temperature))
        change = _number(args.get("temperature_change"), "温度变化")
        if change and temperature is None:
            actions.append((TEMPERATURE_UP if change > 0 else TEMPERATURE_DOWN, abs(change)))
        mode = _text(args.get("mode"))
        if mode:
            actions.append((SET_MODE, self._mode(mode)))
        fan = _text(args.get("fan"))
        if fan:
            actions.append((SET_FAN, self._fan(fan, device)))
        brightness = _number(args.get("brightness"), "亮度")
        if brightness is not None:
            actions.append((SET_BRIGHTNESS, brightness))
        step = _number(args.get("brightness_change"), "亮度变化")
        if step and brightness is None:
            actions.append((BRIGHTER if step > 0 else DIMMER, abs(step)))
        if not actions:
            raise Unknown(f"要对{name}做什么？开、关、调温度、调风速还是调亮度")
        kinds = {a for a, _ in actions}
        if OFF in kinds:  # 关掉就不管别的了
            actions = [(OFF, None)]
        elif ON in kinds and len(actions) > 1 and device.domain in ("light", "fan"):
            actions = [a for a in actions if a[0] != ON]  # 调亮度、调风速本身就会开
        for action, _ in actions:
            if not allowed(action, device.domain):
                raise Unknown(f"{name}不能{_ACTION_WORDS.get(action, '这样调')}")
        return [Command(action, device, name, value) for action, value in actions]

    @staticmethod
    def _mode(text: str) -> str:
        key = normalize(text).removesuffix("模式")
        if key in MODES:
            return MODES[key]
        if key in MODE_WORDS and key != "off":
            return key
        for word, mode in (("冷", "cool"), ("热", "heat"), ("暖", "heat"), ("湿", "dry"), ("风", "fan_only"), ("自动", "auto")):
            if word in key:
                return mode
        raise Unknown("模式只能是制冷、制热、除湿、送风、自动")

    def _fan(self, text: str, device: Device) -> str:
        key = normalize(text).removesuffix("档").removesuffix("风").removesuffix("速")
        if device.domain == "climate":
            modes = [str(m) for m in ((self.ha.state(device.entity_id) or {}).get("attributes") or {}).get("fan_modes") or []]
            for candidate in (text, key):
                if candidate in modes:
                    return candidate
        if key in FAN_LEVELS:
            return FAN_LEVELS[key]
        if key in ("max", "mid", "min", "auto"):
            return key
        raise Unknown("风速只能是最大、高、中、低、最小、自动")

    # ---- 工具 ----

    async def control(self, device: Any = "", **args: Any) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            catalog = self.catalog()
            targets = self.resolve(device, catalog)
            commands = [c for d, name in targets for c in self.commands(d, name, args)]
        except Unknown as exc:
            return {"success": False, "message": str(exc), "error": "bad arguments"}
        result = await self._run(commands, [name for _, name in targets])
        log.info("control %s %s → %s「%s」%d ms", device, {k: v for k, v in args.items() if v not in (None, "")},
                 "成功" if result["success"] else "失败", result["message"], _ms(started))
        return result

    async def status(self, device: Any = "") -> dict[str, Any]:
        started = time.perf_counter()
        catalog = self.catalog()
        try:
            if _text(device):
                targets = self.resolve(device, catalog)
            else:
                targets = self._everyday(catalog)
        except Unknown as exc:
            return {"success": False, "message": str(exc), "error": "bad arguments"}
        if not targets:
            return {"success": False, "message": "名录里还没有设备", "error": "empty catalog"}
        result = await self._run([Command(QUERY, d, name) for d, name in targets], [name for _, name in targets])
        if self.ha.connected.is_set() and result.get("error") == "failed":  # 某台离线也是一种状态，照实念就行
            result = {"success": True, "message": result["message"]}
        log.info("status %s → %s「%s」%d ms", device or "全部", "成功" if result["success"] else "失败", result["message"], _ms(started))
        return result

    def _everyday(self, catalog: Catalog) -> list[tuple[Device, str]]:
        """不说设备名：配置里写了的设备（用户常用的），没配置就名录前几个。"""
        by_id = {d.entity_id: d for d in catalog.devices}
        chosen = [(by_id[e["entity"]], e["names"][0]) for e in self.config() if e.get("entity") in by_id and e.get("names")]
        return chosen[:MAX_STATUS] or [(d, d.label) for d in catalog.devices[:MAX_STATUS]]

    async def call_service(self, device: Any = "", service: Any = "", data: Any = None) -> dict[str, Any]:
        """兜底：直接调 HA 的服务。设备在全部实体里按名字找（扫地机、场景不在名录里）。"""
        started = time.perf_counter()
        try:
            entity_id, name = self._resolve_any(device)
            domain, service_name = self._service(service, entity_id)
            payload = self._data(data)
        except Unknown as exc:
            return {"success": False, "message": str(exc), "error": "bad arguments"}
        result = await self._deadline(self._call_service(domain, service_name, entity_id, name, payload), [name])
        log.info("call_service %s.%s %s %s → %s「%s」%d ms", domain, service_name, entity_id, payload,
                 "成功" if result["success"] else "失败", result["message"], _ms(started))
        return result

    def _resolve_any(self, device: Any) -> tuple[str, str]:
        raw = _text(device)
        if not raw:
            raise Unknown("要说是哪台设备")
        if re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", raw) and raw in self.ha.states:
            return raw, raw
        try:
            found = self.resolve(raw, self.catalog())
            if len(found) == 1:
                return found[0][0].entity_id, found[0][1]
        except Unknown:
            pass
        key = normalize(raw)
        hits = [e for e, s in self.ha.states.items()
                if key in normalize(str((s.get("attributes") or {}).get("friendly_name") or ""))]
        if len(hits) == 1:
            return hits[0], raw
        if hits:
            names = "、".join(str((self.ha.states[e].get("attributes") or {}).get("friendly_name") or e) for e in hits[:5])
            raise Unknown(f"{raw}对上好几个：{names}，说具体一点")
        raise Unknown(f"Home Assistant 里没有叫{raw}的设备")

    @staticmethod
    def _service(service: Any, entity_id: str) -> tuple[str, str]:
        text = _text(service).lower()
        own = entity_id.partition(".")[0]
        domain, _, name = text.rpartition(".")
        domain = domain or own
        if not re.fullmatch(r"[a-z_]+", domain) or not re.fullmatch(r"[a-z_]+", name or ""):
            raise Unknown("service 要写成“域.服务”，比如 vacuum.start")
        if domain not in (own, "homeassistant"):
            raise Unknown(f"{entity_id} 只能用 {own}. 开头的服务")
        return domain, name

    @staticmethod
    def _data(data: Any) -> dict[str, Any]:
        if data in (None, ""):
            return {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                raise Unknown("data 要是 JSON 对象") from None
        if not isinstance(data, dict):
            raise Unknown("data 要是 JSON 对象")
        data = {k: v for k, v in data.items() if k not in ("entity_id", "device_id", "area_id")}
        return data

    async def _call_service(self, domain: str, service: str, entity_id: str, name: str, data: dict[str, Any]) -> Outcome:
        if not await self.ha.ready():
            return Outcome("家里的 Home Assistant 没连上", False, {})
        try:
            try:
                await self.ha.call_service(domain, service, entity_id, data)
            except (Lost, NotConnected):
                if not await self.ha.ready():
                    raise
                await self.ha.call_service(domain, service, entity_id, data)
        except Refused as exc:
            return Outcome(f"{name}没执行成功：{exc}", False, {})
        except TimeoutError:
            return Outcome(f"{name}没有回应", False, {})
        except HAError:
            return Outcome("家里的 Home Assistant 没连上", False, {})
        return Outcome(f"{name}已执行{domain}.{service}", True, {})

    # ---- 执行，最多等 DEADLINE_SECONDS ----

    async def _run(self, commands: list[Command], names: Iterable[str]) -> dict[str, Any]:
        return await self._deadline(self.controller.run(commands), names)

    async def _deadline(self, work: Any, names: Iterable[str]) -> dict[str, Any]:
        task = asyncio.ensure_future(work)
        try:
            outcome: Outcome = await asyncio.wait_for(asyncio.shield(task), DEADLINE_SECONDS)
        except TimeoutError:
            task.add_done_callback(_late)
            who = "、".join(dict.fromkeys(names))
            # 米家设备一般半秒内就回；等这么久多半是设备离线，小米云过几秒会回“设备操作超时”
            return {"success": False, "message": f"{who}没有回应，可能离线了", "error": "device did not answer in time"}
        result: dict[str, Any] = {"success": outcome.ok, "message": outcome.speech}
        if not outcome.ok:
            result["error"] = "failed"
        return result


def _late(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("超时后的命令出错：%r", exc)
    else:
        log.info("超时后的命令完成：%s", task.result().speech)


def _ms(start: float) -> int:
    return int(round((time.perf_counter() - start) * 1000))
