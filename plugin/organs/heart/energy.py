"""精力：0–100，是时间的函数——醒着掉、睡着回；读的时候现算，不挂定时器。

判据先写在这里：model.py 里的数是默认值，不是验收线——要改节奏就改 model，
本文件不抄第二份数字，也不在逻辑里写 magic number。

本模块只有这一把尺：清醒 ENERGY_AWAKE_PER_H（−4/小时）与睡眠 ENERGY_SLEEP_PER_MIN
（按分钟回血），按 model.ENERGY_SLICE_MIN（5 分钟）切片积分。这把尺不许与 mood 的
3 小时 / 7 天、offset 的 6 小时 / 4 小时共用，也不抽成通用衰减函数递出去——只有本
模块自己的一段私有积分。

结算即落盘：read() 把 value 与 updated_at 一起写回，"重启不重复扣"就靠这一条，
下次只算从那之后的新 Δt。步进积分有步数上限 ENERGY_STEP_CAP（= 一整天 288 片）：
超出的整天用闭式折算结清（一天只算一次映射，逐天套用），所以"上次结算在 30 天前"
也算得完、且耗时可忽略。Δt 小于 ENERGY_MIN_GAP_MIN 分钟就不动，连文件都不碰。

夹断只为省算力，不改变结果：一天以内的事件按切片精确算；超出一天按整天闭式折算，
结果与逐片积分相同（写测试钉住）。

作息窗口（几点算睡、几点算醒）从外面注入，不在本文件里写死：DEFAULT_SLEEP_WINDOW
只是尚未启动实际休息生命周期时的回退。已有心的rest账时，按实际阶段及已保存的
硬撑/醒来截止分段结算；转换时先结清旧阶段，不能把硬撑按钟点算成回血。
本模块不读取虚构日程，也不自行生成睡眠事件。

去重按 event_id 的容量上限 FIFO，不设过期时间：本模块只持有一把时间尺（settle 那把），
不养第二个时钟。

档位（累 / 偏累 / 一般 / 充沛）是带迟滞的读数：值越线不等于档位变，判据看档位，
不看裸值。迟滞带宽 ENERGY_TIER_GAP 只给换档用，不参与任何时间结算；档位跟着状态
一起落盘（"tier" 那一栏），旧文件没有这一栏就按值现算一次补写，老数据的复算结果
一个字都不变。

ENERGY_MIN_GAP_MIN 那条管的是「值」的结算（不重复扣、不白写盘），不是「档」的持久化：
档位是状态，变了就必须落——只写 tier 这一栏，value / updated_at / deducted 一个字节
都不动；档位没变就一个字节都不写，短间隔"连文件都不碰"的老规矩照旧。否则同一时刻
连着读，每一读都从旧档起判，迟滞在连续读数下等于没有。

状态 schema（不许悄悄加字段）：
{"version": 1, "value": float, "updated_at": iso, "day": "YYYY-MM-DD",
 "deducted": float, "last_burst_at": iso|null, "tier": str|null}
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from organs.day import model as day_model
from organs.heart import model, store

ENERGY_FILE = "energy.json"

# 默认值写在这里；日子线接管后由它提供，心不自己去读日程。
DEFAULT_SLEEP_WINDOW: Tuple[int, int] = (
    int(model.ENERGY_SLEEP_START_H),
    int(model.ENERGY_SLEEP_END_H),
)

# 派生量（不是新常数）：一次补算最多走多少片 = ENERGY_MAX_SETTLE_H 小时 ÷ 每片
# ENERGY_SLICE_MIN 分钟。24 × 60 / 5 = 288——这个除法就是把"小时"换成"片"：
# 一整天 24 × 60 = 1440 分钟，除以 5 分钟一片，正好 288 片，所以它同时也是
# "一天多少片"，超出的部分按整天闭式折算。
ENERGY_STEP_CAP = int(
    float(model.ENERGY_MAX_SETTLE_H) * 60.0 / float(model.ENERGY_SLICE_MIN)
)

# 档位词：30 取自 model.ENERGY_LOW（"累"的门槛），50 / 75 是本模块自己的档界——
# 默认值，不是验收线。
_ENERGY_TIERS = ((float(model.ENERGY_LOW), "累"), (50.0, "偏累"), (75.0, "一般"))

# 迟滞带宽，3 点内不换档：单位是「点」，不是时间——它不参与结算，只决定换不换档。
# 进档看本档的上界，退档要跌过下界再让出这么多点，所以 30 附近来回抖也不会档位狂跳。
ENERGY_TIER_GAP = 3.0

# 进程内的连发去重：同一 event_id 只扣一次。只活这个进程（与道的"会话级覆盖"同一个
# 规矩）；落盘那一格 last_burst_at 管的是"同一分钟内重复调用"，重启后最多重扣一次，
# 而且仍受单日封顶约束。去重按 event_id 的容量上限 FIFO，不设过期时间：本模块只持有
# 一把时间尺（settle 那把），不养第二个时钟——幂等键的意义就是不随时间失效。
_SEEN_BURSTS: Dict[str, float] = {}
_SEEN_BURSTS_MAX = 64


# ---- 内部小工具 ----------------------------------------------------------


def _path(root: Any) -> Path:
    base = Path(root) if root is not None else store.default_root()
    return base / ENERGY_FILE


def _as_float(value: Any, default: float) -> float:
    try:
        if isinstance(value, bool) or value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, high: Optional[float] = None) -> float:
    top = float(model.ENERGY_MAX) if high is None else float(high)
    return min(top, max(float(model.ENERGY_MIN), value))


def _tier_words() -> Tuple[str, ...]:
    """四档由低到高：档界就是 _ENERGY_TIERS 的那三个数，不抄第二份。"""
    return tuple(word for _, word in _ENERGY_TIERS) + ("充沛",)


def _tier(value: float) -> str:
    """裸读：value 落在哪一档，不看旧档（无迟滞）。

    只给「旧文件没有 tier 栏」那一次补写用：老数据的复算结果必须与改动前逐字一致。
    """
    words = _tier_words()
    for index, (edge, _) in enumerate(_ENERGY_TIERS):
        if float(value) < edge:
            return words[index]
    return words[-1]


def _tier_hyst(current: Optional[str], value: float) -> str:
    """迟滞换档：给上一档和这次的值，返回这次该落的档（一次最多走一格）。

    档 index i：0=累、1=偏累、2=一般、3=充沛；每档的上界 b = [30, 50, 75, +inf]
    （b[3] 是 +inf，所以顶档没有"再上"）。于是：

    - 升档要 value >= b[i]；
    - 降档要 value <= b[i-1] - ENERGY_TIER_GAP；
    - 两个都不满足就保持原档。

    所以同一个值越线不等于换档：下界那 3 点是缓冲，值在里面来回抖，档位不动。
    current 不认识（旧文件没这一栏 / 写了乱七八糟的词）就按 value 现算一次，不带
    迟滞——没有上一档可继承时，答案就是裸读。
    """
    words = _tier_words()
    if current is None or str(current) not in words:
        return _tier(value)
    index = words.index(str(current))
    bounds = tuple(edge for edge, _ in _ENERGY_TIERS)
    # 拿"报出去的那个值"比：切片积分会积出 72.00000000000006 这种尾巴，读出来是 72.0，
    # 档位也得按 72.0 判，不然边界上会出现"显示 72.0 却还在上一档"。(_tier 不动：那是
    # 旧数据的复算路，必须与改动前逐字一致。)
    number = round(float(value), 1)
    if index < len(bounds) and number >= bounds[index]:
        return words[index + 1]
    if index > 0 and number <= bounds[index - 1] - ENERGY_TIER_GAP:
        return words[index - 1]
    return words[index]


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


def _cap(wake_cap: Any) -> float:
    """回血封顶：None 就是现有的满值 ENERGY_MAX（行为一字不变）；给了就只封睡着那一段。

    这是外面那张睡眠账的接线口——"连续两晚没睡够 → 起值下调 5"由调用方传进来，
    本模块不去算那张账。醒着的消耗与两次扣减不受它影响。
    """
    if wake_cap is None:
        return float(model.ENERGY_MAX)
    bad = "wake_cap 要传 %s–%s 之间的数（None = 满值 %s）；收到 %r" % (
        model.ENERGY_MIN,
        model.ENERGY_MAX,
        model.ENERGY_MAX,
        wake_cap,
    )
    if isinstance(wake_cap, bool) or not isinstance(wake_cap, (int, float)):
        raise ValueError(bad)
    cap = float(wake_cap)
    if not float(model.ENERGY_MIN) <= cap <= float(model.ENERGY_MAX):
        raise ValueError(bad)
    return cap


def _ceiling(stamp: Any, window: Tuple[int, int], cap: float) -> float:
    """这一片的上限：睡着是回血封顶 cap，醒着还是 ENERGY_MAX。"""
    if _is_asleep(stamp, window):
        return min(float(cap), float(model.ENERGY_MAX))
    return float(model.ENERGY_MAX)


def _is_asleep(stamp: Any, window: Tuple[int, int]) -> bool:
    """按时间戳自己的挂钟小时判睡没睡；跨午夜时窗口是 [start,24)∪[0,end)。"""
    start_h, end_h = window
    hour = stamp.hour
    if start_h > end_h:
        return hour >= start_h or hour < end_h
    return start_h <= hour < end_h


def _delta(stamp: Any, minutes: float, window: Tuple[int, int]) -> float:
    """一个切片的净增量（还没夹进 0–100）：睡着按分钟回血，醒着按小时掉。

    增量只跟切片的钟点、时长、作息窗口有关，与 value 无关——整天折算靠的就是这条。
    """
    if _is_asleep(stamp, window):
        return float(model.ENERGY_SLEEP_PER_MIN) * minutes
    return float(model.ENERGY_AWAKE_PER_H) * (minutes / 60.0)


def _step(
    value: float,
    stamp: Any,
    minutes: float,
    window: Tuple[int, int],
    cap: Optional[float] = None,
) -> float:
    """一个切片：睡着按分钟回血（到 cap 封顶），醒着按小时掉；每片都夹进 0–cap。"""
    top = _ceiling(
        stamp, window, float(model.ENERGY_MAX) if cap is None else float(cap)
    )
    return _clamp(value + _delta(stamp, minutes, window), top)


def _day_fold(
    cursor: Any, window: Tuple[int, int], cap: Optional[float] = None
) -> Tuple[float, float, float]:
    """一整天（ENERGY_STEP_CAP 片）的折算映射，写成 (offset, floor, ceiling)：
    v → min(ceiling, max(floor, v + offset))。

    每片都是"加一个增量再夹进 ENERGY_MIN–上限"（睡着那一段的上限是 cap），这种映射
    对复合封闭，288 片能合成
    一个映射；增量与 value 无关，只跟起始钟点与作息窗口有关，而起点前进整整一天后
    钟点不变，所以同一个映射可以逐天套用——一天只算一次，不是每天再走 288 片。
    """
    low = float(model.ENERGY_MIN)
    high = float(model.ENERGY_MAX)
    sleep_cap = high if cap is None else float(cap)
    slice_min = float(model.ENERGY_SLICE_MIN)
    offset = 0.0
    floor = low
    ceiling = high
    stamp = cursor
    for _ in range(ENERGY_STEP_CAP):
        delta = _delta(stamp, slice_min, window)
        offset += delta
        top = _ceiling(stamp, window, sleep_cap)
        floor = min(top, max(low, floor + delta))
        ceiling = min(top, max(low, ceiling + delta))
        stamp = stamp + timedelta(minutes=slice_min)
    return offset, floor, ceiling


def _default_state(at: str) -> Dict[str, Any]:
    return {
        "version": model.VERSION,
        "value": float(model.ENERGY_WAKE_DEFAULT),
        "updated_at": at,
        "day": day_model.day_key(at, cutover_hour=0),
        "deducted": 0.0,
        "last_burst_at": None,
        "tier": None,
    }


def _load(root: Any, at: str) -> Tuple[Dict[str, Any], bool]:
    """读状态；返回 (状态, 文件是否本来就在)。缺字段补默认，坏 JSON 回默认。"""
    data = store.read_json(_path(root), {})
    existed = bool(data)
    if not existed:
        return _default_state(at), False
    updated = data.get("updated_at")
    last_burst = data.get("last_burst_at")
    tier = data.get("tier")
    state = {
        "version": model.VERSION,
        "value": _clamp(_as_float(data.get("value"), model.ENERGY_WAKE_DEFAULT)),
        "updated_at": updated if isinstance(updated, str) and updated else at,
        "day": data.get("day")
        if isinstance(data.get("day"), str) and data.get("day")
        else day_model.day_key(at, cutover_hour=0),
        "deducted": max(0.0, _as_float(data.get("deducted"), 0.0)),
        "last_burst_at": last_burst
        if isinstance(last_burst, str) and last_burst
        else None,
        # 旧文件没有这一栏（或写了不认识的词）就是 None：本次按值现算一次补写，
        # 下一次起才走迟滞。
        "tier": tier if isinstance(tier, str) and tier in _tier_words() else None,
    }
    return state, True


def _settle(
    state: Dict[str, Any],
    at: str,
    window: Tuple[int, int],
    wake_cap: Any = None,
    *, root: Any = None, force: bool = False,
) -> Dict[str, Any]:
    """把 updated_at → at 的 Δt 按 ENERGY_SLICE_MIN 切片积分，就地改 state。

    - wake_cap：睡着那一段的回血封顶（None → ENERGY_MAX，行为不变）；醒着不受影响；
    - Δt < ENERGY_MIN_GAP_MIN 分钟：什么都不动；
    - 步进积分最多走 ENERGY_STEP_CAP 片（一整天）：再往后的整天用 _day_fold 闭式
      折算，不足一天的尾巴继续逐片算——夹断只为省算力，整段 Δt 都会结清；
    - 起点永远推到 at：下次只算新 Δt，这就是"重启不重复扣"的依据。
    """
    cap = _cap(wake_cap)  # 先验参数：坏值不许糊到"这次没动所以没看出来"
    start_iso = str(state["updated_at"])
    end_ts = model.parse_ts(at)
    try:
        start_ts = model.parse_ts(start_iso)
    except ValueError:
        start_ts = end_ts
        start_iso = at
    gap_min = (end_ts - start_ts).total_seconds() / 60.0
    today = day_model.day_key(at, cutover_hour=0)
    if state["day"] != today:
        state["deducted"] = 0.0
        state["day"] = today
    if gap_min <= 0 or (not force and gap_min < float(model.ENERGY_MIN_GAP_MIN)):
        return {
            "from": start_iso,
            "to": start_iso,
            "hours": 0.0,
            "capped": False,
            "steps": 0,
            "folded_days": 0,
        }

    from . import rest
    resting = rest.state(root)
    phase = resting['phase']
    if phase != 'untracked':
        asleep = phase in ('asleep', 'deep')
        boundaries = []
        if phase == 'holding' and resting['hold_until'] < resting['wake_at']:
            boundaries.append((model.parse_ts(resting['hold_until']), True))
        if phase != 'awake':
            boundaries.append((model.parse_ts(resting['wake_at']), False))
        # 精力可能先于日子tick被页面读取，仍须按已经保存的截止时间分段结算。
        cursor, value = start_ts, float(state['value'])
        for boundary, next_asleep in boundaries + [(end_ts, False)]:
            until = min(boundary, end_ts)
            minutes = max(0, (until-cursor).total_seconds()/60)
            if minutes:
                delta = float(model.ENERGY_SLEEP_PER_MIN)*minutes if asleep else float(model.ENERGY_AWAKE_PER_H)*minutes/60
                value = _clamp(value + delta, cap if asleep else None)
                cursor = until
            if boundary >= end_ts:
                break
            asleep = next_asleep
        state['value'] = value
        state['updated_at'] = at
        return {'from': start_iso, 'to': at, 'hours': round(gap_min/60, 2),
                'capped': False, 'steps': 1, 'folded_days': 0}

    slice_min = float(model.ENERGY_SLICE_MIN)
    day_min = ENERGY_STEP_CAP * slice_min  # 一整天 = ENERGY_STEP_CAP 片
    value = float(state["value"])
    # 旧状态可能写成 UTC；睡眠窗口按本次结算的钟面时间，不能沿用旧偏移。
    cursor = start_ts.astimezone(end_ts.tzinfo)
    remaining = gap_min
    steps = 0

    # 第一段：逐片积分，最多 ENERGY_STEP_CAP 片（一整天）
    while remaining > 1e-9 and steps < ENERGY_STEP_CAP:
        take = min(slice_min, remaining)
        value = _step(value, cursor, take, window, cap)
        cursor = cursor + timedelta(minutes=take)
        remaining -= take
        steps += 1

    # 第二段：剩下的整天按闭式折算——映射算一次，逐天套用
    folded_days = int((remaining + 1e-9) // day_min)
    if folded_days > 0:
        offset, floor, ceiling = _day_fold(cursor, window, cap)
        for _ in range(folded_days):
            value = min(ceiling, max(floor, value + offset))
        cursor = cursor + timedelta(days=folded_days)
        remaining -= folded_days * day_min

    # 第三段：不足一天的尾巴仍逐片算，一片不省
    while remaining > 1e-9:
        take = min(slice_min, remaining)
        value = _step(value, cursor, take, window, cap)
        cursor = cursor + timedelta(minutes=take)
        remaining -= take
        steps += 1

    state["value"] = _clamp(value)
    state["updated_at"] = at
    return {
        "from": start_iso,
        "to": at,
        "hours": round(gap_min / 60.0, 2),
        "capped": folded_days > 0,
        "steps": steps,
        "folded_days": folded_days,
    }


def _retier(state: Dict[str, Any]) -> str:
    """值定了之后才调：按迟滞把这次该落的档写回 state（并返回它）。"""
    state["tier"] = _tier_hyst(state.get("tier"), state["value"])
    return str(state["tier"])


def _snapshot(state: Dict[str, Any], info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "value": round(float(state["value"]), 1),
        "tier": str(state["tier"]),
        "settled_from": info["from"],
        "settled_to": info["to"],
        "hours": round(float(info["hours"]), 2),
        "capped": bool(info["capped"]),
        "steps": int(info["steps"]),
        "folded_days": int(info["folded_days"]),
    }


def _save(root: Any, state: Dict[str, Any], reason="时间结算", source=None, at=None) -> None:
    from . import changes
    previous = store.read_json(_path(root), {})
    store.write_json(_path(root), state)
    names = {"value": "精力", "tier": "精力档位", "deducted": "当日额外消耗"}
    changes.record(root, at=at or state['updated_at'], group='energy', reason=reason, source=source,
                   from_at=previous.get('updated_at'),
                   before={label: previous.get(key) for key, label in names.items()},
                   after={label: state[key] for key, label in names.items()})


def _save_tier(root: Any, tier: str, at: str) -> None:
    """只把 tier 这一栏落盘：value / updated_at / deducted 一个字节都不动。

    短间隔（Δ < ENERGY_MIN_GAP_MIN）不该结算、也不该重写值，但档位是状态：换了档就得
    落下，否则下一次读又从旧档起判，迟滞在连续读数下等于没有。没换档就什么都不写。
    """
    data = store.read_json(_path(root), {})
    if not data or data.get("tier") == tier:
        return
    from . import changes
    before = data.get('tier')
    data["tier"] = tier
    store.write_json(_path(root), data)
    changes.record(root, at=at, group='energy', reason='精力档位结算',
                   before={'精力档位': before}, after={'精力档位': tier})


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
    wake_cap: Optional[float] = None,
    *, force: bool = False,
) -> Dict[str, Any]:
    """读此刻的精力：从 updated_at 到 now 现算积分，然后写回（value + updated_at）。

    sleep_window 不传就用 DEFAULT_SLEEP_WINDOW；传了就是「这段时间算她醒着还是睡着」
    的档，本模块不自己判断日程。
    wake_cap 是睡眠回血的封顶（None → 满值 ENERGY_MAX，行为一字不变）：只封睡着的那
    一段，醒着的消耗与两次扣减一律不动。它是外面那张睡眠账的接线口，这里只接参数，
    不去算那张账、也不读它的数据源。
    档位带迟滞：值越线不等于档位变，判据看 tier，不看裸值。
    返回 {"value", "tier", "settled_from", "settled_to", "hours", "capped",
    "steps", "folded_days"}：steps 是这次真正走过的切片数，folded_days 是按整天
    闭式折算掉的天数（两者都只为把"省了多少"报出来，不影响 value）。
    """
    at = now if isinstance(now, str) and now else model.now_iso()
    window = _window(sleep_window)
    state, existed = _load(root, at)
    was = state["tier"]
    info = _settle(state, at, window, wake_cap, root=root, force=force)
    _retier(state)
    if info["hours"] > 0 or not existed or (force and info['to'] != info['from']):
        _save(root, state)
    elif was is not None and state["tier"] != was:
        # 换了档，但这段间隔不足 ENERGY_MIN_GAP_MIN：值不动，档位照样落下。
        # was 为 None 是"旧文件补写"而不是换档——那一次仍等第一次真结算才落，
        # 短间隔"一个字节都不碰"的老规矩不为它破。
        _save_tier(root, str(state["tier"]), at)
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
    info = _settle(state, at, window, root=root)
    _retier(state)

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
    _retier(state)  # 扣减改了值，档位再定一次（迟滞一次只走一格）
    _save(root, state, '时间结算与连续消息消耗', event_id, at=at)
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
    info = _settle(state, at, window, root=root)
    _retier(state)
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
    _retier(state)  # 扣减改了值，档位再定一次
    _save(root, state, '时间结算与长回复消耗', str(length) + '字', at=at)
    return _result(state, info, cost, None)


def overview(
    root: Any = None,
    now: Optional[str] = None,
    sleep_window: Optional[Tuple[int, int]] = None,
    wake_cap: Optional[float] = None,
) -> Dict[str, Any]:
    """面板用的总览：read 的全部字段 + 今天已经额外扣掉多少（deducted）。

    sleep_window / wake_cap 与 read 同一个口径，原样传透（回血封顶只管睡着那一段）。
    """
    at = now if isinstance(now, str) and now else model.now_iso()
    window = _window(sleep_window)
    state, existed = _load(root, at)
    was = state["tier"]
    info = _settle(state, at, window, wake_cap, root=root)
    _retier(state)
    if info["hours"] > 0 or not existed:
        _save(root, state)
    elif was is not None and state["tier"] != was:
        # 同 read：间隔不够结算，但换了档；旧文件补写（was 为 None）仍等真结算才落。
        _save_tier(root, str(state["tier"]), at)
    out = _snapshot(state, info)
    out["deducted"] = round(float(state["deducted"]), 2)
    return out
