"""睡眠账：只记三样——上次入睡时刻、睡了多久、连续熬夜天数（相位漂移）。

为什么不引入腺苷：少一个物质就少一个半衰期；清醒时长是可算的——两次投递的时刻
相减就是了，不需要一个浓度模型去逼近它。所以本文件只持有一把尺（钟），不持有任何
清除/衰减常数，代码里也没有哪一类东西的符号名；上面这句解释只出现在本段话里一次。

账本形状（sleep.json；config 落盘、人可改、改文件即生效，不用重启）：
{"version": 1,
 "config": {"late_tolerance_min": 60, "enough_min": 360, "drift_per_late_night_h": 0.5,
            "drift_cap_h": 3.0, "wake_penalty_after_late_nights": 2, "wake_penalty": 5.0},
 "last_asleep_at": iso|null, "last_awake_at": iso|null, "last_duration_min": float|null,
 "nights": [{"asleep_at": iso, "awake_at": iso, "duration_min": float,
             "late": bool, "enough": bool}],
 "consecutive_late_nights": 0, "consecutive_short_nights": 0}
nights 只留最近 MAX_NIGHTS 条，超了裁最旧；坏行/坏时间戳跳过，不炸调用方。

两条判据（都在 record 落账时判，判完就写进那一夜）：
- 熬夜：入睡时刻比「这一夜的基准起点 + config.late_tolerance_min」还晚（够这个数就算晚）。
  "这一夜的起点"＝以醒来时刻回看 24 小时里最近的那次基准起点——22:30 睡是早、00:30 睡
  是晚 90 分钟、中午才睡是晚得更久，都由这一条判。锚点用**基准**作息，不用漂移后的窗口：
  漂移是晚睡的结果，拿它回头去判晚睡会把晚睡判成按时，账自己抵消，连续熬夜攒不起来。
- 睡够：duration_min >= config.enough_min。

三个派生量，全部读时现算、不落盘、不改账：
- drift_hours = 连续熬夜夜数 × drift_per_late_night_h，夹在 ±drift_cap_h 内。只做修正，
  不发明作息：窗口整体后移，起点还是人定的那一档。
- effective_window：基准窗口整体后移 drift 小时再取模 24。窗口只有整点（energy 认的就是
  两枚整数小时），所以漂移按四舍五入落成整点：0.5h 进 1h，1.5h 落 2h。
- wake_cap：连续没睡够 >= wake_penalty_after_late_nights 夜 → ENERGY_MAX − wake_penalty
  （= 95），否则 ENERGY_MAX（= 100）。这就是"连续两晚没睡够 → 次日起始值下调 5"的落点，
  energy 的睡眠回血封顶拿它。

"现在"一律读时现算：公开函数都收 now（缺省 model.now_iso()），没有后台进程、没有定时器。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from organs.heart import changes, energy, events, model, store

SLEEP_FILE = "sleep.json"
VERSION = 1

MAX_NIGHTS = 30  # 只留最近 30 夜，超了裁最旧
OVERVIEW_NIGHTS = 5  # overview 只看最近 5 夜

KIND_ASLEEP = events.KIND_ASLEEP
KIND_AWAKE = events.KIND_AWAKE
KINDS = (KIND_ASLEEP, KIND_AWAKE)

#: 出厂配置；改 sleep.json 里的 config 即生效，这里只是默认值。
CONFIG_DEFAULTS: Dict[str, Any] = {
    "late_tolerance_min": 60,
    "enough_min": 360,
    "drift_per_late_night_h": 0.5,
    "drift_cap_h": 3.0,
    # 名字沿用需求里的叫法：数的是**连续没睡够**的夜数，不是熬夜夜数。
    "wake_penalty_after_late_nights": 2,
    "wake_penalty": 5.0,
}


# ---- 内部小工具 ----------------------------------------------------------


def _path(root: Any) -> Path:
    """账本路径：root 给了就用 root，没给就用 store.default_root()（实盘）。"""
    base = Path(root) if root is not None else store.default_root()
    return base / SLEEP_FILE


def _at(now: Optional[str]) -> str:
    """「现在」：调用方给了就用它，没给就是此刻。"""
    return now if isinstance(now, str) and now else model.now_iso()


def _num(value: Any, default: Any) -> float:
    """转 float；坏值回默认（读入口不许炸调用方）。"""
    if isinstance(value, bool):
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value: Any, default: Any) -> int:
    if isinstance(value, bool):
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _stamp(value: Any) -> Optional[datetime]:
    """时间戳串 → aware datetime；空/坏值 → None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return model.parse_ts(value)
    except ValueError:
        return None


