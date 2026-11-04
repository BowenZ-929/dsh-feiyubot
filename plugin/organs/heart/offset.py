"""heart-offset：六轴临时偏移（日事件 / 周结算 / 无事件回落）。

本模块的尺只有一把，单位是**天**：连续 model.OFFSET_IDLE_DAYS（3）天没有事件，
该轴朝 0 回落 model.OFFSET_IDLE_PER_DAY（1）。判据写在 docs/modules/heart.md
§2 offset「会被什么改变」，本模块只是照判据挑尺。

不借的尺（名单写死，省得下次又借回来）：
- 不借 mood.now 的 3 小时，也不借心情基线那把 7 天的；
- 不借参考册 M12 那对 6h / 4h —— 那是**情绪量**的尺，搬到 offset 上就是拿别人的
  手段套自己的判据。CONTRIBUTING「做法可变、判据不可」，判据赢。
- 为什么不用半衰：同一个判据还写着面板要看得到"本周黏人轴 +1.5"。6h 半衰叠上死区，
  一个 +1 的日偏移 18 小时内就归 0，面板挂不到"本周"这个尺度上，那条判据当场不成立。
  天尺线性走 1，读数的衰减节奏与周结算同尺度，才对得上判据。

做法（数值是默认值不是验收线）：
- 每条轴记 idle_anchor_at：上一次"有事件"或"周结算"把值写下去的时刻。旧状态里没有
  这一栏就按 updated_at 兜底（旧数据照旧能读，不许拿默认值补一个偏移出来）。
- steps = 距 idle_anchor_at 的整小时数 // (24 * model.OFFSET_IDLE_DAYS)；
  value 朝 0 走 steps * model.OFFSET_IDLE_PER_DAY，过头就停在 0。
- 死区：|v| < model.OFFSET_IDLE_PER_DAY * 0.5 直接归 0，免得盘上永远挂着
  0.0001 这种"看起来还在"的残渣。
- 结算次序钉死：先衰减 → 再叠加 → 最后统一钳制。衰减只有本模块这一份私有实现。
- 日事件给单轴单日 ±1（多打的截断并记账），周结算是**覆盖式** ±2（不是叠加，
  被覆盖掉的日偏移要进账本）。
- 落盘只落"写下去的值"：衰减一个字节都不存，全在读到的那一刻按时间戳现算，
  所以读多少遍、谁先结算，算出来都一样，不会重复衰减。
- 永不写回魂：本模块拿不到基线卡的文件路径、不 import 基线卡模块、不写卡。
- 没有定时器：所有"随时间前进"的量都在读的那一刻按时间戳现算。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from organs.heart import model, store

OFFSET_FILE = "offset.json"
LEDGER_FILE = "offset_ledger.jsonl"
STATE_VERSION = 1

# 死区：衰减到这个量级以下就当 0，别留残渣。
DEAD_ZONE = model.OFFSET_IDLE_PER_DAY * 0.5

# 账本行的 kind：截断 / 越界 / 覆盖，三种都只增不改。
KIND_TRUNCATE = "truncate"
KIND_CLAMP = "clamp"
KIND_OVERWRITE = "overwrite"


def _paths(root: Any = None):
    """状态文件与账本文件的落点；root 缺省是仓里的 data 目录。"""
    base = Path(root) if root is not None else store.DATA_DIR
    return base / OFFSET_FILE, base / LEDGER_FILE


def _empty_state() -> Dict[str, Any]:
    return {"version": STATE_VERSION, "axes": {}, "week": ""}


def _load_state(root: Any = None) -> Dict[str, Any]:
    """读状态；坏文件按空状态起（读入口不许炸调用方）。"""
    state_path, _ = _paths(root)
    state = store.read_json(state_path, _empty_state())
    if not isinstance(state.get("axes"), dict):
        state["axes"] = {}
    state.setdefault("version", STATE_VERSION)
    if not isinstance(state.get("week"), str):
        state["week"] = ""
    return state


def _row(state: Dict[str, Any], axis: str) -> Dict[str, Any]:
    """取一轴的状态行（拷贝出来改，改完再写回）。"""
    rows = state.get("axes") or {}
    row = rows.get(axis)
    return dict(row) if isinstance(row, dict) else {}


def _value_of(row: Dict[str, Any]) -> float:
    try:
        return float(row.get("value", model.OFFSET_BASE) or model.OFFSET_BASE)
    except (TypeError, ValueError):
        return model.OFFSET_BASE


def _day_delta_of(row: Dict[str, Any]) -> float:
    try:
        return float(row.get("day_delta", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _anchor_of(row: Dict[str, Any]) -> str:
    """这一轴的闲置起算点：优先 idle_anchor_at，旧状态退回 updated_at。

    两栏都没有或不是字符串就当"没有起算点"（空串）：不编一个假时刻补上，
    更不拿它补偏移出来。
    """
    anchor = row.get("idle_anchor_at")
    if isinstance(anchor, str) and anchor:
        return anchor
    stamp = row.get("updated_at")
    return stamp if isinstance(stamp, str) else ""


def _idle_steps(anchor_at: str, now: str) -> int:
    """距起算点过了几个整 model.OFFSET_IDLE_DAYS 天；不足一天不算。"""
    if not anchor_at:
        return 0
    hours = model.hours_between(anchor_at, now)
    if hours <= 0:
        return 0
    return int(hours // (24 * model.OFFSET_IDLE_DAYS))


def _decay(value: float, anchor_at: str, now: str) -> float:
    """本模块私有的回落：按整 model.OFFSET_IDLE_DAYS 天线性朝 0 走，含死区。

    只给本模块用：别处不许 import 去当"统一衰减函数"（各器官各有各的尺）。
    刻度是量化的，零头留着下一次读再算，不四舍五入也不提前吃掉。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not number:
        return 0.0
    steps = _idle_steps(anchor_at, now)
    if steps:
        shift = steps * model.OFFSET_IDLE_PER_DAY
        number = max(0.0, number - shift) if number > 0 else min(0.0, number + shift)
    if abs(number) < DEAD_ZONE:
        return 0.0
    return number


