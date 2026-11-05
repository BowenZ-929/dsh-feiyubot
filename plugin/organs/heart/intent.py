"""心的 intent：pending 里同时有好几个念头时，按意图权重排序，再决定值不值得开口。

这一层只回答两件事：**此刻最值得开口的是哪一件事**，以及**值不值得开口**。
它不回答"必须开口"——过了阈值只是拿到开口的资格，闭不闭嘴仍由 engage 的门定。
所以 engage 只在 pass=False 时加一道"少说"，pass=True 时一个字都不加。

五个意图全部**现算**，不新增念头类型、不落任何意图状态（照 heart.md：
窗口是现算的，窗口滑走自然恢复，没有需要回滚的东西）：
- unfinished-chat（没聊完）：最近 CHAT_WINDOW_MIN 分钟内有 replied，且窗内
  没有**收尾信号**。收尾信号 = 比最后一条 replied 新的 normal，且它之后没有
  再出现 ignored / harsh（话说完了，也没留下没人接的话）。
- unresolved（没放下的事）：pending.open_thoughts 里存在 attempts >= 1 的念头。
- unsaid（没出口的话）：最近 UNSAID_WINDOW_MIN 分钟内**尾部连续 ignored** 达到
  UNSAID_IGNORED_TAIL 条，或存在 open 的 wanted_praise 念头。
- bored（无聊）：精力**档**在"一般"及以上（档由 energy 给，本层不重算；
  调用方只注入裸值时按 energy 自己的档界 30/50/75 现算一次）且今日互动条数
  <= BORED_TODAY_MAX。
- lonely（孤单）：最近 LONELY_WINDOW_MIN 分钟（6 小时）零事件，且熟稔 < 1.0。

**清醒度修正（clarity）**：精力 value >= 75 → fresh（系数 1.0）；30–75 → normal
（0.85）；< 30 → tired（0.7）；若此刻落在夜间窗口（model.NIGHT_START_H 到
NIGHT_END_H，跨午夜同样算）再乘 night（0.8）。每条意图的
`weight = 表里的权重 × clarity 系数`，`score = top.weight`，
`pass = score >= threshold`；hit=False 的意图不参与 top。

**关于这张权重表（engage_weights.json）——必须说清的两句话**：
1. 这些数是我们按自己的场景标定的**默认值**，不是从任何参考实现抄来的数字；
   "没聊完 > 没放下的事 > 没出口的话 > 无聊 > 孤单"这个次序是我们自己同意的
   轻重，数字只是把这个次序写成能算的东西。
2. 它们是**默认值，不是验收线**：改 engage_weights.json 就是改她的脾气，
   改完不用动一行代码；本文件的 DEFAULT_* 只是"没有文件时的出厂值"。
   出厂就带一份在 store.DATA_DIR/engage_weights.json（data/.gitignore 里放行了它），
   人照着改就行，每次现读、改完即生效；root 也能指到实盘那份。
   文件不存在、坏掉、缺键、越界一律不抛错（参数表读入永不抛错，这条照 heart.md 的
   M8：缺字段补默认、越界夹回、解析失败退回内置默认并 WARN 一行）——文件在就以
   文件为准，文件不在才用 DEFAULT_*。

本模块不持有任何半衰、不挂定时器/线程：时间一律现算，公开函数都收 now；
写盘只发生在被调用的子模块自己的节奏里（energy.read 结算、pending 标过期），
intent 自己不写任何文件。
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from organs.heart import energy, events, model, pending, ports, store

WEIGHTS_FILE = "engage_weights.json"
VERSION = 1

# 五个意图的固定次序：同权重时按这个次序排；默认权重也照这个次序递减。
INTENT_UNFINISHED_CHAT = "unfinished-chat"
INTENT_UNRESOLVED = "unresolved"
INTENT_UNSAID = "unsaid"
INTENT_BORED = "bored"
INTENT_LONELY = "lonely"
INTENT_ORDER = (
    INTENT_UNFINISHED_CHAT,
    INTENT_UNRESOLVED,
    INTENT_UNSAID,
    INTENT_BORED,
    INTENT_LONELY,
)

# 出厂默认（我们自己的标定，见文件头）：没聊完 > 没放下 > 没出口 > 无聊 > 孤单。
DEFAULT_THRESHOLD = 0.55
DEFAULT_WEIGHTS = {
    INTENT_UNFINISHED_CHAT: 0.9,
    INTENT_UNRESOLVED: 0.75,
    INTENT_UNSAID: 0.6,
    INTENT_BORED: 0.35,
    INTENT_LONELY: 0.3,
}
DEFAULT_CLARITY = {"tired": 0.7, "normal": 0.85, "fresh": 1.0, "night": 0.8}

# 判据常数（本层自己的尺；不借 mood 的 3h/7d，也不借 offset 的半衰）。
CHAT_WINDOW_MIN = 30.0
# 与 engage 的"近期事件窗口"同一个口径（120 分钟）。
UNSAID_WINDOW_MIN = 120.0
UNSAID_IGNORED_TAIL = 2
LONELY_WINDOW_MIN = 360.0
# 熟稔那把尺在脑：这里只借 engage 已经用过的"还不熟"那条线，不另造一个数。
LONELY_FAMILIARITY_MAX = float(model.ENGAGE_FAMILIARITY_LOW)
# energy 自己的档名（累 / 偏累 / 一般 / 充沛），"一般及以上"就这后两档。
# 档是 energy 的判据（它会带迟滞），本层只认档名、不重算档；只有在调用方
# 只注入裸值时才按 energy 的档界 30 / 50 / 75 现算一次。
BORED_TIERS = ("一般", "充沛")
BORED_ENERGY_MIN = 50.0
BORED_TODAY_MAX = 3
# 清醒度的三档分界（与 energy 的档界同值，但这里问的是"她现在够不够清醒"）。
FRESH_MIN = 75.0
NORMAL_MIN = 30.0

# 权重表的取值范围：权重与阈值 0–1；清醒度是乘数，留一点上浮余地。
_WEIGHT_RANGE = (0.0, 1.0)
_CLARITY_RANGE = (0.1, 2.0)


# ---- 权重表读入 ----------------------------------------------------------


def _weights_path(root: Any = None) -> Path:
    """权重表路径：root 给了就用 root，没给就是出厂参数目录 store.DATA_DIR。"""
    base = Path(root) if root is not None else store.DATA_DIR
    return base / WEIGHTS_FILE


def _as_float(value: Any, default: float) -> Optional[float]:
    """转 float；bool / None / 坏串一律当读不懂（返回 None）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamped(value: Any, default: float, low: float, high: float,
             label: str, notes: List[str]) -> float:
    """缺键补默认、坏值回默认、越界夹回；每一处都记一条 note（合成一行 WARN）。"""
    num = _as_float(value, default)
    if num is None:
        if value is not None:
            notes.append("%s 读不懂（%r），用默认 %.2f" % (label, value, default))
        else:
            notes.append("缺 %s，用默认 %.2f" % (label, default))
        return float(default)
    if num < low or num > high:
        notes.append("%s=%.2f 越界，夹回 [%.1f, %.1f]" % (label, num, low, high))
        return min(high, max(low, num))
    return num


