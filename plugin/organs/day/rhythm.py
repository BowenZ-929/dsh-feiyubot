"""节律：60 秒一次的 tick——只判断到没到点，外加一次纯算术的「下一次几点」。

三条判据：
1. **唯一的循环**。tick() 是纯函数（给了 now_ts 就完全确定，不读真实时钟）；线程只有
   这一个，默认关，只有 devserver --day 才开。宿主 cron、第二个定时器都不许出现，
   测试里直接调 tick()，不依赖线程。
2. tick 做四件事：为"当天"把 limits.json 的 kinds 排好（plan.ensure，幂等）、
   数到点（pending 里 at <= now 的）、**把到点的夜真跑并结账**、用算术算出下一次绝对
   时刻（pending 里最早一条 at > now；一条都没有就 now + tick_seconds）。
   到点这一段是闭环：调 night.run（四步固定顺序，每步自己兜错）→ 写夜日志 →
   把那条计划标 consumed 并带上四步状态摘要。不留给别人一条永远没人执行的死账。
   过期的那条（关机错过的夜）标 expired，**不补跑**——补跑结算的是别人的一天。
   单飞：进程内 NIGHT_LOCK 非阻塞抢，抢不到就 skipped="night_running"，同一晚不跑两遍。
   **不生成任何内容**：排的只是配置里写好的钟点，日程内容归 schedule，夜的四步归 night。
3. 换日只看 model.day_key 这一处：day_key(now) 与上次记下的不同 → rollovers + 1 并记下
   新的一天。所以 23:59 与次日 05:00 两次 tick 只换一次日，同一个 now 再跑不重复数。

状态（rhythm.json，不许悄悄加顶层键）：
    {"version": 1, "day": "YYYY-MM-DD"|"", "rollovers": 0, "ticks": 0,
     "last_tick_at": iso|"", "last_error": ""}
"""
from __future__ import annotations

import json
import os
import threading
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import model, night, plan

STATE_FILE = "rhythm.json"
VERSION = 1
THREAD_NAME = "day-rhythm"
NIGHT_KIND = "night"

# 夜只能一个人跑：tick 线程与手工 POST /day/tick 可能同时到点。
# 非阻塞抢锁，抢不到就跳过（night.skipped = "night_running"）——同一晚绝不跑两遍。
NIGHT_LOCK = threading.Lock()


# ---- 状态 -----------------------------------------------------------------


def state_path(root: Any = None) -> Path:
    return model.day_root(root) / STATE_FILE


def _empty() -> Dict[str, Any]:
    return {"version": VERSION, "day": "", "rollovers": 0, "ticks": 0,
            "last_tick_at": "", "last_error": ""}


def load_state(root: Any = None) -> Dict[str, Any]:
    """读状态；文件不在就是刚落地（第一次 tick 只记下今天，不算换日）。"""
    target = state_path(root)
    data = _empty()
    if not target.exists():
        return data
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return data
    if not isinstance(raw, dict):
        return data
    for key in ("day", "last_tick_at", "last_error"):
        if isinstance(raw.get(key), str):
            data[key] = raw[key]
    for key in ("rollovers", "ticks"):
        try:
            data[key] = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            data[key] = 0
    return data


def _save_state(data: Dict[str, Any], root: Any = None) -> Path:
    target = state_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(".tmp-%d-state" % os.getpid())
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(target))
    return target


# ---- 到点之后：夜真跑并结账 ------------------------------------------------


def _night_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """夜跑完的摘要：四步各自 status + 状态计数 + failed + 落了几行。"""
    steps = result.get("steps") or {}
    tally = Counter(str(row.get("status")) for row in steps.values())
    return {"steps": {name: row.get("status") for name, row in steps.items()},
            "statuses": dict(tally),
            "failed": list(result.get("failed") or []),
            "written": int(result.get("written") or 0)}


def _settle_night(due_rows: List[Dict[str, Any]], at: str, *, root: Any = None,
                  brain: Any = None, heart_root: Any = None) -> Dict[str, Any]:
    """到点的夜里：过期的那条标 expired（**不跑**），当天的那条真跑并 consume。

    返回 {"executed": [...], "expired": [...], "skipped": "night_running"|None}。
    """
    out: Dict[str, Any] = {"executed": [], "expired": [], "skipped": None}
    rows = [row for row in due_rows if row.get("kind") == NIGHT_KIND]
    if not rows:
        return out
    today = model.day_key(at)
    stale = [row for row in rows if str(row.get("day") or model.day_key(row["at"])) < today]
    for row in stale:
        gone = plan.expire(
            [row["id"]], root=root, now_ts=at,
            reason=("过期不补：这条属于 %s，现在已经是 %s（关机错过的夜不补跑，"
                    "补跑结算的是别人的一天）"
                    % (row.get("day") or model.day_key(row["at"]), today)))
        out["expired"].extend(gone["expired"])
    stale_ids = {row["id"] for row in stale}
    fresh = [row for row in rows if row["id"] not in stale_ids]
    if not fresh:
        return out
    if not NIGHT_LOCK.acquire(blocking=False):
        out["skipped"] = "night_running"
        return out
    try:
        for row in fresh:
            result = night.run(at, root=root, brain=brain, heart_root=heart_root)
            summary = _night_summary(result)
            plan.consume([row["id"]], root=root, now_ts=at, results={row["id"]: summary})
            out["executed"].append({"id": row["id"], "at": row["at"],
                                    "ok": bool(result.get("ok")), "summary": summary})
    finally:
        NIGHT_LOCK.release()
    return out


# ---- tick（纯函数） --------------------------------------------------------


