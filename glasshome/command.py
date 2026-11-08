"""把一句话认成家居命令（做填空题）。

一句话先按逗号、“然后”这类词拆成几小句，每小句去掉客气话（请、帮我、一下、吧……）之后，
必须正好套进某个句式：“开 __”“把 __ 调到 __ 度”“__ 风力调到最大”……空格里填设备名。
没说设备名的小句沿用前一句的设备：“空调制冷调到 24°，风力调到最大”是空调的三件事。

只认整句：会话里麦克风一直开着，旁人说的长句、闲聊、里面恰好带着“关灯”两个字的话，都不能当成命令
（阅读器吃过亏：子串匹配把进度带回了书头）。有一小句套不上，整句都不算，交给大模型或 HA 的 Assist。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .devices import Catalog, Device, normalize

#: 一小句超过这么多字不当命令
MAX_CHARS = 24
#: 整句超过这么多字不当命令
MAX_TOTAL = 60

ON, OFF = "on", "off"
SET_TEMPERATURE, TEMPERATURE_UP, TEMPERATURE_DOWN = "set_temperature", "temperature_up", "temperature_down"
SET_BRIGHTNESS, BRIGHTER, DIMMER = "set_brightness", "brighter", "dimmer"
SET_MODE, SET_FAN, QUERY = "set_mode", "set_fan", "query"
ACTIONS = frozenset({ON, OFF, SET_TEMPERATURE, TEMPERATURE_UP, TEMPERATURE_DOWN, SET_BRIGHTNESS, BRIGHTER, DIMMER, SET_MODE, SET_FAN, QUERY})

#: 空调模式的说法 → HA 的 hvac_mode
MODES = {"制冷": "cool", "冷风": "cool", "制热": "heat", "暖风": "heat", "除湿": "dry", "抽湿": "dry", "送风": "fan_only", "自动": "auto"}
#: 风速的说法 → 档位（control.py 按设备支持的档位换成具体的值）
FAN_LEVELS = {
    "最大": "max", "最高": "max", "最强": "max", "大": "max", "高": "max", "强": "max",
    "中等": "mid", "中间": "mid", "中": "mid",
    "最小": "min", "最低": "min", "最弱": "min", "小": "min", "低": "min", "弱": "min", "静音": "min",
    "自动": "auto",
}


@dataclass(frozen=True)
class Command:
    action: str
    device: Device
    name: str  # 用户说的设备名，回话时照着说
    value: float | str | None = None


@dataclass(frozen=True)
class Ambiguous:
    """名字对上了不止一个设备。"""

    name: str


_PUNCT = re.compile(r"[，。！？、,.!?~～…\"'“”‘’：:；;（）()\[\]【】]+")
#: 数字中间的小数点先换成“点”，不然去标点时 25.5 成了 255
_DECIMAL = re.compile(r"(?<=\d)[.．](?=\d)")
#: 拆小句：标点和连接词
_SPLIT = re.compile(r"[，,。；;！!？?]+|然后|并且|同时|顺便|接着")
_LEAD = re.compile(r"^(请你|请|麻烦你|麻烦|帮我|帮忙|给我|你|现在|那就|那|嗯|好|再)+")
_TAIL = re.compile(r"(一下吧|一下|吧|啊|呀|哦|呢|哈|好吗|好不好|行吗|可以吗|谢谢)+$")

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"(?P<n>\d{1,3}(?:点\d)?|[零〇一二两三四五六七八九十百点]{1,6})"
_UNIT = r"(?:度|摄氏度|℃|°c|°)"


def parse_number(text: str) -> float | None:
    """“26”“26.5”“二十六”“二十六点五”“一百” → 数。"""
    if re.fullmatch(r"\d{1,3}([.点]\d)?", text):
        return float(text.replace("点", "."))
    whole, _, frac = text.partition("点")
    value = _cn_int(whole)
    if value is None:
        return None
    if frac:
        if len(frac) != 1 or frac not in _CN_DIGITS:
            return None
        return value + _CN_DIGITS[frac] / 10
    return float(value)


def _cn_int(text: str) -> int | None:
    if not text:
        return None
    if text == "一百":
        return 100
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    tens, sep, ones = text.partition("十")
    if not sep:
        return None
    high = 1 if tens == "" else _CN_DIGITS.get(tens)
    low = 0 if ones == "" else _CN_DIGITS.get(ones)
    if high is None or low is None or len(tens) > 1 or len(ones) > 1:
        return None
    return high * 10 + low


def plain(text: str) -> str:
    """只去标点和空白，“好了”“谢谢”这类结束语这样比。"""
    return normalize(_PUNCT.sub("", _DECIMAL.sub("点", text)))


def clean(text: str) -> str:
    """去标点、空白、客气话。"""
    text = plain(text)
    for _ in range(3):  # “请帮我”“一下吧”这类叠着的
        text = _TAIL.sub("", _LEAD.sub("", text))
    return text


def clauses(text: str) -> list[str]:
    """拆成小句，每句去掉客气话，空的不要。"""
    parts = (clean(p) for p in _SPLIT.split(_DECIMAL.sub("点", text)))
    return [p for p in parts if p]


# 设备名替换成 D 之后整句匹配。顺序有讲究：长的说法在前
_VERB_SET = r"(?:调到|调成|调至|调为|设为|设成|设置为|设置成|设到|开到|改成|改到|定到|定在|调在|到)"
_MODE = r"(?P<m>" + "|".join(MODES) + r")"
_FAN = r"(?P<f>" + "|".join(sorted(FAN_LEVELS, key=len, reverse=True)) + r")"
_PATTERNS: list[tuple[tuple[str, ...], re.Pattern[str]]] = [
    ((QUERY,), re.compile(r"^D(?:现在)?(?:设的|调的|设定的|开的|设定|定的)?(?:是)?(?:是不是开着的?|是不是关着的?|开着吗|关着吗|开了吗|关了吗|开着没有?|关了没有?|是开着的吗|是关着的吗|什么状态|状态|几度|多少度|温度多少|温度是多少|是多少度|湿度多少|湿度是多少)$")),
    ((QUERY,), re.compile(r"^(?:看看|查查|查一下|看一下)D(?:的)?(?:状态|温度|湿度)?$")),
    ((ON,), re.compile(r"^(?:打开|开启|启动|开一下|开开|开)D$")),
    ((ON,), re.compile(r"^把?D(?:打开|开启|启动|开一下|开开|开了|开起来|开)$")),
    ((OFF,), re.compile(r"^(?:关闭|关掉|关上|关一下|停止|停掉|关)D$")),
    ((OFF,), re.compile(r"^把?D(?:关闭|关掉|关上|关一下|关了|停了|停掉|关)$")),
    # “空调制冷 24 度”“空调制冷调到 24°”：一小句两件事
    ((SET_MODE, SET_TEMPERATURE), re.compile(r"^把?D(?:调到|调成|开|设为|切到|切换到|换成|改成|用|开到)?" + _MODE + r"(?:模式)?" + _VERB_SET + r"?" + _NUM + _UNIT + r"?$")),
    ((SET_MODE,), re.compile(r"^把?D(?:调到|调成|开|设为|切到|切换到|换成|改成|用)?" + _MODE + r"(?:模式)?$")),
    ((SET_FAN,), re.compile(r"^把?D(?:的)?(?:风力|风速|风量|风)(?:调到|调成|开到|设为|设成|改成|调|开|设|开成)?" + _FAN + r"(?:档|挡|风)?$")),
    ((SET_FAN,), re.compile(r"^把?D(?:开|调到|调成|开成)?" + _FAN + r"(?:档|挡)?风$")),
    ((TEMPERATURE_UP,), re.compile(r"^把?D(?:的)?(?:温度)?(?:调高|升高|提高|加|高)" + _NUM + r"?" + _UNIT + r"?(?:一点|一些|点)?$")),
    ((TEMPERATURE_DOWN,), re.compile(r"^把?D(?:的)?(?:温度)?(?:调低|降低|降|低|减)" + _NUM + r"?" + _UNIT + r"?(?:一点|一些|点)?$")),
    ((BRIGHTER,), re.compile(r"^把?D(?:调亮|亮)(?:一点|一些|点)?$")),
    ((DIMMER,), re.compile(r"^把?D(?:调暗|暗)(?:一点|一些|点)?$")),
    # 数值：空调是温度，灯是亮度。动词和单位至少有一个，“空调26”也算（温度）
    ((SET_TEMPERATURE,), re.compile(r"^把?D(?:的)?(?:温度)?" + _VERB_SET + r"?" + _NUM + _UNIT + r"$")),
    ((SET_TEMPERATURE,), re.compile(r"^把?D(?:的)?(?:温度)?" + _VERB_SET + _NUM + r"$")),
    ((SET_BRIGHTNESS,), re.compile(r"^把?D(?:的)?(?:亮度)?" + _VERB_SET + r"?(?:百分之)?" + _NUM + r"(?:%|％)?(?:的亮度)?$")),
]

#: 动作 → 能用在哪几种设备上；没列的动作（开、关）除传感器外都行，查询都行
ONLY = {
    SET_TEMPERATURE: {"climate"},
    TEMPERATURE_UP: {"climate"},
    TEMPERATURE_DOWN: {"climate"},
    SET_MODE: {"climate"},
    SET_FAN: {"climate", "fan"},
    SET_BRIGHTNESS: {"light"},
    BRIGHTER: {"light"},
    DIMMER: {"light"},
}


def allowed(action: str, domain: str) -> bool:
    if action == QUERY:
        return True
    if domain == "sensor":
        return False
    return domain in ONLY.get(action, {domain})


def _match(rest: str, device: Device) -> list[tuple[str, float | str | None]] | None:
    domain = device.domain
    for actions, pattern in _PATTERNS:
        found = pattern.match(rest)
        if not found:
            continue
        groups = found.groupdict()
        number = None
        if groups.get("n"):
            number = parse_number(groups["n"])
            if number is None:
                continue
        result: list[tuple[str, float | str | None]] = []
        for action in actions:
            if not allowed(action, domain):
                if action == SET_TEMPERATURE and domain == "light":  # “台灯调到50”：灯是亮度
                    action = SET_BRIGHTNESS
                elif action == SET_BRIGHTNESS and domain == "climate" and "%" not in rest and "百分之" not in rest:
                    action = SET_TEMPERATURE  # “空调26”：空调是温度
                else:
                    result = []
                    break
            if action == SET_MODE:
                value: float | str | None = MODES[groups["m"]]
            elif action == SET_FAN:
                value = FAN_LEVELS[groups["f"]]
            elif action in (TEMPERATURE_UP, TEMPERATURE_DOWN):
                value = number if number is not None else 1.0
            else:
                value = number
            result.append((action, value))
        if result:
            return result
    return None


def parse(text: str, catalog: Catalog) -> list[Command] | Ambiguous | None:
    """认不出来返回 None。一小句认不出，整句都不算。"""
    if len(plain(text)) > MAX_TOTAL:
        return None
    commands: list[Command] = []
    last: tuple[Device, str] | None = None
    for clause in clauses(text):
        if len(clause) > MAX_CHARS:
            return None
        match = catalog.find(clause)
        if match is None:
            if last is None:
                return None
            device, name = last
            rest = "D" + clause.removeprefix("把")
        elif match.device is None:
            return Ambiguous(match.name)
        else:
            device, name = match.device, match.name
            rest = clause[: match.start] + "D" + clause[match.end :]
        found = _match(rest, device)
        if found is None:
            return None
        commands.extend(Command(action, device, name, value) for action, value in found)
        last = (device, name)
    return commands or None
