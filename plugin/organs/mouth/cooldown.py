"""嘴的冷却：心说「这个话题还在冷却里」，嘴把这句话说成一句人话。

分工还是那一条：**心管肯不肯与还冷却多久，嘴管怎么说。** 心（organs/heart/refuse.py）只落记录、
查冷却、守额度；它的 check() 回到嘴这边的是 items 里那一格（cooling / remaining_min / reason）。
那几样数字到了这里只变成两样东西：

- **说出口的那一句**：一律用 data/refusal.json 里 refused-topic 那三态说法之中的一句（用哪一种取
  data/cooldown.json 的 shape 键）。冷却**不是第四种拒绝**，它就是「这个话题我不接」那条老话——
  这里一个字的台词都不写、也不抄，抄一份就有两处真源。所以 say() 只决定「用哪一句」，
  句子本体仍由 refusal.say 从那份数据里取。
- **面板上那一句**：line() 把话题、理由、还剩多久拼成一句人话（形如「她在拒绝「医院」，理由
  refused-topic，还剩 4.0 小时」）。模板、两个单位（小时 / 分钟）、以及「多少分钟以上才按小时说」
  全在 data/cooldown.json 的 line 节里：改口径是改数据，不是改代码。

**纯函数：不读盘、不取时钟。** 本文件不 import time / datetime / random，也不调 warehouse.load——
冷却数据由调用方读好交进来（table 参数），所以同一份输入两次调用拿到同一句话，能离线预览。
唯一要说明的一处：say() 的那句话经由 refusal.say 从 data/refusal.json 取，那是拒绝说法的唯一主人，
本文件只指认「用哪一句」；除了这一处引借，本文件一次 IO 都没有。

缺键不补默认值：表里少一个键、单位模板填不出来、或者明明没在冷却却要一句拒绝，一律抛
CooldownBroken 并点名叫人去看数据——补一句默认台词，代码里就又多了一处真源。
"""
from __future__ import annotations

from typing import Any, Mapping

from . import refusal as refusal_words

__all__ = ["CooldownBroken", "line", "note", "say"]

SHAPE_KEY = "shape"                     # 冷却一律走 refusal.json 里的哪一句（三态说法之一）
LINE_KEY = "line"                       # 面板那一句：模板 + 两个单位 + 换算分界
NOTES_KEY = "notes"                     # 三种情形那几句注记（进回包，不进她的台词）
LINE_FIELDS = ("template", "minutes", "hours", "hours_from_min")   # line 节必须有的四样
UNITS = ("minutes", "hours")            # 两个单位模板：不到分界用前者，到了用后者


