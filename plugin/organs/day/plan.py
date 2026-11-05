"""计划的幂等与闸门：绝对时刻入账，重跑不重复，拦截在调度层。

三条判据：
1. 计划存**绝对时刻**（model.iso 归一过的带偏移 ISO8601）。同一时刻换个偏移写
   （09:00+08:00 与 01:00+00:00）落到同一个 id。
2. id 由 (kind, at) 算出来，不是随机数：所以同一 now 连跑两次，第二次拿到的是
   同一个 id，落进 duplicates，pending/consumed 一条都不增。幂等不靠「记得跑过」。
3. 两道闸门拦在这里，**不进 pending**，而且只拦 optional 那类：
   - min_gap：同类型两条计划之间至少隔 min_gap_minutes（默认 10 分钟）；
   - 额度：同类型每天最多 daily_cap 条（默认 12），reason 码是 quota_optional。
   **required 的计划既不看 min_gap 也不看额度**：该跑的那件事不该被"今天排满了"挡掉。
   required 由谁定：候选自己写的 required > kinds 里那一栏的 required > **默认 optional**。
   所以 kinds 里没写的、kind 根本不在 kinds 里（临时 add 的候选）一律 optional——
   额度管"可有可无"，写错一个 kind 名不该变成免检。
   被拦的候选记进 blocked 留痕（reason 给机器、message 给人），而且**事后不许补**：
   同一个 (kind, at) 再进来一次仍然被拦，理由照旧，不会因为后来松了就偷偷放行。
4. ensure() 只为"当天该跑的那几件事"排计划：limits.json 的 kinds 里每个 kind 确保
   **恰有一条** pending（已有就一条不新增），跨天自然重排。本期 kinds 只有 night
   （03:30，min_gap 10 分钟、每天 1 条）。日程**不进**计划：这一期由 /day/schedule/draw
   手工触发，等壳线有心/嘴的意愿再接计划。
5. 计划有五种结局，分五栏存，不许混：pending（还没到点/还没跑）、consumed（跑过了，
   带 result 摘要）、blocked（被闸门拦下，带 reason）、expired（关机错过，**不补跑**）、
   never_ran（到了点、该跑，但首跑 + 重试都没跑成，带 reason 与重试快照）。
   expired 不占 min_gap / daily_cap：它本来没发生，占名额会把当天那条挤掉。
   never_ran 与 expired 一样算"已处理"：同一天不许再排第二条夜（它也是没跑成，不重排）。
   blocked 是"闸门拦的"，never_ran 是"该跑却没跑成"——两种没落地不能叠成一个桶。
6. 失败的那次**不下账**：那条留在 pending，行里带夜间快照 night（第几次尝试、四步各自
   结果、哪几步没成功、下次几点重试 next_retry_at），退避窗口内的那一条**不算到点**——
   due() 与 next_at() 都认这个字段，判断只在本文件这一处，别处不许再判一遍。

账的形状（plans.json，不许悄悄加顶层键）：
    {"version": 1, "pending": [行], "consumed": [行], "blocked": [行],
     "expired": [行], "never_ran": [行]}
    行：{id, kind, at, day, status, required, note, created_at, consumed_at}
    blocked 行：{id, kind, at, day, required, reason, message, blocked_at}
    never_ran 行：/ expired 行 = 原行 + {status, reason, expired_at|never_ran_at}
    pending 行里的夜间快照：night = {"attempts": 0..n, "steps": {名字: {status, detail}},
                                      "failed_steps": [名字], "next_retry_at": iso|None}
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import model

PLANS_FILE = "plans.json"
VERSION = 1
STATUS_PENDING = "pending"
STATUS_CONSUMED = "consumed"
REASON_MIN_GAP = "min_gap"
# 额度只约束 optional 那类，码就叫 quota_optional：读到它就知道"这是可有可无那类的额度"。
REASON_QUOTA_OPTIONAL = "quota_optional"
# 旧名留成别名：额度以前按 kind 无差别计（码叫 daily_cap），现在只拦 optional，
# 读 REASON_DAILY_CAP 的地方拿到的仍是"额度不够"这同一个理由。
REASON_DAILY_CAP = REASON_QUOTA_OPTIONAL
KINDS_KEY = "kinds"
# 计划行上的 required：True 的不受 min_gap / 额度约束；没写＝optional（受约束）
REQUIRED_KEY = "required"
# 到期没执行的计划不补跑，标这一栏：它不进 pending（没人会再去执行它），
# 也不占 min_gap / daily_cap（它本来就没发生）。
STATUS_EXPIRED = "expired"
# 到了点、该跑，但首跑 + 重试都没跑成：第五栏。与 expired 并列而不是并进 blocked——
# blocked 是闸门拦的（根本没该它跑），never_ran 是真去试了、试满次数仍然没成了。
STATUS_NEVER_RAN = "never_ran"
# 计划行里的夜间快照：下一 tick 靠它决定"只重跑哪几步、沿用哪几步的结果"
NIGHT_KEY = "night"


class PlanError(ValueError):
    """计划账坏了：调用方要看见，不许当空账继续跑（空账会把闸门全放开）。"""


# ---- 账 -------------------------------------------------------------------


def path(root: Any = None) -> Path:
    """计划账的位置：日子线数据根下的 plans.json。"""
    return model.day_root(root) / PLANS_FILE


def _empty() -> Dict[str, Any]:
    return {"version": VERSION, "pending": [], "consumed": [], "blocked": [],
            "expired": [], "never_ran": []}


def load(root: Any = None) -> Dict[str, Any]:
    """读账；文件不在就是空账（第一次跑没有文件是正常的），读坏了才抛 PlanError。"""
    target = path(root)
    if not target.exists():
        return _empty()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanError("计划账读不出来：%s（%s）" % (target, exc))
    if not isinstance(raw, dict):
        raise PlanError("计划账顶层要是对象：%s" % target)
    data = _empty()
    for key in ("pending", "consumed", "blocked", "expired", "never_ran"):
        rows = raw.get(key)
        data[key] = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        for row in data[key]:
            for field in ("id", "kind", "at"):
                if not isinstance(row.get(field), str) or not row[field]:
                    raise PlanError("计划账里有一行缺 %s（%s）：行不齐就没法算闸门"
                                    % (field, target))
    return data


def save(data: Dict[str, Any], root: Any = None) -> Path:
    """整份换掉：先写同目录临时件再 os.replace，中途崩了不会留半个账。"""
    target = path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(".tmp-%d-%s" % (os.getpid(), secrets.token_hex(4)))
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(target))
    return target


def plan_id(kind: str, at: str) -> str:
    """(kind, at) → 稳定 id：同一时刻同一类永远是同一个 id，幂等就靠它。"""
    digest = hashlib.sha1(("%s|%s" % (kind, at)).encode("utf-8")).hexdigest()
    return "pl-" + digest[:12]


# ---- 读 -------------------------------------------------------------------


def plan_ids(data: Dict[str, Any]) -> List[str]:
    """pending + consumed 的 id（排序后），验收比对两次 tick 用的是它。"""
    return sorted(row.get("id", "") for row in data["pending"] + data["consumed"])


def counts(data: Dict[str, Any]) -> Dict[str, int]:
    """五栏各自几条 + total（真跑过或还没跑的那几条）。never_ran 与 expired / blocked
    一样单独数：账上不数它，就等于"没跑成"在计数里看不见。"""
    return {"pending": len(data["pending"]), "consumed": len(data["consumed"]),
            "blocked": len(data["blocked"]), "expired": len(data["expired"]),
            "never_ran": len(data["never_ran"]),
            "total": len(data["pending"]) + len(data["consumed"])}


def listing(root: Any = None) -> Dict[str, Any]:
    """GET /day/plan：pending / consumed / blocked / expired / never_ran 五栏分开。

    混一栏等于看不出"拦没拦""跑没跑"：blocked 是闸门拦下的，expired 是关机错过的，
    never_ran 是该跑却没跑成的，consumed 是真跑过的——四种没落地的原因不能叠成一个桶。
    """
    data = load(root)
    rows = [row for column in ("pending", "consumed", "blocked", "expired", "never_ran")
            for row in data[column]]
    for row in rows:
        # 每行都带 required：人一眼就能看出这条受不受额度管（老账里没这个键的按 optional）
        row[REQUIRED_KEY] = _row_required(row)
    return {"ok": True, "root": str(model.day_root(root)), "pending": data["pending"],
            "consumed": data["consumed"], "blocked": data["blocked"],
            "expired": data["expired"], "never_ran": data["never_ran"],
            "counts": counts(data), "plan_ids": plan_ids(data)}


def _row_required(row: Dict[str, Any]) -> bool:
    """账上这一行是不是 required；没写、写坏了一律 optional（额度管"可有可无"）。"""
    return row.get(REQUIRED_KEY) is True


def required_of(item: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> bool:
    """这条候选算不算"必须跑的"：候选自己写的 > kinds 里那一栏 > 默认 optional。

    默认 optional 是有意的：kinds 里没写、甚至 kind 根本不在 kinds 里的候选（临时 add
    的那条）都受额度管——写错一个 kind 名不该变成免检。
    """
    declared = item.get(REQUIRED_KEY)
    if declared is not None:
        return declared is True
    kinds = (config or {}).get(KINDS_KEY) or {}
    entry = kinds.get(item.get("kind"))
    entry = entry if isinstance(entry, dict) else {}
    return entry.get(REQUIRED_KEY) is True


def retry_at(row: Dict[str, Any]) -> Optional[datetime]:
    """这条计划的下次重试时刻（退避窗口的右边界）；没有就是按 at 到点。"""
    raw = night_of(row).get("next_retry_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return model.parse_ts(raw)
    except ValueError:
        return None


def night_of(row: Dict[str, Any]) -> Dict[str, Any]:
    """计划行里的夜间快照；没有就返回空字典（＝这一步还没试过，下一次是首跑）。"""
    snap = row.get(NIGHT_KEY)
    return snap if isinstance(snap, dict) else {}


def _now(now_ts: Any = None) -> datetime:
    return model.parse_ts(now_ts) if now_ts is not None else model.parse_ts(model.now_iso())


def due(now_ts: Any = None, *, root: Any = None,
        data: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """到点的计划：pending 里 at <= now **且退避窗口已过**的那些（按时刻升序）。只看，不改。

    "窗口内不算到点"只在这里判一次：重试失败的那条带着 next_retry_at，窗口没过就继续
    留在 pending 里等，别处（rhythm 尤其）不许再判第二遍。
    data 传了就用那份已经读好的账（一次请求里省一次读盘）。
    """
    at = _now(now_ts)
    ledger = data if data is not None else load(root)
    rows: List[Dict[str, Any]] = []
    for row in ledger["pending"]:
        if model.parse_ts(row["at"]) > at:
            continue
        retry = retry_at(row)
        if retry is not None and retry > at:
            continue        # 还在退避窗口里：这一 tick 不算到点，等窗口过了再来
        rows.append(row)
    return sorted(rows, key=lambda row: row["at"])


def next_at(now_ts: Any = None, *, root: Any = None,
            data: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """下一次几点：pending 里最早一条"还要等到什么时候"；没有就 None（纯算术）。

    退避中的那条不按它的 at 报（那个时刻早就过去了），按 next_retry_at 报——
    否则"下一次几点"会说谎。
    """
    at = _now(now_ts)
    ledger = data if data is not None else load(root)
    ahead: List[str] = []
    for row in ledger["pending"]:
        when = model.parse_ts(row["at"])
        retry = retry_at(row)
        if when > at:
            ahead.append(row["at"])
        elif retry is not None and retry > at:
            ahead.append(model.iso(retry))
    return min(ahead) if ahead else None


# ---- 写 -------------------------------------------------------------------


def _candidate(item: Any) -> Dict[str, Any]:
    """候选 → {kind, at, note, required}；at 归一成绝对时刻，kind 不许空。

    required 只有候选自己明确写了才是 True/False，没写就是 None（到 add 里再按配置定）。
    """
    declared: Any = None
    if isinstance(item, dict):
        kind = str(item.get("kind") or "").strip()
        raw_at = item.get("at")
        note = str(item.get("note") or "")
        declared = item.get(REQUIRED_KEY)
    elif isinstance(item, (tuple, list)) and len(item) >= 2:
        kind = str(item[0] or "").strip()
        raw_at = item[1]
        note = ""
    else:
        raise ValueError("候选计划要 {kind, at} 字典或 (kind, at) 二元组，收到 %r" % (item,))
    if not kind:
        raise ValueError("候选计划缺 kind：%r" % (item,))
    return {"kind": kind, "at": model.iso(raw_at), "note": note,
            "required": None if declared is None else declared is True}


def thresholds(config: Optional[Dict[str, Any]], kind: str) -> Tuple[int, int]:
    """这一类的闸门 → (min_gap 秒, 每天上限)；kind 自己那一栏优先，否则退回全局。

    数值一律从 config（limits.json）来，这里只做取用与兜底，不写死第二份阈值。
    """
    cfg = config or {}
    kinds = cfg.get(KINDS_KEY) or {}
    per = kinds.get(kind) if isinstance(kinds.get(kind), dict) else {}
    gap = per.get("min_gap_minutes", cfg.get("min_gap_minutes", model.DEFAULTS["min_gap_minutes"]))
    cap = per.get("daily_cap", cfg.get("daily_cap", model.DEFAULTS["daily_cap"]))
    try:
        return int(gap) * 60, int(cap)
    except (TypeError, ValueError):
        return (int(model.DEFAULTS["min_gap_minutes"]) * 60,
                int(model.DEFAULTS["daily_cap"]))


def _clock(value: Any) -> Tuple[int, int]:
    """'HH:MM' → (时, 分)；坏值当场抛，不猜一个钟点排下去。"""
    parts = str(value if value is not None else "").strip().split(":")
    if len(parts) != 2:
        raise ValueError("kind 的 at 要 'HH:MM'（例：03:30），收到 %r" % (value,))
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        raise ValueError("kind 的 at 要 'HH:MM'（例：03:30），收到 %r" % (value,))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("kind 的 at 要 'HH:MM'（00:00–23:59），收到 %r" % (value,))
    return hour, minute


def kind_at(day: str, clock: Any, cutover_hour: Optional[int] = None) -> str:
    """某一天的某个钟点 → 绝对时刻。

    换日点是 04:00，所以 03:30 这种钟点落在"下一天凌晨"：这里用 model.day_key 自己
    当裁判——算出来的时刻归到哪一天就算哪一天，本文件不另写一套换日算术。
    """
    hour, minute = _clock(clock)
    base = datetime.fromisoformat("%sT00:00:00" % day).replace(tzinfo=model.local_tz())
    base = base + timedelta(hours=hour, minutes=minute)
    if model.day_key(base, cutover_hour) != day:
        base = base + timedelta(days=1)
    return model.iso(base)


def ensure(now_ts: Any = None, *, root: Any = None,
           config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """为"当天"把 kinds 里每个 kind 确保恰有一条计划：已有的原样返回，一条不新增。

    跨天自然重排：候选时刻由 (day_key(now), kind.at) 算出来，换一天就是另一条。
    生成同样过闸门（min_gap / daily_cap，kind 自己那栏优先），被拦的进 blocked 带 reason。
    """
    cfg = config or model.load_limits()
    at = model.iso(now_ts) if now_ts is not None else model.now_iso()
    day = model.day_key(at)
    kinds = cfg.get(KINDS_KEY) or {}
    cutover = int(cfg["day_cutover_hour"])
    rows: List[Dict[str, Any]] = []
    for kind in sorted(kinds):
        entry = kinds[kind] if isinstance(kinds[kind], dict) else {}
        rows.append({"kind": kind, "at": kind_at(day, entry.get("at"), cutover),
                     "note": "自动排：%s %s" % (day, entry.get("at"))})
    out = add(rows, root=root, now_ts=at, config=cfg)
    out["day"] = day
    out["kinds"] = sorted(kinds)
    return out


def add(candidates: Iterable[Any], *, root: Any = None, now_ts: Any = None,
        config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """候选计划过闸门入账：added / duplicates / blocked 三份，一个候选只落一处。

    幂等：同一个 (kind, at) 第二次进来落 duplicates（或原样落 blocked），账不变。
    闸门只拦 optional：先看 min_gap（与同类型最近的一条隔多久），再看额度（同类型当天
    几条 optional）。required 的那条两条都不看——该跑的事不该被"今天排满了"挡掉。
    """
    cfg = config or model.load_limits()
    created = model.iso(now_ts) if now_ts is not None else model.now_iso()
    data = load(root)

    taken: Dict[str, str] = {}
    same_kind: Dict[str, List[datetime]] = {}
    per_day: Counter = Counter()
    for row in data["pending"] + data["consumed"]:
        taken[str(row.get("id", ""))] = str(row.get("status") or STATUS_PENDING)
        kind = str(row.get("kind", ""))
        same_kind.setdefault(kind, []).append(model.parse_ts(row["at"]))
        if not _row_required(row):
            # 额度只数 optional：required 的行不占"可有可无"那类的名额
            per_day[(kind, str(row.get("day") or model.day_key(row["at"])))] += 1
    for row in data["expired"]:
        # 过期的计划不再重排（它就是那一条，重排等于偷偷补跑），但也不占闸门：
        # 它本来没发生，占着名额就等于把今天的那条挤掉。
        taken[str(row.get("id", ""))] = STATUS_EXPIRED
    for row in data["never_ran"]:
        # 试满次数也没跑成的那条与 expired 一样算"已处理"：同一天不许再排第二条夜。
        # 但同样不占闸门——它没成功，占名额只是把明天那条挤掉。
        taken[str(row.get("id", ""))] = STATUS_NEVER_RAN
    blocked_index = {str(row.get("id", "")): row for row in data["blocked"]}

    added: List[Dict[str, Any]] = []
    duplicates: List[str] = []
    blocked: List[Dict[str, Any]] = []
    for item in sorted((_candidate(entry) for entry in candidates), key=lambda entry: entry["at"]):
        pid = plan_id(item["kind"], item["at"])
        if pid in taken:
            duplicates.append(pid)
            continue
        if pid in blocked_index:
            # 事后不许补：同一条候选再进来，理由照旧，不因为今天松了就放行
            blocked.append(dict(blocked_index[pid]))
            continue
        at = model.parse_ts(item["at"])
        day = model.day_key(item["at"])
        required = required_of(item, cfg)
        reason = message = ""
        if not required:
            min_gap_seconds, cap = thresholds(cfg, item["kind"])
            nearest = min((abs((at - other).total_seconds())
                           for other in same_kind.get(item["kind"], [])), default=None)
            if nearest is not None and nearest < min_gap_seconds:
                reason = REASON_MIN_GAP
                message = ("同类型两条计划之间至少隔 %d 分钟，这一条离最近的一条只隔 %d 秒"
                           % (min_gap_seconds // 60, int(nearest)))
            elif per_day[(item["kind"], day)] >= cap:
                reason = REASON_QUOTA_OPTIONAL
                message = ("这是「可有可无」那类的额度：同类型每天最多 %d 条，%s 已经排了 %d 条"
                           "（required 的计划不受这条额度管）"
                           % (cap, day, per_day[(item["kind"], day)]))
        if reason:
            row = {"id": pid, "kind": item["kind"], "at": item["at"], "day": day,
                   REQUIRED_KEY: required, "reason": reason, "message": message,
                   "blocked_at": created}
            data["blocked"].append(row)
            blocked_index[pid] = row
            blocked.append(row)
            continue
        row = {"id": pid, "kind": item["kind"], "at": item["at"], "day": day,
               "status": STATUS_PENDING, REQUIRED_KEY: required, "note": item["note"],
               "created_at": created, "consumed_at": None}
        data["pending"].append(row)
        taken[pid] = STATUS_PENDING
        same_kind.setdefault(item["kind"], []).append(at)
        if not required:
            per_day[(item["kind"], day)] += 1
        added.append(row)

    if added or blocked:
        save(data, root)
    return {"ok": True, "now": created, "added": added, "duplicates": duplicates,
            "blocked": blocked, "counts": counts(data), "plan_ids": plan_ids(data)}


def expire(ids: Iterable[str], *, root: Any = None, now_ts: Any = None,
           reason: str = "") -> Dict[str, Any]:
    """把错过的 pending 计划划进 expired：**不执行、不补跑**，理由写进那一条。

    补跑两晚的夜比不跑更糟：夜是"这一天"的结算，隔天再跑结算的是别人的一天。
    """
    at = model.iso(now_ts) if now_ts is not None else model.now_iso()
    data = load(root)
    wanted = set(str(item) for item in (ids or []))
    moved: List[Dict[str, Any]] = []
    keep: List[Dict[str, Any]] = []
    for row in data["pending"]:
        if str(row.get("id")) in wanted:
            gone = dict(row)
            gone["status"] = STATUS_EXPIRED
            gone["expired_at"] = at
            gone["reason"] = reason
            data["expired"].append(gone)
            moved.append(gone)
        else:
            keep.append(row)
    data["pending"] = keep
    missing = sorted(wanted - {str(row.get("id")) for row in moved})
    if moved:
        save(data, root)
    return {"ok": True, "expired": moved, "missing": missing, "counts": counts(data)}


def consume(ids: Iterable[str], *, root: Any = None, now_ts: Any = None,
            results: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """把指定的 pending 计划划进 consumed（谁用了谁负责调；本函数只动账）。

    results: {计划 id → 结果摘要}，写进那一行的 result 字段——"跑过了"要能说清跑成什么样。
    """
    at = model.iso(now_ts) if now_ts is not None else model.now_iso()
    data = load(root)
    wanted = set(str(item) for item in (ids or []))
    moved: List[Dict[str, Any]] = []
    keep: List[Dict[str, Any]] = []
    for row in data["pending"]:
        if str(row.get("id")) in wanted:
            done = dict(row)
            done["status"] = STATUS_CONSUMED
            done["consumed_at"] = at
            if results and str(row.get("id")) in results:
                done["result"] = results[str(row.get("id"))]
            data["consumed"].append(done)
            moved.append(done)
        else:
            keep.append(row)
    data["pending"] = keep
    missing = sorted(wanted - {str(row.get("id")) for row in moved})
    if moved:
        save(data, root)
    return {"ok": True, "consumed": moved, "missing": missing,
            "counts": counts(data), "plan_ids": plan_ids(data)}


def note_night(ids: Iterable[str], *, root: Any = None, now_ts: Any = None,
               nights: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """把这一次尝试的快照写进那条 pending：**状态不动、不下账**。

    失败的那次留在 pending 等下一 tick——"记一行、继续、consume"等于假装跑过了，
    那一夜的东西就被静默丢掉。快照同时是下一 tick 的输入：只重跑没成功的那几步。
    """
    data = load(root)
    wanted = set(str(item) for item in (ids or []))
    payloads = nights or {}
    touched: List[Dict[str, Any]] = []
    for row in data["pending"]:
        pid = str(row.get("id"))
        if pid in wanted and isinstance(payloads.get(pid), dict):
            row[NIGHT_KEY] = dict(payloads[pid])
            touched.append(row)
    missing = sorted(wanted - {str(row.get("id")) for row in touched})
    if touched:
        save(data, root)
    return {"ok": True, "noted": touched, "missing": missing, "counts": counts(data)}


def never_ran(ids: Iterable[str], *, root: Any = None, now_ts: Any = None,
              reasons: Optional[Dict[str, str]] = None,
              nights: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """把试满次数的 pending 划进 never_ran（第五栏）：不补跑、不留死账、理由写进那一条。

    与 expire 同一套手法：换状态、留原因、把重试快照一并带过去——事后看账就能回答
    "哪一步、试了几次"。
    """
    at = model.iso(now_ts) if now_ts is not None else model.now_iso()
    data = load(root)
    wanted = set(str(item) for item in (ids or []))
    reasons = reasons or {}
    payloads = nights or {}
    moved: List[Dict[str, Any]] = []
    keep: List[Dict[str, Any]] = []
    for row in data["pending"]:
        pid = str(row.get("id"))
        if pid in wanted:
            gone = dict(row)
            gone["status"] = STATUS_NEVER_RAN
            gone["never_ran_at"] = at
            gone["reason"] = str(reasons.get(pid, ""))
            if isinstance(payloads.get(pid), dict):
                gone[NIGHT_KEY] = dict(payloads[pid])
            data["never_ran"].append(gone)
            moved.append(gone)
        else:
            keep.append(row)
    data["pending"] = keep
    missing = sorted(wanted - {str(row.get("id")) for row in moved})
    if moved:
        save(data, root)
    return {"ok": True, "never_ran": moved, "missing": missing,
            "counts": counts(data)}
