"""effective_state：全仓唯一合成点。

这是全仓唯一合成点：engage 的门、面板的档位、嘴的语气档都从这里取；
任何一处自己再算一遍就是第二处真源。

为什么只有这一处：面板说档位词、engage 说原始数，两套换算一旦分叉，就会出现
「面板显示还行，她却闭嘴」——与当年「判了但没说出哪个门」是同一类病。所以：

- 档界只在 mood.tier_of / mood.direction_of 里（五档、方向），本模块一个字都不重写；
- 轴档只在 card.tier_of / card.tier_word 里（均分刻度、五档词），本模块只负责
  「卡的 inner/shown + 该轴偏移」这一步相加，连四舍五入与夹刻度都用 card._bounded，
  不另写一份；
- 禁令只在 card.TIER_BANS 里，本模块按生效档位去查，不发明新句子。

本模块不做任何时间衰减：mood 的衰减在 mood.read 里，offset 的回落与钳制在
offset.read / overview 里。effective_state 只把两边的读数合成一次，
now 参数收下但不使用——它由调用方对齐 mood / offset 的读数时刻。

返回三块（键名对外冻结）：

- "mood"：{"now", "baseline", "tier", "direction"}；
- "axes"：{轴: {"inner_value","shown_value","inner_tier","shown_tier",
  "inner_word","shown_word","offset","greyed"}}——无数的轴只有 {"greyed": True}，
  不含数值（更不写 0）；
- "voice"：{"tone", "direction", "bans"}——tone 就是 mood 那一档的词（嘴读它定冷热），
  bans 是六轴里按生效档位命中的禁令句。

机械合成，不许在此加任何启发式 / 权重 / 阈值。坏卡照旧抛 card.CardError，
由 api 层转「拒载」。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from organs.heart import card as card_module
from organs.heart import mood


def card_path(root: Any = None) -> Optional[Path]:
    """数据目录 → 卡文件路径；root 是 None 就回 None（交给 card.load_card 的默认路径）。

    root 按「数据目录」解释：目录里找 card.CARD_FILE（baseline-card.json）；
    恰好给的是文件路径也认，按文件用（目录优先）。这条规则只有这一处——
    api/heart.py 的 _card_path 直接使唤它，两边不可能分叉。
    """
    if root is None:
        return None
    target = Path(root)
    if target.is_file():
        return target
    return target / card_module.CARD_FILE


def _offset_values(offset: Any) -> Dict[str, float]:
    """offset 的读数 → {轴: 偏移值}；缺省 / 空 / 坏一律按 0。

    收两种形状，值都用 offset 模块已经算好的那一个（本模块不重算回落、不钳制二次）：
    - offset.overview / read 的快照 {"week", "at", "axes": {轴: {"value": ...}}} → 取 value；
    - 直接的 {轴: 数} 映射（card.render_text 的老口径）。
    """
    table = offset
    if isinstance(offset, Mapping) and "axes" in offset:
        table = offset.get("axes")
    if not isinstance(table, Mapping):
        return {}
    values: Dict[str, float] = {}
    for axis, row in table.items():
        raw = row.get("value") if isinstance(row, Mapping) else row
        if raw is None or isinstance(raw, bool):
            values[str(axis)] = 0.0
            continue
        try:
            values[str(axis)] = float(raw)
        except (TypeError, ValueError):
            values[str(axis)] = 0.0
    return values


def _axis_row(card_obj: card_module.Card, axis_id: str, deltas: Dict[str, float]) -> Dict[str, Any]:
    """一轴：卡里的 inner/shown 加上该轴偏移，再向 card 要档与词。

    没有值的轴没有档：只回 {"greyed": True}，不写数、不写 0
    （六轴完整之后走不到这条分支：任何轴都必带 inner/shown）。
    """
    axis = card_obj.axes[axis_id]
    if axis.pending:
        return {"greyed": True}
    delta = float(deltas.get(axis_id, 0.0) or 0.0)
    low, high = card_obj.scale_min, card_obj.scale_max
    inner_value = card_module._bounded(axis.inner + delta, low, high)
    shown_value = card_module._bounded(axis.shown + delta, low, high)
    inner_tier = card_module.tier_of(inner_value, low, high)
    shown_tier = card_module.tier_of(shown_value, low, high)
    return {
        "inner_value": inner_value,
        "shown_value": shown_value,
        "inner_tier": inner_tier,
        "shown_tier": shown_tier,
        "inner_word": card_module.tier_word(axis_id, inner_tier),
        "shown_word": card_module.tier_word(axis_id, shown_tier),
        "offset": delta,
        "greyed": False,
    }


def axis_rows(card_obj: card_module.Card,
              offsets: Any = None) -> Dict[str, Dict[str, Any]]:
    """六轴一次算齐；次序跟卡里的轴序（面板与文本的顺序都由它定）。"""
    deltas = _offset_values(offsets)
    return dict((axis_id, _axis_row(card_obj, axis_id, deltas))
                for axis_id in card_obj.axes)


def _axis_bans(axis_id: str, row: Dict[str, Any]) -> List[str]:
    """一轴命中的禁令：心里、面上两个档位各查一次，同句只留一条（次序照旧）。"""
    if row.get("greyed"):
        return []
    bans: List[str] = []
    for tier in (row["inner_tier"], row["shown_tier"]):
        ban = card_module.TIER_BANS.get(axis_id, {}).get(tier)
        if ban and ban not in bans:
            bans.append(ban)
    return bans


def _all_bans(rows: Dict[str, Dict[str, Any]]) -> List[str]:
    """六轴合起来的禁令句（嘴只读这一份，不自己发明禁令）。"""
    bans: List[str] = []
    for axis_id, row in rows.items():
        for ban in _axis_bans(axis_id, row):
            if ban not in bans:
                bans.append(ban)
    return bans


def mood_block(mood_baseline: float, mood_now: float) -> Dict[str, Any]:
    """心情这一块：两个数 + 档位词 + 方向；档界只从 mood 拿，这里不写第二套。

    /heart/mood 与 effective_state 都用它，所以面板读到的档位词与 voice.tone 同源。
    """
    now = float(mood_now)
    baseline = float(mood_baseline)
    return {
        "now": now,
        "baseline": baseline,
        "tier": mood.tier_of(now),
        "direction": mood.direction_of(now, baseline),
    }


def effective_state(
    mood_baseline: float,
    mood_now: float,
    offset: Any = None,
    card_obj: Optional[card_module.Card] = None,
    root: Any = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """三个块的唯一合成点；now 收下不用（本函数不做时间衰减，见模块头）。

    offset 缺省 / 空 → 按 0。
    card_obj 缺省时按 root 目录里的卡文件加载（root 按数据目录解释，见 card_path；
    恰好给的是卡文件路径也认）。root 也给不出卡 → 照旧抛 card.CardError
    （card-missing / card-corrupt …），由 api 层转「拒载」。
    """
    mood_read = mood_block(mood_baseline, mood_now)
    card_object = card_obj if card_obj is not None else card_module.load_card(card_path(root))
    rows = axis_rows(card_object, offset)
    return {
        "mood": mood_read,
        "axes": rows,
        "voice": {
            "tone": mood_read["tier"],
            "direction": mood_read["direction"],
            "bans": _all_bans(rows),
        },
    }


def render_text(card_obj: card_module.Card, offsets: Any = None) -> str:
    """把合成出的档位渲染成只含档位词的一段文本（输出里不许出现 0-9）。

    card.render_text 是这个函数的薄包装：文本的档位与禁令也必须走同一个合成点，
    否则「面板说的档」和「嘴上念的档」又成了两处真源。
    """
    lines: List[str] = ["性格基线（只讲档位，不讲数字）："]
    for axis_id, row in axis_rows(card_obj, offsets).items():
        if row.get("greyed"):
            lines.append("- %s：无数据（跳过注入）。" % axis_id)
            continue
        lines.append("- %s：心里%s，面上%s。" % (
            axis_id, row["inner_word"], row["shown_word"]))
        for ban in _axis_bans(axis_id, row):
            lines.append("  %s" % ban)
    return "\n".join(lines)
