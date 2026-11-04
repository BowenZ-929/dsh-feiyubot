"""日子线的底座：时间、换日、阈值——「今天算哪天」在本项目只有这一处实现。

判据（先写在这里，别处不许再判一次）：
- 换日点 04:00，**本地时区**：凌晨 4:00 之前的时刻归前一天。全项目只有 model.day_key()
  这一处判断「新的一天」；脑与心各自留着一份旧实现（这一波不许碰它们），
  日子线的新代码一律走这里。
- 换日点与三条闸门阈值（tick 间隔 / 同类型硬下限 / 每日上限）的默认值写在本文件的
  DEFAULTS 里，实际取值优先读 data/limits.json——改节奏改那份配置，不改代码。
- 时间一律可注入：对外函数都收 now_ts，不给才取本地当前时刻。测试不许依赖真实时钟。
- 落盘根三级：构造参数 > 环境变量 DAY_ROOT > 包内 data/（与家的 home_root 同一套规矩）。
- 计划一律存**绝对时刻**（带偏移的 ISO8601），不存「今天 9 点」这种相对说法：
  换日、补算、跨时区都不会把同一条计划算出两个时刻。

状态/配置文件名（盘上就这几个）：
    limits.json  人写的配置（本文件读，不写）
    plans.json   计划账（plan.py 写）
    rhythm.json  节律状态（rhythm.py 写）
    night_log.jsonl  夜日志（night.py 写，一行一次）
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Dict, Optional

ENV_ROOT = "DAY_ROOT"
LIMITS_FILE = "limits.json"
DATA_DIR = Path(__file__).resolve().parent / "data"

# 兜底默认值：盘上的 limits.json 读不到、写坏了，就按这一份跑（并如实报出来源）
DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "tick_seconds": 60,          # 节律线程的间隔
    "min_gap_minutes": 10,       # 同类型两条计划之间的硬下限
    "daily_cap": 12,             # 同类型每天最多几条
    "day_cutover_hour": 4,       # 本地时区的换日钟点
    # 自动排程：哪个 kind 每天几点排一条，全在 data/limits.json 的 kinds 里。
    # 兜底是空的——配置读不到就不自动排（宁可少排，也不在代码里另写一份钟点）。
    "kinds": {},
}
LIMIT_KEYS = tuple(DEFAULTS)

# limits.json 只在人改它的时候变，所以按 (路径, mtime) 缓存一次读盘结果
_LIMITS_CACHE: Dict[str, Any] = {"key": None, "values": None}


# ---- 时间 -----------------------------------------------------------------


def local_tz() -> tzinfo:
    """本地时区：问这台机器的时钟要，不写死偏移（换台机器就是那台的本地时区）。"""
    return datetime.now().astimezone().tzinfo


def parse_ts(text: Any) -> datetime:
    """ISO8601 → 带时区的 datetime；naive 按本地时区补，坏值抛 ValueError。

    "Z" 这种写法 Python 3.9 的 fromisoformat 不认，先换成 +00:00 再解。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("时间戳要一个非空字符串，收到 %r" % (text,))
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("时间戳无法解析：%r" % (text,)) from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=local_tz())
    return stamp


def _stamp(value: Any) -> datetime:
    """datetime 原样用，字符串走 parse_ts（两处对外函数共用这一个入口）。"""
    return value if isinstance(value, datetime) else parse_ts(value)


def iso(value: Any) -> str:
    """datetime 或 ISO 串 → 本地时区的绝对时刻串（秒级，同一时刻只有一个写法）。"""
    stamp = _stamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=local_tz())
    return stamp.astimezone(local_tz()).replace(microsecond=0).isoformat()


def now_iso() -> str:
    """此刻（本地时区）。只在这里问真实时钟；其余函数一律收注入的 now_ts。"""
    return iso(datetime.now(local_tz()))


def day_key(value: Any = None, cutover_hour: Optional[int] = None) -> str:
    """换日点前归昨天 → "YYYY-MM-DD"（本地时区）。本项目判断新的一天的唯一实现。"""
    hour = cutover_hour if cutover_hour is not None else limits()["day_cutover_hour"]
    stamp = _stamp(value) if value is not None else datetime.now(local_tz())
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=local_tz())
    return (stamp.astimezone(local_tz()) - timedelta(hours=int(hour))).date().isoformat()


# ---- 阈值 -----------------------------------------------------------------


def day_root(root: Any = None) -> Path:
    """日子线的数据根：构造参数 > DAY_ROOT > 包内 data/。"""
    if root is not None:
        return Path(root)
    env = os.environ.get(ENV_ROOT, "")
    if env:
        return Path(env)
    return DATA_DIR


def limits_file(root: Any = None) -> Path:
    """limits.json 的位置：给了目录就在那底下找，否则用包内 data/ 那份。"""
    return day_root(root) / LIMITS_FILE


def load_limits(root: Any = None) -> Dict[str, Any]:
    """读阈值配置：读不到/解析不了就整份兜底 DEFAULTS，不抛。

    只补 DEFAULTS 里有的键；配置里多出来的键原样留着（以后加阈值不用改这里）。
    """
    path = limits_file(root)
    values: Dict[str, Any] = dict(DEFAULTS)
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        key = (str(path), None)
    if _LIMITS_CACHE["key"] == key and _LIMITS_CACHE["values"] is not None:
        return dict(_LIMITS_CACHE["values"])
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = {}
    if isinstance(loaded, dict):
        values.update(loaded)
    numeric = ("tick_seconds", "min_gap_minutes", "daily_cap", "day_cutover_hour")
    for name in numeric:
        try:
            values[name] = int(values[name])
        except (TypeError, ValueError):
            values[name] = int(DEFAULTS[name])
    _LIMITS_CACHE["key"] = key
    _LIMITS_CACHE["values"] = dict(values)
    return values


def limits(root: Any = None) -> Dict[str, Any]:
    """load_limits 的短名字（读的地方多，写全名太长）。"""
    return load_limits(root)