def _decayed_value(row: Dict[str, Any], now: str) -> float:
    """一轴此刻的回落值（读时现算，不改 row）。"""
    return _decay(_value_of(row), _anchor_of(row), now)


def _clamp(value: float, limit: float) -> float:
    """对称钳制：±limit。"""
    if value > limit:
        return limit
    if value < -limit:
        return -limit
    return float(value)


def _week_key(now: str) -> str:
    """"YYYY-Www"：ISO 周，周一开头。"""
    stamp = model.parse_ts(now)
    iso = stamp.date().isocalendar()
    return "%04d-W%02d" % (iso[0], iso[1])


def read(root: Any = None, now: Optional[str] = None) -> Dict[str, Any]:
    """纯读：现算每轴回落后的值与是否被钳制；一个字节都不写盘。"""
    stamp = now or model.now_iso()
    state = _load_state(root)
    axes: Dict[str, Dict[str, Any]] = {}
    for axis in model.AXES:
        row = _row(state, axis)
        decayed = _decayed_value(row, stamp)
        axes[axis] = {
            "axis": axis,
            "value": _clamp(decayed, model.OFFSET_MAX),
            "decayed_value": decayed,
            "day_delta": _day_delta_of(row),
            "day": row.get("day") or "",
            "updated_at": row.get("updated_at") or "",
            "idle_anchor_at": _anchor_of(row),
            "clamped": abs(decayed) > model.OFFSET_MAX + 1e-9,
        }
    return {"version": STATE_VERSION, "week": state.get("week") or "",
            "at": stamp, "axes": axes}


def overview(root: Any = None, now: Optional[str] = None) -> Dict[str, Any]:
    """面板读数：每轴 {value, decayed_value, day_delta, clamped}；同样不写盘。"""
    snap = read(root=root, now=now)
    axes: Dict[str, Dict[str, Any]] = {}
    for axis, row in snap["axes"].items():
        axes[axis] = {
            "value": row["value"],
            "decayed_value": row["decayed_value"],
            "day_delta": row["day_delta"],
            "clamped": row["clamped"],
        }
    return {"week": snap["week"], "at": snap["at"], "axes": axes}