def _warn(notes: List[str], target: Path) -> None:
    """参数表读入只 WARN 一行，且永不抛错（heart.md M8）。"""
    if not notes:
        return
    shown = "；".join(notes[:4])
    if len(notes) > 4:
        shown += "（等 %d 处）" % len(notes)
    warnings.warn(
        "心·intent：权重表 %s %s" % (target.name, shown),
        RuntimeWarning, stacklevel=3)


def _factory_defaults() -> Dict[str, Any]:
    """内置默认（没文件时的出厂值）：深拷一份，调用方改了也不污染出厂值。"""
    return {
        "version": VERSION,
        "threshold": float(DEFAULT_THRESHOLD),
        "weights": dict(DEFAULT_WEIGHTS),
        "clarity": dict(DEFAULT_CLARITY),
    }


def load_weights(root: Any = None) -> Dict[str, Any]:
    """读权重表：{`version`, `threshold`, `weights`, `clarity`}。

    读不到 / 坏 JSON / 顶层不是对象 → 整张表用内置默认；缺键补默认、越界夹回；
    三种情况都只 WARN 一行，**永不抛错**（M8：参数表读入不许把调用方炸掉）。
    返回的 dict 一定是完整形状，五个意图与四档清醒度一个不缺。
    """
    target = _weights_path(root)
    notes: List[str] = []
    raw: Optional[Dict[str, Any]] = None
    try:
        parsed = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        notes.append("读不到（%s：%s），整张表用内置默认"
                     % (type(exc).__name__, exc))
    else:
        if isinstance(parsed, dict):
            raw = parsed
        else:
            notes.append("顶层不是 JSON 对象（收到 %s），整张表用内置默认"
                         % type(parsed).__name__)
    if raw is None:
        table = _factory_defaults()
        _warn(notes, target)
        return table

    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        version = VERSION
    table: Dict[str, Any] = {
        "version": int(version),
        "threshold": _clamped(raw.get("threshold"), DEFAULT_THRESHOLD,
                              _WEIGHT_RANGE[0], _WEIGHT_RANGE[1],
                              "threshold", notes),
        "weights": {},
        "clarity": {},
    }
    weights_raw = raw.get("weights")
    if not isinstance(weights_raw, dict):
        weights_raw = {}
        notes.append("缺 weights 一节，五个权重全用默认")
    for name in INTENT_ORDER:
        table["weights"][name] = _clamped(
            weights_raw.get(name), DEFAULT_WEIGHTS[name],
            _WEIGHT_RANGE[0], _WEIGHT_RANGE[1], "weights.%s" % name, notes)
    clarity_raw = raw.get("clarity")
    if not isinstance(clarity_raw, dict):
        clarity_raw = {}
        notes.append("缺 clarity 一节，四档系数全用默认")
    for name in DEFAULT_CLARITY:
        table["clarity"][name] = _clamped(
            clarity_raw.get(name), DEFAULT_CLARITY[name],
            _CLARITY_RANGE[0], _CLARITY_RANGE[1], "clarity.%s" % name, notes)
    _warn(notes, target)
    return table


