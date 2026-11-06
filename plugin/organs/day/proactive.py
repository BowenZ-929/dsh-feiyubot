"""主动开口：判「她想不想说」（动念）→ 落一行账 → 交给计划层 → 到点再判一次。

判据（先写在这里，别处不许再判一次）：

1. **「想开口」的判定与理由码只在本文件这一处算**。三个驱动源各一个理由码：
   - `thought`：心的念头非空（heart.pending.open_thoughts）；
   - `unfinished`：脑的 today 里**最后一手是别人说的、她当天没接上**的那条；
   - `unscheduled`：今天还没定下要做什么（日程日志里没有当天的条目），
     **并且此刻在 unscheduled_window 里**（proactive.json，默认 9–12 点）：
     那是个常驻条件，窗外它不算由头——否则等于每天固定时间硬灌。
   三个同时成立时按 REASON_ORDER 取第一个：**一次动念只排一条计划**，理由要能说清是哪个。
2. **两条额度，超了都记 `quota`、都不排计划**，靠账上的 `quota.scope` 分清是哪一条：
   - 谁：limits.json 的 proactive.per_who_daily_cap——"同一个人一天别被她想太多次"；
   - 哪个由头：proactive.json 的 per_reason_daily_cap——"同一个由头一天只用几次"
     （数动念账今天这个 reason_code 的 planned / held 行，判 SILENT 的那次也算用掉）。
3. **先看静音**：已静音 → 账上 `muted`、**不排计划**（"安静一会儿"优先于"她想说"）。
4. **engage 的硬门照样管她**：用真实读数建 EngageInput（mood / energy 走心的公开读、
   muted 走 heart.proactive.is_muted、intent = proactive）判一次。判 SILENT **仍然生成
   计划**（人要看得出"她想说但因为夜里/双低没说"），但计划标 held、账上写 reason，
   **不调核、不发送**；到点执行时**再判一次**（过几个小时可能就不 SILENT 了），仍 SILENT
   就 consume 并记账，不重试。
5. **交给核之后只有四路**，都如实落账、都 consume：`declined`（她说这轮不想说）/
   `empty`（空产出，不重试）/ `error`（核打挂或解析不出，带 error_kind；主动开口
   **不做退避重试**——下一 tick 可以由动念重新产生，受每人每日上限约束）/ `sent`
   （**先** delivery.intake 落盘，**再** heart.proactive.sent 记一条 pending）。
6. **连 mute_after 次没人接** → 静音；**刚刚进入静音那一次**调一次
   heart.mood.apply_event("cold") 并落一行 `cold`，同一次静音只落一次（幂等看 muted_until）。
7. **谁**：脑的人名录里最近 lately_talked_hours 内说过话的人中熟稔最高者；没有就退回
   配置里的 owner。对方最近这么近还主动找过她 → 计划 priority 减
   lately_talked_downgrade（账上记降权前 / 后两个数）。
8. **本文件不改心、不改脑、不改核**：心的账只走它的公开口子（is_muted / sent / outcome /
   apply_event），脑只读（people_list / today_entries），核只调 organs.mind.complete。
   缺什么就报，不自己造。

动念账（proactive_log.jsonl，日子线根下，一行一次动念）：
    {"at", "stage", "reason_code", "who", "because", "priority",
     "engage": {"state", "reason"}, "action"} + 需要时带 priority_before / priority_after /
    plan_id / plan_blocked / delivery_id / error_kind / error / muted_until / streak
    action ∈ planned / held / quota / muted / blocked / declined / empty / error / sent / cold
    stage ∈ consider（动念那一刻）/ due（计划到点再判）/ outcome（结算回执）

时间一律可注入 now_ts；root 是日子线自己的根，heart_root 是心的根（两个不许混）。
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from organs.brain import store as brain_store
from organs.heart import energy as heart_energy
from organs.heart import engage as heart_engage
from organs.heart import model as heart_model
from organs.heart import mood as heart_mood
from organs.heart import pending as heart_pending
from organs.heart import proactive as heart_proactive

from . import model, plan, proactive_ask

KIND = "proactive"
CONFIG_FILE = "proactive.json"
LOG_FILE = "proactive_log.jsonl"
VERSION = 1

#: 三个驱动源的理由码（闭合集合：不许长出第四个）。
REASON_THOUGHT = "thought"
REASON_UNFINISHED = "unfinished"
REASON_UNSCHEDULED = "unscheduled"
REASONS = (REASON_THOUGHT, REASON_UNFINISHED, REASON_UNSCHEDULED)
#: 三个都成立时取谁：一次动念只排一条计划，理由要能说清是哪个。
REASON_ORDER = (REASON_THOUGHT, REASON_UNFINISHED, REASON_UNSCHEDULED)

ACT_PLANNED = "planned"
ACT_HELD = "held"
ACT_QUOTA = "quota"
ACT_MUTED = "muted"
ACT_BLOCKED = "blocked"
ACT_DECLINED = "declined"
ACT_EMPTY = "empty"
ACT_ERROR = "error"
ACT_SENT = "sent"
ACT_COLD = "cold"
ACTIONS = (ACT_PLANNED, ACT_HELD, ACT_QUOTA, ACT_MUTED, ACT_BLOCKED,
           ACT_DECLINED, ACT_EMPTY, ACT_ERROR, ACT_SENT, ACT_COLD)

STAGE_CONSIDER = "consider"
STAGE_DUE = "due"
STAGE_OUTCOME = "outcome"

#: engage 的理由码 → 动念账上的短码。只改一个名字（双低在账上叫 double-low，
#: 人读账时一眼看得出是"心与精力都低"），其余照抄 engage 的码，不另造一套语汇。
HOLD_CODES = {heart_model.REASON_DOUBLE_LOW: "double-low"}

#: owner 与提示词这两块的真源在 data/proactive.json；这里是兜底（文件读不到时还能跑）。
DEFAULTS: Dict[str, Any] = {
    "version": VERSION,
    "owner": "主人",
    "because_chars": 40,
    "plan_delay_minutes": 5,
    "base_priority": 50,
    "reason_priority": {REASON_THOUGHT: 60, REASON_UNFINISHED: 50, REASON_UNSCHEDULED: 40},
    "lately_talked_hours": 24,
    "lately_talked_downgrade": 20,
    # 每个理由码一天最多动念几次：不是每个人，是每个"由头"——同一个由头一天只用一次
    # （unscheduled 默认 1：那是"今天还没定下做什么"，定下来之前问一次就够了）。
    "per_reason_daily_cap": {REASON_THOUGHT: 3, REASON_UNFINISHED: 3, REASON_UNSCHEDULED: 1},
    # unscheduled 这条由头只在上午成立：它描述的是"今天还没定下来做什么"，
    # 一个常驻条件不该整天成立（否则等于每天固定时间硬灌三条，那就不是由头了）。
    # 本地时区的整点，左闭右开；from > to 表示跨零点。
    "unscheduled_window": {"from": 9, "to": 12},
}
#: 每人每天上限的兜底：真源是 limits.json 的 proactive.per_who_daily_cap
#: （与 plan 的闸门同一份配置文件），代码里只留这一份兜底。
DEFAULT_PER_WHO_DAILY_CAP = 3
#: overview 默认给账尾几行。
LOG_SHOWN = 20


class ProactiveError(ValueError):
    """参数坏了（比如 outcome 的 result 不认识）：调用方要看见，由 api 层译成 400。"""


# ---- 小工具 ---------------------------------------------------------------


def config_path(root: Any = None) -> Path:
    """参数文件：注入的根里有一份就用它，否则读包内 data/proactive.json。"""
    own = model.day_root(root) / CONFIG_FILE
    return own if own.exists() else model.DATA_DIR / CONFIG_FILE


def load_config(root: Any = None, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读参数：整份缺就兜底 DEFAULTS；坏值按 DEFAULTS 那一项修，不抛。"""
    values = dict(DEFAULTS)
    if isinstance(config, dict) and config:
        values.update(config)
    else:
        try:
            loaded = json.loads(config_path(root).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            values.update(loaded)
    for name in ("because_chars", "plan_delay_minutes", "base_priority",
                 "lately_talked_hours", "lately_talked_downgrade"):
        try:
            values[name] = float(values[name])
        except (TypeError, ValueError):
            values[name] = float(DEFAULTS[name])
    if not isinstance(values.get("reason_priority"), dict):
        values["reason_priority"] = dict(DEFAULTS["reason_priority"])
    if not isinstance(values.get("per_reason_daily_cap"), dict):
        values["per_reason_daily_cap"] = dict(DEFAULTS["per_reason_daily_cap"])
    values["owner"] = str(values.get("owner") or DEFAULTS["owner"])
    return values


def per_reason_cap(config: Optional[Dict[str, Any]], reason_code: str) -> int:
    """这个理由码一天最多动念几次（proactive.json 的 per_reason_daily_cap）。

    没写这一条的理由码按 1 算（不是无限）：新长出来的由头要先被人看一眼，
    不许默认就能一天用三次。0 是合法值——等于"这条由头这一版不用"。
    """
    cfg = config or load_config()
    table = cfg.get("per_reason_daily_cap")
    table = table if isinstance(table, dict) else DEFAULTS["per_reason_daily_cap"]
    try:
        return max(0, int(table.get(reason_code, 1)))
    except (TypeError, ValueError):
        return 1


def unscheduled_window(config: Optional[Dict[str, Any]]) -> Tuple[int, int]:
    """unscheduled 那条例外的时间窗（本地时区整点，左闭右开）；坏值退回兜底。"""
    cfg = config or load_config()
    raw = cfg.get("unscheduled_window")
    raw = raw if isinstance(raw, dict) else DEFAULTS["unscheduled_window"]
    try:
        start, end = int(raw.get("from")), int(raw.get("to"))
    except (TypeError, ValueError):
        raw = DEFAULTS["unscheduled_window"]
        start, end = int(raw["from"]), int(raw["to"])
    return start, end


def in_unscheduled_window(now_ts: Any, config: Optional[Dict[str, Any]] = None) -> bool:
    """这一刻在不在 unscheduled 的窗里：窗外那个"今天还没定"的常驻条件不算由头。"""
    start, end = unscheduled_window(config)
    hour = model.parse_ts(now_ts).hour
    if start == end:
        return False        # 空窗：这条由头这一版不成立
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def per_who_cap(root: Any = None, limits: Optional[Dict[str, Any]] = None) -> int:
    """同一个 who 一天最多几条主动开口计划：limits.json 的 proactive.per_who_daily_cap。

    与 limits.json 里 night.max_retries 同一套摆法（顶层一节放这一类自己的参数）；
    kinds.proactive 那一栏里写同一个键也认（两份写法都在这个文件里说明，先顶层后 kinds）。
    """
    cfg = limits if isinstance(limits, dict) and limits else model.load_limits(root)
    section = cfg.get(KIND) if isinstance(cfg.get(KIND), dict) else {}
    kinds = cfg.get("kinds") if isinstance(cfg.get("kinds"), dict) else {}
    entry = kinds.get(KIND) if isinstance(kinds.get(KIND), dict) else {}
    for source in (section, entry):
        if "per_who_daily_cap" not in source:
            continue
        try:
            return max(1, int(source["per_who_daily_cap"]))
        except (TypeError, ValueError):
            break
    return DEFAULT_PER_WHO_DAILY_CAP


def log_path(root: Any = None) -> Path:
    """动念账：日子线数据根下的 proactive_log.jsonl（一行一次动念）。"""
    return model.day_root(root) / LOG_FILE


def _now(now_ts: Any = None) -> str:
    return model.iso(now_ts) if now_ts is not None else model.now_iso()


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _hours_between(earlier: Any, later: Any) -> Optional[float]:
    """两个时刻差几小时；任何一个读不出来就 None（当作"没有这个时刻"）。"""
    try:
        return (model.parse_ts(later) - model.parse_ts(earlier)).total_seconds() / 3600.0
    except ValueError:
        return None


def _clip(text: Any, chars: Any) -> str:
    """由头取真话前 N 字（N 在配置里）：账是给人看的，不整段抄。"""
    body = " ".join(str(text if text is not None else "").split())
    try:
        limit = max(1, int(chars))
    except (TypeError, ValueError):
        limit = int(DEFAULTS["because_chars"])
    return body if len(body) <= limit else body[:limit]


def append_log(root: Any, row: Dict[str, Any]) -> Path:
    """追加一行动念账：先写同目录临时件再 append 整行，崩了不会留半行。"""
    target = log_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    tmp = target.with_name(".%s.tmp-%d-%s" % (target.name, os.getpid(), secrets.token_hex(4)))
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        with open(target, "ab") as handle:
            handle.write(tmp.read_bytes())
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return target


def log_lines(root: Any = None) -> List[Dict[str, Any]]:
    """动念账的每一行（坏行原样跳过，不因为一行坏了读不了整份）。"""
    target = log_path(root)
    if not target.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return rows
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def log_tail(root: Any = None, limit: int = LOG_SHOWN) -> List[Dict[str, Any]]:
    """账尾几行（GET /day/proactive 用）；limit <= 0 就是全部。"""
    rows = log_lines(root)
    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = LOG_SHOWN
    return rows[-count:] if count > 0 else rows

# ---- 谁（她该去找谁） ------------------------------------------------------


def _people(brain: Any, at: str) -> List[Dict[str, Any]]:
    """脑的人名录（只读）；没载入脑就是空的——退回 owner，不凭空造一个人。"""
    if brain is None:
        return []
    try:
        return list(brain.people_list(at) or [])
    except Exception:  # noqa: BLE001 - 脑读不出来不该让动念整条断掉
        return []


def pick_who(now_ts: Any = None, *, root: Any = None, brain: Any = None,
             config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """她该去找谁：名录里最近 lately_talked_hours 内说过话的人中熟稔最高者；没有就 owner。

    返回 {"who", "source": "recent-talk"|"owner", "familiarity", "last_interaction"}。
    "最近说过话"这个窗口与降权用同一把尺（lately_talked_hours）：最近找过她的那个人，
    也是我这轮该不该先开口的参照。
    """
    cfg = config or load_config(root)
    at = _now(now_ts)
    hours = _num(cfg.get("lately_talked_hours"), 24.0)
    best: Optional[Dict[str, Any]] = None
    for view in _people(brain, at):
        if not isinstance(view, dict):
            continue
        last = view.get("last_interaction")
        age = _hours_between(last, at) if last else None
        if age is None or age < 0 or age > hours:
            continue
        who = str(view.get("id") or view.get("display") or "").strip()
        if not who:
            continue
        familiar = _num(view.get("familiarity"), 0.0)
        if best is None or familiar > best["familiarity"]:
            best = {"who": who, "familiarity": familiar, "last_interaction": str(last)}
    if best is None:
        return {"who": str(cfg.get("owner") or DEFAULTS["owner"]), "source": "owner",
                "familiarity": 0.0, "last_interaction": ""}
    return {"who": best["who"], "source": "recent-talk", "familiarity": best["familiarity"],
            "last_interaction": best["last_interaction"]}


def _today(brain: Any, at: str) -> List[Dict[str, Any]]:
    """脑的 today 在役条目（只读）；没载入脑就是空的。"""
    if brain is None:
        return []
    try:
        return list(brain.today_entries(at) or [])
    except Exception:  # noqa: BLE001 - 脑读不出来不该让动念整条断掉
        return []


def _lately_talked(who: str, at: str, *, brain: Any = None,
                   config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """对方最近有没有主动找过她：从脑的 today 里现算（不落任何"冷度"状态）。

    返回 {"lately_talked": bool, "downgrade": int, "hours": float, "last_at": str}。
    """
    cfg = config or load_config()
    hours = _num(cfg.get("lately_talked_hours"), 24.0)
    downgrade = int(_num(cfg.get("lately_talked_downgrade"), 0.0))
    last_at = ""
    for entry in _today(brain, at):
        if str(entry.get("who") or "") != who:
            continue
        if str(entry.get("side") or brain_store.SIDE_OTHER) == brain_store.SIDE_SELF:
            continue
        stamp = str(entry.get("last_ts") or entry.get("ts") or "")
        age = _hours_between(stamp, at) if stamp else None
        if age is None or age < 0 or age > hours:
            continue
        if stamp > last_at:
            last_at = stamp
    return {"lately_talked": bool(last_at), "downgrade": downgrade if last_at else 0,
            "hours": hours, "last_at": last_at}


# ---- 三个驱动源 -----------------------------------------------------------


def _unfinished(brain: Any, at: str) -> Optional[Dict[str, Any]]:
    """未了的事：today 里最后一手是别人说的（她当天没接上）的那条。"""
    entries = _today(brain, at)
    if not entries:
        return None
    last = entries[-1]
    if str(last.get("side") or brain_store.SIDE_OTHER) == brain_store.SIDE_SELF:
        return None
    text = str(last.get("text") or last.get("quote") or "").strip()
    if not text:
        return None
    return {"who": str(last.get("who") or ""), "text": text,
            "at": str(last.get("last_ts") or last.get("ts") or "")}


def _scheduled_today(root: Any, at: str) -> bool:
    """今天定下要做什么了没有：日程日志里有当天的条目就算定了。

    读不出来（日程配置坏了）就**当作已定**：宁可这一轮不开口，也不许凭空编一个
    "今天还没定"的由头。
    """
    from . import schedule      # 延迟导入：日程那边还在长，别让日子线的导入顺序被它绑住
    try:
        return bool(schedule.log_lines(root, day=model.day_key(at)))
    except Exception:  # noqa: BLE001
        return True


def sources(now_ts: Any = None, *, root: Any = None, heart_root: Any = None,
            brain: Any = None, config: Optional[Dict[str, Any]] = None
            ) -> List[Dict[str, Any]]:
    """此刻有哪些"想开口"的由头（按 REASON_ORDER 排），每个带理由码与一句真话。

    这是**唯一**一处判"她想不想开口"的地方：三块读数各自只有一条判据，
    别处（节律、HTTP 层）不许再判一遍。
    """
    cfg = config or load_config(root)
    at = _now(now_ts)
    chars = cfg.get("because_chars")
    found: Dict[str, Dict[str, Any]] = {}

    thoughts = heart_pending.open_thoughts(root=heart_root, now=at)
    if thoughts:
        top = sorted(thoughts, key=lambda row: str(row.get("created_at") or ""))[-1]
        because = "念头 %s" % (top.get("kind") or "")
        if top.get("axis"):
            because += "（%s 那一轴）" % top["axis"]
        if top.get("source"):
            because += "，来源 %s" % top["source"]
        found[REASON_THOUGHT] = {"reason_code": REASON_THOUGHT,
                                 "because": _clip(because, chars),
                                 "thought_id": str(top.get("id") or ""), "who": ""}

    talk = _unfinished(brain, at)
    if talk is not None:
        found[REASON_UNFINISHED] = {"reason_code": REASON_UNFINISHED,
                                    "because": _clip(talk["text"], chars),
                                    "thought_id": "", "who": talk["who"]}

    if not _scheduled_today(root, at) and in_unscheduled_window(at, cfg):
        found[REASON_UNSCHEDULED] = {
            "reason_code": REASON_UNSCHEDULED,
            "because": _clip("今天还没定下要做什么（日程日志里没有 %s 的条目）"
                             % model.day_key(at), chars),
            "thought_id": "", "who": ""}

    return [found[code] for code in REASON_ORDER if code in found]


# ---- 真实读数 → engage 的一次判决 ------------------------------------------


def _insulted_today(heart_root: Any, at: str) -> bool:
    """当天有没有被凶过：事件账本里从换日点算起有没有 harsh（现算，不落状态）。"""
    from organs.heart import events as heart_events
    try:
        cutover = int(model.load_limits()["day_cutover_hour"])
        day = model.day_key(at, cutover)
        start = model.parse_ts("%sT00:00:00" % day) + timedelta(hours=cutover)
        span = max(0.0, (model.parse_ts(at) - start).total_seconds() / 60.0)
        rows = heart_events.recent(heart_root, at, window_min=span,
                                   kinds=(heart_events.KIND_HARSH,))
    except (ValueError, TypeError, KeyError):
        return False
    return bool(rows)


def readings(now_ts: Any = None, *, root: Any = None, heart_root: Any = None,
             brain: Any = None, who: str = "", config: Optional[Dict[str, Any]] = None
             ) -> Dict[str, Any]:
    """动念时手上握着的那几个数：mood / energy 走心的公开读，muted 走它的静音读数。

    EngageInput 直接建（不走 from_effective）：effective_state 要一张卡，动念不该因为
    "卡读不出来"就整条断掉；而且 mood_block 的 now 就是 mood.read 的 now——同一个数，
    不必为它多要一份合成块。config 收下不用（读数没有一项来自 proactive.json）。
    """
    at = _now(now_ts)
    mood_read = heart_mood.read(heart_root, at)
    energy_read = heart_energy.read(heart_root, at)
    try:
        energy_value = float(energy_read.get("value"))
    except (TypeError, ValueError):
        energy_value = 0.0
    familiarity = 0.0
    for view in _people(brain, at):
        if isinstance(view, dict) and str(view.get("id") or "") == who:
            familiarity = _num(view.get("familiarity"), 0.0)
            break
    today_count = len(_today(brain, at))
    muted = heart_proactive.is_muted(heart_root, at)
    window = heart_engage.window_signal(heart_root, at)
    inp = heart_engage.EngageInput(
        mood_now=float(mood_read.get("now") or 0.0),
        energy=energy_value,
        familiarity=familiarity,
        today_count=today_count,
        intent=heart_model.INTENT_PROACTIVE,
        who=who,
        now=at,
        insulted_today=_insulted_today(heart_root, at),
        window=window,
        muted=muted,
    )
    return {"engage": heart_engage.decide(inp), "mood": mood_read, "energy": energy_read,
            "familiarity": familiarity, "today_count": today_count, "muted": muted,
            "window": window}


def hold_code(engage_read: Dict[str, Any]) -> str:
    """engage 的主因 → 动念账上的短码（双低叫 double-low，其余照抄它的码）。"""
    reason = str((engage_read or {}).get("reason") or "")
    return HOLD_CODES.get(reason, reason)


def _engage_block(engage_read: Dict[str, Any]) -> Dict[str, str]:
    """账上的 engage 那一块：态 + 短码（人读账只要这两样）。"""
    return {"state": str((engage_read or {}).get("state") or ""),
            "reason": hold_code(engage_read)}


# ---- 计划 -----------------------------------------------------------------


def _plan_at(at: str, cfg: Dict[str, Any]) -> str:
    """计划几点到点：动念时刻 + plan_delay_minutes（配置里那个数，不是写死的钟点）。"""
    delay = _num(cfg.get("plan_delay_minutes"), 0.0)
    return model.iso(model.parse_ts(at) + timedelta(minutes=delay))


def _plans_today_for(who: str, at: str, root: Any = None) -> int:
    """这个人今天已经有几条 proactive 计划（pending / consumed / expired / never_ran 都算）。"""
    day = model.day_key(at)
    count = 0
    try:
        data = plan.load(root)
    except plan.PlanError:
        return 0
    for column in ("pending", "consumed", "expired", "never_ran"):
        for row in data[column]:
            if row.get("kind") != KIND or str(row.get("who") or "") != who:
                continue
            if str(row.get("day") or model.day_key(row["at"])) == day:
                count += 1
    return count


def _reasons_today(reason_code: str, at: str, root: Any = None) -> int:
    """这个理由码今天已经动念过几次：读动念账里 action 是 planned / held 的那几行。

    数"由头被用掉的次数"，不是数计划：判 SILENT 时计划仍然生成（held），那一次也算
    这条由头用掉了——否则夜里那几次动念会白记。
    """
    day = model.day_key(at)
    count = 0
    for row in log_lines(root):
        if str(row.get("reason_code") or "") != reason_code:
            continue
        if str(row.get("action") or "") not in (ACT_PLANNED, ACT_HELD):
            continue
        try:
            if model.day_key(str(row.get("at") or "")) != day:
                continue
        except ValueError:
            continue
        count += 1
    return count


def _pending_proactive(root: Any = None) -> Optional[Dict[str, Any]]:
    """pending 里还有没有一条 proactive 计划（有就说明"想说"这件事已经在路上了）。"""
    try:
        rows = plan.load(root)["pending"]
    except plan.PlanError:
        return None
    for row in rows:
        if row.get("kind") == KIND:
            return row
    return None


def _priority(cfg: Dict[str, Any], reason_code: str) -> int:
    table = cfg.get("reason_priority") if isinstance(cfg.get("reason_priority"), dict) else {}
    try:
        return int(table.get(reason_code, cfg.get("base_priority")))
    except (TypeError, ValueError):
        return int(DEFAULTS["base_priority"])


def _row(at: str, stage: str, reason_code: str, who: str, because: str, priority: int,
         engage_read: Dict[str, Any], action: str, **extra: Any) -> Dict[str, Any]:
    """一行动念账：必填那七样先摆好，其余按需附上（没有的键不写，账要干净）。"""
    row: Dict[str, Any] = {"at": at, "stage": stage, "reason_code": reason_code,
                           "who": who, "because": because, "priority": int(priority),
                           "engage": _engage_block(engage_read), "action": action}
    for key, value in extra.items():
        # 空值一律不写：账要干净（空字符串、空字典、None 都是"这一栏没有"，不是"有但为空"）
        if value is None or value == "" or (isinstance(value, dict) and not value):
            continue
        row[key] = value
    return row



# ---- 动念（唯一一次判定 + 落一行账） ---------------------------------------


def consider(now_ts: Any = None, *, root: Any = None, heart_root: Any = None,
             brain: Any = None, config: Optional[Dict[str, Any]] = None,
             limits: Optional[Dict[str, Any]] = None, idle_only: bool = False
             ) -> Dict[str, Any]:
    """手工/事件驱动地评估一次"她想不想开口"：最多落一行账、最多排一条计划。

    次序钉死（先静音、再额度、再 engage）：安静一会儿优先于她想说，额度优先于她自己
    以为的"现在正好"。判 SILENT 照旧生成计划，只是标 held——人要看得出
    "她想说，但因为夜里/双低没说"。
    """
    at = _now(now_ts)
    cfg = config or load_config(root)
    fired = sources(at, root=root, heart_root=heart_root, brain=brain, config=cfg)
    if not fired:
        return {"ok": True, "at": at, "fired": False, "skipped": "no-source",
                "sources": [], "row": None, "plan": None}
    if idle_only and _pending_proactive(root) is not None:
        return {"ok": True, "at": at, "fired": False, "skipped": "pending-plan",
                "sources": [item["reason_code"] for item in fired], "row": None, "plan": None}

    top = fired[0]
    picked = pick_who(at, root=root, brain=brain, config=cfg)
    who = picked["who"]
    talks = _lately_talked(who, at, brain=brain, config=cfg)
    before = _priority(cfg, top["reason_code"])
    after = max(0, before - int(talks["downgrade"]))
    read = readings(at, root=root, heart_root=heart_root, brain=brain, who=who, config=cfg)
    engage_read = read["engage"]
    code = hold_code(engage_read)
    muted = read["muted"]
    cap = per_who_cap(root, limits)
    used = _plans_today_for(who, at, root)
    reason_cap = per_reason_cap(cfg, top["reason_code"])
    reason_used = _reasons_today(top["reason_code"], at, root)

    # 次序：静音 > 两条额度（谁 / 哪个由头）> engage 的硬门 > 排计划。
    # 两条额度都叫 quota，靠 scope 分开——"被哪条额度挡的"要能在账上读出来。
    quota: Dict[str, Any] = {}
    if muted.get("muted") is True:
        action = ACT_MUTED
    elif used >= cap:
        action = ACT_QUOTA
        quota = {"scope": "who", "used": used, "cap": cap}
    elif reason_used >= reason_cap:
        action = ACT_QUOTA
        quota = {"scope": "reason", "reason_code": top["reason_code"],
                 "used": reason_used, "cap": reason_cap}
    elif str(engage_read.get("state")) == heart_model.STATE_SILENT:
        action = ACT_HELD
    else:
        action = ACT_PLANNED

    planned: Optional[Dict[str, Any]] = None
    blocked = ""
    if action in (ACT_PLANNED, ACT_HELD):
        out = plan.add([{"kind": KIND, "at": _plan_at(at, cfg), "note": top["because"],
                         "priority": after, "who": who, "held": action == ACT_HELD,
                         "held_reason": code if action == ACT_HELD else "",
                         "reason_code": top["reason_code"],
                         "thought_id": top.get("thought_id") or ""}],
                       root=root, now_ts=at, config=limits or model.load_limits(root))
        if out["added"]:
            planned = out["added"][0]
        else:
            # 闸门拦下了（min_gap / 保险丝）：动念是真的，计划没生成——账上不许说"planned"。
            action = ACT_BLOCKED
            blocked = str((out["blocked"][0].get("reason") if out["blocked"] else "duplicate"))

    row = _row(at, STAGE_CONSIDER, top["reason_code"], who, top["because"], after,
               engage_read, action,
               priority_before=before, priority_after=after,
               lately_talked=talks["last_at"] if talks["lately_talked"] else "",
               plan_id=(planned or {}).get("id", ""), plan_blocked=blocked,
               muted_until=muted.get("muted_until") if muted.get("muted") else "",
               streak=muted.get("streak") if muted.get("muted") else "",
               quota=quota)
    append_log(root, row)
    return {"ok": True, "at": at, "fired": True, "sources": [item["reason_code"] for item in fired],
            "reason_code": top["reason_code"], "action": action,
            "who": who, "who_source": picked["source"],
            "because": top["because"], "priority": after,
            "priority_before": before, "priority_after": after,
            "lately_talked": talks["lately_talked"], "downgrade": talks["downgrade"],
            "engage": _engage_block(engage_read), "muted": muted,
            "quota": {"used": used, "cap": cap, "remaining": max(0, cap - used),
                      "scope": quota.get("scope", ""), "blocked_by": quota,
                      "reason": {"code": top["reason_code"], "used": reason_used,
                                 "cap": reason_cap}},
            "row": row, "plan": planned}


# ---- 到点：再判一次，然后要么说、要么记账 -----------------------------------


def _is_pending(pid: str, root: Any = None) -> bool:
    """这条计划还在 pending 里吗（到点执行只许跑一次：已经 consumed 的不许再跑一遍）。"""
    try:
        rows = plan.load(root)["pending"]
    except plan.PlanError:
        return True     # 账坏了不许当成"没有这一条"——那样会把该跑的吞掉
    return any(str(row.get("id")) == pid for row in rows)


def _consume(row: Dict[str, Any], at: str, root: Any, summary: Dict[str, Any]) -> None:
    """把这条计划划进 consumed（带上这一次的结果摘要）。"""
    plan.consume([str(row.get("id"))], root=root, now_ts=at,
                 results={str(row.get("id")): summary})


def run_due(row: Dict[str, Any], now_ts: Any = None, *, root: Any = None,
            heart_root: Any = None, brain: Any = None,
            config: Optional[Dict[str, Any]] = None, complete: Any = None,
            env: Optional[Mapping[str, str]] = None, transport: Any = None
            ) -> Dict[str, Any]:
    """到点的 proactive 计划：**再判一次** engage，然后要么交给核、要么只记账。

    只有四条出口：still-SILENT → consume + held；她这轮不想说 → consume + declined；
    空产出 → consume + empty（不重试）；核打挂/解析不出 → consume + error（不重试）；
    成功 → intake 先落盘，再 heart.proactive.sent 记一条 pending，最后 consume + sent。
    """
    at = _now(now_ts)
    cfg = config or load_config(root)
    pid = str(row.get("id") or "")
    if pid and not _is_pending(pid, root):
        # 已经跑过（consumed / expired / never_ran）：不重复执行、不重复落账。
        return {"ok": False, "id": pid, "at": at, "action": "", "skipped": "not-pending"}
    who = str(row.get("who") or "") or pick_who(at, root=root, brain=brain, config=cfg)["who"]
    reason_code = str(row.get("reason_code") or "")
    because = str(row.get("note") or "")
    priority = int(_num(row.get("priority"), 0.0))
    thought_id = str(row.get("thought_id") or "")
    read = readings(at, root=root, heart_root=heart_root, brain=brain, who=who, config=cfg)
    engage_read = read["engage"]
    code = hold_code(engage_read)

    if str(engage_read.get("state")) == heart_model.STATE_SILENT:
        _consume(row, at, root, {"action": ACT_HELD, "reason": code, "sent": False})
        ledger = _row(at, STAGE_DUE, reason_code, who, because, priority, engage_read,
                      ACT_HELD, plan_id=pid, reason=code)
        append_log(root, ledger)
        return {"ok": True, "id": pid, "at": at, "action": ACT_HELD, "reason": code,
                "sent": False, "row": ledger}

    ctx = {"who": who, "because": because, "tone": str(row.get("tone") or ""),
           "now": at, "root": root}
    outcome = proactive_ask.ask(ctx, complete=complete, config=cfg, env=env,
                               transport=transport)

    if not outcome.get("ok"):
        kind = str(outcome.get("error_kind") or "")
        error = str(outcome.get("error") or "")
        _consume(row, at, root, {"action": ACT_ERROR, "error_kind": kind, "error": error,
                                 "sent": False})
        ledger = _row(at, STAGE_DUE, reason_code, who, because, priority, engage_read,
                      ACT_ERROR, plan_id=pid, error_kind=kind, error=error)
        append_log(root, ledger)
        return {"ok": False, "id": pid, "at": at, "action": ACT_ERROR, "error_kind": kind,
                "error": error, "sent": False, "row": ledger}

    if outcome.get("send") is not True:
        _consume(row, at, root, {"action": ACT_DECLINED, "sent": False})
        ledger = _row(at, STAGE_DUE, reason_code, who, because, priority, engage_read,
                      ACT_DECLINED, plan_id=pid)
        append_log(root, ledger)
        return {"ok": True, "id": pid, "at": at, "action": ACT_DECLINED, "sent": False,
                "row": ledger}

    text = str(outcome.get("text") or "").strip()
    if not text:
        _consume(row, at, root, {"action": ACT_EMPTY, "sent": False,
                                 "chunks": len(outcome.get("chunks") or [])})
        ledger = _row(at, STAGE_DUE, reason_code, who, because, priority, engage_read,
                      ACT_EMPTY, plan_id=pid)
        append_log(root, ledger)
        return {"ok": True, "id": pid, "at": at, "action": ACT_EMPTY, "sent": False,
                "row": ledger}

    from .delivery import intake as delivery_intake
    queued = delivery_intake(text, to=who, now_ts=at, root=root)
    if not queued.get("ok"):
        error = str(queued.get("message") or "")
        _consume(row, at, root, {"action": ACT_ERROR, "error_kind": "delivery",
                                 "error": error, "sent": False})
        ledger = _row(at, STAGE_DUE, reason_code, who, because, priority, engage_read,
                      ACT_ERROR, plan_id=pid, error_kind="delivery", error=error)
        append_log(root, ledger)
        return {"ok": False, "id": pid, "at": at, "action": ACT_ERROR,
                "error_kind": "delivery", "error": error, "sent": False, "row": ledger}

    # 先落盘（上面那一步），再记一条"她主动开口了"的 pending：顺序颠倒是谎报。
    sent = heart_proactive.sent(heart_root, at)
    if thought_id:
        heart_pending.attempt(thought_id, root=heart_root, now=at)
    item = (queued.get("item") or {}).get("id", "")
    _consume(row, at, root, {"action": ACT_SENT, "sent": True, "delivery_id": item,
                             "chunks": len(outcome.get("chunks") or []),
                             "chars": len(text)})
    ledger = _row(at, STAGE_DUE, reason_code, who, because, priority, engage_read,
                  ACT_SENT, plan_id=pid, delivery_id=item)
    append_log(root, ledger)
    return {"ok": True, "id": pid, "at": at, "action": ACT_SENT, "sent": True,
            "delivery_id": item, "pending": sent, "row": ledger}


def settle_due(due_rows: List[Dict[str, Any]], now_ts: Any = None, *, root: Any = None,
               heart_root: Any = None, brain: Any = None,
               config: Optional[Dict[str, Any]] = None, complete: Any = None,
               env: Optional[Mapping[str, str]] = None, transport: Any = None
               ) -> Dict[str, Any]:
    """tick 的到点那一半：只挑 proactive 那几条，逐条 run_due，按 action 计数。"""
    out: Dict[str, Any] = {"rows": [], "actions": {}}
    for row in due_rows or []:
        if str(row.get("kind")) != KIND:
            continue
        result = run_due(row, now_ts, root=root, heart_root=heart_root, brain=brain,
                         config=config, complete=complete, env=env, transport=transport)
        out["rows"].append(result)
        action = str(result.get("action") or "")
        out["actions"][action] = out["actions"].get(action, 0) + 1
    return out



# ---- 结算回执：连三次没人接 → 静音 + 一次 cold ------------------------------


def _cold_logged(root: Any, muted_until: Any) -> bool:
    """这一次静音是不是已经落过 cold 了（幂等看 muted_until：同一次静音只落一次）。"""
    stamp = str(muted_until or "")
    if not stamp:
        return False
    for row in log_lines(root):
        if row.get("action") == ACT_COLD and str(row.get("muted_until") or "") == stamp:
            return True
    return False


def settle(result: Any, now_ts: Any = None, *, who: str = "", root: Any = None,
           heart_root: Any = None, brain: Any = None,
           config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """结算最新那次主动开口的结果（replied / ignored），刚进静音时落一次 cold。

    同一段静音只落一行 cold：判据是"这一下之前还没静音、这一下之后静音了"
    （＝刚刚进入），外加 muted_until 那一道幂等锁——只看 streak >= mute_after 会在
    第 4、5 次没人接时反复落 cold，那不是"连三次没人接"那一下。
    """
    at = _now(now_ts)
    cfg = config or load_config(root)
    code = str(result or "").strip()
    if code not in heart_proactive.SETTLE_OUTCOMES:
        raise ProactiveError("result 只认 %s，收到 %r"
                            % ("、".join(heart_proactive.SETTLE_OUTCOMES), result))
    label = str(who or "").strip() or pick_who(at, root=root, brain=brain, config=cfg)["who"]
    before = heart_proactive.is_muted(heart_root, at)
    settled = heart_proactive.outcome(code, heart_root, at)
    cold: Optional[Dict[str, Any]] = None
    ledger: Optional[Dict[str, Any]] = None
    # "刚刚进入静音"＝这一次之前还没静音、这一次之后静音了。只看 streak 到了 3 会重复落
    # cold（第 4 次、第 5 次没人接也是 streak >= 3）；was_muted 这一条才是"那一次"。
    entered = bool(settled.get("ok") and settled.get("muted") and not before.get("muted"))
    if entered and not _cold_logged(root, settled.get("muted_until")):
        event = heart_mood.apply_event(heart_model.EVENT_COLD, root=heart_root, now=at, who=label)
        cold = {"event": event, "muted_until": settled.get("muted_until"),
                "streak": settled.get("streak")}
        ledger = _row(at, STAGE_OUTCOME, "", label,
                      "连着 %s 次主动开口没人接（静音到 %s）"
                      % (settled.get("streak"), settled.get("muted_until")),
                      0, {"state": heart_model.STATE_SILENT,
                          "reason": heart_model.REASON_PROACTIVE_MUTED}, ACT_COLD,
                      muted_until=settled.get("muted_until"), streak=settled.get("streak"),
                      skipped=event.get("skipped") if isinstance(event, dict) else "")
        append_log(root, ledger)
    return {"ok": bool(settled.get("ok")), "at": at, "result": code, "who": label,
            "was_muted": before.get("muted"), "outcome": settled, "cold": cold,
            "row": ledger,
            "readings": {"streak": settled.get("streak"), "muted": settled.get("muted"),
                         "muted_until": settled.get("muted_until"),
                         "mute_after": settled.get("mute_after")}}


# ---- 读数（面板 / GET /day/proactive） -------------------------------------


def overview(now_ts: Any = None, *, root: Any = None, heart_root: Any = None,
             brain: Any = None, config: Optional[Dict[str, Any]] = None,
             limits: Optional[Dict[str, Any]] = None, limit: int = LOG_SHOWN
             ) -> Dict[str, Any]:
    """账尾 + 此刻的读数：给谁、降权前后各是多少、额度用了几条、静音与 streak。只读不写盘。

    **不动念**：GET 不该顺手排一条计划（那是写动作），所以这里一个源都不判，
    只把"如果现在动念会拿着什么数"摆出来。
    """
    at = _now(now_ts)
    cfg = config or load_config(root)
    picked = pick_who(at, root=root, brain=brain, config=cfg)
    who = picked["who"]
    talks = _lately_talked(who, at, brain=brain, config=cfg)
    priorities: Dict[str, Dict[str, int]] = {}
    for code in REASONS:
        before = _priority(cfg, code)
        priorities[code] = {"before": before,
                            "after": max(0, before - int(talks["downgrade"]))}
    cap = per_who_cap(root, limits)
    used = _plans_today_for(who, at, root)
    reasons = dict((code, {"used": _reasons_today(code, at, root),
                           "cap": per_reason_cap(cfg, code)})
                   for code in REASONS)
    muted = heart_proactive.is_muted(heart_root, at)
    rows = log_tail(root, limit)
    tally: Dict[str, int] = {}
    for row in log_lines(root):
        action = str(row.get("action") or "")
        tally[action] = tally.get(action, 0) + 1
    pending = _pending_proactive(root)
    return {"ok": True, "now": at, "root": str(model.day_root(root)),
            "who": who, "who_source": picked["source"],
            "familiarity": picked["familiarity"],
            "lately_talked": talks["lately_talked"], "lately_talked_at": talks["last_at"],
            "downgrade": talks["downgrade"], "priority": priorities,
            "quota": {"used": used, "cap": cap, "remaining": max(0, cap - used),
                      "reasons": reasons},
            "unscheduled_window": list(unscheduled_window(cfg)),
            "in_unscheduled_window": in_unscheduled_window(at, cfg),
            "muted": muted, "streak": muted.get("streak"),
            "muted_until": muted.get("muted_until"),
            "pending_plan": pending, "counts": tally, "log": rows}