def _text_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _empty() -> Dict[str, Any]:
    """空账：config 默认值就在这儿，第一次落盘即写进文件。"""
    return {
        "version": VERSION,
        "config": dict(CONFIG_DEFAULTS),
        "last_asleep_at": None,
        "last_awake_at": None,
        "last_duration_min": None,
        "nights": [],
        "consecutive_late_nights": 0,
        "consecutive_short_nights": 0,
    }


def _config(data: Dict[str, Any]) -> Dict[str, Any]:
    """生效配置：文件里的 config 覆盖默认值，额外键原样保留，已知键按默认值的类型转。"""
    cfg: Dict[str, Any] = dict(CONFIG_DEFAULTS)
    raw = data.get("config")
    if isinstance(raw, dict):
        cfg.update(raw)
        for key, default in CONFIG_DEFAULTS.items():
            cfg[key] = _int(cfg.get(key), default) if isinstance(default, int) else _num(
                cfg.get(key), default
            )
    return cfg


def _night(raw: Any) -> Optional[Dict[str, Any]]:
    """归一化一夜；两个时刻读不出顺序就跳过（坏行不猜、不救）。"""
    if not isinstance(raw, dict):
        return None
    asleep = _stamp(raw.get("asleep_at"))
    awake = _stamp(raw.get("awake_at"))
    if asleep is None or awake is None or awake < asleep:
        return None
    result = {
        "asleep_at": model.iso(asleep),
        "awake_at": model.iso(awake),
        # 起止时间减去实际硬撑时长；旧账未记录硬撑时仍按原口径。
        "duration_min": (awake - asleep).total_seconds() / 60.0,
        "late": bool(raw.get("late")),
        "enough": bool(raw.get("enough")),
    }
    if raw.get('awake_minutes'):
        result['awake_minutes'] = max(0.0, float(raw['awake_minutes']))
        result['duration_min'] = max(0.0, result['duration_min'] - result['awake_minutes'])
    return result


def _load(root: Any) -> Dict[str, Any]:
    """读账本并归一化；坏 JSON / 坏行一律回默认值。"""
    data = store.read_json(_path(root), {})
    nights: List[Dict[str, Any]] = []
    raw_nights = data.get("nights")
    for raw in raw_nights if isinstance(raw_nights, list) else []:
        night = _night(raw)
        if night is not None:
            nights.append(night)
    return {
        "version": VERSION,
        "config": data.get("config") if isinstance(data.get("config"), dict) else {},
        "last_asleep_at": _text_or_none(data.get("last_asleep_at")),
        "last_awake_at": _text_or_none(data.get("last_awake_at")),
        "last_duration_min": None
        if data.get("last_duration_min") is None
        else _num(data.get("last_duration_min"), 0.0),
        "nights": nights[-MAX_NIGHTS:],
        "consecutive_late_nights": max(0, _int(data.get("consecutive_late_nights"), 0)),
        "consecutive_short_nights": max(0, _int(data.get("consecutive_short_nights"), 0)),
    }


def _save(root: Any, data: Dict[str, Any]) -> None:
    """整文件覆盖写：config 一并落盘（人可改的就是那一格）。"""
    store.write_json(
        _path(root),
        {
            "version": VERSION,
            "config": _config(data),
            "last_asleep_at": data.get("last_asleep_at"),
            "last_awake_at": data.get("last_awake_at"),
            "last_duration_min": data.get("last_duration_min"),
            "nights": data["nights"][-MAX_NIGHTS:],
            "consecutive_late_nights": data["consecutive_late_nights"],
            "consecutive_short_nights": data["consecutive_short_nights"],
        },
    )


def _base_window(base: Any) -> Tuple[int, int]:
    """验基准作息 (start_h, end_h)；不传就用 energy.DEFAULT_SLEEP_WINDOW。"""
    if base is None:
        return energy.DEFAULT_SLEEP_WINDOW
    bad = "作息窗口要传 (start_h, end_h)：两个 0–23 的整数小时，且 start != end；收到 %r" % (
        base,
    )
    if not isinstance(base, (tuple, list)) or len(base) != 2:
        raise ValueError(bad)
    start_h, end_h = base
    for hour in (start_h, end_h):
        if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
            raise ValueError(bad)
    if start_h == end_h:
        raise ValueError(bad)
    return (int(start_h), int(end_h))


