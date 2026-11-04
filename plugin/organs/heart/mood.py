"""心情：两个数（now / baseline），事件只给有界增量，回落走指数衰减；读时算，不挂定时器。

判据先写在这里：model.py 里的数是默认值，不是验收线——要改节奏就改 model，
本文件不抄第二份数字，也不在逻辑里写 magic number。

本模块只持有两把尺，也只有这两把：
1. now 回落到 baseline，半衰 model.MOOD_NOW_HALF_LIFE_H（3 小时）；
2. baseline 自己慢漂回 model.MOOD_BASELINE_DEFAULT，半衰
   model.MOOD_BASELINE_HALF_LIFE_H（7 天）。
两把尺都在本模块，但**不许合成一个通用衰减函数**给别处用：一把尺一段私有函数，
宁可重复几行，也不把两把尺搅成一把、更不往别的模块递。offset 的 6 小时（以及
精力轴的 4 小时）与本模块无关，本模块不引用、不共用函数、不抄常数；熟稔 21 天
那把尺在脑，心既不持有也不碰。

落盘两件：
- mood.json：状态（schema 见下），只由 apply_event / settle_day 写；
- mood_traj.jsonl：轨迹账本，只由 read() 顺手追加一行——"读的时候顺手记一笔"，
  没有定时器、没有线程、没有 cron。

面板能看轨迹（trajectory）、不能改；人工覆盖只算一次，下次 settle_day 结算盖掉。
本轮不实现人工覆盖，规矩先写在这里。

事件给的是有界增量，两条规矩：
1. 渐近线天花板：正向增量乘余量 (1 − 当前值/model.MOOD_CEILING)——天花板就是
   MOOD_MAX，一顿夸只能逼近它，顶不满；
2. 热度折扣：同一类型事件 model.MOOD_HEAT_WINDOW_MIN 分钟内第 n+1 次，
   增益 × 1/(1 + n/model.MOOD_HEAT_K)——越密越薄。
负向（cold / conflict / insult）两个折扣**一个都不给**：疲惫与压力是代价，不打折，
只挡地板（不许穿底）。
上一轮那两条"第 2 次起半档 / 单日同向封顶 N 档"连同
model.MOOD_DIMINISH_FACTOR、model.MOOD_DAILY_SAME_DIRECTION_CAP_TIERS 一起撤了——
留着会让第 3 条之后恒为 0，曲线就出不来。判据与顺序见 apply_event 的 docstring。

状态 schema（不许悄悄加字段）：
{"version": 1, "now": float, "baseline": float, "updated_at": iso,
 "day": "YYYY-MM-DD", "conflict_used": int, "window": {who: "YYYY-MM-DDTHH"}}
day_events / day_delta_tiers 两栏随旧日上限一起退役：不再写；旧文件里读到就
静默忽略（不炸，也不搬进新字段）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from organs.heart import events, model, store

MOOD_FILE = "mood.json"
TRAJ_FILE = "mood_traj.jsonl"

# 事件 → 档。被骂（insult）与冲突同为 −2 档；model 里没有单独的 MOOD_INSULT，
# 这里复用冲突的档值，不改 model。
_EVENT_TIERS = {
    model.EVENT_PRAISE: model.MOOD_PRAISE,
    model.EVENT_COLD: model.MOOD_COLD,
    model.EVENT_CONFLICT: model.MOOD_CONFLICT,
    model.EVENT_INSULT: model.MOOD_CONFLICT,
}

# 档位词：五档，与 model.TIER_COUNT 对齐。档界围着默认基线对称铺开：
# 平常带半宽 = 一档（MOOD_UNIT），冰点界 = MOOD_LOW，两边的档界由此推出，
# 不另抄数字——所以默认基线 60 落在"平常"正中间，不会一睁眼就"偏热"。
# 词本身是本模块的默认值，不是验收线。
_TIERS = ("冰点", "偏冷", "平常", "偏热", "滚烫")

# 方向：离基线不到半档就算中性——半档是 MOOD_UNIT / 2，不是新数字。
_DIRECTION_HOT = "偏热"
_DIRECTION_NEUTRAL = "中性"
_DIRECTION_COLD = "偏冷"



# ---- 内部小工具 ----------------------------------------------------------


def _path(root: Any) -> Path:
    """状态文件路径：root 给了就用 root，没给就用 store.default_root()（实盘）。"""
    base = Path(root) if root is not None else store.default_root()
    return base / MOOD_FILE


def _traj_path(root: Any) -> Path:
    base = Path(root) if root is not None else store.default_root()
    return base / TRAJ_FILE


def _as_float(value: Any, default: float) -> float:
    """坏值一律回默认（bool 不算数）。"""
    try:
        if isinstance(value, bool) or value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool) or value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float) -> float:
    return min(float(model.MOOD_MAX), max(float(model.MOOD_MIN), value))


def _load(root: Any, at: str) -> Dict[str, Any]:
    """读状态；缺字段 / 坏字段一律补默认，坏 JSON 交给 store 回默认。"""
    data = store.read_json(_path(root), {})
    window = data.get("window")
    state = {
        "version": model.VERSION,
        "now": _clamp(_as_float(data.get("now"), model.MOOD_BASELINE_DEFAULT)),
        "baseline": _clamp(
            _as_float(data.get("baseline"), model.MOOD_BASELINE_DEFAULT)
        ),
        "updated_at": data.get("updated_at")
        if isinstance(data.get("updated_at"), str) and data.get("updated_at")
        else at,
        "day": data.get("day")
        if isinstance(data.get("day"), str) and data.get("day")
        else model.day_key(at),
        "conflict_used": max(0, _as_int(data.get("conflict_used"), 0)),
        "window": dict(window) if isinstance(window, dict) else {},
    }
    # day_events / day_delta_tiers 两栏已退役：旧文件里读到就静默丢掉，不搬也不炸。
    return state


def _hour_key(at: str) -> str:
    """小时桶 "YYYY-MM-DDTHH"：同一个人同一小时只算一次。"""
    return model.parse_ts(at).strftime("%Y-%m-%dT%H")


def tier_of(value: float) -> str:
    """五档：平常带半宽一档，冰点界取 MOOD_LOW，热冷两侧镜像。公开给别的模块用。"""
    default = float(model.MOOD_BASELINE_DEFAULT)
    half = float(model.MOOD_UNIT)
    cold_edge = float(model.MOOD_LOW) - default  # 冰点界（负数）
    gap = float(value) - default
    if gap < cold_edge:
        return _TIERS[0]
    if gap < -half:
        return _TIERS[1]
    if gap < half:
        return _TIERS[2]
    if gap < -cold_edge:
        return _TIERS[3]
    return _TIERS[4]


def direction_of(value: float, baseline: float) -> str:
    """离基线不到半档算中性；热冷两端是同一个门槛。公开给别的模块用。"""
    gap = float(value) - float(baseline)
    half = float(model.MOOD_UNIT) / 2.0
    if gap >= half:
        return _DIRECTION_HOT
    if gap <= -half:
        return _DIRECTION_COLD
    return _DIRECTION_NEUTRAL


def _decay_now(value: float, baseline: float, hours: float) -> float:
    """第一把尺：now 往 baseline 走，半衰 MOOD_NOW_HALF_LIFE_H。只给 now 用。"""
    factor = 0.5 ** (float(hours) / float(model.MOOD_NOW_HALF_LIFE_H))
    return float(baseline) + (float(value) - float(baseline)) * factor


def _decay_baseline(baseline: float, hours: float) -> float:
    """第二把尺：baseline 自己往回漂，半衰 MOOD_BASELINE_HALF_LIFE_H。只给 baseline 用。

    往 model.MOOD_BASELINE_DEFAULT 漂——日子过去一段时间，基线自己回到默认值。
    """
    factor = 0.5 ** (float(hours) / float(model.MOOD_BASELINE_HALF_LIFE_H))
    default = float(model.MOOD_BASELINE_DEFAULT)
    return default + (float(baseline) - default) * factor


def _hours_since(older: str, newer: str) -> float:
    """两刻相差几小时；时间戳坏了当 0（坏数据不许炸调用方）。"""
    try:
        return model.hours_between(older, newer)
    except ValueError:
        return 0.0


def _effective(state: Dict[str, Any], at: str) -> Dict[str, float]:
    """现算到 at 这一刻的两个值：先 baseline 自己漂，再 now 往漂好的 baseline 走。"""
    hours = _hours_since(str(state["updated_at"]), at)
    baseline = _clamp(_decay_baseline(float(state["baseline"]), hours))
    current = _clamp(_decay_now(float(state["now"]), baseline, hours))
    return {"now": current, "baseline": baseline}


def _sample(root: Any, at: str, now_value: float, baseline: float) -> None:
    """读的时候顺手记一笔：与账本最后一行隔够 MOOD_SAMPLE_MIN_GAP_MIN 才追加。

    没有定时器；记的是"有人来读"的采样点，不是时钟的采样点。
    """
    rows = store.read_jsonl(_traj_path(root), limit=1)
    if rows:
        last = rows[-1].get("at")
        if isinstance(last, str) and last:
            try:
                gap_min = model.hours_between(last, at) * 60.0
            except ValueError:
                gap_min = float(model.MOOD_SAMPLE_MIN_GAP_MIN)
            if gap_min < float(model.MOOD_SAMPLE_MIN_GAP_MIN):
                return
    row = {
        "at": at,
        "now": round(float(now_value), 1),
        "baseline": round(float(baseline), 1),
        "tier": tier_of(now_value),
    }
    store.append_jsonl(_traj_path(root), row)


# ---- 对外 ----------------------------------------------------------------


def read(root: Any = None, now: Optional[str] = None) -> Dict[str, Any]:
    """读此刻的心情：现算，不写状态；只在账本上顺手记一笔。

    返回 {"now", "baseline", "direction", "tier", "as_of", "updated_at"}，
    两个数都四舍五入到一位小数（说人话够用，落盘仍留全精度）。
    """
    at = now if isinstance(now, str) and now else model.now_iso()
    state = _load(root, at)
    values = _effective(state, at)
    _sample(root, at, values["now"], values["baseline"])
    return {
        "now": round(values["now"], 1),
        "baseline": round(values["baseline"], 1),
        "direction": direction_of(values["now"], values["baseline"]),
        "tier": tier_of(values["now"]),
        "as_of": at,
        "updated_at": state["updated_at"],
    }


def apply_event(
    kind: str, root: Any = None, now: Optional[str] = None, who: str = "主人"
) -> Dict[str, Any]:
    """一个事件：先现算衰减，再按"渐近线 + 热度"折算增量，切掉超出的部分，最后落账。

    落账顺序钉死，别调换：
    1. 现算衰减得 before（_effective：baseline 先漂，now 再往漂好的 baseline 走）；
    2. 名义增量 nominal = 名义档 × MOOD_UNIT（praise +1、cold −1、conflict −2、insult −2）；
    3. 正向（nominal > 0）两个折扣一起上：
       n = events.same_kind_since(kind, …) 本热度窗口内已有几次（不含本次）
       heat = 1 / (1 + n / MOOD_HEAT_K)           ← 越密越薄
       headroom = 1 − before / MOOD_CEILING       ← 越接近天花板越加不动
       delta = nominal × heat × headroom
    4. 负向（nominal ≤ 0）一个折扣都不给：delta = nominal，
       再 delta = max(delta, −before) 不许穿底——疲惫与压力是代价，不打折；
    5. after = clamp(before + delta)，写回 now / baseline / updated_at / day /
       conflict_used / window。day_events / day_delta_tiers 两栏随旧日上限退役，不再写；
    6. events.append(kind, who, root, now, delta=…, after=…)：本模块只读账本一次（数热度），
       落账时也只投递一次；心不自己编事件。

    - kind 不在 model.EVENTS 里 → ValueError，不静默吞掉；
    - 两道旧闸照旧保留在前面，被挡时状态一个字节不改、账本也不落行：
      同一人同一小时最多一次：{"ok": False, "skipped": "windowed"}；
      冲突 / 被骂单日封顶 MOOD_CONFLICT_DAILY_CAP 次：{"ok": False, "skipped": "capped"}；
    - 成功返回 {"ok": True, "before", "after", "delta", "heat", "headroom", "skipped",
      "kind", "who", "tier"}：delta 是真正落账的分差（被夹到刻度边界时比名义增量小），
      heat 是这次用的热度系数、headroom 是渐近线系数（负向两者都是 1.0：没打过折）。
    """
    if kind not in model.EVENTS:
        raise ValueError("不认识的事件：%r（只认 %s）" % (kind, "、".join(model.EVENTS)))
    at = now if isinstance(now, str) and now else model.now_iso()
    state = _load(root, at)
    hour = _hour_key(at)
    today = model.day_key(at)
    label = who if isinstance(who, str) and who else "主人"

    if state["window"].get(label) == hour:
        return {"ok": False, "skipped": "windowed"}

    capping = kind in (model.EVENT_CONFLICT, model.EVENT_INSULT)
    used = state["conflict_used"] if state["day"] == today else 0
    if capping and used >= int(model.MOOD_CONFLICT_DAILY_CAP):
        return {"ok": False, "skipped": "capped"}

    # 1) 现算衰减
    values = _effective(state, at)
    before = values["now"]
    # 2) 名义档 → 分
    nominal = float(_EVENT_TIERS[kind]) * float(model.MOOD_UNIT)
    heat = 1.0
    headroom = 1.0
    if nominal > 0.0:
        # 3) 正向：热度折扣 × 渐近线余量
        n = events.same_kind_since(
            kind, root=root, now=at, window_min=float(model.MOOD_HEAT_WINDOW_MIN)
        )
        heat = 1.0 / (1.0 + float(n) / float(model.MOOD_HEAT_K))
        headroom = max(0.0, 1.0 - before / float(model.MOOD_CEILING))
        delta = nominal * heat * headroom
    else:
        # 4) 负向：不折扣、不缩放，只挡地板
        delta = max(nominal, -before)
    # 5) 落账
    after = _clamp(before + delta)

    window = dict((k, v) for k, v in state["window"].items() if v == hour)
    window[label] = hour
    state.update(
        {
            "now": after,
            "baseline": values["baseline"],
            "updated_at": at,
            "day": today,
            "conflict_used": used + (1 if capping else 0),
            "window": window,
        }
    )
    store.write_json(_path(root), state)
    # 6) 同一本账落一行
    events.append(
        kind,
        who=label,
        root=root,
        now=at,
        delta=round(after - before, 4),
        after=round(after, 4),
    )
    return {
        "ok": True,
        "before": round(before, 1),
        "after": round(after, 1),
        "delta": round(after - before, 1),
        "heat": round(heat, 4),
        "headroom": round(headroom, 4),
        "skipped": None,
        "kind": kind,
        "who": label,
        "tier": tier_of(after),
    }


def settle_day(root: Any = None, now: Optional[str] = None) -> Dict[str, Any]:
    """日结：把当日净变化的一半写进 baseline，另一半不给。

    状态 schema 里没有"日初锚点"这一格（schema 定死，不许加字段），所以当日净变化
    就取结算时刻现算出来的 now 与 baseline 的差——baseline 只在结算时移动，这个差
    正好是今天累积下来的净变化。写进去的一半夹在刻度内，然后把当日的账归零：
    conflict_used，day 落到今天（这是新的一天，不是昨天）。
    """
    at = now if isinstance(now, str) and now else model.now_iso()
    state = _load(root, at)
    values = _effective(state, at)
    today = model.day_key(at)
    absorbed = (values["now"] - values["baseline"]) / 2.0
    after_baseline = _clamp(values["baseline"] + absorbed)

    window = dict((k, v) for k, v in state["window"].items() if v == _hour_key(at))
    state.update(
        {
            "now": values["now"],
            "baseline": after_baseline,
            "updated_at": at,
            "day": today,
            "conflict_used": 0,
            "window": window,
        }
    )
    store.write_json(_path(root), state)
    return {
        "ok": True,
        "baseline_before": round(values["baseline"], 1),
        "baseline_after": round(after_baseline, 1),
        "absorbed": round(after_baseline - values["baseline"], 1),
        "now": round(values["now"], 1),
        "day": today,
    }


def trajectory(root: Any = None, limit: int = 200) -> List[Dict[str, Any]]:
    """轨迹账本的最后 limit 条，给面板画曲线；只读，面板不能改。"""
    rows = store.read_jsonl(_traj_path(root), limit=int(limit))
    return rows
