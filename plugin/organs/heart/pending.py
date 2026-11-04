"""念头层：由已有状态派生「想要什么」，不自己长维度。

三个来源，全部只读别人的状态：
- 连续被冷落（events 账本里尾部连续 ignored >= ignore_threshold）→ wanted_praise；
- 疲劳低（energy.read 的 value <= model.ENERGY_LOW）→ wanted_rest；
- 六轴某轴 |offset| >= axis_threshold → wanted_axis（轴名照抄 offset 的轴，只取最大的那一轴）。
本模块**不新建维度**：没有第四条来源，也没有自己的轴表。

念头能做的事：进注入、当选一次主动开口的理由（attempts 计数）。
**念头只影响说话方式与要不要开口，永不进证据链、永不被当作发生过的事。**

settle 只由事件触发（嘴 / 脑投递）：本模块只把事件交给 mood.apply_event，
**绝不直接写心情那本落盘文件**——写别人的状态是越界，本模块不碰。

fade（TTL 到 / attempts 超限）时按 config.release 向对应轴轻微回吐（正偏移往回退、
负偏移往回涨），回吐幅度就写在本文件的 config 里，人可改，改文件即生效、不用重启。

为什么这些常数留在本文件、不进 model.py：release / ttl_min / max_attempts /
max_open / ignore_window_min / ignore_threshold / axis_threshold 是念头层的局部参数，
只被本模块读，只回答「念头活多久、最多长几个、fade 时回吐多少」——不是跨模块共享的
枚举或尺。model.py 只放跨器官共用的名字与数值，把这些搬进去会让人误以为别的器官
也该读它们。

时间一律读时现算：公开函数都收 now，缺省取 model.now_iso()；本模块不挂定时器、
不起线程、不排 cron——过期只在被读到的那一刻顺手标 faded。
本模块不 import 脑，也不提供任何把念头写进记忆 / 证据的入口。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from organs.heart import energy, events, model, mood, offset, store

PENDING_FILE = "pending.json"
VERSION = 1

# 三类念头：三个来源各对一个，kind 是闭合集合，不接受第四种。
KIND_PRAISE = "wanted_praise"
KIND_REST = "wanted_rest"
KIND_AXIS = "wanted_axis"
KINDS = (KIND_PRAISE, KIND_REST, KIND_AXIS)

STATE_OPEN = "open"
STATE_SETTLED = "settled"
STATE_FADED = "faded"
STATES = (STATE_OPEN, STATE_SETTLED, STATE_FADED)

SOURCE_IGNORED = "ignored-window"
SOURCE_ENERGY = "energy-low"
SOURCE_OFFSET = "offset-high"

# 回吐记进 offset 账本时用的来源标记（只标注，不改 offset 的判据）。
RELEASE_SOURCE = "pending-release"

# 本模块的局部参数：只给念头层用，故留在这里不进 model.py（理由见文件头）。
CONFIG_DEFAULTS: Dict[str, float] = {
    "release": 0.5,
    "ttl_min": 180,
    "max_attempts": 3,
    "max_open": 3,
    "ignore_window_min": 60,
    "ignore_threshold": 3,
    "axis_threshold": 1.5,
}

# settle 认的两种短叫法；其余键按「念头 id」或「轴名」解释。
SETTLE_ALIASES = {
    "praise": KIND_PRAISE,
    KIND_PRAISE: KIND_PRAISE,
    "rest": KIND_REST,
    KIND_REST: KIND_REST,
}

# created_at 坏掉时的排序锚：排到最后，不猜它比别人早。
_FALLBACK_TS = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---- 内部小工具 ----------------------------------------------------------


def _path(root: Any) -> Path:
    """念头文件路径：root 给了就用 root，没给就用 store.DATA_DIR。"""
    base = Path(root) if root is not None else store.DATA_DIR
    return base / PENDING_FILE


def _at(now: Optional[str]) -> str:
    """「现在」：调用方给了就用它，没给就是此刻。"""
    return now if isinstance(now, str) and now else model.now_iso()


def _empty() -> Dict[str, Any]:
    """空状态：config 默认值就在这里，第一次落盘即写进文件。"""
    return {"version": VERSION, "config": dict(CONFIG_DEFAULTS), "thoughts": []}


def _num(value: Any, default: Any) -> float:
    """转 float；坏值回默认（读入口不许炸调用方）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value: Any, default: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _stamp(value: Any) -> Optional[datetime]:
    """时间戳串 → aware datetime；空 / 坏值 → None（不当成过期）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return model.parse_ts(value)
    except ValueError:
        return None


def _text_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _config(data: Dict[str, Any]) -> Dict[str, Any]:
    """生效配置：文件里的 config 覆盖默认值，额外键原样保留，已知键转数值。"""
    cfg: Dict[str, Any] = dict(CONFIG_DEFAULTS)
    raw = data.get("config")
    if isinstance(raw, dict):
        cfg.update(raw)
        for key, default in CONFIG_DEFAULTS.items():
            cfg[key] = _num(cfg.get(key), default)
    return cfg


def _new_id(rows: List[Dict[str, Any]]) -> str:
    """短 id：p-xxxx，躲开已有 id。"""
    taken = {row.get("id") for row in rows}
    for _ in range(64):
        candidate = "p-" + uuid.uuid4().hex[:4]
        if candidate not in taken:
            return candidate
    return "p-" + uuid.uuid4().hex[:8]


def _load(root: Any) -> Dict[str, Any]:
    """读念头文件并归一化；坏 JSON / 坏行一律跳过，不炸调用方。"""
    data = store.read_json(_path(root), _empty())
    rows = data.get("thoughts")
    cleaned: List[Dict[str, Any]] = []
    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind")
        if kind not in KINDS:
            continue  # 认不出的念头不猜、不救
        state = raw.get("state")
        cleaned.append(
            {
                "id": _text_or_none(raw.get("id")) or _new_id(cleaned),
                "kind": kind,
                "axis": raw.get("axis") if isinstance(raw.get("axis"), str) and raw.get("axis") else None,
                "created_at": raw.get("created_at") if isinstance(raw.get("created_at"), str) else "",
                "expires_at": raw.get("expires_at") if isinstance(raw.get("expires_at"), str) else "",
                "attempts": max(0, _int(raw.get("attempts"), 0)),
                "state": state if state in STATES else STATE_OPEN,
                "settled_at": _text_or_none(raw.get("settled_at")),
                "faded_at": _text_or_none(raw.get("faded_at")),
                "source": raw.get("source") if isinstance(raw.get("source"), str) else "",
            }
        )
    version = data.get("version")
    return {
        "version": version if isinstance(version, int) else VERSION,
        "config": data.get("config") if isinstance(data.get("config"), dict) else {},
        "thoughts": cleaned,
    }


def _save(root: Any, data: Dict[str, Any]) -> None:
    """整文件覆盖写：config 一并落盘（release 就在那儿，人可改）。"""
    store.write_json(
        _path(root),
        {"version": VERSION, "config": _config(data), "thoughts": data["thoughts"]},
    )


def _open_rows(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [row for row in data["thoughts"] if row["state"] == STATE_OPEN]


def _created_key(row: Dict[str, Any]) -> datetime:
    stamp = _stamp(row.get("created_at"))
    return stamp if stamp is not None else _FALLBACK_TS


def _view(row: Dict[str, Any], at: str) -> Dict[str, Any]:
    """一条念头的对外读数：原字段 + 剩余 TTL（读时现算）+ 过期标记。"""
    out = dict(row)
    expires = _stamp(row.get("expires_at"))
    moment = _stamp(at)
    left: Optional[float] = None
    expired = False
    if expires is not None and moment is not None:
        left = round(max(0.0, (expires - moment).total_seconds() / 60.0), 1)
        expired = expires <= moment
    out["ttl_left_min"] = left
    out["expired"] = expired
    return out


# ---- 回吐 ---------------------------------------------------------------


def _release(axis: Optional[str], at: str, cfg: Dict[str, Any], root: Any) -> Optional[Dict[str, Any]]:
    """fade 时向对应轴回吐 config.release：正偏移往回退、负偏移往回涨。

    没有轴、release <= 0、轴上本来就是 0 → 一个字节都不动 offset。
    """
    amount = _num(cfg.get("release"), 0.0)
    if not axis or amount <= 0.0:
        return None
    row = (offset.overview(root=root, now=at).get("axes") or {}).get(axis) or {}
    value = _num(row.get("value"), 0.0)
    if abs(value) < 1e-9:
        return None
    delta = -amount if value > 0.0 else amount
    return offset.apply_daily(axis, delta, root=root, now=at, source=RELEASE_SOURCE)


def _expire(data: Dict[str, Any], at: str, root: Any) -> List[Dict[str, Any]]:
    """把 TTL 已到的 open 念头顺手标 faded（并回吐）；TTL 只在这里现算。"""
    cfg = _config(data)
    moment = _stamp(at)
    faded: List[Dict[str, Any]] = []
    if moment is None:
        return faded
    for row in data["thoughts"]:
        if row["state"] != STATE_OPEN:
            continue
        expires = _stamp(row.get("expires_at"))
        if expires is None or expires > moment:
            continue  # 坏 expires_at 不猜、不当过期，交给 attempts 上限兜底
        row["state"] = STATE_FADED
        row["faded_at"] = at
        faded.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "axis": row["axis"],
                "release": _release(row["axis"], at, cfg, root),
            }
        )
    return faded


# ---- 派生 ---------------------------------------------------------------


def _candidates(root: Any, at: str, cfg: Dict[str, Any]) -> List[Tuple[str, Optional[str], str]]:
    """现算此刻能长出的念头（只读别人的状态，不看本文件）。"""
    found: List[Tuple[str, Optional[str], str]] = []

    run = 0
    for row in reversed(events.recent(root=root, now=at, window_min=cfg["ignore_window_min"])):
        if row.get("kind") == events.KIND_IGNORED:
            run += 1
            continue
        break
    if run >= _int(cfg["ignore_threshold"], 3):
        found.append((KIND_PRAISE, None, SOURCE_IGNORED))

    try:
        tired = float(energy.read(root=root, now=at).get("value"))
    except (TypeError, ValueError):
        tired = float(model.ENERGY_MAX)
    if tired <= float(model.ENERGY_LOW):
        found.append((KIND_REST, None, SOURCE_ENERGY))

    best: Optional[Tuple[str, float]] = None
    for axis, row in (offset.overview(root=root, now=at).get("axes") or {}).items():
        try:
            size = abs(float(row.get("value")))
        except (AttributeError, TypeError, ValueError):
            continue
        if size >= _num(cfg["axis_threshold"], 1.5) and (best is None or size > best[1]):
            best = (axis, size)
    if best is not None:
        found.append((KIND_AXIS, best[0], SOURCE_OFFSET))

    return found


def _already_open(open_rows: List[Dict[str, Any]], kind: str, axis: Optional[str]) -> bool:
    """幂等闸：同 kind（轴念头按 kind+axis）已经有 open 的，就不再新建。"""
    for row in open_rows:
        if row["kind"] != kind:
            continue
        if kind != KIND_AXIS or row["axis"] == axis:
            return True
    return False


def _new_thought(
    kind: str, axis: Optional[str], source: str, cfg: Dict[str, Any], at: str,
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """造一条 open 念头；expires_at 只在这里写一次，之后读时现算。"""
    moment = _stamp(at) or datetime.now(timezone.utc)
    return {
        "id": _new_id(rows),
        "kind": kind,
        "axis": axis,
        "created_at": at,
        "expires_at": model.iso(moment + timedelta(minutes=_num(cfg["ttl_min"], 180.0))),
        "attempts": 0,
        "state": STATE_OPEN,
        "settled_at": None,
        "faded_at": None,
        "source": source,
    }


# ---- 对外 ---------------------------------------------------------------


def derive(root: Any = None, now: Optional[str] = None) -> Dict[str, Any]:
    """现算能长出哪些念头并落盘。

    三个来源见文件头；同一 kind（轴念头按 kind+axis）已有 open 的不再新建——
    重复调用幂等。open 数到 max_open 就不再收新念头，已 open 的按 created_at 保留
    最先的（先到先得，新来的被记进 dropped，不进文件）。
    返回 {"ok", "at", "config", "created"(ids), "dropped", "expired", "open"}。
    """
    at = _at(now)
    data = _load(root)
    cfg = _config(data)
    expired = _expire(data, at, root)
    open_rows = _open_rows(data)
    created: List[str] = []
    dropped: List[Dict[str, Any]] = []
    for kind, axis, source in _candidates(root, at, cfg):
        if _already_open(open_rows, kind, axis):
            continue
        if len(open_rows) >= _int(cfg["max_open"], 3):
            dropped.append(
                {"kind": kind, "axis": axis, "source": source, "reason": "max-open"}
            )
            continue
        row = _new_thought(kind, axis, source, cfg, at, data["thoughts"])
        data["thoughts"].append(row)
        open_rows.append(row)
        created.append(row["id"])
    _save(root, data)
    return {
        "ok": True,
        "at": at,
        "config": cfg,
        "created": created,
        "dropped": dropped,
        "expired": expired,
        "open": [_view(row, at) for row in open_rows],
    }


def open_thoughts(root: Any = None, now: Optional[str] = None) -> List[Dict[str, Any]]:
    """未过期的 open 念头；TTL 在这里现算，过期的顺手标 faded（含回吐）。"""
    at = _at(now)
    data = _load(root)
    expired = _expire(data, at, root)
    if expired or not _path(root).exists():
        _save(root, data)
    return [_view(row, at) for row in _open_rows(data)]


def _match(key: str, open_rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按 id / praise / rest / 轴名找要结算的那一条；多条同 kind 时取最先的。"""
    if not isinstance(key, str) or not key:
        return None
    ordered = sorted(open_rows, key=_created_key)
    for row in ordered:
        if row["id"] == key:
            return row
    kind = SETTLE_ALIASES.get(key)
    if kind is None and key in model.AXES:
        kind = KIND_AXIS
    if kind is None:
        return None
    for row in ordered:
        if row["kind"] != kind:
            continue
        if kind == KIND_AXIS and row["axis"] != key:
            continue
        return row
    return None