def _drift(count: int, cfg: Dict[str, Any]) -> float:
    """连续熬夜夜数 × 每夜漂移，夹在 ±drift_cap_h 内；只做修正，不发明作息。"""
    cap = abs(_num(cfg.get("drift_cap_h"), CONFIG_DEFAULTS["drift_cap_h"]))
    raw = max(0, int(count)) * _num(
        cfg.get("drift_per_late_night_h"), CONFIG_DEFAULTS["drift_per_late_night_h"]
    )
    return min(cap, max(-cap, raw))


def _shift(base: Tuple[int, int], drift: float) -> Tuple[int, int]:
    """基准窗口整体后移 drift 小时并取模 24；漂移四舍五入落成整点（0.5h 进 1h）。"""
    shift = int(math.floor(float(drift) + 0.5))
    return ((base[0] + shift) % 24, (base[1] + shift) % 24)


def _late(asleep: datetime, awake: datetime, cfg: Dict[str, Any], base: Tuple[int, int]) -> bool:
    """这一夜算不算熬夜：入睡时刻晚于「这一夜的基准起点 + late_tolerance_min」。

    锚点＝以醒来时刻回看 24 小时里最近的那次基准起点（24 小时内只有一次，不会含糊），
    所以 22:30 睡是早（−30 分钟）、00:30 睡是晚 90 分钟、中午才睡是晚得更久。
    只认基准作息，不认漂移后的窗口——见模块开头那条。
    """
    anchor = awake.replace(hour=int(base[0]), minute=0, second=0, microsecond=0)
    if anchor > awake:
        anchor -= timedelta(days=1)
    delay_min = (asleep - anchor).total_seconds() / 60.0
    return delay_min >= _num(cfg.get("late_tolerance_min"), CONFIG_DEFAULTS["late_tolerance_min"])


def _wake_cap(short_nights: int, cfg: Dict[str, Any]) -> float:
    """连续没睡够 >= 门槛 → ENERGY_MAX − wake_penalty，否则 ENERGY_MAX。"""
    threshold = _int(
        cfg.get("wake_penalty_after_late_nights"),
        CONFIG_DEFAULTS["wake_penalty_after_late_nights"],
    )
    if int(short_nights) >= threshold:
        return float(model.ENERGY_MAX) - _num(cfg.get("wake_penalty"), CONFIG_DEFAULTS["wake_penalty"])
    return float(model.ENERGY_MAX)


def _awake_minutes(data: Dict[str, Any], now: Optional[str], root: Any) -> Optional[float]:
    """本次连续清醒时长；实际休息优先，旧账仅按已记录的睡醒事件判断。"""
    from . import rest
    current = rest.state(root)
    if current['phase'] in ('asleep', 'deep'):
        return 0.0
    if current['phase'] in ('awake', 'holding'):
        stamp = _stamp(current['since'])
    else:
        stamp = _stamp(data.get("last_awake_at"))
        asleep = _stamp(data.get("last_asleep_at"))
        if asleep is not None and (stamp is None or asleep > stamp):
            return 0.0
    if stamp is None:
        return None
    delta = (model.parse_ts(_at(now)) - stamp).total_seconds() / 60.0
    return max(0.0, delta)


# ---- 对外 ----------------------------------------------------------------


