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
   **失败的那次不下账**：计划留 pending，行里写下这一次的夜间快照（第几次尝试、四步
   各自结果、哪几步没成功），下一 tick 只重跑没成功的那几步、其余沿用上次结果。
   首跑 1 次 + 最多重试 max_retries 次（limits.json 的 night.max_retries，默认 3），
   每次失败到下一次重试之间按 night.backoff_minutes 退避（累加：T → T+1 → T+6 → T+21），
   窗口右边界落进计划行的 night.next_retry_at；退避窗口内 plan.due() 不把那条算到点
   （"窗口内不算到点"只在 plan 里判一次，本文件不再判第二遍）。
   试满仍失败 → 进 never_ran 第五栏 + 一条 never_ran 审计，**不许静默 consume**。
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
import warnings
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import model, night, plan, proactive

STATE_FILE = "rhythm.json"
VERSION = 1
THREAD_NAME = "day-rhythm"
NIGHT_KIND = "night"
#: 主动开口那一类计划的 kind（到点要再判一次 engage，然后要么说、要么只记账）。
PROACTIVE_KIND = "proactive"
# 兜底：真正的取值在 limits.json 的 night.max_retries（首跑 1 次 + 重试这么多次）。
# 代码里只留这一份兜底，别处不许再写第二份阈值。
DEFAULT_MAX_RETRIES = 3
# 兜底同上：真正的取值在 limits.json 的 night.backoff_minutes（每次失败后等几分钟）。
DEFAULT_BACKOFF_MINUTES = (1, 5, 15)

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


