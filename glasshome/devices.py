"""设备名录：用户嘴里的名字 → HA 实体。

名字有两个来源，配置文件优先：

- ``devices.toml`` 里写的说法（``[[device]]`` 下的 ``entity`` 和 ``names``）；
- HA 里实体的 ``friendly_name``，去掉空格。米家的名字常常很长（“米家智能台灯 Pro”），所以常用设备要在配置里起短名。

同一个名字对上多个实体时，配置里写的照用（用户自己定的），自动取的名字丢掉，免得猜错设备。
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 认得的设备种类。别的种类（传感器、自动化……）不进名录
DOMAINS = frozenset({"light", "switch", "climate", "fan", "cover", "humidifier", "input_boolean", "media_player"})

#: 传感器只收这几种，只能问（“屋里多少度”）
SENSOR_CLASSES = frozenset({"temperature", "humidity"})

_SPACE = re.compile(r"[\s　·•\-_/]+")


def wanted(entity_id: str, state: Mapping[str, Any]) -> bool:
    """这个实体进不进名录。"""
    domain = entity_id.partition(".")[0]
    if domain == "sensor":
        return (state.get("attributes") or {}).get("device_class") in SENSOR_CLASSES
    return domain in DOMAINS


def normalize(text: str) -> str:
    """名字和句子都这样处理后再比：去空白和连接符，英文小写。"""
    return _SPACE.sub("", text).lower()


@dataclass(frozen=True)
class Device:
    entity_id: str
    names: tuple[str, ...]

    @property
    def domain(self) -> str:
        return self.entity_id.partition(".")[0]

    @property
    def label(self) -> str:
        """播报时用的名字：配置里写的第一个，或者 friendly_name。"""
        return self.names[0] if self.names else self.entity_id


@dataclass(frozen=True)
class Match:
    device: Device | None  # None：这个名字对上不止一个设备
    name: str  # 句子里匹配到的那段（归一化后）
    start: int
    end: int


class Catalog:
    def __init__(self, devices: Iterable[Device] = ()) -> None:
        self.devices: list[Device] = list(devices)
        self._by_name: dict[str, list[Device]] = {}
        for device in self.devices:
            for name in device.names:
                key = normalize(name)
                if key and device not in self._by_name.setdefault(key, []):
                    self._by_name[key].append(device)

    @classmethod
    def build(cls, configured: Iterable[Mapping[str, Any]], states: Mapping[str, Mapping[str, Any]]) -> Catalog:
        """配置 + HA 当前状态 → 名录。"""
        names: dict[str, list[str]] = {}
        taken: set[str] = set()
        for entry in configured:
            entity = str(entry.get("entity", "")).strip()
            spoken = [str(n).strip() for n in entry.get("names", []) if str(n).strip()]
            if entity and spoken:
                names.setdefault(entity, []).extend(spoken)
                taken.update(normalize(n) for n in spoken)
        auto: dict[str, list[str]] = {}
        for entity, state in states.items():
            if not wanted(entity, state):
                continue
            friendly = str((state.get("attributes") or {}).get("friendly_name") or "").strip()
            # 米家的名字是“设备名  实体名”（两个空格），“空调  空调”“灯泡2  灯”：设备名那段也能叫
            candidates = {normalize(friendly), normalize(re.split(r"\s{2,}", friendly)[0])}
            for key in candidates:
                if key and key not in taken and entity not in auto.get(key, []):
                    auto.setdefault(key, []).append(entity)
        for key, entities in auto.items():
            if len(entities) == 1:  # 重名的自动名字不要
                names.setdefault(entities[0], []).append(key)
        return cls(Device(entity, tuple(spoken)) for entity, spoken in names.items())

    def by_domain(self, domain: str) -> list[Device]:
        return [d for d in self.devices if d.domain == domain]

    def named(self, key: str) -> list[Device]:
        """名字正好是 ``key``（归一化后）的设备。"""
        return list(self._by_name.get(key, []))

    def containing(self, fragment: str) -> list[Device]:
        """名字里含 ``fragment`` 的设备（“净化”→ 空气净化器），按名录顺序、不重复。"""
        found: list[Device] = []
        for key, devices in self._by_name.items():
            if fragment and fragment in key:
                found.extend(d for d in devices if d not in found)
        return found

    def find(self, sentence: str) -> Match | None:
        """句子里最长的那个名字。``sentence`` 要先归一化。"""
        best: Match | None = None
        for key, devices in self._by_name.items():
            start = sentence.find(key)
            if start < 0:
                continue
            if best is None or len(key) > len(best.name):
                device = devices[0] if len(devices) == 1 else None
                best = Match(device, key, start, start + len(key))
        return best


def load_config(path: Path | None) -> list[dict[str, Any]]:
    """读 ``devices.toml``；没有这个文件就是空表。"""
    if path is None or not path.exists():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = data.get("device", [])
    if not isinstance(entries, list):
        raise ValueError("devices.toml 的 device 要写成 [[device]] 列表")
    return [e for e in entries if isinstance(e, dict)]