def record(kind: str, root: Any = None, now: Optional[str] = None, *, awake_minutes: float = 0.0) -> Dict[str, Any]:
    """落一笔睡眠事件：kind ∈ {"asleep", "awake"}。

    先由 events.deliver 投进事件账本（同一笔时刻，两本账不各取各的表），再更新本账：
    asleep 记 last_asleep_at；awake 配上一笔 asleep 算 duration_min 并写进 nights。
    实际休息生命周期仅在该夜结束时投awake，awake_minutes扣除期间硬撑，不把中断拆成两夜。
    上一笔还没醒就再来一笔 awake（同一夜重复投递）不重复记夜——一夜只落一条。
    返回 {"kind", "at", "night", "consecutive_late_nights", "consecutive_short_nights",
    "drift_hours", "wake_cap"}；night 是刚写进 nights 的那一夜，没配上就是 None。
    """
    if kind not in KINDS:
        raise ValueError("不认识的睡眠事件：%r（只认 %s）" % (kind, "、".join(KINDS)))
    at = events.deliver(kind, root=root, now=_at(now))["at"]
    stamp = model.parse_ts(at)

    data = _load(root)
    # 只记录实际写入的睡眠字段；窗口与回血上限仍由现有概览计算。
    labels = {"last_asleep_at": "上次入睡", "last_awake_at": "上次醒来",
              "last_duration_min": "上一觉时长（分钟）",
              "consecutive_late_nights": "连续晚睡（夜）",
              "consecutive_short_nights": "连续未睡够（夜）"}
    before = {label: data[key] for key, label in labels.items()}
    cfg = _config(data)
    base = energy.DEFAULT_SLEEP_WINDOW
    night: Optional[Dict[str, Any]] = None

    if kind == KIND_ASLEEP:
        data["last_asleep_at"] = at
    else:
        last_asleep = _stamp(data.get("last_asleep_at"))
        prior_awake = _stamp(data.get("last_awake_at"))
        fresh = last_asleep is not None and stamp >= last_asleep
        if fresh and prior_awake is not None and prior_awake > last_asleep:
            fresh = False  # 这一笔 asleep 已经配过醒了，不再记第二夜
        if fresh:
            late = _late(last_asleep, stamp, cfg, base)
            duration_min = max(0.0, (stamp - last_asleep).total_seconds() / 60.0 - awake_minutes)
            enough = duration_min >= _num(cfg.get("enough_min"), CONFIG_DEFAULTS["enough_min"])
            night = {
                "asleep_at": model.iso(last_asleep),
                "awake_at": at,
                "duration_min": duration_min,
                "late": late,
                "enough": enough,
            }
            if awake_minutes:
                night['awake_minutes'] = awake_minutes
            data["nights"].append(night)
            data["nights"] = data["nights"][-MAX_NIGHTS:]
            data["last_duration_min"] = duration_min
            data["consecutive_late_nights"] = 0 if not late else data["consecutive_late_nights"] + 1
            data["consecutive_short_nights"] = (
                0 if enough else data["consecutive_short_nights"] + 1
            )
        data["last_awake_at"] = at

    _save(root, data)
    changes.record(root, at=at, group="sleep", reason="入睡" if kind == KIND_ASLEEP else "醒来",
                   source=kind, before=before, after={label: data[key] for key, label in labels.items()})
    drift = _drift(data["consecutive_late_nights"], cfg)
    return {
        "kind": kind,
        "at": at,
        "night": night,
        "consecutive_late_nights": data["consecutive_late_nights"],
        "consecutive_short_nights": data["consecutive_short_nights"],
        "drift_hours": drift,
        "wake_cap": _wake_cap(data["consecutive_short_nights"], cfg),
    }


def drift_hours(root: Any = None, now: Optional[str] = None) -> float:
    """相位漂移几小时：连续熬夜夜数 × 每夜漂移，夹在 ±drift_cap_h 内。读时现算。"""
    data = _load(root)
    return _drift(data["consecutive_late_nights"], _config(data))


def effective_window(
    root: Any = None, now: Optional[str] = None, base: Any = None
) -> Tuple[int, int]:
    """生效作息 (start_h, end_h)：base（默认 (23, 8)）整体后移 drift 小时取模 24。读时现算。

    窗口只出整点（energy 认的就是两枚整数小时），所以 0.5h 的漂移在窗口上体现为 +1h——
    这只是取整，不是精度丢失：精确值在 drift_hours 里，窗口只是它落到的那个整点。
    """
    data = _load(root)
    return _shift(_base_window(base), _drift(data["consecutive_late_nights"], _config(data)))


def wake_cap(root: Any = None, now: Optional[str] = None) -> float:
    """睡眠回血的封顶：连续没睡够够数 → 100 − wake_penalty，否则 100。读时现算。"""
    data = _load(root)
    return _wake_cap(data["consecutive_short_nights"], _config(data))


def awake_minutes(root: Any = None, now: Optional[str] = None) -> Optional[float]:
    """本次连续清醒分钟数；睡着为 0，尚无状态或睡醒记录为 None。只读不推进状态。"""
    return _awake_minutes(_load(root), now, root)


def overview(root: Any = None, now: Optional[str] = None) -> Dict[str, Any]:
    """面板 / 接口用的总览：config、上一夜、两个连续计数、三个派生量、最近 5 夜。"""
    data = _load(root)
    cfg = _config(data)
    drift = _drift(data["consecutive_late_nights"], cfg)
    return {
        "config": cfg,
        "last_asleep_at": data["last_asleep_at"],
        "last_awake_at": data["last_awake_at"],
        "last_duration_min": data["last_duration_min"],
        "consecutive_late_nights": data["consecutive_late_nights"],
        "consecutive_short_nights": data["consecutive_short_nights"],
        "drift_hours": drift,
        "effective_window": _shift(energy.DEFAULT_SLEEP_WINDOW, drift),
        "wake_cap": _wake_cap(data["consecutive_short_nights"], cfg),
        "awake_minutes": _awake_minutes(data, now, root),
        "nights": data["nights"][-OVERVIEW_NIGHTS:],
    }