# ---- 读数 ----------------------------------------------------------------


def _at(now: Optional[str]) -> str:
    """「此刻」：调用方给了就用它，没给就读时现算。"""
    return now if isinstance(now, str) and now else model.now_iso()


def _num(value: Any, default: float) -> float:
    """上下文里读来的数；坏值回默认。"""
    num = _as_float(value, default)
    return float(default) if num is None else num


def _in_night(hour: int) -> bool:
    """hour 是否落在夜间窗口里；窗口按 model 的起点/终点算，跨午夜也对。"""
    start = model.NIGHT_START_H
    end = model.NIGHT_END_H
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


# 脑给的读数缺失时只 WARN 一次：心没有脑也能排，只是少了今日条数与熟稔两个数。
_MISSING_READOUT: Optional["ports.BrainReadout"] = None


def _default_readout() -> "ports.BrainReadout":
    """调用方没注入 context 时的降级读数：两个数都是 0，只在第一次构造时 WARN。"""
    global _MISSING_READOUT
    if _MISSING_READOUT is None:
        _MISSING_READOUT = ports.from_brain(None)
    return _MISSING_READOUT


def _tier_by_value(value: float) -> str:
    """只注入裸值时的档名：按 energy 自己的档界（30 / 50 / 75）现算一次。"""
    if value >= FRESH_MIN:
        return "充沛"
    if value >= BORED_ENERGY_MIN:
        return "一般"
    if value >= NORMAL_MIN:
        return "偏累"
    return "累"


def _energy_reading(root: Any, at: str,
                    ctx: Dict[str, Any]) -> Tuple[float, str]:
    """精力读数：value 与档名。

    context 注入了 energy_value / energy_tier 就用它（调用方已经算过）；
    否则读 energy 现算——档名是 energy 的判据（它带迟滞），本层只搬运、不重算。
    读坏了按醒来默认值兜底并 WARN 一行。
    """
    ctx_tier = ctx.get("energy_tier")
    if ctx.get("energy_value") is not None:
        value = _num(ctx.get("energy_value"), float(model.ENERGY_WAKE_DEFAULT))
    else:
        try:
            snap = energy.read(root=root, now=at)
        except Exception as exc:  # 读精力失败不该让排序停摆
            warnings.warn(
                "心·intent：读 energy 失败（%s：%s），精力按 %d 算"
                % (type(exc).__name__, exc, model.ENERGY_WAKE_DEFAULT),
                RuntimeWarning, stacklevel=3)
            value = float(model.ENERGY_WAKE_DEFAULT)
        else:
            value = _num(snap.get("value"), float(model.ENERGY_WAKE_DEFAULT))
            if isinstance(ctx_tier, str) and ctx_tier:
                return value, ctx_tier
            tier = snap.get("tier")
            if isinstance(tier, str) and tier:
                return value, tier
            return value, _tier_by_value(value)
    if isinstance(ctx_tier, str) and ctx_tier:
        return value, ctx_tier
    return value, _tier_by_value(value)


