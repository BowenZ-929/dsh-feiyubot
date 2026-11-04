"""嘴的语气档：魂的姿态 + 心的读数 + 称呼那边的关系档 → 一个整数档与一句口径行。

三路输入各管一件事：
- 魂的「待人姿态」（manner.stance）是底色：**原句照抄**进 Tone.stance，不改一个字；
- 魂 identity 的自称口径（self_names.display）决定这一行里她怎么自称自己，
  同时是判「违禁自称」的授权名单（称呼那边挑出来的词落在名单外又被当自称用，就退中性叫法）；
- 心（情绪/精力）还没做：heart 为 None 就是**未接入**——按中性档算，但返回里必须说出口
  （heart_source = "heart:未接入"），不许把「未接入」当成 mood=0 静默混过去。

档位与折算全在 organs/mouth/data/address_table.json 的 tone 节里（经 warehouse.load 读入），
本文件不写死任何一个数：档界、软度、禁玩笑、心的折算，都是改数据不改代码。

热加载：from_store 每次调用都 store.raw(...) 现读三张卡（SoulStore.raw 内部按 (mtime, size,
inode) 比对重载），本文件不留任何模块级缓存——魂改了，下一回合就变。

纯：不 import time / datetime / random；不碰宿主；文件 IO 只经 warehouse。

红线：缺卡、缺姿态一律按中性出声，不抛错——嘴是出口的最后一段，宁可中性，也不许把出口打断。
数据文件缺节缺键（section/need）照旧抛 PolicyMiss：那是数据坏了，要人修，不是一句回话的事。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .model import need, section
from .warehouse import load

__all__ = ["Tone", "from_store", "render"]


@dataclass(frozen=True)
class Tone:
    """一个语气档：整数档 + 一句可读口径行，加上它取自哪儿的几条实话。

    字段（第四批 api 照这个接，改字段等于改接口）：
      tier          语气档 1..4（整数）；心未接入时等于关系档，心的折算一律不参与
      label         这一档的名字（「客气」…），来自数据
      line          一句可读的口径行：档位、称呼、姿态原句、自称、心的实话都在里面
      stance        魂的待人姿态原句，照抄不改；魂没给就是空串
      self_name     魂 identity 的自称口径（display）；魂没给就是空串
      address       来自 addressee 的称呼；退中性时是数据里的回退词
      heart_source  "heart:未接入" / "heart:接入但无读数" / "heart:已接入（…）"
      softness      软度数值：本档软度 + 心的折算（未接入时就是本档软度，即中性值）
      ban_joke      这一档禁不禁开玩笑（心的折算不许绕过本档，只能加严或放松）
      relation      关系档 1..4（来自 addressee），用来解释 tier 是怎么来的
    """

    tier: int
    label: str
    line: str
    stance: str
    self_name: str
    address: str
    heart_source: str
    softness: float
    ban_joke: bool
    relation: int


def render(cards: Mapping[str, Any], addressee: Any,
           heart: Optional[Mapping[str, Any]] = None,
           table: Optional[dict] = None) -> Tone:
    """把魂的姿态、称呼那边的关系档、心的读数，合成一个语气档与一句口径行。

    cards 是魂的原始卡（store.raw("manner") / raw("identity") / raw("bounds") 拿到的 dict，
    按块名装成一份 Mapping）。addressee 是 address.resolve 的产物（dataclass 或同样字段的
    dict 都认）。heart 的形状：{"mood": float|None, "energy": float|None,
    "offset": dict|None, "engage": float|None}——任一为 None 就按中性处理，并如实报 heart_source。
    table 不给就现读 organs/mouth/data/address_table.json。
    """
    book = table if isinstance(table, Mapping) else load("address_table")
    rules = section(book, "tone")
    heart_rules = section(rules, "heart")
    low = int(need(rules, "tier_min"))
    high = int(need(rules, "tier_max"))
    neutral_tier = int(need(rules, "neutral_tier"))
    neutral_address = str(need(section(book, "fallback"), "address"))
    manner = _block(cards, "manner")
    identity = _block(cards, "identity")
    stance = _text(manner.get("stance")).strip()
    self_name, allowed = _self_names(identity)
    relation = _clamp(_int_or(_field(addressee, "relation"), neutral_tier), low, high)
    address = _guard(_text(_field(addressee, "address")).strip(), book, allowed, neutral_address)
    readings, heart_source, got = _heart(heart, heart_rules)
    tier_delta, softness_delta, joke = _shifts(readings, heart_rules) if got else (0, 0.0, None)
    tier = _clamp(relation + tier_delta, low, high)
    step = section(rules, "tiers")[str(tier)]
    softness = round(float(need(step, "softness")) + softness_delta,
                     int(need(rules, "softness_digits")))
    ban_joke = bool(need(step, "ban_joke"))
    if joke is not None:
        ban_joke = joke
    label = str(need(step, "label"))
    heart_text = heart_source[len(str(need(heart_rules, "source_prefix"))):] \
        if heart_source.startswith(str(need(heart_rules, "source_prefix"))) else heart_source
    line = str(need(rules, "line")).format(
        label=label, address=address, stance=stance or "魂没给姿态",
        self_name=self_name or "魂没给自称", heart=heart_text)
    return Tone(tier=tier, label=label, line=line, stance=stance, self_name=self_name,
                address=address, heart_source=heart_source, softness=softness,
                ban_joke=ban_joke, relation=relation)


def from_store(store: Any, addressee: Any,
               heart: Optional[Mapping[str, Any]] = None,
               table: Optional[dict] = None) -> Tone:
    """便捷入口：每次调用都 store.raw(...) 现读三张卡，不缓存（魂改了下一回合就变）。

    SoulStore.raw() 内部先比一次 (mtime, size, inode)，盘上变过就重载——
    所以「改完下一回合生效」是现读的自然结果，本文件不许在模块级留副本，那会当场破坏它。
    """
    cards = {"identity": store.raw("identity"),
             "manner": store.raw("manner"),
             "bounds": store.raw("bounds")}
    return render(cards, addressee, heart=heart, table=table)


def _guard(address: str, book: Mapping[str, Any],
           allowed: Tuple[str, ...], neutral: str) -> str:
    """自称红线：称呼词落在授权名单之外又被当自称用，就退到中性叫法（不抛错）。

    addressee.banned_self 已经由 address.resolve 记下；这一层只负责「别把那个词用出去」。
    """
    guard = section(book, "self_guard")
    words = [str(item).strip() for item in (guard.get("words") or [])
             if isinstance(item, str) and item.strip()]
    if address and address in words and address not in allowed:
        return neutral
    return address or neutral


def _shifts(readings: Mapping[str, Any], rules: Mapping[str, Any]) -> Tuple[int, float, Optional[bool]]:
    """心的折算：档位偏移、软度偏移、以及「禁不禁开玩笑」的强制值（None = 不改本档）。

    每条折算政策各自给出 softness_delta / tier_delta / ban_joke；禁玩笑只许加严或放松一档，
    两条政策打架时「禁」赢——心的读数不许把红线放松。
    """
    tier_delta = 0
    softness_delta = 0.0
    forced: List[Any] = []
    energy = _number(readings.get("energy"))
    if energy is not None and energy < float(need(rules, "low_energy_below")):
        policy = section(rules, "low_energy")
        tier_delta += int(need(policy, "tier_delta"))
        softness_delta += float(need(policy, "softness_delta"))
        forced.append(policy.get("ban_joke"))
    mood = _number(readings.get("mood"))
    if mood is not None and mood >= float(need(rules, "high_mood_at_least")):
        policy = section(rules, "high_mood")
        tier_delta += int(need(policy, "tier_delta"))
        softness_delta += float(need(policy, "softness_delta"))
        forced.append(policy.get("ban_joke"))
    offset = readings.get("offset")
    if isinstance(offset, Mapping):
        extra = _number(offset.get("softness"))
        if extra is not None:
            softness_delta += extra
    joke: Optional[bool] = None
    for item in forced:
        if item is True:
            joke = True
        elif item is False and joke is None:
            joke = False
    return tier_delta, softness_delta, joke


def _heart(heart: Optional[Mapping[str, Any]], rules: Mapping[str, Any]
           ) -> Tuple[Dict[str, Any], str, bool]:
    """心的四样读数、一句实话、以及「这次真的拿到读数了吗」。

    心还没做：heart 为 None 就是未接入，一个字都不许编成 0；给了 dict 但四项全 None
    是「接入但无读数」，也照实说。只有真正拿到至少一项读数才算接得上。
    """
    prefix = str(need(rules, "source_prefix"))
    if heart is None or not isinstance(heart, Mapping):
        return {}, prefix + str(need(rules, "not_connected")), False
    readings: Dict[str, Any] = {}
    for name in ("mood", "energy", "offset", "engage"):
        value = heart.get(name)
        if value is None:
            continue
        readings[name] = value
    if not readings:
        return {}, prefix + str(need(rules, "connected_empty")), False
    item = str(need(rules, "reading_item"))
    sep = str(need(rules, "reading_sep"))
    shown = sep.join(item.format(name=name, value=_show(value)) for name, value in readings.items())
    return readings, prefix + str(need(rules, "connected")).format(readings=shown), True


def _block(cards: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """取魂的一块原始卡；缺了、坏了都当空块（缺卡不许把出口打断）。"""
    part = cards.get(name) if isinstance(cards, Mapping) else None
    return part if isinstance(part, Mapping) else {}


def _self_names(identity: Mapping[str, Any]) -> Tuple[str, Tuple[str, ...]]:
    """魂的自称口径：display 是这一行里用的那个，整张名单拿去做授权核对。"""
    names = identity.get("self_names")
    if not isinstance(names, Mapping):
        return "", ()
    display = _text(names.get("display")).strip()
    raw = names.get("synonyms")
    items: List[str] = [display] if display else []
    if isinstance(raw, list):
        items.extend(_text(item).strip() for item in raw)
    allowed = tuple(item for item in items if item)
    return display, allowed


def _field(addressee: Any, name: str) -> Any:
    """addressee 的字段：dataclass（address.Addressee）与同样字段的 dict 都认。"""
    if isinstance(addressee, Mapping):
        return addressee.get(name)
    return getattr(addressee, name, None)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _show(value: Any) -> str:
    """读数怎么念：整数不带小数点，其余照 Python 的短写。"""
    if isinstance(value, float):
        return "%g" % value
    return str(value)


def _text(value: Any) -> str:
    """只管字符串；别的类型一律当空串，绝不把 None 带进出口。"""
    return value if isinstance(value, str) else ""