def apply_daily(axis: str, delta: float, root: Any = None,
                now: Optional[str] = None, source: str = "event") -> Dict[str, Any]:
    """一个日事件：先衰减、再叠加（单日单轴封顶 ±1）、最后统一钳制。

    超出单日上限的部分截断，并往账本写一行 truncate；撞上总幅上限写 clamp。
    落地时把 idle_anchor_at 刷成事件时刻——有事件就重新起算。
    """
    if axis not in model.AXES:
        raise ValueError("未知轴：%r" % (axis,))
    stamp = now or model.now_iso()
    today = model.day_key(stamp)
    state = _load_state(root)

    row = _row(state, axis)
    decayed_before = _decayed_value(row, stamp)
    if (row.get("day") or "") != today:
        row["day"] = today
        row["day_delta"] = 0.0
    day_before = _day_delta_of(row)
    cap = model.OFFSET_DAILY_PER_AXIS
    requested = day_before + float(delta)
    capped = _clamp(requested, cap)
    truncated = abs(requested) > cap + 1e-9
    applied = capped - day_before

    raw_value = decayed_before + applied
    value = _clamp(raw_value, model.OFFSET_MAX)
    clamped = abs(raw_value) > model.OFFSET_MAX + 1e-9

    row["value"] = value
    row["day_delta"] = capped
    row["updated_at"] = stamp
    row["idle_anchor_at"] = stamp
    state["axes"][axis] = row
    state["version"] = STATE_VERSION

    state_path, ledger_path = _paths(root)
    store.write_json(state_path, state)
    if truncated:
        store.append_jsonl(ledger_path, {
            "kind": KIND_TRUNCATE, "axis": axis, "day": today,
            "requested": requested, "applied": capped, "cap": cap,
            "source": source, "at": stamp,
        })
    if clamped:
        store.append_jsonl(ledger_path, {
            "kind": KIND_CLAMP, "axis": axis, "raw": raw_value, "value": value,
            "limit": model.OFFSET_MAX, "source": source, "at": stamp,
        })
    return {
        "axis": axis, "value": value, "decayed_value": decayed_before,
        "applied": applied, "day_delta": capped, "day": today,
        "source": source, "truncated": truncated, "clamped": clamped, "at": stamp,
    }


def settle_week(values: Dict[str, float], root: Any = None,
                now: Optional[str] = None) -> Dict[str, Any]:
    """周结算：先衰减，再把给出的轴**覆盖**成周值（±2），最后统一钳制。

    覆盖不叠加；被覆盖掉的日偏移逐轴写一行 overwrite 进账本。
    结算过的轴同样刷新 idle_anchor_at（新值新起算）；没结算的轴一个字节不动，
    让它们的闲置天数继续攒着，不会被别人的一次结算清零。
    """
    for axis in values:
        if axis not in model.AXES:
            raise ValueError("未知轴：%r" % (axis,))
    stamp = now or model.now_iso()
    week = _week_key(stamp)
    today = model.day_key(stamp)
    state = _load_state(root)

    settled: Dict[str, Dict[str, Any]] = {}
    for axis in model.AXES:
        if axis not in values:
            continue
        row = _row(state, axis)
        daily_before = _clamp(_decayed_value(row, stamp), model.OFFSET_MAX)
        week_value = _clamp(float(values[axis]), model.OFFSET_WEEK_MAX)
        week_value = _clamp(week_value, model.OFFSET_MAX)
        row["value"] = week_value
        row["day_delta"] = 0.0
        row["day"] = today
        row["updated_at"] = stamp
        row["idle_anchor_at"] = stamp
        state["axes"][axis] = row
        settled[axis] = {
            "axis": axis, "value": week_value, "daily_before": daily_before,
            "week_value": week_value,
            "overwritten": abs(daily_before - week_value) > 1e-9,
        }
    state["version"] = STATE_VERSION
    state["week"] = week

    state_path, ledger_path = _paths(root)
    store.write_json(state_path, state)
    for axis, info in settled.items():
        store.append_jsonl(ledger_path, {
            "kind": KIND_OVERWRITE, "axis": axis,
            "daily_before": info["daily_before"], "week_value": info["week_value"],
            "week": week, "at": stamp,
        })
    return {"week": week, "at": stamp, "axes": settled}
