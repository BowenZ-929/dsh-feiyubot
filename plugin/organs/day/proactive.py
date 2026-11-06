"""主动开口：判「她想不想说」（动念）→ 落一行账 → 交给计划层 → 到点再判一次。

判据（先写在这里，别处不许再判一次）：

1. **「想开口」的判定与理由码只在本文件这一处算**。五个驱动源各一个理由码：
   - `pending_task`：家的 task_room（默认「工作台」）里有一张**任务卡快到点了**——
     `params.task` 为真、`params.due` 能读成时刻、没完成（`progress < 100`；被拿走的卡
     默认查询就看不见）、当天没提醒过，且 due 在 due_soon_minutes 内、或过期不超过
     overdue_grace_minutes。**快到点的事比"她想说"更硬**，所以它排 REASON_ORDER 第一位。
     那句 because **只能由卡上的标题与 due 的本地时刻拼出来**（模板在两个数据文件里），
     不走 because_chars 的截断：逐字来自卡上，才有"不编"可言。
   - `thought`：心的念头非空（heart.pending.open_thoughts）；
   - `unfinished`：脑的 today 里**最后一手是别人说的、她当天没接上**的那条；
   - `own_doing`：她独处时做了点自己的事、做完想跟你说一声（organs/day/solitude.py
     往由头池 proactive_own_doing.jsonl 写一条，本文件读它）。**只在"真做了"之后才有**：
     没读到正文的日记、今天没有真素材的草稿都不许由头；一次独处最多一条，池里挂着
     没被用掉的那条就不再写第二条。用掉之后（动念账上落了这个 id）就不再出现。
   - `unscheduled`：今天还没定下要做什么（日程日志里没有当天的条目），
     **并且此刻在 unscheduled_window 里**（proactive.json，默认 9–12 点）：
     那是个常驻条件，窗外它不算由头——否则等于每天固定时间硬灌。
   几个同时成立时按 REASON_ORDER 取第一个：**一次动念只排一条计划**，理由要能说清是哪个。
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
    reason_code=pending_task 的那几行另带 card_id / task_due（由头是卡上的事，账要能追回那张卡）。

task 账（proactive_task_log.jsonl，一行一次**评估**）：
    {"at", "card_id", "name", "due", "progress", "action", "reason", "reminded_count"}
    action ∈ planned / held / quota / skipped
    skipped 那一行另带 count，并且**同日同卡同因只留一行**（proactive.json 的
    task_log_dedupe_skipped，默认开）：再遇上只就地改 at 与 count——一个 60 秒的 tick
    乘一张挂着的卡就是一天上千行噪声，而那些行不计任何额度，纯粹把账翻得更难看清。
    planned / held / quota 一次一行，永不合并：那是"她想开口"的真记录。
    reminded_count 是**这一行之前**这张卡已经提醒过几次（所以 planned 那一行常常是 0，
    下一条才是 1：它是判额度用的那个数，不是"这一次算完之后的数"）。
    **读过的每一张卡都留一行**（没到期 / 已提醒 / 已完成 / 没有任务标记 也是 skipped）：
    否则"为什么没提醒"就查不出来。但这行**不计入由头额度**——额度只算 planned / held，
    与动念账同一条尺子（_reasons_today 只数动念账，这本账只数"这张卡提醒过几次"）。
    每条任务最多提醒 per-card 几次的口径在 limits.json 的 proactive.task_remind_per_card
    （默认 1），数的是 task 账里这张卡的 planned / held 行，**不是"今天"**：一张卡一辈子
    只骚扰人一次。

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
TASK_LOG_FILE = "proactive_task_log.jsonl"
#: 由头池（第五类 own_doing）：独处做完一件事想跟你说一声时，solitude 往这里写一条，
#: sources() 读它——另起一套就变成第二个动念。
OWN_DOING_FILE = "proactive_own_doing.jsonl"
VERSION = 1

#: 五个驱动源的理由码（闭合集合：不许再长出第六个）。
REASON_PENDING_TASK = "pending_task"
REASON_THOUGHT = "thought"
REASON_UNFINISHED = "unfinished"
REASON_UNSCHEDULED = "unscheduled"
#: 第五类：她独处时做了点自己的事，做完想跟你说一声（由 organs/day/solitude.py 写进由头池，
#: 走的是同一条链：由头 → 计划 → 到点再判一次 engage → 说或只说给自己听）。
REASON_OWN_DOING = "own_doing"
REASONS = (REASON_PENDING_TASK, REASON_THOUGHT, REASON_UNFINISHED, REASON_UNSCHEDULED,
           REASON_OWN_DOING)
#: 几个都成立时取谁：一次动念只排一条计划，理由要能说清是哪个。
#: pending_task 排第一——快到点的事比"她想说"更硬；own_doing 排最后——那是她自己的事，
#: 别人的由头先说话。
REASON_ORDER = (REASON_PENDING_TASK, REASON_THOUGHT, REASON_UNFINISHED, REASON_UNSCHEDULED,
                REASON_OWN_DOING)

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
#: task 账上的"这一轮没提醒"（没到期 / 已提醒 / 已完成 / 没有任务标记）：只这本账有这个动作。
ACT_SKIPPED = "skipped"
ACTIONS = (ACT_PLANNED, ACT_HELD, ACT_QUOTA, ACT_MUTED, ACT_BLOCKED,
           ACT_DECLINED, ACT_EMPTY, ACT_ERROR, ACT_SENT, ACT_COLD)

#: 核回来的东西与卡上的事对不上（既没提标题、也没提那个时刻）：自己长出来的一类错，
#: 与"核打挂了"（error_kind 是 LLMError.kind）和"回执解析不出"（parse）分开记。
ERR_OFFTASK = "offtask"

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
    "reason_priority": {REASON_PENDING_TASK: 70, REASON_THOUGHT: 60,
                        REASON_UNFINISHED: 50, REASON_UNSCHEDULED: 40,
                        REASON_OWN_DOING: 35},
    "lately_talked_hours": 24,
    "lately_talked_downgrade": 20,
    # 每个理由码一天最多动念几次：不是每个人，是每个"由头"——同一个由头一天只用一次
    # （unscheduled 默认 1：那是"今天还没定下做什么"，定下来之前问一次就够了）。
    "per_reason_daily_cap": {REASON_PENDING_TASK: 3, REASON_THOUGHT: 3,
                             REASON_UNFINISHED: 3, REASON_UNSCHEDULED: 1,
                             REASON_OWN_DOING: 1},
    # unscheduled 这条由头只在上午成立：它描述的是"今天还没定下来做什么"，
    # 一个常驻条件不该整天成立（否则等于每天固定时间硬灌三条，那就不是由头了）。
    # 本地时区的整点，左闭右开；from > to 表示跨零点。
    "unscheduled_window": {"from": 9, "to": 12},
    # pending_task 这一条由头：多久之内要到的算"快到了"、过期多久之内还值得提一句，
    # 任务是哪间房里的、那句 because 怎么拼、交给核时那条"不许编"的规矩长什么样。
    "due_soon_minutes": 10,
    "overdue_grace_minutes": 120,
    "task_room": "工作台",
    "task_when_format": "%H:%M",
    "task_because": "{when} 要交的那件事：{name}",
    "task_rule": "这条由头来自一张卡：标题是「{name}」，时间是 {when}。"
                 "只能用给定的标题与时间，不许添加卡上没有的事。",
    # skipped 行同日同卡同因只留一行（就地更新 at 与 count）：一个 60 秒的 tick
    # 乘一张挂着的卡就是一天上千行噪声，而那些行不计任何额度。planned / held /
    # quota 不合并——那些是"她想开口"的真记录，一次一行。
    "task_log_dedupe_skipped": True,
}
#: 每人每天上限的兜底：真源是 limits.json 的 proactive.per_who_daily_cap
#: （与 plan 的闸门同一份配置文件），代码里只留这一份兜底。
DEFAULT_PER_WHO_DAILY_CAP = 3
#: 每条任务卡最多提醒几次的兜底：真源是 limits.json 的 proactive.task_remind_per_card。
DEFAULT_TASK_REMIND_PER_CARD = 1
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
                 "lately_talked_hours", "lately_talked_downgrade",
                 "due_soon_minutes", "overdue_grace_minutes"):
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


def task_window(config: Optional[Dict[str, Any]] = None) -> Tuple[float, float]:
    """提醒的两条窗口（分钟）：多久之内要到的算"快到了" / 过期多久之内还值得提一句。"""
    cfg = config or load_config()
    soon = _num(cfg.get("due_soon_minutes"), DEFAULTS["due_soon_minutes"])
    grace = _num(cfg.get("overdue_grace_minutes"), DEFAULTS["overdue_grace_minutes"])
    return max(0.0, soon), max(0.0, grace)


def task_room_of(config: Optional[Dict[str, Any]] = None) -> str:
    """任务卡放在哪间房里（proactive.json 的 task_room）：不是写死的房名。"""
    cfg = config or load_config()
    name = str(cfg.get("task_room") or "").strip()
    return name or str(DEFAULTS["task_room"])


def task_template(config: Optional[Dict[str, Any]], key: str) -> str:
    """proactive.json 里的那句模板（because / when_format / rule）；坏值退回兜底。"""
    cfg = config or load_config()
    value = cfg.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return str(DEFAULTS.get(key) or "")


def task_remind_cap(root: Any = None, limits: Optional[Dict[str, Any]] = None) -> int:
    """每张任务卡最多提醒几次：limits.json 的 proactive.task_remind_per_card。

    与 per_who_cap 同一套摆法（顶层 proactive 那一节，kinds.proactive 里写同一个键也认）。
    0 是合法值——等于"这条由头这一版不用"，不是无限。
    """
    cfg = limits if isinstance(limits, dict) and limits else model.load_limits(root)
    section = cfg.get(KIND) if isinstance(cfg.get(KIND), dict) else {}
    kinds = cfg.get("kinds") if isinstance(cfg.get("kinds"), dict) else {}
    entry = kinds.get(KIND) if isinstance(kinds.get(KIND), dict) else {}
    for source in (section, entry):
        if "task_remind_per_card" not in source:
            continue
        try:
            return max(0, int(source["task_remind_per_card"]))
        except (TypeError, ValueError):
            break
    return DEFAULT_TASK_REMIND_PER_CARD


def task_dedupe(config: Optional[Dict[str, Any]] = None) -> bool:
    """skipped 行要不要去重（proactive.json 的 task_log_dedupe_skipped，默认 true）。

    缺这一项就是 true：默认那头是不撑爆账本的那一头。写了 false 才逐条落。
    """
    cfg = config or load_config()
    value = cfg.get("task_log_dedupe_skipped", DEFAULTS["task_log_dedupe_skipped"])
    if isinstance(value, bool):
        return value
    return _truthy(value)


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


def task_log_path(root: Any = None) -> Path:
    """task 账：日子线数据根下的 proactive_task_log.jsonl（一行一次**评估**）。

    单开一本账，不往动念账里掺：动念账数的是"由头被用掉几次"（额度就读它），
    这本数的是"这张卡提醒过几次"——两把尺子混在一本就再也数不清了。
    """
    return model.day_root(root) / TASK_LOG_FILE


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
    return _append_line(log_path(root), row)


def append_task_log(root: Any, row: Dict[str, Any],
                    config: Optional[Dict[str, Any]] = None) -> Path:
    """写一行 task 账：与动念账同一套 append（崩了不留半行）。

    唯一的例外是 skipped 那一行：同日同卡同因只留一行，**就地**把 at 与 count 改掉
    （去重开关在 proactive.json 的 task_log_dedupe_skipped，默认开）。
    planned / held / quota 一次一行，永远不合并——那是"她想开口"的真记录。
    """
    target = task_log_path(root)
    if str(row.get("action") or "") == ACT_SKIPPED and task_dedupe(config):
        if _merge_task_row(target, row):
            return target
    return _append_line(target, row)


def _merge_task_row(target: Path, row: Dict[str, Any]) -> bool:
    """同日同卡同因的 skipped 行就地更新；合并掉了回 True（不追加新行）。

    键是 (day, card_id, action, reason)：换一天、换一张卡、换一个原因就是另一件事，
    该新起一行。合并只改 at（最近一次判的时刻）与 count（这一天因同一原因判过几次），
    其余字段不动——那行说的还是同一句话。
    """
    rows = _read_lines(target)
    if not rows:
        return False
    try:
        day = model.day_key(str(row.get("at") or ""))
    except ValueError:
        return False
    card_id = str(row.get("card_id") or "")
    reason = str(row.get("reason") or "")
    for index in range(len(rows) - 1, -1, -1):
        existing = rows[index]
        if str(existing.get("action") or "") != ACT_SKIPPED:
            continue
        if str(existing.get("card_id") or "") != card_id:
            continue
        if str(existing.get("reason") or "") != reason:
            continue
        try:
            if model.day_key(str(existing.get("at") or "")) != day:
                continue
        except ValueError:
            continue
        existing["at"] = str(row.get("at") or existing.get("at") or "")
        existing["count"] = int(existing.get("count") or 1) + 1
        _rewrite_lines(target, rows)
        return True
    return False


def _rewrite_lines(target: Path, rows: List[Dict[str, Any]]) -> None:
    """整本重写（同目录临时件 + os.replace）：只给"就地把一行改掉"用，坏不到原文。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(".%s.tmp-%d-%s" % (target.name, os.getpid(), secrets.token_hex(4)))
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _append_line(target: Path, row: Dict[str, Any]) -> Path:
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


