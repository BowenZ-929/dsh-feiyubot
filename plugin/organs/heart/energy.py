"""精力：0–100，是时间的函数——醒着掉、睡着回；读的时候现算，不挂定时器。

判据先写在这里：model.py 里的数是默认值，不是验收线——要改节奏就改 model，
本文件不抄第二份数字，也不在逻辑里写 magic number。

本模块只有这一把尺：清醒 ENERGY_AWAKE_PER_H（−4/小时）与睡眠 ENERGY_SLEEP_PER_MIN
（按分钟回血），按 model.ENERGY_SLICE_MIN（5 分钟）切片积分。这把尺不许与 mood 的
3 小时 / 7 天、offset 的 6 小时 / 4 小时共用，也不抽成通用衰减函数递出去——只有本
模块自己的一段私有积分。

结算即落盘：read() 把 value 与 updated_at 一起写回，"重启不重复扣"就靠这一条，
下次只算从那之后的新 Δt。单次最多补算 model.ENERGY_MAX_SETTLE_H 小时，超出部分
不追（起点仍推到 now）。Δt 小于 ENERGY_MIN_GAP_MIN 分钟就不动，连文件都不碰。

作息窗口（几点算睡、几点算醒）从外面注入，不在本文件里写死：DEFAULT_SLEEP_WINDOW
只是默认值；注入进来的是「她此刻该算醒着还是睡着」的档，心不自己判断日程。默认值
写在这里；日子线接管后由它提供，心不自己去读日程。

去重按 event_id 的容量上限 FIFO，不设过期时间：本模块只持有一把时间尺（settle 那把），
不养第二个时钟。

状态 schema（不许悄悄加字段）：
{"version": 1, "value": float, "updated_at": iso, "day": "YYYY-MM-DD",
 "deducted": float, "last_burst_at": iso|null}
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from organs.heart import model, store

ENERGY_FILE = "energy.json"

# 默认值写在这里；日子线接管后由它提供，心不自己去读日程。
DEFAULT_SLEEP_WINDOW: Tuple[int, int] = (
    int(model.ENERGY_SLEEP_START_H),
    int(model.ENERGY_SLEEP_END_H),
)

# 档位词：30 取自 model.ENERGY_LOW（"累"的门槛），50 / 75 是本模块自己的档界——
# 默认值，不是验收线。
_ENERGY_TIERS = ((float(model.ENERGY_LOW), "累"), (50.0, "偏累"), (75.0, "一般"))

# 进程内的连发去重：同一 event_id 只扣一次。只活这个进程（与道的"会话级覆盖"同一个
# 规矩）；落盘那一格 last_burst_at 管的是"同一分钟内重复调用"，重启后最多重扣一次，
# 而且仍受单日封顶约束。去重按 event_id 的容量上限 FIFO，不设过期时间：本模块只持有
# 一把时间尺（settle 那把），不养第二个时钟——幂等键的意义就是不随时间失效。
_SEEN_BURSTS: Dict[str, float] = {}
_SEEN_BURSTS_MAX = 64


# ---- 内部小工具 ----------------------------------------------------------


def _path(root: Any) -> Path:
    base = Path(root) if root is not None else store.DATA_DIR
    return base / ENERGY_FILE


def _as_float(value: Any, default: float) -> float:
    try:
        if isinstance(value, bool) or value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float) -> float:
    return min(float(model.ENERGY_MAX), max(float(model.ENERGY_MIN), value))


def _tier(value: float) -> str:
    for edge, word in _ENERGY_TIERS:
        if float(value) < edge:
            return word
    return "充沛"


def _window(sleep_window: Any) -> Tuple[int, int]:
    """把注入的作息窗口验成 (start_h, end_h)；None 之外一律不许糊过去。

    注入的是「她此刻该算醒着还是睡着」的档，心不自己判断日程，只认这两个小时数。
    """
    if sleep_window is None:
        return DEFAULT_SLEEP_WINDOW
    bad = "作息窗口要传 (start_h, end_h)：两个 0–23 的整数小时，且 start != end；收到 %r" % (
        sleep_window,
    )
    if not isinstance(sleep_window, (tuple, list)) or len(sleep_window) != 2:
        raise ValueError(bad)
    start_h, end_h = sleep_window
    for hour in (start_h, end_h):
        if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
            raise ValueError(bad)
    if start_h == end_h:
        raise ValueError(bad)
    return (int(start_h), int(end_h))


def _is_asleep(stamp: Any, window: Tuple[int, int]) -> bool:
    """按时间戳自己的挂钟小时判睡没睡；跨午夜时窗口是 [start,24)∪[0,end)。"""
    start_h, end_h = window
    hour = stamp.hour
    if start_h > end_h:
        return hour >= start_h or hour < end_h
    return start_h <= hour < end_h


def _step(value: float, stamp: Any, minutes: float, window: Tuple[int, int]) -> float:
    """一个切片：睡着按分钟回血，醒着按小时掉；每片都夹进 0–100。"""
    if _is_asleep(stamp, window):
        value += float(model.ENERGY_SLEEP_PER_MIN) * minutes
    else:
        value += float(model.ENERGY_AWAKE_PER_H) * (minutes / 60.0)
    return _clamp(value)


def _default_state(at: str) -> Dict[str, Any]:
    return {
        "version": model.VERSION,
        "value": float(model.ENERGY_WAKE_DEFAULT),
        "updated_at": at,
        "day": model.day_key(at),
        "deducted": 0.0,
        "last_burst_at": None,
    }


def _load(root: Any, at: str) -> Tuple[Dict[str, Any], bool]:
    """读状态；返回 (状态, 文件是否本来就在)。缺字段补默认，坏 JSON 回默认。"""
    data = store.read_json(_path(root), {})
    existed = bool(data)
    if not existed:
        return _default_state(at), False
    updated = data.get("updated_at")
    last_burst = data.get("last_burst_at")
    state = {
        "version": model.VERSION,
        "value": _clamp(_as_float(data.get("value"), model.ENERGY_WAKE_DEFAULT)),
        "updated_at": updated if isinstance(updated, str) and updated else at,
        "day": data.get("day")
        if isinstance(data.get("day"), str) and data.get("day")
        else model.day_key(at),
        "deducted": max(0.0, _as_float(data.get("deducted"), 0.0)),
        "last_burst_at": last_burst
        if isinstance(last_burst, str) and last_burst
        else None,
    }
    return state, True


def _settle(
    state: Dict[str, Any], at: str, window: Tuple[int, int]
) -> Dict[str, Any]:
    """把 updated_at → at 的 Δt 按 5 分钟切片积分，就地改 state，返回结算说明。

    - Δt < ENERGY_MIN_GAP_MIN 分钟：什么都不动；
    - 单次最多补算 ENERGY_MAX_SETTLE_H 小时，超出的不追；
    - 起点永远推到 at：下次只算新 Δt，这就是"重启不重复扣"的依据。
    """
    start_iso = str(state["updated_at"])
    end_ts = model.parse_ts(at)
    try:
        start_ts = model.parse_ts(start_iso)
    except ValueError:
        start_ts = end_ts
        start_iso = at
    gap_min = (end_ts - start_ts).total_seconds() / 60.0
    today = model.day_key(at)
    if state["day"] != today:
        state["deducted"] = 0.0
        state["day"] = today
    if gap_min < float(model.ENERGY_MIN_GAP_MIN):
        return {"from": start_iso, "to": start_iso, "hours": 0.0, "capped": False}

    capped = gap_min / 60.0 > float(model.ENERGY_MAX_SETTLE_H)
    budget = min(gap_min, float(model.ENERGY_MAX_SETTLE_H) * 60.0)
    value = float(state["value"])
    cursor = start_ts
    slice_min = float(model.ENERGY_SLICE_MIN)
    remaining = budget
    while remaining > 1e-9:
        take = min(slice_min, remaining)
        value = _step(value, cursor, take, window)
        cursor = cursor + timedelta(minutes=take)
        remaining -= take
    state["value"] = _clamp(value)
    state["updated_at"] = at
    return {
        "from": start_iso,
        "to": at,
        "hours": round(budget / 60.0, 2),
        "capped": capped,
    }


def _snapshot(state: Dict[str, Any], info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "value": round(float(state["value"]), 1),
        "tier": _tier(state["value"]),
        "settled_from": info["from"],
        "settled_to": info["to"],
        "hours": round(float(info["hours"]), 2),
        "capped": bool(info["capped"]),
    }


def _save(root: Any, state: Dict[str, Any]) -> None:
    store.write_json(_path(root), state)


def _dedupe_burst(at: str, event_id: Optional[str]) -> bool:
    """这次连发是不是重复的：同一 event_id 永远只算一次（不随时间失效）。

    表满 _SEEN_BURSTS_MAX 条就淘汰最久未见的；没给 id 的"同一分钟"由落盘的
    last_burst_at 判（_same_minute），这里只管幂等键。
    """
    if event_id is None:
        return False
    key = str(event_id)
    if key in _SEEN_BURSTS:
        return True
    if len(_SEEN_BURSTS) >= _SEEN_BURSTS_MAX:
        oldest = min(_SEEN_BURSTS, key=lambda k: _SEEN_BURSTS[k])
        _SEEN_BURSTS.pop(oldest, None)
    _SEEN_BURSTS[key] = model.parse_ts(at).timestamp()
    return False


def _same_minute(a: Optional[str], b: str) -> bool:
    if not isinstance(a, str) or not a:
        return False
    try:
        left = model.parse_ts(a)
        right = model.parse_ts(b)
    except ValueError:
        return False
    return left.replace(second=0, microsecond=0) == right.replace(
        second=0, microsecond=0
    )


def _result(state: Dict[str, Any], info: Dict[str, Any], cost: float, skipped: Optional[str]) -> Dict[str, Any]:
    out = _snapshot(state, info)
    out.update(
        {
            "ok": skipped is None,
            "cost": round(float(cost), 1),
            "deducted": round(float(state["deducted"]), 2),
            "skipped": skipped,
        }
    )
    return out


# ---- 对外 ----------------------------------------------------------------


def read(
    root: Any = None,
    now: Optional[str] = None,
    sleep_window: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    """读此刻的精力：从 updated_at 到 now 现算积分，然后写回（value + updated_at）。

    sleep_window 不传就用 DEFAULT_SLEEP_WINDOW；传了就是「这段时间算她醒着还是睡着」
    的档，本模块不自己判断日程。
    返回 {"value", "tier", "settled_from", "settled_to", "hours", "capped"}。
    """
    at = now if isinstance(now, str) and now else model.now_iso()
    window = _window(sleep_window)
    state, existed = _load(root, at)
    info = _settle(state, at, window)
    if info["hours"] > 0 or not existed:
        _save(root, state)
    return _snapshot(state, info)


def apply_burst(
    root: Any = None,
    now: Optional[str] = None,
    event_id: Optional[str] = None,
    sleep_window: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    """一次连发：−ENERGY_BURST_COST，一次连发只扣一次（同一 event_id / 同一分钟）。

    sleep_window 同上：结算那一段用的是注入的作息档，不是写死的钟点。
    """
    at = now if isinstance(now, str) and now else model.now_iso()
    window = _window(sleep_window)
    state, existed = _load(root, at)
    info = _settle(state, at, window)

    if _dedupe_burst(at, event_id) or _same_minute(state["last_burst_at"], at):
        if info["hours"] > 0 or not existed:
            _save(root, state)
        return _result(state, info, 0.0, "duplicate")

    room = float(model.ENERGY_DAILY_CAP) - float(state["deducted"])
    if room <= 0:
        if info["hours"] > 0 or not existed:
            _save(root, state)
        return _result(state, info, 0.0, "capped")

    cost = min(float(model.ENERGY_BURST_COST), room)
    state["value"] = _clamp(float(state["value"]) - cost)
    state["deducted"] = float(state["deducted"]) + cost
    state["last_burst_at"] = at
    _save(root, state)
    return _result(state, info, cost, None)


def apply_long_reply(
    chars: int,
    root: Any = None,
    now: Optional[str] = None,
    sleep_window: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    """一条长回复：chars > ENERGY_LONG_REPLY_CHARS 才 −ENERGY_LONG_REPLY_COST。

    sleep_window 同上：结算那一段用的是注入的作息档，不是写死的钟点。
    """
    at = now if isinstance(now, str) and now else model.now_iso()
    window = _window(sleep_window)
    state, existed = _load(root, at)
    info = _settle(state, at, window)
    try:
        length = int(chars)
    except (TypeError, ValueError):
        length = 0

    if length <= int(model.ENERGY_LONG_REPLY_CHARS):
        if info["hours"] > 0 or not existed:
            _save(root, state)
        return _result(state, info, 0.0, None)

    room = float(model.ENERGY_DAILY_CAP) - float(state["deducted"])
    if room <= 0:
        if info["hours"] > 0 or not existed:
            _save(root, state)
        return _result(state, info, 0.0, "capped")

    cost = min(float(model.ENERGY_LONG_REPLY_COST), room)
    state["value"] = _clamp(float(state["value"]) - cost)
    state["deducted"] = float(state["deducted"]) + cost
    _save(root, state)
    return _result(state, info, cost, None)


def overview(
    root: Any = None,
    now: Optional[str] = None,
    sleep_window: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    """面板用的总览：read 的全部字段 + 今天已经额外扣掉多少（deducted）。"""
    at = now if isinstance(now, str) and now else model.now_iso()
    window = _window(sleep_window)
    state, existed = _load(root, at)
    info = _settle(state, at, window)
    if info["hours"] > 0 or not existed:
        _save(root, state)
    out = _snapshot(state, info)
    out["deducted"] = round(float(state["deducted"]), 2)
    return out
