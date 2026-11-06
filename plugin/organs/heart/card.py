"""heart-baseline：六轴性格卡（从魂整卡迁入，只读不写）。

判据（数值是默认值不是验收线）：
- 真源只有一个：**实盘是唯一真源；出厂那份是 seed 料，改了不影响实盘。**
  实盘 = store.default_root()/baseline-card.json（HEART_ROOT > <仓库>/home/organs/heart），
  出厂 = organs/heart/data/baseline-card.json（SEED_CARD_PATH，只给新机器播种与测试用）。
- 卡是常量：本模块既不持有衰减尺，也不往盘上写一个字节——坏卡一律
  CardError，绝不补默认值（补默认值等于给基线多加一个写者）。
- 数值只留内部：进文本的只有档位词，render_text() 的输出里不许出现
  任何阿拉伯数字 0-9。
- 六轴完整（2026-11-06 人定稿）：六轴都有 inner / shown，都有五档词与低档禁令；
  不再有 pending 轴这一状态，view()["pending"] 因此恒为空表（键保留，面板与测试都读它）。
- 尺子归属：mood 的两把尺（3h / 7d）与 offset 的尺（6h / 4h）各在各的模块，
  本模块一把都不用、一把都不借。

卡文件由人给、程序只读；卡的 _handoff 段随整卡迁入，原样保留。
"""
from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from organs.heart import model, store

CARD_FILE = "baseline-card.json"
#: 出厂 seed 料（包内 data/）：只给新机器播种与测试用，改了不影响实盘。
SEED_CARD_PATH = store.DATA_DIR / CARD_FILE

# 拒载码：每一种坏法一个码，绝不合并成"卡坏了"。
CARD_MISSING = "card-missing"
CARD_CORRUPT = "card-corrupt"
SCALE_INVALID = "scale-invalid"
AXES_INVALID = "axes-invalid"
AXIS_DUPLICATE = "axis-duplicate"
AXIS_UNKNOWN = "axis-unknown"
AXIS_MISSING = "axis-missing"
AXIS_UNPAIRED = "axis-unpaired"
AXIS_NOT_INT = "axis-not-int"
AXIS_OUT_OF_RANGE = "axis-out-of-range"
#: 六轴完整之后，任何一根轴带 pending: true 都是坏卡——不再有 pending 轴这一状态。
AXIS_PAIRED_BUT_PENDING = "axis-paired-but-pending"


def _issue(code: str, axis: str, message: str) -> Dict[str, str]:
    """一条拒载理由：码 / 轴 / 人话。"""
    return {"code": code, "axis": axis, "message": message}


class CardError(Exception):
    """坏卡：一律抛出，绝不补默认值。

    .code 是第一条理由的码（表里第一个坏法），.issues 是全部理由，
    面板/日志按 issues 逐条报，别只报第一条。
    """

    def __init__(self, code: Any, issues: Any = None, message: str = ""):
        self.code = str(code) if code is not None else ""
        rows: List[Dict[str, str]] = []
        if isinstance(issues, list):
            rows = [row for row in issues if isinstance(row, dict)]
        elif isinstance(issues, dict):
            rows = [issues]
        if not rows:
            rows = [_issue(self.code, "", message or "坏卡")]
        self.issues = rows
        text = message or "；".join(
            "%s%s: %s" % (row.get("code", ""),
                          ("/" + row["axis"]) if row.get("axis") else "",
                          row.get("message", ""))
            for row in rows
        )
        super().__init__(text)


@dataclass(frozen=True)
class Axis:
    """一根轴：六轴都成对，inner / shown 都在；pending 恒为 False（历史机制，留给坏卡拒载）。"""

    id: str
    inner: Optional[int] = None
    shown: Optional[int] = None
    pending: bool = False


@dataclass(frozen=True)
class Card:
    """整卡：六轴的字典 + 成对名单 + 刻度（pending 名册保留，六轴完整后恒为空）。"""

    axes: Dict[str, Axis]
    paired: Tuple[str, ...]
    pending: Tuple[str, ...]
    scale_min: int
    scale_max: int