def settle(
    kind_or_id: str, root: Any = None, now: Optional[str] = None, who: str = "主人"
) -> Dict[str, Any]:
    """结算一条念头：只由事件触发（嘴 / 脑投递 praise / rest / 轴名）。

    praise → 标 settled 后调用 mood.apply_event("praise")；rest → 只标 settled，
    不投事件。本模块永远不碰心情的落盘文件。
    没有对应的 open 念头 → {"ok": False, "skipped": "no-open"}，状态一个字节不改
    （除非顺带标了已过期的念头）。
    """
    at = _at(now)
    data = _load(root)
    expired = _expire(data, at, root)
    target = _match(kind_or_id, _open_rows(data))
    if target is None:
        if expired or not _path(root).exists():
            _save(root, data)
        return {"ok": False, "skipped": "no-open", "key": kind_or_id, "at": at,
                "expired": expired}
    target["state"] = STATE_SETTLED
    target["settled_at"] = at
    _save(root, data)
    event = None
    if target["kind"] == KIND_PRAISE:
        event = mood.apply_event(model.EVENT_PRAISE, root=root, now=at, who=who)
    return {
        "ok": True,
        "id": target["id"],
        "kind": target["kind"],
        "axis": target["axis"],
        "state": target["state"],
        "settled_at": at,
        "key": kind_or_id,
        "event": event,
    }