class CooldownBroken(ValueError):
    """冷却这边不合格：没给数据表、表里缺键、单位填不出来、没在冷却却要一句拒绝。

    与 refusal 那边同一条规矩：**失败必须响**，且报错信息里带着缺的那一节的名字，好让人一眼找到
    data/cooldown.json 里哪一处写错了。不猜一个形状继续走、也不补一句默认台词顶上。
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__("冷却这边 %s（数据在 organs/mouth/data/cooldown.json，"
                         "由 warehouse.load(\"cooldown\") 读出来交给本文件）" % detail)


def say(topic: Any, cooling: Any, table: Mapping[str, Any]) -> str:
    """冷却里那一句拒绝：形状取自 table 的 shape，台词取自 data/refusal.json（一个字都不在这里）。

    cooling 是心那份 check() 的 items 里那一格（或在冷却这个事实本身）：不在冷却就没有「要拒的
    那一句」，照着说就是凭空拒绝——这时抛 CooldownBroken，不编一句顶上去。

    topic 只用来在报错信息里点人：说哪一句与话题无关（冷却话题一律同一句），所以它不参与取句。
    """
    book = _book(table)
    if not _is_cooling(cooling):
        raise CooldownBroken("没在冷却里（topic=%r）：这时没有要拒的那一句" % (topic,))
    return refusal_words.say(str(_word(book, SHAPE_KEY)))


def line(topic: Any, remaining_min: Any, reason: Any, table: Mapping[str, Any]) -> str:
    """面板那一句人话：话题、理由、还剩多久，按数据里的模板与单位拼出来。

    remaining_min 是心给的分钟数（可以带小数，负数当 0）：到了 hours_from_min 分钟就说「还剩 N 小时」，
    不到就说「还剩 N 分钟」——分界与两个单位的写法都在数据里，本文件不算成词、也不写死小数位。
    话题或理由空着抛 CooldownBroken：一句「她在拒绝「」，理由 ，还剩 4.0 小时」比不显示更糟。
    """
    cfg = _line_cfg(_book(table))
    minutes = _minutes(remaining_min)
    limit = _number(cfg["hours_from_min"])
    if minutes >= limit:
        left = _fill(str(cfg[UNITS[1]]), "line." + UNITS[1], value=minutes / 60.0)
    else:
        left = _fill(str(cfg[UNITS[0]]), "line." + UNITS[0], value=minutes)
    return _fill(str(cfg["template"]), "line.template", topic=_text(topic, "line.template"),
                 reason=_text(reason, "line.template"), left=left)


def note(table: Mapping[str, Any], key: Any) -> str:
    """三种情形那几句注记（cooling / clear / no_topic）：原样取数据，代码里一句都不留。

    注记进回包的 refusal.note——读的是「这一轮冷却这一关判成了什么、为什么」，不是她要说的话，
    所以它不进台词那条路。认不出的键当场抛并指名（猜一个键，账上就会多一句不知从哪儿来的话）。
    """
    book = _book(table)
    notes = book.get(NOTES_KEY)
    if not isinstance(notes, Mapping):
        raise CooldownBroken("的数据里没有一节可用的 %r" % (NOTES_KEY,))
    want = key.strip() if isinstance(key, str) else ""
    for name, value in notes.items():
        if name == want:
            if not isinstance(value, str) or not value.strip():
                raise CooldownBroken("的 %s.%r 不是一句非空字符串（缺键不补默认值）"
                                     % (NOTES_KEY, name))
            return value
    raise CooldownBroken("的 %s 里没有 %r 那一条（只认 %s）"
                         % (NOTES_KEY, key, "、".join(repr(one) for one in notes)))


def _book(table: Any) -> Mapping[str, Any]:
    """要一份已经读好的冷却数据表：本文件不读盘，所以这里只认交进来的那份。"""
    if not isinstance(table, Mapping) or not table:
        raise CooldownBroken("没拿到一份可用的数据表（table=%r）：本文件不读盘、也不替你现读一份"
                             % (table,))
    return table


def _line_cfg(book: Mapping[str, Any]) -> Mapping[str, Any]:
    """取 line 那一节：四样都在，缺一个就抛并指名（缺键不补默认值）。"""
    part = book.get(LINE_KEY)
    if not isinstance(part, Mapping):
        raise CooldownBroken("的数据里没有一节可用的 %r" % (LINE_KEY,))
    for name in LINE_FIELDS:
        if name == "hours_from_min":
            _number(part.get(name), "line." + name)
            continue
        value = part.get(name)
        if not isinstance(value, str) or not value.strip():
            raise CooldownBroken("的 %s 节缺 %r，或它不是一句非空字符串（缺键不补默认值）"
                                 % (LINE_KEY, name))
    return part


def _word(book: Mapping[str, Any], name: str) -> str:
    """取一个非空字符串键（说不清是哪一句时当场抛，别把空串送去 refusal）。"""
    value = book.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CooldownBroken("缺 %r，或它不是一句非空字符串（缺键不补默认值）" % (name,))
    return value


def _is_cooling(cooling: Any) -> bool:
    """心给的那一格算不算「在冷却里」：键值表认 cooling 那一栏，布尔直接认，其余一律不算。"""
    if isinstance(cooling, Mapping):
        return bool(cooling.get("cooling"))
    return cooling is True


def _minutes(value: Any) -> float:
    """剩下几分钟：数字（不是 bool）才收，负数当 0；其余当场抛——编一个 0 会读出「已经到期」。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CooldownBroken("的 remaining_min 不是个数（读到 %r）：没有这个数就拼不出「还剩多久」"
                             % (value,))
    return max(0.0, float(value))


def _number(value: Any, where: str = "line.hours_from_min") -> float:
    """取一个数（bool / None / 坏串一律抛）：分界与字数都从数据来，读不动就是数据坏了。"""
    if isinstance(value, bool):
        raise CooldownBroken("的 %s 是个布尔（读到 %r），不是数" % (where, value))
    try:
        return float(value)
    except (TypeError, ValueError):
        raise CooldownBroken("的 %s 不是个数（读到 %r）" % (where, value)) from None


def _text(value: Any, where: str) -> str:
    """话题 / 理由落成一句能填进模板的字；空着当场抛（空话题拼出来的那句话不能给人看）。"""
    body = value.strip() if isinstance(value, str) else ""
    if not body:
        raise CooldownBroken("的 %s 缺话题或理由（读到 %r）" % (where, value))
    return body


def _fill(template: str, where: str, **fields: Any) -> str:
    """把模板填成人话；数据里没填的占位符就抛，别把 {left} 这种半成品发出去。"""
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise CooldownBroken("的 %s 这句填不出来：%s" % (where, exc)) from None
