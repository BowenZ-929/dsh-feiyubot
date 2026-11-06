"""「十分钟后叫我」这一类话：装配前认一次，认出来就落到手的一张卡上。

她回一句「记着了」本身是空的——真正的动作是**手替她记下一件事**（家·工作台的一张卡），
再由日子线按 pending_task 这条由头到点提醒她。这个模块只做那一段：**认一句话 → 抽一个时刻
→ 交出手去落卡**。它不是通用工具框架，只认「记事」这一件事。

三段，次序钉死：

1. **意图**（intent.words）：句子里出现「叫我／提醒我／说一声」任一个词，这一轮就算
   「要我记事」。有没有时间另说——有时间才落卡，没时间只反问（「回头提醒我」就走这条）。
   这条判据只看词表，不看标点、也不看这一轮是谁说的。
2. **抽时刻**（time）：
   · 相对——「十分钟后 / 半小时后 / 两个钟头后」：当前时刻 + N 分钟（单位换算全在数据里）；
   · 绝对——剩下的那半句交给**手的时间语** organs/hand/timelang.py 的 parse：
     「明天中午」「晚上八点」「8点半」都是它的表。**这里不写第二套时间解析**。
   两句都认不出 → 不猜：回一句 say.ask（「几点？」），**一张卡都不建**。猜错一个时刻，
   比多问一句贵得多。
3. **落卡**（handle）：organs/hand/actions.py 的 add_task（人给的号当 who，去掉时间词的那
   半句当 title，认出来的时刻当 due）。**成功才说 say.ok，失败说 say.fail**——没落成的卡
   不许在嘴上记成「记着了」。

**词表、噪声词、数字表、三句文案全在 data/remind.json**：代码里一个中文词都没有，改数据就改
行为；数据缺键当场 ValueError 并点名缺的是哪一格（不在代码里补默认值，补了人还以为改的是
数据）。读法同 assembly.json：实盘 <仓库>/home/organs/mind/remind.json 优先（存在才读），
否则读出厂 organs/mind/data/remind.json。

**中文数字先换成阿拉伯数字再往下走**（八点 → 8点）：同一刻，只是换成手的时间语认得的那副
写法。「半」不换——「点半」那半句要原样留给 timelang 判。卡名也取自这一份，所以卡上写的是
8 点不是 八点，读起来是同一件事。

回包那一格（packet，五键冻结）：{parsed, due, card_id, ok, why}——有没有认出时刻、落在哪一刻
（ISO 带偏移）、哪张卡、成没成、为什么没成（no_time 或手那边的码）。**普通闲聊这一格根本不
出现**（handle 回 None，调用方一个字都不改）。
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .. import paths
from ..hand import actions as hand_actions
from ..hand import store as hand_store
from ..hand import timelang

DATA_NAME = "remind.json"

#: 回包那一格的五个键（冻结）：认没认出时刻 / 落在哪一刻 / 哪张卡 / 成没成 / 为什么没成。
PACKET_KEYS = ("parsed", "due", "card_id", "ok", "why")
#: why 的一档：有意图、没时间——反问，不落卡。
WHY_NO_TIME = "no_time"
#: 抽时刻的两条路（回包不露这两档，账与测试要用）。
KIND_RELATIVE = "relative"      # 「十分钟后」：当前 + N 分钟
KIND_ABSOLUTE = "absolute"      # 「明天中午」「晚上八点」：交给手的时间语


# —— 数据 ——————————————————————————————————————


def load_config(root: Optional[Any] = None) -> Dict[str, Any]:
    """读词表与文案：显式 root 只读那一份；否则实盘覆盖（存在才读）→ 出厂数据。

    两份都没有是错，不是「用一套默认词表悄悄跑起来」——词表就是判据，没有默认值。
    """
    if root is not None:
        return _read(Path(root) / DATA_NAME)
    live = paths.organ_dir("mind") / DATA_NAME
    if live.is_file():
        return _read(live)
    factory = paths.factory_dir("mind") / DATA_NAME
    if factory.is_file():
        return _read(factory)
    raise ValueError("找不到记事的词表：%s 与 %s 都不存在" % (live, factory))


def _read(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("读不了记事的词表 %s：%s" % (path, exc)) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError("记事的词表不是 JSON：%s：%s" % (path, exc)) from exc
    return _check(data, str(path))


def _get(spec: Any, dotted: str, where: str) -> Any:
    """按点号取一格；缺了当场点名。数据里没有的规矩不许在代码里补。"""
    node = spec
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ValueError("remind.json 缺 %s：%s" % (dotted, where))
        node = node[part]
    return node


def _text(spec: Any, dotted: str, where: str) -> str:
    """一格非空的话（文案与格式串）。空串等于没写，照样当场报。"""
    value = _get(spec, dotted, where)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("remind.json 的 %s 要一句非空的话：%s" % (dotted, where))
    return value.strip()


def _words(spec: Any, dotted: str, where: str, *, allow_empty: bool = False) -> List[str]:
    """一格词表：列表，每一项都得是非空词；除非说了可以空，空表等于没配。"""
    value = _get(spec, dotted, where)
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError("remind.json 的 %s 要一份%s词表：%s"
                         % (dotted, "可以空的" if allow_empty else "非空的", where))
    words: List[str] = []
    for word in value:
        if not isinstance(word, str) or not word.strip():
            raise ValueError("remind.json 的 %s 里有空词：%s" % (dotted, where))
        words.append(word.strip())
    return words


def _table(spec: Any, dotted: str, where: str) -> Dict[str, float]:
    """一格对照表（数字 / 单位换算）：键非空、值是真数（bool 不算数）。"""
    value = _get(spec, dotted, where)
    if not isinstance(value, dict) or not value:
        raise ValueError("remind.json 的 %s 要一份非空的对照表：%s" % (dotted, where))
    table: Dict[str, float] = {}
    for key, number in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("remind.json 的 %s 里有空键：%s" % (dotted, where))
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError("remind.json 的 %s.%s 要一个数：%s" % (dotted, key, where))
        table[key.strip()] = number
    return table


def _check(data: Any, where: str) -> Dict[str, Any]:
    """校验并归一化：缺节、缺键、类型不对一律当场报（名字写全，人才知道改哪一格）。"""
    if not isinstance(data, dict):
        raise ValueError("记事的词表不是一份对象：%s" % where)
    version = _get(data, "version", where)
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ValueError("remind.json 的 version 要正整数：%s" % where)
    return {
        "version": version,
        "intent": {"words": _words(data, "intent.words", where)},
        "time": {
            "strip": _words(data, "time.strip", where),
            "trim": _text(data, "time.trim", where),
            "numbers": {"units": _table(data, "time.numbers.units", where),
                        "tens": _table(data, "time.numbers.tens", where),
                        "half": _table(data, "time.numbers.half", where)},
            "relative": {"units": _table(data, "time.relative.units", where),
                         "filler": _words(data, "time.relative.filler", where,
                                          allow_empty=True),
                         "suffix": _words(data, "time.relative.suffix", where)},
        },
        "say": {"ok": _text(data, "say.ok", where),
                "ask": _text(data, "say.ask", where),
                "fail": _text(data, "say.fail", where),
                "when_format": _text(data, "say.when_format", where)},
    }


# —— 认一句话（纯函数：只判，不落任何东西）————————————————


def plan(text: Any, *, now: Any = None,
         config: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """一句话 → 这一轮要不要记事。**None 就是普通闲聊**：一个字都不动，一张卡都不建。

    回来的是 {kind, due, title}：kind 是抽时刻走了哪条路（认不出时刻是空串），due 是带偏移的
    ISO（认不出是 None），title 是卡名（去掉时间词的那半句）。
    """
    cfg = config or load_config()
    said = str(text if text is not None else "").strip()
    if not said or not _intent(said, cfg):
        return None
    trimmed = _normalize(said, cfg)
    found = _relative(trimmed, cfg, now) or _absolute(trimmed, cfg, now)
    if found is None:
        # 有意图、没时间（「回头提醒我」「待会儿提醒我」）：卡名的底子就是原话本身。
        return {"kind": "", "due": None,
                "title": trimmed.strip(cfg["time"]["trim"]) or trimmed}
    due, phrase, kind = found
    return {"kind": kind, "due": due, "title": _title(trimmed, phrase, cfg)}


def _intent(text: str, cfg: Mapping[str, Any]) -> bool:
    """命中任一意图词就算「要我记事」。词表在数据里，这里一个词都没有。"""
    return any(word in text for word in cfg["intent"]["words"])


def _normalize(text: str, cfg: Mapping[str, Any]) -> str:
    """中文数字 → 阿拉伯数字（八点 → 8点）：同一刻，只是换成时间语认得的那副写法。

    **「半」不换**：「点半」那半句要原样留给 timelang 判。换不动的串原样留着（不猜）。
    """
    numbers = cfg["time"]["numbers"]
    chars = "".join(sorted(numbers["units"])) + "".join(sorted(numbers["tens"]))
    if not chars:
        return text
    pattern = re.compile("[%s]+" % re.escape(chars))

    def swap(match: Any) -> str:
        value = _number(match.group(0), cfg)
        if value is None:
            return match.group(0)
        return str(int(value)) if float(value).is_integer() else str(value)

    return pattern.sub(swap, text)


def _number(raw: str, cfg: Mapping[str, Any]) -> Optional[float]:
    """中文数字 → 数（十五 = 15、二十 = 20、二十五 = 25）；认不出 None。

    算法只有这一处：个位记在手上，碰上十／百就乘上去累加（「十八」= 1×10 + 8）。
    单位与十百本身全从数据来，将来要加「廿」这种字只改数据。
    """
    numbers = cfg["time"]["numbers"]
    if raw in numbers["half"]:
        return numbers["half"][raw]
    total, digit = 0.0, None
    for char in raw:
        if char in numbers["units"]:
            digit = numbers["units"][char]
        elif char in numbers["tens"]:
            total += (1.0 if digit is None else float(digit)) * numbers["tens"][char]
            digit = None
        else:
            return None
    return total + (0.0 if digit is None else float(digit))


def _count(raw: str, cfg: Mapping[str, Any]) -> Optional[float]:
    """「十分钟后」里的那个数：阿拉伯数字直接读，中文数字走 _number。"""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _number(raw, cfg)


def _relative(text: str, cfg: Mapping[str, Any],
              now: Any = None) -> Optional[Tuple[str, str, str]]:
    """「N 分钟后 / N 个钟头后」→ (带偏移的 due, 认掉的那一段原文, 走了哪条路)。

    单位换算（分钟 1 / 小时 60 …）全在数据里：加一种说法只改数据，不改这里。
    """
    match = _relative_pattern(cfg).search(text)
    if match is None:
        return None
    count = _count(match.group("count"), cfg)
    if count is None:
        return None
    minutes = count * cfg["time"]["relative"]["units"][match.group("unit")]
    base = hand_store.now_dt(now)
    return ((base + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
            match.group(0), KIND_RELATIVE)


def _relative_pattern(cfg: Mapping[str, Any]) -> Any:
    """相对时间的正则：数 + （个）+ 单位 + 后缀，三样全从数据拼。

    每次现拼：数据改了这一刻就生效——缓存一份旧的要失效，失效一次就会骗人一次。
    """
    numbers = cfg["time"]["numbers"]
    relative = cfg["time"]["relative"]
    count_chars = ("".join(sorted(numbers["units"])) + "".join(sorted(numbers["tens"]))
                   + "".join(sorted(numbers["half"])))
    units = "|".join(sorted(relative["units"], key=len, reverse=True))
    suffixes = "|".join(sorted(relative["suffix"], key=len, reverse=True))
    fillers = "".join("(?:%s)?" % re.escape(word) for word in relative["filler"])
    return re.compile(r"(?P<count>\d+(?:\.\d+)?|[%s]+)%s(?P<unit>%s)(?:%s)"
                      % (re.escape(count_chars), fillers, units, suffixes))


def _absolute(text: str, cfg: Mapping[str, Any],
              now: Any = None) -> Optional[Tuple[str, str, str]]:
    """剩下的那半句交给手的时间语（timelang.parse）：「明天中午」「晚上8点」都是它的表。

    先把意图词与噪声词（「提醒我」「帮我」「吧」…）削掉，留下的才是纯时刻；削不成、认不出
    就是认不出——**不猜**。
    """
    candidate = text
    for word in sorted(set(cfg["intent"]["words"]) | set(cfg["time"]["strip"]),
                       key=len, reverse=True):
        candidate = candidate.replace(word, "")
    candidate = candidate.strip(cfg["time"]["trim"])
    if not candidate:
        return None
    due = timelang.parse(candidate, now=now)
    if due is None:
        return None
    return due, candidate, KIND_ABSOLUTE


def _title(text: str, phrase: str, cfg: Mapping[str, Any]) -> str:
    """卡名 = **去掉时间词的那半句**（「十分钟后叫我」→「叫我」）。

    只剩时间词时退回原话：家有门禁，卡上不能没有名字（「明天中午」这张卡至少还认得出是什么）。
    """
    trim = cfg["time"]["trim"]
    rest = text.replace(phrase, " ", 1).strip(trim) if phrase else ""
    rest = " ".join(rest.split())
    return rest or text.strip(trim) or text


# —— 记事（会落卡：手的 add_task）————————————————————


def handle(text: Any, *, who: str = "", now: Any = None, root: Optional[Any] = None,
           config: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """认出来就落卡。**普通闲聊回 None**（调用方据此一个字节都不动）。

    root 是**手自己的实盘根**（家按兄弟目录换算，与手那边同一条口径）。who 是这一轮的号：
    卡上记的是「他要记的事」，不记成她自己的。
    """
    cfg = config or load_config()
    found = plan(text, now=now, config=cfg)
    if found is None:
        return None
    if found["due"] is None:
        # 有意图没时间：反问一句，**一张卡都不建**（不落卡就没有回执可给）
        return {"parsed": False, "due": None, "card_id": None, "ok": False,
                "why": WHY_NO_TIME, "say": cfg["say"]["ask"], "title": found["title"]}

    title = found["title"]
    result = hand_actions.add_task(who=who, title=title, due=found["due"], now=now,
                                   root=root)
    ok = str(result.get("state") or "") == "done"
    if ok:
        # 成功才说这句：卡真落下了，嘴上那句「记着了」才不是空话
        return {"parsed": True, "due": found["due"],
                "card_id": str(result.get("card_id") or "") or None, "ok": True, "why": "",
                "say": _render(cfg["say"]["ok"],
                               {"name": title, "when": _when(found["due"], cfg)}),
                "title": title}
    return {"parsed": True, "due": found["due"], "card_id": None, "ok": False,
            "why": str(result.get("code") or ""), "say": cfg["say"]["fail"], "title": title}


def packet(read: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """回包那一格：只留冻结的五个键（say 是嘴里那句，已经进了 reply，不在这儿重复）。"""
    if not read:
        return {}
    return {key: read.get(key) for key in PACKET_KEYS}


def _when(due: Any, cfg: Mapping[str, Any]) -> str:
    """给人看的那一刻（本地时区）。读不出来就原样给，不编。"""
    try:
        return hand_store.parse_ts(due).astimezone().strftime(cfg["say"]["when_format"])
    except (TypeError, ValueError):
        return str(due)


def _render(template: str, fields: Mapping[str, Any]) -> str:
    """占位符逐个替换（不认 str.format：文案里本来就带着别的花括号）。"""
    text = str(template)
    for key, value in fields.items():
        text = text.replace("{%s}" % key, str(value))
    return text