def append_row(target: Path, row: Dict[str, Any]) -> Path:
    """一行的写法公开出去：别的账（独处的 idle 账）与动念账用同一套，崩了都不留半行。"""
    return _append_line(target, row)


def read_rows(target: Path) -> List[Dict[str, Any]]:
    """一本 jsonl 账的每一行（坏行原样跳过）：与 append_row 成对，别处不必各写一份。"""
    return _read_lines(target)


def own_doing_path(root: Any = None) -> Path:
    """由头池：日子线数据根下的 proactive_own_doing.jsonl（一行一条 own_doing）。"""
    return model.day_root(root) / OWN_DOING_FILE


def append_own_doing(root: Any, row: Dict[str, Any]) -> Path:
    """往由头池追加一条（只增：被用掉不删这一行，靠动念账上的 own_doing_id 认领）。"""
    return _append_line(own_doing_path(root), row)


def own_doing_lines(root: Any = None) -> List[Dict[str, Any]]:
    """由头池的每一行（坏行原样跳过）。"""
    return _read_lines(own_doing_path(root))


def open_own_doing(root: Any = None, now_ts: Any = None) -> Optional[Dict[str, Any]]:
    """今天还没被用掉的那条 own_doing（没有就 None）。

    "用掉"= 动念账上已经有一行带这个 id（planned / held / quota 都算：那一次由头已经
    说出口了，无论说成没成）。只认当天的行——昨天那句"想跟你说一声"到今天不该再冒出来。
    """
    at = _now(now_ts)
    day = model.day_key(at)
    used = set()
    for row in log_lines(root):
        rid = str(row.get("own_doing_id") or "")
        if rid:
            used.add(rid)
    latest: Optional[Dict[str, Any]] = None
    for row in own_doing_lines(root):
        rid = str(row.get("id") or "")
        if not rid or rid in used:
            continue
        if not str(row.get("because") or "").strip():
            continue
        try:
            if model.day_key(str(row.get("at") or "")) != day:
                continue
        except ValueError:
            continue
        latest = row
    return latest