def _is_int(value: Any) -> bool:
    """真 int；bool 是 int 的子类，不许混进来。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _check_scale(data: Dict[str, Any]) -> Tuple[int, int]:
    """刻度两件事：在、且是整数、且 min < max。"""
    scale = data.get("scale")
    if not isinstance(scale, dict):
        raise CardError(SCALE_INVALID, [_issue(SCALE_INVALID, "", "缺 scale 或不是 dict")])
    low = scale.get("min")
    high = scale.get("max")
    if not _is_int(low) or not _is_int(high):
        raise CardError(SCALE_INVALID, [_issue(SCALE_INVALID, "", "scale 的 min/max 不是整数")])
    if low >= high:
        raise CardError(SCALE_INVALID, [_issue(SCALE_INVALID, "", "scale 的 min >= max")])
    return low, high


def _check_axes_list(data: Dict[str, Any]) -> List[Any]:
    """axes 必须是 list；不是就当场拒载，别硬猜。"""
    entries = data.get("axes")
    if not isinstance(entries, list):
        raise CardError(AXES_INVALID, [_issue(AXES_INVALID, "", "axes 不是 list")])
    return entries


def _check_entry(axis_id: str, entry: Dict[str, Any],
                 scale_min: int, scale_max: int) -> List[Dict[str, str]]:
    """单轴校验：六轴都成对——查配对 / 整数 / 值域，带 pending: true 一律拒载。

    PENDING_AXES 那一支随 2026-11-06 的定稿一起删了：六轴各有数之后，
    还能出现 pending 轴只可能是有人把补齐的轴标回 pending，那本身就是坏卡，见下面第一条。
    """
    issues: List[Dict[str, str]] = []
    if entry.get("pending") is True:
        issues.append(_issue(AXIS_PAIRED_BUT_PENDING, axis_id,
                             "六轴完整，任何轴都不许再标 pending: true"))
        return issues
    for key in ("inner", "shown"):
        if key not in entry or entry[key] is None:
            issues.append(_issue(AXIS_UNPAIRED, axis_id, "成对轴缺 %s" % key))
    if issues:
        return issues
    for key in ("inner", "shown"):
        if not _is_int(entry[key]):
            issues.append(_issue(AXIS_NOT_INT, axis_id,
                                 "%s 不是整数：%r" % (key, entry[key])))
    if issues:
        return issues
    for key in ("inner", "shown"):
        value = entry[key]
        if value < scale_min or value > scale_max:
            issues.append(_issue(AXIS_OUT_OF_RANGE, axis_id,
                                 "%s=%r 超出刻度 [%r, %r]" % (key, value, scale_min, scale_max)))
    return issues


def live_card_path() -> Path:
    """实盘卡：store.default_root()（HEART_ROOT > <仓库>/home/organs/heart）/ CARD_FILE。"""
    return store.default_root() / CARD_FILE


def _default_card_path() -> Path:
    """缺省解析：实盘在就用实盘；实盘没有 → 回退出厂 seed 并 WARN 一行。"""
    live = live_card_path()
    if live.exists():
        return live
    warnings.warn(
        "实盘没有卡（%s），按出厂 seed 只读回退；要落盘请先播种。" % live,
        stacklevel=3,
    )
    return SEED_CARD_PATH


def load_card(path: Any = None) -> Card:
    """读卡并做成对校验；坏卡一律 CardError，绝不补默认值，也不写盘。

    path 缺省**实盘优先**（live_card_path()）：实盘有卡就读实盘；实盘没有就回退
    SEED_CARD_PATH（出厂 seed 料）并 warnings.warn 一行。读不到 → card-missing，
    坏 JSON / 不是 dict → card-corrupt，其余逐类见 _check_*。
    """
    target = Path(path) if path is not None else _default_card_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise CardError(CARD_MISSING, [_issue(CARD_MISSING, "", "读不到卡文件：%s（%s）" % (target, exc))]) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CardError(CARD_CORRUPT, [_issue(CARD_CORRUPT, "", "JSON 坏：%s" % exc)]) from exc
    if not isinstance(data, dict):
        raise CardError(CARD_CORRUPT, [_issue(CARD_CORRUPT, "", "卡顶层不是 dict")])

    scale_min, scale_max = _check_scale(data)
    entries = _check_axes_list(data)

    issues: List[Dict[str, str]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            issues.append(_issue(AXES_INVALID, "", "axes 里有一项不是 dict"))
            continue
        axis_id = entry.get("id")
        if not isinstance(axis_id, str) or not axis_id.strip():
            issues.append(_issue(AXES_INVALID, "", "轴缺 id 或 id 不是字符串"))
            continue
        if axis_id in seen:
            issues.append(_issue(AXIS_DUPLICATE, axis_id, "轴 id 重复"))
            continue
        seen[axis_id] = entry
        if axis_id not in model.AXES:
            issues.append(_issue(AXIS_UNKNOWN, axis_id, "不在 model.AXES 里的轴"))
            continue
        issues.extend(_check_entry(axis_id, entry, scale_min, scale_max))

    for axis_id in model.AXES:
        if axis_id not in seen:
            issues.append(_issue(AXIS_MISSING, axis_id, "缺这根轴"))

    if issues:
        raise CardError(issues[0]["code"], issues)

    axes: Dict[str, Axis] = {}
    for axis_id in model.AXES:
        entry = seen[axis_id]
        pending = axis_id in model.PENDING_AXES
        axes[axis_id] = Axis(
            id=axis_id,
            inner=None if pending else entry["inner"],
            shown=None if pending else entry["shown"],
            pending=pending,
        )
    paired = tuple(axis_id for axis_id in model.AXES if axis_id in model.PAIRED_AXES)
    pending_ids = tuple(axis_id for axis_id in model.AXES if axis_id in model.PENDING_AXES)
    return Card(axes=axes, paired=paired, pending=pending_ids,
                scale_min=scale_min, scale_max=scale_max)


def tier_of(value: int, scale_min: int, scale_max: int) -> int:
    """值 → 1..TIER_COUNT 档（均分刻度，两端含在首尾档里）。"""
    span = scale_max - scale_min
    if span <= 0:
        raise ValueError("刻度必须是 min < max")
    ratio = (float(value) - float(scale_min)) / float(span)
    tier = int(ratio * model.TIER_COUNT) + 1
    if tier < 1:
        return 1
    if tier > model.TIER_COUNT:
        return model.TIER_COUNT
    return tier


# 档位词：六轴各五个词，一档最低、五档最高、中间档一律「一般」，词面贴轴义。
# 六轴完整（2026-11-06 人定稿）——少写一轴就是那句「None」回到 render_text 里，
# 所以 set(TIER_WORDS) == set(model.AXES) 有一条机器守卫钉着。
TIER_WORDS: Dict[str, Tuple[str, ...]] = {
    "服气": ("顶得凶", "不太服", "一般", "挺服", "特别服"),
    "心疼": ("冷", "淡", "一般", "心疼", "特别心疼"),
    "想黏人": ("拒人", "淡着", "一般", "黏", "很黏"),
    "怕失去": ("稳", "不动", "一般", "怕", "很怕"),
    "耐心": ("没耐性", "有点急", "一般", "有耐心", "特别有耐心"),
    "记性": ("转身就忘", "记不住", "一般", "记得住", "过目不忘"),
}

# 档位禁令：每一档配一句"这档时不许干什么"，随档位单调，不进魂。
# 全部是默认值、可调；嘴只读这份表，不自己发明禁令。
TIER_BANS: Dict[str, Dict[int, str]] = {
    "想黏人": {
        1: "黏人低档时不许连发三条以上（默认值，可调）",
        2: "黏人低档时不许连发三条以上（默认值，可调）",
    },
    "服气": {
        1: "不服时不许改用敬语（默认值，可调）",
    },
    "心疼": {
        1: "心疼低档时不许说「随便你」（默认值，可调）",
    },
    "怕失去": {
        1: "怕失去低档时不许主动提「分开」（默认值，可调）",
    },
    "耐心": {
        1: "没耐性时不许连问三次以上（默认值，可调）",
        2: "没耐性时不许连问三次以上（默认值，可调）",
    },
    "记性": {
        1: "记性低档时不许硬说「我记得」——问一遍比编一句强（默认值，可调）",
        2: "记性低档时不许硬说「我记得」——问一遍比编一句强（默认值，可调）",
    },
}


def tier_word(axis_id: str, tier: int) -> Optional[str]:
    """查档位词：未知轴 / 越界档一律返回 None，绝不造词（六轴都在表里，真卡不会落空）。"""
    words = TIER_WORDS.get(axis_id)
    if not words:
        return None
    if tier < 1 or tier > len(words):
        return None
    return words[tier - 1]


def _bounded(value: float, low: int, high: int) -> int:
    """四舍五入后夹回刻度内，给渲染用。"""
    number = int(round(float(value)))
    if number < low:
        return low
    if number > high:
        return high
    return number


def _levels(card: Card, axis_id: str, offsets: Dict[str, float]) -> Tuple[int, int]:
    """一轴的 inner/shown 档：baseline + offset 相加后再分档。"""
    axis = card.axes[axis_id]
    delta = float(offsets.get(axis_id, 0.0) or 0.0)
    inner = _bounded(axis.inner + delta, card.scale_min, card.scale_max)
    shown = _bounded(axis.shown + delta, card.scale_min, card.scale_max)
    return (tier_of(inner, card.scale_min, card.scale_max),
            tier_of(shown, card.scale_min, card.scale_max))


def render_text(card: Card, offsets: Optional[Dict[str, float]] = None) -> str:
    """把 baseline + offset 渲染成只含档位词的一段文本（不许出现 0-9）。

    薄包装：档位的合成与渲染都在 organs/heart/effective.py（全仓唯一合成点）。
    函数级 import 是刻意的——effective 要取本模块的档界与禁令表，模块级 import 会成环。
    """
    from organs.heart import effective

    return effective.render_text(card, offsets)


def view(card: Card, offsets: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """给面板 / API 的结构化读数：档位词，数值留内部。

    六轴完整之后 pending 恒为空表，但键不许删——面板与既有读法都读它。
    """
    extra = offsets or {}
    loaded: List[str] = []
    pending: List[Dict[str, Any]] = []
    tiers: Dict[str, Dict[str, Any]] = {}
    for axis_id in card.axes:
        axis = card.axes[axis_id]
        if axis.pending:
            pending.append({"axis": axis_id, "greyed": True})
            continue
        loaded.append(axis_id)
        inner_tier, shown_tier = _levels(card, axis_id, extra)
        tiers[axis_id] = {
            "inner_tier": inner_tier,
            "shown_tier": shown_tier,
            "inner_word": tier_word(axis_id, inner_tier),
            "shown_word": tier_word(axis_id, shown_tier),
        }
    return {
        "loaded": loaded,
        "pending": pending,
        "tiers": tiers,
        "scale": {"min": card.scale_min, "max": card.scale_max},
    }