def tick(now_ts: Any = None, *, root: Any = None,
         config: Optional[Dict[str, Any]] = None, brain: Any = None,
         heart_root: Any = None) -> Dict[str, Any]:
    """一次 tick：排当天（幂等）→ 数到点 → 到点的夜真跑并结账 → 算下一次 → 换日记账。

    "到夜按固定顺序调四步"就是这一条路：tick 自己排、自己执行、自己 consume。
    brain / heart_root 是注入给 night.run 的那两个根（缺省由各器官走自己的默认根）。
    """
    cfg = config or model.load_limits()
    at = model.iso(now_ts) if now_ts is not None else model.now_iso()
    day = model.day_key(at)
    state = load_state(root)
    rolled = bool(state["day"]) and state["day"] != day
    if rolled:
        state["rollovers"] += 1
    state["day"] = day
    state["last_tick_at"] = at
    state["ticks"] += 1
    _save_state(state, root)

    scheduled = plan.ensure(at, root=root, config=cfg)
    ledger = plan.load(root)
    due_rows = plan.due(at, root=root, data=ledger)
    nightly = _settle_night(due_rows, at, root=root, brain=brain, heart_root=heart_root)
    ledger = plan.load(root)   # 夜跑完账变了（consumed / expired），重读一遍再算
    ahead = plan.next_at(at, root=root, data=ledger)
    every = int(cfg["tick_seconds"])
    return {
        "ok": True,
        "now": at,
        "day": day,
        "rolled_over": rolled,
        "rollovers": state["rollovers"],
        "ticks": state["ticks"],
        "interval_seconds": every,
        "due": [row.get("id", "") for row in due_rows],
        "due_plans": due_rows,
        # 到点的夜怎么处置了：executed（真跑完并 consume）/ expired（过期不补）/ skipped（有人在跑）
        "night": nightly,
        "next_at": ahead,
        # 没有待办就不是"没有下一次"，而是"下一次按节律再问一遍"：纯算术，不猜内容
        "next_tick_at": model.iso(model.parse_ts(at) + timedelta(seconds=every)),
        "counts": plan.counts(ledger),
        "plan_ids": plan.plan_ids(ledger),
        # 这一 tick 为"当天"排了什么：added 是新建的，duplicates 是本来就在（幂等）
        "ensured": {"day": scheduled["day"], "kinds": scheduled["kinds"],
                    "added": [row["id"] for row in scheduled["added"]],
                    "duplicates": scheduled["duplicates"],
                    "blocked": scheduled["blocked"]},
    }


# ---- 唯一的那个线程 --------------------------------------------------------

_LOCK = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop: Optional[threading.Event] = None
_interval: float = 0.0
_root: Any = None
_brain: Any = None
_heart_root: Any = None
_last_error: str = ""


def status() -> Dict[str, Any]:
    """线程现状：跑没跑、间隔多少、上一次错在哪（错了不假装没事）。"""
    thread = _thread
    return {"running": bool(thread is not None and thread.is_alive()),
            "interval_seconds": _interval, "root": None if _root is None else str(_root),
            "brain": _brain is not None, "heart_root": None if _heart_root is None else str(_heart_root),
            "thread": THREAD_NAME, "last_error": _last_error}


def start(root: Any = None, interval: Optional[float] = None, brain: Any = None,
          heart_root: Any = None) -> Dict[str, Any]:
    """开节律线程（默认关，只有 --day 才走到这儿）；已经在跑就不起第二个。

    brain / heart_root 与 tick 的同名参数是一回事：线程是夜里真正动手的那个人，
    不带上它们，线程跑出来的夜第 1 步就只会记"脑未载入"。
    """
    global _thread, _stop, _interval, _root, _brain, _heart_root
    with _LOCK:
        if _thread is not None and _thread.is_alive():
            return {"ok": True, "started": False, "running": True, "interval_seconds": _interval,
                    "message": "节律线程已经在跑，不起第二个"}
        every = float(interval if interval is not None else model.load_limits()["tick_seconds"])
        stop_event = threading.Event()
        thread = threading.Thread(target=_loop, args=(stop_event, root, every, brain, heart_root),
                                  name=THREAD_NAME, daemon=True)
        _stop, _thread, _interval, _root = stop_event, thread, every, root
        _brain, _heart_root = brain, heart_root
        thread.start()
    return {"ok": True, "started": True, "running": True, "interval_seconds": every}


def stop(timeout: float = 5.0) -> Dict[str, Any]:
    """关节律线程：置停止位并等它自己走完当前这一觉（不 kill，不留半截）。"""
    global _thread, _stop, _interval, _root, _brain, _heart_root
    with _LOCK:
        thread, stop_event = _thread, _stop
        _thread, _stop, _interval, _root = None, None, 0.0, None
        _brain, _heart_root = None, None
    if stop_event is not None:
        stop_event.set()
    if thread is not None:
        thread.join(timeout)
        return {"ok": True, "running": thread.is_alive(), "stopped": not thread.is_alive()}
    return {"ok": True, "running": False, "stopped": True}


def _loop(stop_event: threading.Event, root: Any, every: float, brain: Any,
          heart_root: Any) -> None:
    """一个循环，一次一觉：错了一次记一行继续（一件坏事不许让节律停摆）。"""
    global _last_error
    while not stop_event.wait(every):
        try:
            tick(root=root, brain=brain, heart_root=heart_root)
            _last_error = ""
        except Exception as exc:  # noqa: BLE001 - 线程里的错不许无声无息
            _last_error = "%s: %s" % (type(exc).__name__, exc)