def log_lines(root: Any = None) -> List[Dict[str, Any]]:
    """动念账的每一行（坏行原样跳过，不因为一行坏了读不了整份）。"""
    return _read_lines(log_path(root))


def task_log_lines(root: Any = None) -> List[Dict[str, Any]]:
    """task 账的每一行（坏行同样原样跳过：一行坏了不该让整本读不出来）。"""
    return _read_lines(task_log_path(root))


def _read_lines(target: Path) -> List[Dict[str, Any]]:
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
    return _tail(log_lines(root), limit)


def task_log_tail(root: Any = None, limit: int = LOG_SHOWN) -> List[Dict[str, Any]]:
    """task 账的尾几行。"""
    return _tail(task_log_lines(root), limit)


def _tail(rows: List[Dict[str, Any]], limit: Any) -> List[Dict[str, Any]]:
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


# ---- 任务卡（家的工作台上那张快到点的卡 → pending_task 这条由头） -----------


def _truthy(value: Any) -> bool:
    """params.task 为真：JSON 的 true / 1 / "true" / "1" / "yes" 都认，别的一概不算。

    不按 Python 的真假判：那样 "false" 这种非空串也会被当成"是任务"，
    于是拿一句假话去提醒人。
    """
    if value is True or value == 1:
        return True
    return isinstance(value, str) and value.strip().lower() in ("1", "true", "yes")