def attempt(thought_id: str, root: Any = None, now: Optional[str] = None) -> Dict[str, Any]:
    """为一次主动开口 / 注入记一次尝试：attempts + 1。

    超过 config.max_attempts（第 max_attempts + 1 次）→ state=faded 并按 release
    回吐该轴。没有这条 open 念头 → {"ok": False, "skipped": "no-open"}。
    """
    at = _at(now)
    data = _load(root)
    cfg = _config(data)
    expired = _expire(data, at, root)
    target = None
    for row in _open_rows(data):
        if row["id"] == thought_id:
            target = row
            break
    if target is None:
        if expired or not _path(root).exists():
            _save(root, data)
        return {"ok": False, "skipped": "no-open", "id": thought_id, "at": at,
                "expired": expired}
    target["attempts"] = max(0, _int(target["attempts"], 0)) + 1
    faded = False
    released = None
    if target["attempts"] > _int(cfg["max_attempts"], 3):
        target["state"] = STATE_FADED
        target["faded_at"] = at
        faded = True
        released = _release(target["axis"], at, cfg, root)
    _save(root, data)
    return {
        "ok": True,
        "id": target["id"],
        "at": at,
        "attempts": target["attempts"],
        "max_attempts": _int(cfg["max_attempts"], 3),
        "state": target["state"],
        "faded": faded,
        "release": released,
    }


def overview(root: Any = None, now: Optional[str] = None) -> Dict[str, Any]:
    """面板 / 接口用的总览：config + 每个念头的剩余 TTL、attempts、state。

    纯读，不写盘：过期只在 open_thoughts / derive / settle / attempt 里落笔。
    """
    at = _at(now)
    data = _load(root)
    rows = [_view(row, at) for row in data["thoughts"]]
    open_now = [row for row in rows if row["state"] == STATE_OPEN and not row["expired"]]
    return {
        "version": data["version"],
        "at": at,
        "config": _config(data),
        "open": len(open_now),
        "thoughts": rows,
    }