def _readings(root: Any, at: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """这一轮排序手里的四个数：精力、小时、今日互动条数、熟稔。

    context 允许注入 {"today_count", "familiarity", "energy_value", "hour"}
    （外加可选的 "who"）；没注入的那几项才去 energy / ports 现读。
    """
    who = ctx.get("who") if isinstance(ctx.get("who"), str) and ctx.get("who") else "主人"
    today = ctx.get("today_count")
    familiar = ctx.get("familiarity")
    readout: Optional["ports.BrainReadout"] = None
    if today is None or familiar is None:
        readout = _default_readout()
    if today is None:
        today = readout.today_count(who, at) if readout is not None else 0
    if familiar is None:
        familiar = readout.familiarity(who, at) if readout is not None else 0.0
    hour = ctx.get("hour")
    if hour is None:
        hour = model.parse_ts(at).hour
    value, tier = _energy_reading(root, at, ctx)
    return {
        "energy_value": value,
        "energy_tier": tier,
        "hour": int(_num(hour, 0.0)) % 24,
        "today_count": int(_num(today, 0.0)),
        "familiarity": _num(familiar, 0.0),
    }


# ---- 五个意图的判据（全部现算，不落状态） ---------------------------------


def _minutes(row: Dict[str, Any], at: str) -> float:
    """一条事件距 at 几分钟；坏时间戳给一个很大的数（当作老早的事）。"""
    try:
        return model.hours_between(row.get("at"), at) * 60.0
    except ValueError:
        return 1e9


def _later(row: Dict[str, Any], other: Dict[str, Any]) -> bool:
    """row 是否比 other 新（两边都已被 events.recent 验过时间戳）。"""
    return model.hours_between(other.get("at"), row.get("at")) > 0.0


def _unfinished_chat(root: Any, at: str) -> Tuple[bool, str]:
    """没聊完：窗内有 replied，且没有收尾信号。"""
    rows = events.recent(root=root, now=at, window_min=CHAT_WINDOW_MIN)
    if not rows:
        return False, "最近 %.0f 分钟一条事件都没有" % CHAT_WINDOW_MIN
    replied = [row for row in rows if row.get("kind") == events.KIND_REPLIED]
    if not replied:
        return False, "最近 %.0f 分钟没有 replied（%d 条别的互动）" % (
            CHAT_WINDOW_MIN, len(rows))
    last = replied[-1]
    closing: Optional[Dict[str, Any]] = None
    for row in rows:
        if row.get("kind") != events.KIND_NORMAL:
            continue
        if _later(row, last):
            closing = row
    if closing is not None:
        hanging = [row for row in rows
                   if row.get("kind") in (events.KIND_IGNORED, events.KIND_HARSH)
                   and _later(row, closing)]
        if not hanging:
            return False, "最后一条 replied 之后有 normal 收尾（%.0f 分钟前），话已经说完" % (
                _minutes(closing, at))
    return True, "窗内最后一条 replied 在 %.0f 分钟前，之后没有 normal 收尾" % (
        _minutes(last, at))


def _unresolved(root: Any, at: str,
                thoughts: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """没放下的事：open 念头里有 attempts >= 1 的（试过但没落地的才算）。"""
    tried = [row for row in thoughts if int(_num(row.get("attempts"), 0.0)) >= 1]
    if tried:
        top = max(int(_num(row.get("attempts"), 0.0)) for row in tried)
        return True, "open 念头 %d 条里有 %d 条已试过（最多 attempts %d，如 %s）" % (
            len(thoughts), len(tried), top, tried[0].get("kind"))
    return False, "open 念头 %d 条，attempts 都还是 0" % len(thoughts)


def _unsaid(root: Any, at: str,
            thoughts: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """没出口的话：尾部连续 ignored 到线，或有 open 的 wanted_praise 念头。"""
    rows = events.recent(root=root, now=at, window_min=UNSAID_WINDOW_MIN)
    tail = 0
    for row in reversed(rows):
        if row.get("kind") != events.KIND_IGNORED:
            break
        tail += 1
    praise = [row for row in thoughts if row.get("kind") == pending.KIND_PRAISE]
    reasons: List[str] = []
    if tail >= UNSAID_IGNORED_TAIL:
        reasons.append("尾部连续 %d 条 ignored（线 %d）" % (tail, UNSAID_IGNORED_TAIL))
    if praise:
        reasons.append("有 open 的 wanted_praise 念头 %d 条" % len(praise))
    if reasons:
        return True, "；".join(reasons)
    return False, "尾部连续 ignored 只有 %d 条（要 ≥ %d），也没有 wanted_praise 念头" % (
        tail, UNSAID_IGNORED_TAIL)


def _bored(energy_value: float, energy_tier: str,
           today_count: int) -> Tuple[bool, str]:
    """无聊：精力档在"一般"及以上，且今日互动条数低。"""
    if energy_tier in BORED_TIERS and today_count <= BORED_TODAY_MAX:
        return True, "精力 %.0f「%s」档在「一般」及以上，今日互动只有 %d 条（≤ %d）" % (
            energy_value, energy_tier, today_count, BORED_TODAY_MAX)
    return False, "精力 %.0f「%s」档、今日互动 %d 条——不构成无聊（要「一般」及以上且 ≤ %d 条）" % (
        energy_value, energy_tier, today_count, BORED_TODAY_MAX)


def _lonely(root: Any, at: str, familiarity: float) -> Tuple[bool, str]:
    """孤单：最近 6 小时零事件，且熟稔读数低。"""
    rows = events.recent(root=root, now=at, window_min=LONELY_WINDOW_MIN)
    if rows:
        return False, "最近 %.0f 小时有 %d 条事件，不算零事件" % (
            LONELY_WINDOW_MIN / 60.0, len(rows))
    if familiarity < LONELY_FAMILIARITY_MAX:
        return True, "最近 %.0f 小时零事件，熟稔 %.2f < %.2f" % (
            LONELY_WINDOW_MIN / 60.0, familiarity, LONELY_FAMILIARITY_MAX)
    return False, "最近 %.0f 小时零事件，但熟稔 %.2f ≥ %.2f" % (
        LONELY_WINDOW_MIN / 60.0, familiarity, LONELY_FAMILIARITY_MAX)


def _clarity(energy_value: float, hour: int,
             table: Dict[str, Any]) -> Dict[str, Any]:
    """清醒度修正：精力分三档，夜里再乘 night 那一档；why 里写清每一步。"""
    if energy_value >= FRESH_MIN:
        band, word = "fresh", "清醒"
    elif energy_value >= NORMAL_MIN:
        band, word = "normal", "一般"
    else:
        band, word = "tired", "累"
    factor = float(table["clarity"][band])
    why = "精力 %.0f →「%s」档（系数 %.2f）" % (energy_value, word, factor)
    if _in_night(hour):
        night = float(table["clarity"]["night"])
        factor *= night
        why += "；%d 点落在夜间窗口，再乘 %.2f → %.3f" % (hour, night, factor)
    else:
        why += "；%d 点不在夜间窗口" % hour
    return {"factor": round(factor, 4), "why": why}


# ---- 排序 ----------------------------------------------------------------


def rank(root: Any = None, now: Optional[str] = None,
         context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """算五个意图、加权排序，回答"值不值得开口"。

    返回：:
        {"order": [{"intent", "weight", "hit", "why"}...],   # 按 weight 降序
         "top":   {...}|None,                               # 命中里最重的那个
         "score": float,                                    # = top.weight，没有 top 则 0
         "threshold": float,
         "clarity": {"factor", "why"},
         "pass": bool}                                      # score >= threshold

    weight = 表里的权重 × 清醒度系数；hit=False 的意图在 order 里照样列出（带
    why 说明为什么没成立），但不参与 top。context 可注入
    {"today_count", "familiarity", "energy_value", "hour", "who"}；没注入的
    才去 energy / ports 现读（脑缺失时降级成 0，只 WARN 一行）。
    """
    at = _at(now)
    ctx: Dict[str, Any] = dict(context) if isinstance(context, dict) else {}
    table = load_weights(root)
    readings = _readings(root, at, ctx)
    clarity = _clarity(readings["energy_value"], readings["hour"], table)
    thoughts = pending.open_thoughts(root, at)

    hits: Dict[str, Tuple[bool, str]] = {}
    hits[INTENT_UNFINISHED_CHAT] = _unfinished_chat(root, at)
    hits[INTENT_UNRESOLVED] = _unresolved(root, at, thoughts)
    hits[INTENT_UNSAID] = _unsaid(root, at, thoughts)
    hits[INTENT_BORED] = _bored(readings["energy_value"], readings["energy_tier"],
                              readings["today_count"])
    hits[INTENT_LONELY] = _lonely(root, at, readings["familiarity"])

    order: List[Dict[str, Any]] = []
    for name in INTENT_ORDER:
        hit, why = hits[name]
        order.append({
            "intent": name,
            "weight": round(float(table["weights"][name]) * clarity["factor"], 4),
            "hit": bool(hit),
            "why": why,
        })
    # 权重降序；同权重按 INTENT_ORDER（先天轻重）取先者。
    order.sort(key=lambda row: (-row["weight"], INTENT_ORDER.index(row["intent"])))
    top: Optional[Dict[str, Any]] = None
    for row in order:
        if row["hit"]:
            top = row
            break
    score = round(float(top["weight"]), 4) if top is not None else 0.0
    threshold = float(table["threshold"])
    return {
        "order": order,
        "top": top,
        "score": score,
        "threshold": threshold,
        "clarity": clarity,
        "pass": score >= threshold,
    }