def _render(template: Any, fields: Mapping[str, Any]) -> str:
    """占位符逐个替换（不认 str.format：这些模板里本来就带着别的花括号）。"""
    text = str(template if template is not None else "")
    for key, value in fields.items():
        text = text.replace("{%s}" % key, str(value))
    return text


def task_when(due: Any, config: Optional[Dict[str, Any]] = None) -> str:
    """due 的**本地**时刻（格式取 proactive.json 的 task_when_format，默认 %H:%M）。

    走 model.iso 这一步是为了归一到本地时区：卡上写的是 +00:00，人看到的也得是墙上那个钟点。
    """
    try:
        stamp = model.parse_ts(model.iso(due))
    except ValueError:
        return ""
    return stamp.strftime(task_template(config, "task_when_format"))


def task_because(item: Mapping[str, Any], config: Optional[Dict[str, Any]] = None) -> str:
    """那句 because：只用卡上的标题与 due 的本地时刻，别的一个字都不加。

    刻意不走 because_chars 的截断：这句话要能逐字对回卡上那两样东西，
    截一半就成了"我替她改写了那张卡"。
    """
    return _render(task_template(config, "task_because"),
                   {"when": str(item.get("when") or ""), "name": str(item.get("name") or "")})


def _task_cards(home_root: Any, room: str) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """读那间房（只读）：卡是折叠后的（进度已贴回卡上，被拿走的默认不出现）。

    读不出来（房名不在十间里、家未载入、盘坏了）就回一句原因：这条由头这一轮不成立，
    既不抛崩 tick，也不许凭空编一张卡出来。
    """
    if home_root is None:
        return None, "家未载入（没有 home_root）：这一轮不判任务卡"
    from organs.home import cards as home_cards
    try:
        listing = home_cards.list_room(root=home_root, room=room)
    except Exception as exc:  # noqa: BLE001 - 卡片读不出来不该让动念整条断掉
        return None, "读不了「%s」：%s: %s" % (room, type(exc).__name__, exc)
    return list(listing.get("items") or []), ""


