"""执行一串命令，返回要播的那句话。

- 命令都是“设成某状态”：开、关、温度设成 26 度。“调高一度”按缓存里的目标温度算成绝对值。
  所以连接断在半路（:class:`~glasshome.ha.Lost`）时重试一次也不会多做一遍。
- 缓存里已经是这个状态就不发命令，直接说“已经开着了”：省一个往返。
- 同一台空调的几件事合起来发：模式和温度一次 ``set_temperature``（带 ``hvac_mode``），风速一次 ``set_fan_mode``。
- 回话短：播报越短，用户越早听完。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from .command import (
    BRIGHTER,
    DIMMER,
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
)
from .devices import Device
from .ha import HAError, HomeAssistant, Lost, NotConnected, Refused

log = logging.getLogger(__name__)

#: ClimateEntityFeature.TURN_ON：没有这一位的空调用 set_hvac_mode 开
CLIMATE_TURN_ON = 128
#: 亮度一次调多少
BRIGHTNESS_STEP = 20
MODE_WORDS = {"cool": "制冷", "heat": "制热", "dry": "除湿", "fan_only": "送风", "auto": "自动", "heat_cool": "自动", "off": "关"}
FAN_WORDS = {"max": "最大", "mid": "中档", "min": "最小", "auto": "自动"}
#: 这几种状态不算“开着”
OFF_STATES = frozenset({"off", "closed", "unavailable", "unknown", "standby", "idle"})

Call = tuple[str, str, dict[str, Any]]
Plan = str | tuple[list[Call], str]


@dataclass
class Outcome:
    speech: str
    ok: bool
    #: 各步用时（毫秒），写日志
    timings: dict[str, int]


def _ms(start: float) -> int:
    return int(round((time.perf_counter() - start) * 1000))


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


#: 扫地机：这两种状态算“开着”（在扫、在回去），停在座上、暂停都算关
VACUUM_ON = frozenset({"cleaning", "returning"})
VACUUM_WORDS = {"cleaning": "正在扫", "returning": "正在回充", "docked": "在充电座上", "idle": "停着", "paused": "暂停了", "error": "出故障了"}


def is_on(state: dict[str, Any]) -> bool:
    if str(state.get("entity_id", "")).startswith("vacuum."):
        return str(state.get("state")) in VACUUM_ON
    return str(state.get("state")) not in OFF_STATES


def pick_fan(level: str, modes: list[str]) -> str | None:
    """档位（max / mid / min / auto）→ 设备支持的风速。列表一般从低到高排，“自动”单独挑出来。"""
    autos = [m for m in modes if "auto" in m.lower() or "自动" in m]
    rest = [m for m in modes if m not in autos]
    if level == "auto":
        return autos[0] if autos else None
    if not rest:
        return None
    return {"max": rest[-1], "min": rest[0]}.get(level, rest[len(rest) // 2])


class Controller:
    def __init__(self, ha: HomeAssistant) -> None:
        self.ha = ha

    async def run(self, commands: list[Command]) -> Outcome:
        """按设备分组依次执行；某台失败不影响后面的，回话里分别说。"""
        start = time.perf_counter()
        timings: dict[str, int] = {}
        if not await self.ha.ready():
            return Outcome("家里的 Home Assistant 没连上", False, {"wait": _ms(start)})
        groups: list[tuple[Device, list[Command]]] = []
        for command in commands:
            if groups and groups[-1][0] == command.device:
                groups[-1][1].append(command)
            else:
                groups.append((command.device, [command]))
        speeches: list[str] = []
        ok = True
        for device, group in groups:
            speech, good = await self._run_group(device, group, timings)
            speeches.append(speech)
            ok = ok and good
        timings["total"] = _ms(start)
        return Outcome("，".join(speeches), ok, timings)

    async def _run_group(self, device: Device, group: list[Command], timings: dict[str, int]) -> tuple[str, bool]:
        name = group[0].name
        state = self.ha.state(device.entity_id)
        if state is None:
            return f"{name}在 Home Assistant 里找不到了", False
        if state.get("state") == "unavailable":
            return f"{name}现在离线", False
        try:
            plan = self._plan_climate(group, state) if device.domain == "climate" and len(group) > 1 else self._plan_all(group, state)
        except ValueError as exc:
            return str(exc), False
        if isinstance(plan, str):  # 不用发命令：查询，或者已经是这个状态
            return plan, True
        calls, speech = plan
        try:
            for domain, service, data in calls:
                sent = time.perf_counter()
                await self._call(domain, service, device.entity_id, data)
                timings[f"{domain}.{service}"] = _ms(sent)
        except Refused as exc:
            log.warning("HA 拒绝了 %s：%s", device.entity_id, exc)
            return f"{name}没控制成功", False
        except TimeoutError:
            return f"{name}没有回应", False
        except HAError:
            return "家里的 Home Assistant 没连上", False
        return speech, True

    async def _call(self, domain: str, service: str, entity_id: str, data: dict[str, Any]) -> None:
        """断在半路重试一次：命令都是“设成某状态”，多做一次没有影响。"""
        try:
            await self.ha.call_service(domain, service, entity_id, data)
        except (Lost, NotConnected):
            if not await self.ha.ready():
                raise
            log.info("重连后重试 %s.%s %s", domain, service, entity_id)
            await self.ha.call_service(domain, service, entity_id, data)

    # ---- 命令 → 调用 ----

    def _plan_all(self, group: list[Command], state: dict[str, Any]) -> Plan:
        """一条一条排，调用和回话接起来。"""
        if len(group) == 1:
            return self._plan(group[0], state)
        calls: list[Call] = []
        speeches: list[str] = []
        for command in group:
            plan = self._plan(command, state)
            if isinstance(plan, str):
                speeches.append(plan)
            else:
                calls.extend(plan[0])
                speeches.append(plan[1])
        return (calls, "，".join(speeches)) if calls else "，".join(speeches)

    def _plan_climate(self, group: list[Command], state: dict[str, Any]) -> Plan:
        """同一台空调的几件事合起来：“制冷、24 度、风速最大”两次调用。"""
        name = group[0].name
        attrs = state.get("attributes") or {}
        actions = {c.action: c for c in group}
        if OFF in actions:
            return self._plan(actions[OFF], state)
        if QUERY in actions and len(actions) == 1:
            return self._plan(actions[QUERY], state)
        calls: list[Call] = []
        parts: list[str] = []
        mode = None
        if SET_MODE in actions:
            mode = self._mode(actions[SET_MODE], attrs)
            parts.append(MODE_WORDS.get(mode, mode))
        temperature = None
        for action in (SET_TEMPERATURE, TEMPERATURE_UP, TEMPERATURE_DOWN):
            if action in actions:
                temperature = self._target_temperature(actions[action], attrs)
                parts.append(f"{_number(temperature)}度")
        if temperature is not None:
            data: dict[str, Any] = {"temperature": temperature}
            if mode is not None:
                data["hvac_mode"] = mode
            elif not is_on(state):
                data["hvac_mode"] = self._first_mode(attrs)
            calls.append(("climate", "set_temperature", data))
        elif mode is not None:
            calls.append(("climate", "set_hvac_mode", {"hvac_mode": mode}))
        elif ON in actions and not is_on(state):
            calls.append(self._climate_on(attrs))
        if SET_FAN in actions:
            fan = self._fan_mode(actions[SET_FAN], attrs)
            calls.append(("climate", "set_fan_mode", {"fan_mode": fan}))
            parts.append(f"风速{fan}")
        if not calls:
            return f"{name}已经开着了"
        return calls, name + "，".join(parts or ["开了"])

    def _plan(self, command: Command, state: dict[str, Any]) -> Plan:
        name, domain = command.name, command.device.domain
        attrs = state.get("attributes") or {}
        on = is_on(state)
        action = command.action
        if action == QUERY:
            return self._describe(command, state)
        if action == ON:
            if on:
                return f"{name}已经开着了"
            if domain == "climate":
                return [self._climate_on(attrs)], f"{name}开了"
            if domain == "cover":
                return [("cover", "open_cover", {})], f"{name}打开了"
            if domain == "vacuum":
                return [("vacuum", "start", {})], f"{name}开始扫了"
            return [(self._service_domain(domain), "turn_on", {})], f"{name}开了"
        if action == OFF:
            if not on:
                return f"{name}已经关了"
            if domain == "climate":
                return [("climate", "turn_off", {})], f"{name}关了"
            if domain == "cover":
                return [("cover", "close_cover", {})], f"{name}关上了"
            if domain == "vacuum":
                return [("vacuum", "return_to_base", {})], f"{name}回去充电了"
            return [(self._service_domain(domain), "turn_off", {})], f"{name}关了"
        if action in (SET_TEMPERATURE, TEMPERATURE_UP, TEMPERATURE_DOWN):
            target = self._target_temperature(command, attrs)
            data: dict[str, Any] = {"temperature": target}
            if not on:  # 关着的时候说“调到 26 度”：顺便开
                data["hvac_mode"] = self._first_mode(attrs)
            return [("climate", "set_temperature", data)], f"{name}调到{_number(target)}度了"
        if action == SET_MODE:
            mode = self._mode(command, attrs)
            return [("climate", "set_hvac_mode", {"hvac_mode": mode})], f"{name}调成{MODE_WORDS.get(mode, mode)}了"
        if action == SET_FAN:
            if domain == "climate":
                fan = self._fan_mode(command, attrs)
                return [("climate", "set_fan_mode", {"fan_mode": fan})], f"{name}风速调到{fan}了"
            return self._fan_speed(command, attrs)
        if action == SET_BRIGHTNESS:
            percent = max(1, min(100, int(round(float(command.value or 0)))))
            return [("light", "turn_on", {"brightness_pct": percent})], f"{name}亮度调到{percent}"
        if action in (BRIGHTER, DIMMER):
            size = max(1, min(100, int(round(abs(float(command.value or BRIGHTNESS_STEP))))))
            step = size if action == BRIGHTER else -size
            return [("light", "turn_on", {"brightness_step_pct": step})], f"{name}调{'亮' if step > 0 else '暗'}了"
        raise ValueError("不会这个操作")

    @staticmethod
    def _service_domain(domain: str) -> str:
        return domain if domain in ("light", "switch", "fan", "humidifier", "input_boolean", "media_player") else "homeassistant"

    def _climate_on(self, attrs: dict[str, Any]) -> Call:
        if int(attrs.get("supported_features") or 0) & CLIMATE_TURN_ON:
            return ("climate", "turn_on", {})
        return ("climate", "set_hvac_mode", {"hvac_mode": self._first_mode(attrs)})

    @staticmethod
    def _first_mode(attrs: dict[str, Any]) -> str:
        modes = [str(m) for m in attrs.get("hvac_modes") or [] if m != "off"]
        for preferred in ("cool", "auto", "heat_cool", "heat"):
            if preferred in modes:
                return preferred
        return modes[0] if modes else "cool"

    @staticmethod
    def _mode(command: Command, attrs: dict[str, Any]) -> str:
        mode = str(command.value)
        modes = [str(m) for m in attrs.get("hvac_modes") or []]
        if mode == "auto" and "auto" not in modes and "heat_cool" in modes:
            mode = "heat_cool"
        if modes and mode not in modes:
            raise ValueError(f"{command.name}没有{MODE_WORDS.get(mode, mode)}模式")
        return mode

    @staticmethod
    def _fan_mode(command: Command, attrs: dict[str, Any]) -> str:
        modes = [str(m) for m in attrs.get("fan_modes") or []]
        if str(command.value) in modes:  # 空调自己的档位名（“高”“静音”）原样用
            return str(command.value)
        fan = pick_fan(str(command.value), modes)
        if fan is None:
            raise ValueError(f"{command.name}不能调{FAN_WORDS.get(str(command.value), '')}风速")
        return fan

    @staticmethod
    def _fan_speed(command: Command, attrs: dict[str, Any]) -> Plan:
        """风扇、净化器：按百分比调，“自动”用预设模式。"""
        name, level = command.name, str(command.value)
        if level == "auto":
            presets = [str(p) for p in attrs.get("preset_modes") or []]
            preset = next((p for p in presets if "auto" in p.lower() or "自动" in p), None)
            if preset is None:
                raise ValueError(f"{name}没有自动模式")
            return [("fan", "set_preset_mode", {"preset_mode": preset})], f"{name}调成自动了"
        percent = {"max": 100, "mid": 50, "min": 1}[level]
        return [("fan", "set_percentage", {"percentage": percent})], f"{name}风力调到{FAN_WORDS[level]}了"

    @staticmethod
    def _target_temperature(command: Command, attrs: dict[str, Any]) -> float:
        low = float(attrs.get("min_temp") or 16)
        high = float(attrs.get("max_temp") or 30)
        if command.action == SET_TEMPERATURE:
            target = float(command.value or 0)
        else:
            current = attrs.get("temperature")
            if current is None:
                raise ValueError(f"不知道{command.name}现在设的几度")
            delta = float(command.value or 1)
            target = float(current) + (delta if command.action == TEMPERATURE_UP else -delta)
        if not low <= target <= high:
            raise ValueError(f"{command.name}只能调到{_number(low)}到{_number(high)}度")
        step = float(attrs.get("target_temp_step") or 0.5)
        return round(round(target / step) * step, 1)

    @staticmethod
    def _describe(command: Command, state: dict[str, Any]) -> str:
        name, domain = command.name, command.device.domain
        attrs = state.get("attributes") or {}
        if domain == "sensor":
            unit = str(attrs.get("unit_of_measurement") or "")
            unit = {"°C": "度", "℃": "度", "%": "%"}.get(unit, unit)
            value = state.get("state")
            try:
                value = _number(float(str(value)))
            except ValueError:
                pass
            return f"{name}{value}{unit}"
        if domain == "climate":
            if not is_on(state):
                text = f"{name}关着"
            else:
                mode = MODE_WORDS.get(str(state.get("state")), "")
                target = attrs.get("temperature")
                text = f"{name}开着，{mode}" + (f"{_number(float(target))}度" if target is not None else "")
                if attrs.get("fan_mode"):
                    text += f"，风速{attrs['fan_mode']}"
            room = attrs.get("current_temperature")
            if room is not None:
                text += f"，室温{_number(float(room))}度"
            return text
        if domain == "vacuum":
            return name + VACUUM_WORDS.get(str(state.get("state")), str(state.get("state")))
        if domain == "light" and is_on(state) and attrs.get("brightness") is not None:
            return f"{name}开着，亮度{round(int(attrs['brightness']) * 100 / 255)}"
        return f"{name}{'开着' if is_on(state) else '关着'}"