def max_retries(config: Optional[Dict[str, Any]]) -> int:
    """夜最多重试几次：limits.json 的 night.max_retries，读不到就用兜底值。"""
    entry = (config or {}).get(NIGHT_KIND)
    entry = entry if isinstance(entry, dict) else {}
    try:
        return max(0, int(entry.get("max_retries", DEFAULT_MAX_RETRIES)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RETRIES


def backoff_minutes(config: Optional[Dict[str, Any]]) -> List[int]:
    """每次失败后等几分钟：limits.json 的 night.backoff_minutes，读不到就用兜底值。"""
    entry = (config or {}).get(NIGHT_KIND)
    entry = entry if isinstance(entry, dict) else {}
    raw = entry.get("backoff_minutes", list(DEFAULT_BACKOFF_MINUTES))
    out: List[int] = []
    for value in raw if isinstance(raw, (list, tuple)) else []:
        try:
            out.append(max(0, int(value)))
        except (TypeError, ValueError):
            continue
    return out or list(DEFAULT_BACKOFF_MINUTES)


def _night_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """夜跑完的摘要：四步各自 status + 状态计数 + failed + 落了几行。"""
    steps = result.get("steps") or {}
    tally = Counter(str(row.get("status")) for row in steps.values())
    return {"steps": {name: row.get("status") for name, row in steps.items()},
            "statuses": dict(tally),
            "failed": list(result.get("failed") or []),
            "written": int(result.get("written") or 0)}


def _snapshot(attempt: int, steps: Dict[str, Any], failed: List[str],
              next_retry_at: Optional[str] = None) -> Dict[str, Any]:
    """这一次尝试的夜间快照：下一次就靠它只重跑没成功的那几步、其余沿用结果。

    steps 里存的是那一步的**整份结果**（至少有 status / detail，外加各步自己的
    value / tier / proposals 之类）：沿用要真的原样，砍成两栏就等于重试时把上次
    的读数丢了。
    """
    return {"attempts": int(attempt),
            "steps": {name: dict(item) for name, item in steps.items()},
            "failed_steps": list(failed),
            # 退避窗口的右边界：plan.due() 认这个字段，所以窗口内不会被当成到点
            "next_retry_at": next_retry_at}


def _never_ran_reason(failed: List[str], attempt: int, steps: Dict[str, Any]) -> str:
    """never-ran 的理由：哪一步、试了几次、最后一次错在哪——一句话说清。"""
    detail = str((steps.get(failed[0]) or {}).get("detail") or "") if failed else ""
    reason = ("该跑没跑成：%s 在 %d 次尝试（首跑 1 + 重试 %d）里都没成功"
              % ("、".join(failed), attempt + 1, attempt))
    if detail:
        reason += "（最后一次：%s）" % detail
    return reason + "；不补跑、不静默 consume（审计见 night_log.jsonl 的 never_ran 行）"


def _settle_night(due_rows: List[Dict[str, Any]], at: str, *, root: Any = None,
                  brain: Any = None, heart_root: Any = None,
                  config: Optional[Dict[str, Any]] = None,
                  break_step: Optional[int] = None) -> Dict[str, Any]:
    """到点的夜里：过期的那条标 expired（**不跑**），当天的那条跑并按下场决定去留。

    一次尝试之后只有三种去向（都不许静默）：
    - 全部成功 → consume（带四步摘要）；
    - 有失败、还有重试额度（attempt < max_retries）→ **留 pending**，写下夜间快照，
      下一 tick 只重跑没成功的那几步；
    - 有失败、额度用完 → never_ran 第五栏 + 一条 never_ran 审计（不补跑、不假装跑过）。

    返回 {"executed": [...], "retried": [...], "never_ran": [...], "expired": [...],
          "skipped": "night_running"|None}。
    """
    limit = max_retries(config)
    backoff = backoff_minutes(config)
    out: Dict[str, Any] = {"executed": [], "retried": [], "never_ran": [],
                           "expired": [], "skipped": None}
    rows = [row for row in due_rows if row.get("kind") == NIGHT_KIND]
    if not rows:
        return out
    today = model.day_key(at)
    stale = [row for row in rows if str(row.get("day") or model.day_key(row["at"])) < today]
    for row in stale:
        # 过期优先：换日点已过的那条不补跑、也不写 never-ran——补跑结算的是别人的一天。
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
            snap = plan.night_of(row)
            tried = bool(snap)
            attempt = int(snap.get("attempts") or 0) + 1 if tried else 0
            # 快照说"没成功的那几步"就只重跑它们；空着就当没有快照、四步都跑一遍——
            # 宁可多跑一步，也不许凭一条空列表假装跑过。
            only = (list(snap.get("failed_steps") or []) or None) if tried else None
            carry = (snap.get("steps") or {}) if tried else {}
            result = night.run(at, root=root, brain=brain, heart_root=heart_root,
                               break_step=break_step, only=only, carry=carry,
                               attempt=attempt, backoff_minutes=backoff,
                               max_retries=limit)
            failed = list(result.get("failed") or [])
            if not failed:
                summary = _night_summary(result)
                plan.consume([row["id"]], root=root, now_ts=at, results={row["id"]: summary})
                out["executed"].append({"id": row["id"], "at": row["at"], "attempt": attempt,
                                        "ok": True, "summary": summary})
                continue
            snapshot = _snapshot(attempt, result.get("steps") or {}, failed,
                                 result.get("next_retry_at"))
            if attempt < limit:
                plan.note_night([row["id"]], root=root, now_ts=at,
                                nights={row["id"]: snapshot})
                out["retried"].append({"id": row["id"], "at": row["at"], "attempt": attempt,
                                       "ok": False, "failed_steps": failed,
                                       "next_attempt": attempt + 1,
                                       "next_retry_at": result.get("next_retry_at"),
                                       "max_retries": limit,
                                       "failed": _night_summary(result)})
                continue
            reason = _never_ran_reason(failed, attempt, result.get("steps") or {})
            plan.never_ran([row["id"]], root=root, now_ts=at,
                           reasons={row["id"]: reason}, nights={row["id"]: snapshot})
            night.audit_never_ran(at, root=root,
                                  day=str(row.get("day") or model.day_key(row["at"])),
                                  attempt=attempt, failed_steps=failed, reason=reason,
                                  next_retry_at=None)
            out["never_ran"].append({"id": row["id"], "at": row["at"], "attempt": attempt,
                                     "failed_steps": failed, "reason": reason})
    finally:
        NIGHT_LOCK.release()
    return out


# ---- tick（纯函数） --------------------------------------------------------


def tick(now_ts: Any = None, *, root: Any = None,
         config: Optional[Dict[str, Any]] = None, brain: Any = None,
         heart_root: Any = None, break_step: Optional[int] = None) -> Dict[str, Any]:
    """一次 tick：排当天（幂等）→ 数到点 → 夜与主动开口各自结账 → 再动念一次 → 算下一次 → 换日记账。

    "到夜按固定顺序调四步"就是这一条路：tick 自己排、自己执行、自己下账。
    brain / heart_root 是注入给 night.run 的那两个根（缺省由各器官走自己的默认根）。
    break_step 与 night.run 的同名参数同义，**只给验收用**（让第 N 步当场抛错，
    看失败的那次是不是留 pending、下一 tick 是不是只重跑那一步）。
    """
    cfg = config or model.load_limits()
    at = model.iso(now_ts) if now_ts is not None else model.now_iso()
    # root 给了而 heart_root 没给：按 <root>/heart 派生（与 api/day 的 _roots 同一条规矩）。
    # 动念要读心的念头与读数——忘了传心根就去读真实的心，测试会悄悄碰到实盘数据。
    if heart_root is None and root is not None:
        heart_root = Path(root) / "heart"
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
    nightly = _settle_night(due_rows, at, root=root, brain=brain, heart_root=heart_root,
                            config=cfg, break_step=break_step)
    # 到点的主动开口：夜跑完再轮它（夜是固定的那几件事，她想说话不是议程的一部分）。
    # 这里**不**把 limits.json 那份额配置传下去：主动开口的参数在 data/proactive.json，
    # 两份配置混着传就是两处真源。
    proactive_out = proactive.settle_due(due_rows, at, root=root, brain=brain,
                                        heart_root=heart_root)
    # 到点的跑完，再问一次"她想不想开口"。**硬灌的是时钟，驱动它的是由头**：consider 里
    # 没有由头就不动念、有 pending 计划就不叠（idle_only=True）、超每人每日上限就不排，
    # 而且它一次都不调核、一条都不发——每 tick 问一次不等于每 tick 说一次。
    # 动念坏了不许让节律停摆：错记进 state.last_error 并 WARN，tick 照常返回。
    considered: Dict[str, Any] = {}
    try:
        considered = proactive.consider(at, root=root, brain=brain, heart_root=heart_root,
                                        limits=cfg, idle_only=True)
    except Exception as exc:  # noqa: BLE001 - 动念是旁支，坏了要报出来而不是静默吞掉
        message = "proactive: %s: %s" % (type(exc).__name__, exc)
        considered = {"ok": False, "error": message}
        state["last_error"] = message
        _save_state(state, root)
        warnings.warn("日子·节律：这一 tick 的动念失败了（%s），tick 照常返回" % message,
                      RuntimeWarning, stacklevel=2)
    ledger = plan.load(root)   # 夜与主动开口都动过账了（consumed / expired），重读一遍再算
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
        # 到点的夜怎么处置了：executed（跑成了并 consume）/ retried（失败了，留 pending 等重试）/
        # never_ran（试满次数仍失败，进第五栏 + 一条审计）/ expired（过期不补）/ skipped（有人在跑）
        "night": nightly,
        # 到点的主动开口怎么处置了：rows 逐条（action 是 held/declined/empty/error/sent），
        # actions 是这一次的计数。它不调核就什么也不发——判 SILENT 的那条只记账
        "proactive": proactive_out,
        # 这一 tick 的动念：fired=False 就是没有由头（skipped=no-source / pending-plan），
        # 有由头才有 action（planned / held / quota / muted / blocked）
        "proactive_consider": considered,
        "next_at": ahead,
        # 没有待办就不是"没有下一次"，而是"下一次按节律再问一遍"：纯算术，不猜内容
        "next_tick_at": model.iso(model.parse_ts(at) + timedelta(seconds=every)),
        "counts": plan.counts(ledger),
        "plan_ids": plan.plan_ids(ledger),
        # 这一 tick 为"当天"排了什么：added 是新建的，duplicates 是本来就在（幂等）
        "ensured": {"day": scheduled["day"], "kinds": scheduled["kinds"],
                    # skipped：没有 at 的 kind（事件驱动的那一类），ensure 不替它编钟点
                    "skipped": list(scheduled.get("skipped") or []),
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