def _card_progress(card: Mapping[str, Any]) -> Optional[int]:
    """卡上的进度（fold 贴回来的那个数）；没有就是 None——"没进度"不等于 0。"""
    value = card.get("progress")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _task_credit(root: Any, at: str) -> Tuple[Dict[str, int], set]:
    """task 账里每张卡已经提醒过几次（planned / held 才算），以及今天提醒过的那几张。

    只数这两类动作：skipped 是"评估过但没提醒"，把它算进额度就等于"看一眼用掉一次"。
    """
    day = model.day_key(at)
    counts: Dict[str, int] = {}
    today: set = set()
    for row in task_log_lines(root):
        if str(row.get("action") or "") not in (ACT_PLANNED, ACT_HELD):
            continue
        card_id = str(row.get("card_id") or "")
        if not card_id:
            continue
        counts[card_id] = counts.get(card_id, 0) + 1
        try:
            if model.day_key(str(row.get("at") or "")) == day:
                today.add(card_id)
        except ValueError:
            continue
    return counts, today


def _task_row(at: str, item: Mapping[str, Any], action: str, reason: str) -> Dict[str, Any]:
    """一行 task 账：那八个键一个不多一个不少（账的形状定了就别再漂）。

    skipped 那一行另带 count（这一天因为同一原因被判过几次）：同日同卡同因再遇上
    只把 count 加一，不追加新行——"为什么没提醒"仍然查得到，账也不会被 tick 撑爆。
    """
    row: Dict[str, Any] = {"at": at, "card_id": str(item.get("card_id") or ""),
                           "name": str(item.get("name") or ""),
                           "due": str(item.get("due") or ""),
                           "progress": item.get("progress"),
                           "action": str(action or ACT_SKIPPED),
                           "reason": str(reason or ""),
                           "reminded_count": int(item.get("reminded_count") or 0)}
    if row["action"] == ACT_SKIPPED:
        row["count"] = 1
    return row


def pending_task(now_ts: Any = None, *, root: Any = None, home_root: Any = None,
                 config: Optional[Dict[str, Any]] = None,
                 limits: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """此刻有没有一张"快到点了"的任务卡。**只读**：不落账、不排计划。

    判据全在这一个函数里（别处不许再判一遍）：卡在 task_room 里、params.task 为真、
    params.due 能读成时刻、没完成（progress < 100；被拿走的卡默认查询就看不见）、
    当天没提醒过，且 due 在 due_soon_minutes 内或过期不超过 overdue_grace_minutes。
    同一轮只取**due 最早**的一张：一次动念只排一条计划。

    返回 {"ok", "at", "room", "error", "window", "cards": [每一张的评估], "fired": 这一轮那张}：
    cards 里每一张都带 action（skipped / quota，候选留空）与 reason，由 consider 落进 task 账——
    "为什么没提醒"要查得出来。over_cap 的那张仍然进 fired 候选：它要能记一行 quota，
    但如果还有一张没超额的，就让那张先说（别被一张已经提过的卡一直堵住）。
    """
    at = _now(now_ts)
    cfg = config or load_config(root)
    room = task_room_of(cfg)
    soon, grace = task_window(cfg)
    cap = task_remind_cap(root, limits)
    items, error = _task_cards(home_root, room)
    out: Dict[str, Any] = {"ok": not error, "at": at, "room": room, "error": error,
                           "window": {"due_soon_minutes": soon,
                                      "overdue_grace_minutes": grace, "remind_cap": cap},
                           "cards": [], "fired": None}
    if error:
        return out

    counts, today = _task_credit(root, at)
    now = model.parse_ts(at)
    ready: List[Tuple[Any, Dict[str, Any]]] = []
    held: List[Tuple[Any, Dict[str, Any]]] = []
    for card in items:
        if not isinstance(card, dict):
            continue
        card_id = str(card.get("card_id") or "")
        params = card.get("params") if isinstance(card.get("params"), dict) else {}
        if _truthy(params.get("self")):
            # 她自己给自己开的卡（独处做的那件 c，params.self=true）：不是别人要她做的事，
            # 也没有 due。它连一行 skipped 都不留——task 账数的是"这张卡提醒过几次"，
            # 而她自己的那张永远不该被问"到点了吗"。
            continue
        due_raw = str(params.get("due") or "")
        item: Dict[str, Any] = {"card_id": card_id, "name": str(card.get("name") or ""),
                                "due": due_raw, "when": "",
                                "progress": _card_progress(card),
                                "reminded_count": int(counts.get(card_id, 0)),
                                "action": "", "reason": "", "over_cap": False}
        out["cards"].append(item)

        if not _truthy(params.get("task")):
            item["action"] = ACT_SKIPPED
            item["reason"] = "这张卡上没有任务标记（params.task 不为真）"
            continue
        try:
            due = model.parse_ts(due_raw)
        except ValueError:
            item["action"] = ACT_SKIPPED
            item["reason"] = "params.due 不是合法时刻：%r" % (due_raw,)
            continue
        item["when"] = task_when(due_raw, cfg)
        progress = item["progress"]
        if progress is not None and progress >= 100:
            item["action"] = ACT_SKIPPED
            item["reason"] = "这张卡已经完成（progress=%d）" % progress
            continue
        if card_id and card_id in today:
            item["action"] = ACT_SKIPPED
            item["reason"] = "今天已经提醒过这张卡（%d 次）" % item["reminded_count"]
            continue
        early = (due - now).total_seconds() / 60.0
        # 两句 reason 里**不放会随时间漂的数字**（还有几分钟 / 过期几分钟）：那几句是
        # skipped 行的去重键，放进去就变成"每 tick 一个原因"，账还是会被撑爆。
        # 差了多少分钟从这一行的 at 与 due 一减就有；count 记的是"判过几次"。
        if early > soon:
            item["action"] = ACT_SKIPPED
            item["reason"] = "还没到提醒的时候（窗口 %d 分钟）" % round(soon)
            continue
        if early < -grace:
            item["action"] = ACT_SKIPPED
            item["reason"] = "已经过期超过宽限（宽限 %d 分钟，不再提）" % round(grace)
            continue
        item["over_cap"] = item["reminded_count"] >= cap
        (held if item["over_cap"] else ready).append((due, item))

    if ready or held:
        ready.sort(key=lambda pair: pair[0])
        held.sort(key=lambda pair: pair[0])
        fired = (ready or held)[0][1]
        out["fired"] = fired
        for _due, item in ready + held:
            if item is fired:
                continue
            item["action"] = ACT_SKIPPED
            item["reason"] = "这一轮只提一张（先提 due 最早的那张：%s）" % fired["name"]
        if fired["over_cap"]:
            fired["action"] = ACT_QUOTA
            fired["reason"] = ("这张卡已经提醒过 %d 次（上限 %d）"
                               % (fired["reminded_count"], cap))
    return out


def _flush_task_log(root: Any, at: str, task: Mapping[str, Any], action: str = "",
                    reason: str = "",
                    config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """把这一轮的评估落进 task 账：读过的每张卡一行，被选中的那张按这一轮的判决写。

    action 空就是"这一轮根本没轮到它"（比如没有由头 / 已经有一条待说的计划）：
    那也落一行，记成 skipped 并写明原因——否则"为什么没提醒"就查不出来。
    """
    rows: List[Dict[str, Any]] = []
    chosen = task.get("fired")
    for item in task.get("cards") or []:
        if item is chosen:
            continue
        rows.append(_task_row(at, item, str(item.get("action") or ACT_SKIPPED),
                              str(item.get("reason") or "")))
    if isinstance(chosen, Mapping):
        rows.append(_task_row(at, chosen, str(action or ACT_SKIPPED),
                              str(reason or chosen.get("reason") or "")))
    for row in rows:
        append_task_log(root, row, config=config)
    return rows


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
            brain: Any = None, home_root: Any = None,
            config: Optional[Dict[str, Any]] = None,
            task: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """此刻有哪些"想开口"的由头（按 REASON_ORDER 排），每个带理由码与一句真话。

    这是**唯一**一处判"她想不想开口"的地方：四块读数各自只有一条判据，
    别处（节律、HTTP 层）不许再判一遍。

    task 是已经算好的 pending_task 评估（见 pending_task()）：传进来只是别读两遍卡，
    判定仍然只在那一个函数里——"两张口径"就是这么长出来的。
    """
    cfg = config or load_config(root)
    at = _now(now_ts)
    chars = cfg.get("because_chars")
    found: Dict[str, Dict[str, Any]] = {}

    if task is None:
        task = pending_task(at, root=root, home_root=home_root, config=cfg)
    chosen = task.get("fired")
    if isinstance(chosen, Mapping):
        found[REASON_PENDING_TASK] = {
            "reason_code": REASON_PENDING_TASK,
            # 只用卡上的标题与时刻，且不截断：这句要能逐字对回那张卡
            "because": task_because(chosen, cfg),
            "thought_id": "", "who": "",
            "task": {"card_id": str(chosen.get("card_id") or ""),
                     "name": str(chosen.get("name") or ""),
                     "due": str(chosen.get("due") or ""),
                     "when": str(chosen.get("when") or ""),
                     "reminded_count": int(chosen.get("reminded_count") or 0)},
            "over_cap": bool(chosen.get("over_cap"))}

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

    # 第五类：她独处时做的那件事（solitude 写进由头池、这里读）。句子只由那一步
    # 真做的动作拼出来，所以不走 because_chars 的截断——它是这一步的原文，不是别人的长话。
    doing = open_own_doing(root, at)
    if doing is not None:
        found[REASON_OWN_DOING] = {"reason_code": REASON_OWN_DOING,
                                   "because": str(doing.get("because") or ""),
                                   "thought_id": "", "who": "",
                                   "own_doing_id": str(doing.get("id") or "")}

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


def _task_verdict(action: str, quota: Mapping[str, Any], muted: Mapping[str, Any],
                  code: str, blocked: str) -> Tuple[str, str]:
    """这一轮的判决怎么落到 task 账上（那本账只认 planned / held / quota / skipped）。

    muted / blocked 记 skipped：任务卡本身一点问题没有，是这一刻不该说——账上要能分清
    "这张卡不提醒了"和"这一轮没轮到它"。
    """
    if action == ACT_PLANNED:
        return ACT_PLANNED, ""
    if action == ACT_HELD:
        return ACT_HELD, "判 SILENT（%s）：计划照生，不调核、不发送" % code
    if action == ACT_QUOTA:
        if str(quota.get("scope") or "") == "card":
            return ACT_QUOTA, ("这张卡已经提醒过 %s 次（上限 %s）"
                              % (quota.get("used"), quota.get("cap")))
        return ACT_QUOTA, "被额度挡下（scope=%s）" % (quota.get("scope") or "")
    if action == ACT_MUTED:
        return ACT_SKIPPED, "静音中（到 %s）：这一轮不开口" % (muted.get("muted_until") or "")
    if action == ACT_BLOCKED:
        return ACT_SKIPPED, "计划闸门拦下了这一条（%s）" % (blocked or "")
    return ACT_SKIPPED, "这一轮没有开口（%s）" % action


def consider(now_ts: Any = None, *, root: Any = None, heart_root: Any = None,
             brain: Any = None, home_root: Any = None,
             config: Optional[Dict[str, Any]] = None,
             limits: Optional[Dict[str, Any]] = None, idle_only: bool = False
             ) -> Dict[str, Any]:
    """手工/事件驱动地评估一次"她想不想开口"：最多落一行账、最多排一条计划。

    次序钉死（先静音、再额度、再 engage）：安静一会儿优先于她想说，额度优先于她自己
    以为的"现在正好"。判 SILENT 照旧生成计划，只是标 held——人要看得出
    "她想说，但因为夜里/双低没说"。

    home_root 只给 pending_task 那条由头读卡用（家：task_room 里的卡）；task 账另记一行
    一次评估，连"没提醒"也记——两本账各数各的，别混。
    """
    at = _now(now_ts)
    cfg = config or load_config(root)
    task = pending_task(at, root=root, home_root=home_root, config=cfg, limits=limits)
    fired = sources(at, root=root, heart_root=heart_root, brain=brain,
                    home_root=home_root, config=cfg, task=task)
    if not fired:
        _flush_task_log(root, at, task, config=cfg)
        return {"ok": True, "at": at, "fired": False, "skipped": "no-source",
                "sources": [], "row": None, "plan": None, "pending_task": task}
    if idle_only and _pending_proactive(root) is not None:
        _flush_task_log(root, at, task, ACT_SKIPPED, "已经有一条待说的计划，这一轮不叠",
                        config=cfg)
        return {"ok": True, "at": at, "fired": False, "skipped": "pending-plan",
                "sources": [item["reason_code"] for item in fired], "row": None,
                "plan": None, "pending_task": task}

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

    # 次序：静音 > 三条额度（谁 / 哪张卡 / 哪个由头）> engage 的硬门 > 排计划。
    # 三条额度都叫 quota，靠 scope 分开——"被哪条额度挡的"要能在账上读出来。
    card = top.get("task") if isinstance(top.get("task"), dict) else {}
    quota: Dict[str, Any] = {}
    if muted.get("muted") is True:
        action = ACT_MUTED
    elif used >= cap:
        action = ACT_QUOTA
        quota = {"scope": "who", "used": used, "cap": cap}
    elif top["reason_code"] == REASON_PENDING_TASK and top.get("over_cap"):
        # 每条任务最多提醒几次：数的是 task 账里这张卡的 planned / held 行（limits.json）。
        action = ACT_QUOTA
        quota = {"scope": "card", "card_id": str(card.get("card_id") or ""),
                 "used": int(card.get("reminded_count") or 0),
                 "cap": task_remind_cap(root, limits)}
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
        candidate: Dict[str, Any] = {"kind": KIND, "at": _plan_at(at, cfg),
                                     "note": top["because"],
                                     "priority": after, "who": who,
                                     "held": action == ACT_HELD,
                                     "held_reason": code if action == ACT_HELD else "",
                                     "reason_code": top["reason_code"],
                                     "thought_id": top.get("thought_id") or ""}
        if card:
            # 到点执行时要能判"回执里说的事还是不是卡上那件"：标题与时刻随计划落账。
            candidate["task"] = dict(card)
        out = plan.add([candidate],
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
               card_id=str(card.get("card_id") or ""),
               task_due=str(card.get("due") or ""),
               # own_doing 那一次由头：把这个 id 落账，由头池才知道这条已经被用掉了
               own_doing_id=str(top.get("own_doing_id") or ""),
               quota=quota)
    append_log(root, row)
    _flush_task_log(root, at, task, *_task_verdict(action, quota, muted, code, blocked),
                    config=cfg)
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

    task = row.get("task") if isinstance(row.get("task"), dict) else {}
    ctx: Dict[str, Any] = {"who": who, "because": because,
                           "tone": str(row.get("tone") or ""), "now": at, "root": root}
    if task:
        # 交这一段的是任务卡：提示词里要写明只能用卡上那两样，不许添加卡上没有的事。
        ctx["rule"] = _render(task_template(cfg, "task_rule"),
                              {"name": str(task.get("name") or ""),
                               "when": str(task.get("when") or "")})
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

    if reason_code == REASON_PENDING_TASK:
        # 提醒里说的事必须来自卡上：既没提标题、也没回显那个时刻，就是核自己在编。
        offtask = _offtask(row, outcome)
        if offtask:
            _consume(row, at, root, {"action": ACT_ERROR, "error_kind": ERR_OFFTASK,
                                     "error": offtask, "sent": False})
            ledger = _row(at, STAGE_DUE, reason_code, who, because, priority, engage_read,
                          ACT_ERROR, plan_id=pid, error_kind=ERR_OFFTASK, error=offtask,
                          card_id=str(task.get("card_id") or ""))
            append_log(root, ledger)
            return {"ok": False, "id": pid, "at": at, "action": ACT_ERROR,
                    "error_kind": ERR_OFFTASK, "error": offtask, "sent": False, "row": ledger}

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


def _offtask(row: Mapping[str, Any], outcome: Mapping[str, Any]) -> str:
    """回执里说的事还在不在卡上；不在就回一句原因（空串＝对得上）。

    判据写死：**提醒里说的事必须来自卡上**——chunks 里连卡上的标题都没有、
    也不回显那个时刻，那就是核自己编了一件别的事，宁可这一轮不发。
    """
    task = row.get("task") if isinstance(row.get("task"), dict) else {}
    name = str(task.get("name") or "")
    when = str(task.get("when") or "")
    blob = "\n".join(str(chunk) for chunk in (outcome.get("chunks") or []))
    if name and name in blob:
        return ""
    if when and when in blob:
        return ""
    return "回执里既没有卡上的标题 %r，也没有那个时刻 %r" % (name, when)


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


def _pending_task_view(task: Mapping[str, Any]) -> Dict[str, Any]:
    """overview 里那一条：房名 + 两条窗口 + 每张卡的评估（不带卡的正文与 params）。"""
    return {"ok": bool(task.get("ok")), "room": str(task.get("room") or ""),
            "error": str(task.get("error") or ""), "window": task.get("window") or {},
            "fired": str((task.get("fired") or {}).get("card_id") or ""),
            "cards": [{"card_id": str(item.get("card_id") or ""),
                       "name": str(item.get("name") or ""),
                       "due": str(item.get("due") or ""),
                       "when": str(item.get("when") or ""),
                       "progress": item.get("progress"),
                       "action": str(item.get("action") or ""),
                       "reason": str(item.get("reason") or ""),
                       "reminded_count": int(item.get("reminded_count") or 0)}
                      for item in (task.get("cards") or [])]}


def overview(now_ts: Any = None, *, root: Any = None, heart_root: Any = None,
             brain: Any = None, home_root: Any = None,
             config: Optional[Dict[str, Any]] = None,
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
            # 第四类由头此刻的读数：哪间房、两条窗口、房间里有几张任务卡、哪张快到了。
            # 只读：这里**不落 task 账**（GET 不该写盘），所以也不能调 _flush_task_log。
            "pending_task": _pending_task_view(pending_task(at, root=root,
                                                           home_root=home_root,
                                                           config=cfg, limits=limits)),
            "muted": muted, "streak": muted.get("streak"),
            "muted_until": muted.get("muted_until"),
            "pending_plan": pending, "counts": tally, "log": rows}

