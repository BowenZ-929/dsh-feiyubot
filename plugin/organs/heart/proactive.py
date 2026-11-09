"""主动开口的反馈闭环：她**主动**开口之后有没有人理，只记这一件事。

边界（先说清，免得长成第二个东西）：
- 本模块只管「她主动开口之后有没有人理」，**不管她说什么**（嘴的事），
  也**不管几点该开口**（日子的事）。
- 判据是人定的：额度不是必需品，保险丝是。真人的收敛不靠计数，靠
  「说了没人理，就安静一会儿」——所以这里记的是**结果**，不是次数配额：
  连着 mute_after 次主动开口没人回应 → 静音 mute_hours 小时（默认 3 次 / 12 小时）；
  恢复只有两条路：**对方主动找她**（the_other_side_spoke）或**时间到**（is_muted 读时现算）。

数据文件 proactive.json（config 落盘、人可改，改文件即生效、不用重启）：
    {"version": 1, "config": {"mute_after": 3, "mute_hours": 12},
     "streak": 0, "muted_until": null, "last_sent_at": null,
     "history": [{"at": iso, "outcome": "replied"|"ignored"|"pending"}]}

三条铁律：
- **幂等**：同一条未结算的 pending 不重复记——她一次开口就是一条，重复调 sent()
  不会再记成第二次（history 只长一行）。
- **读时现算**：静音到期不落盘、不挂定时器、不起线程。时间一到 is_muted 自己报
  muted=False（muted_until 报 null，因为这一刻它已经不生效了），文件一个字节都不动。
- **结算必须响**：没有 pending 还来结账 → {"ok": False, "code": "no-pending"}，
  绝不静默改数；坏时间戳 / 坏 JSON 一律回默认值，不把调用方炸掉。

streak 只在两种时候动：ignored 加一、replied 清零（the_other_side_spoke 也清零）。
**时间到只解除静音、不清 streak**：静音到期后她若再主动开口一次、仍然没人理，
streak 从 4 起算 → 立刻又静音。这是「说了没人理就安静一会儿」的原意，不是 bug。

config 的两条约束（防人改出「永远静音」）：mute_after 至少 1（否则 replied 那一支
也会当场静音），mute_hours 至少 0。
"""
from __future__ import annotations

import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from organs.heart import changes, model, store

PROACTIVE_FILE = "proactive.json"
VERSION = 1

OUTCOME_REPLIED = "replied"
OUTCOME_IGNORED = "ignored"
OUTCOME_PENDING = "pending"
OUTCOMES = (OUTCOME_REPLIED, OUTCOME_IGNORED, OUTCOME_PENDING)
#: outcome() 只收这两个：pending 是 sent() 写的，归它自己。
SETTLE_OUTCOMES = (OUTCOME_REPLIED, OUTCOME_IGNORED)

#: 本模块的局部参数（只回答「几次算多、安静多久」），故留在这里不进 model.py：
#: model.py 只放跨器官共用的枚举与尺，搬进去会让人以为别的器官也该读它们。
CONFIG_DEFAULTS: Dict[str, Any] = {"mute_after": 3, "mute_hours": 12}

#: overview 只带最近几条 history：账本可以很长，面板一格放不下。
HISTORY_SHOWN = 5


# ---- 内部小工具 ----------------------------------------------------------


def _path(root: Any) -> Path:
    """数据文件路径：root 给了就用 root，没给就用 store.default_root()（实盘）。"""
    base = Path(root) if root is not None else store.default_root()
    return base / PROACTIVE_FILE


def _at(now: Optional[str]) -> str:
    """「现在」：调用方给了就用它，没给就是此刻。"""
    return now if isinstance(now, str) and now else model.now_iso()


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
    """时间戳串 → aware datetime；空 / 坏值 → None（当作没有这个时刻）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return model.parse_ts(value)
    except ValueError:
        return None


def _text_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _plain(number: float) -> Any:
    """整数值的 float 落盘写成 12（不是 12.0）：config 是给人改的，别逗人。"""
    return int(number) if float(number).is_integer() else float(number)


def _config(data: Dict[str, Any]) -> Dict[str, Any]:
    """生效配置：文件里的 config 覆盖默认值，额外键原样保留，两个已知键归一到安全范围。

    mute_after 至少 1：0 会让 replied 那一支也当场静音（streak=0 >= 0），
    「回一句反而被关」显然不是人的本意。
    """
    cfg: Dict[str, Any] = dict(CONFIG_DEFAULTS)
    raw = data.get("config")
    if isinstance(raw, dict):
        cfg.update(raw)
    cfg["mute_after"] = max(1, _int(cfg.get("mute_after"), CONFIG_DEFAULTS["mute_after"]))
    cfg["mute_hours"] = _plain(max(0.0, _num(cfg.get("mute_hours"),
                                             CONFIG_DEFAULTS["mute_hours"])))
    return cfg


def _empty() -> Dict[str, Any]:
    """空状态：config 默认值就在这里，第一次落盘即写进文件（人可改）。"""
    return {"version": VERSION, "config": dict(CONFIG_DEFAULTS), "streak": 0,
            "muted_until": None, "last_sent_at": None, "history": []}


def _clean_history(rows: Any) -> List[Dict[str, Any]]:
    """旧记录保留原口径；新记录可带对象、会话与确认编号，不替旧记录补猜。"""
    cleaned: List[Dict[str, Any]] = []
    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, dict):
            continue
        outcome = raw.get("outcome")
        if outcome not in OUTCOMES:
            continue
        row = {"at": raw.get("at") if isinstance(raw.get("at"), str) else "",
               "outcome": outcome}
        context = raw.get("context")
        if isinstance(context, dict) and all(isinstance(context.get(k), str) and context[k]
                                             for k in ("who", "session_id", "delivery_id")):
            row["context"] = {k: context[k] for k in ("who", "session_id", "delivery_id")}
        cleaned.append(row)
    return cleaned


def _load(root: Any) -> Dict[str, Any]:
    """读并归一化；坏 JSON / 坏字段一律回默认值，不炸调用方。"""
    data = store.read_json(_path(root), _empty())
    version = data.get("version")
    return {
        "version": version if isinstance(version, int) else VERSION,
        "config": data.get("config") if isinstance(data.get("config"), dict) else {},
        "streak": max(0, _int(data.get("streak"), 0)),
        "muted_until": _text_or_none(data.get("muted_until")),
        "last_sent_at": _text_or_none(data.get("last_sent_at")),
        "history": _clean_history(data.get("history")),
    }


def _save(root: Any, data: Dict[str, Any], *, at: str, reason: str, source: str) -> None:
    """整文件覆盖写：config 一并落盘（mute_after / mute_hours 就在那儿，人可改）。"""
    previous = _load(root)
    store.write_json(
        _path(root),
        {"version": VERSION, "config": _config(data), "streak": int(data["streak"]),
         "muted_until": data["muted_until"], "last_sent_at": data["last_sent_at"],
         "history": list(data["history"])},
    )
    changes.record(root, at=at, group='proactive', reason=reason, source=source,
        before={'连续未获回应次数':previous['streak'], '暂停主动开口至':previous['muted_until'],
                '最近主动开口':previous['last_sent_at'], '等待本次回应':_pending_index(previous['history']) is not None},
        after={'连续未获回应次数':data['streak'], '暂停主动开口至':data['muted_until'],
               '最近主动开口':data['last_sent_at'], '等待本次回应':_pending_index(data['history']) is not None})


def _pending_index(history: List[Dict[str, Any]]) -> Optional[int]:
    """最新一条未结算 pending 的下标；没有就是 None。

    正常账本里 pending 只可能出现在末尾（开口 → 结算 → 再开口），
    从后往前找是为了坏账本也能找到那一条，而不是猜"一定在最后一行"。
    """
    for index in range(len(history) - 1, -1, -1):
        if history[index]["outcome"] == OUTCOME_PENDING:
            return index
    return None


def _muted_view(data: Dict[str, Any], at: str) -> Dict[str, Any]:
    """静音读数（读时现算）：到点就算解除，muted_until 一并报 null。

    这是「时间到也恢复」那半条路的唯一实现处：不写盘、不改 streak、不挂定时器。
    """
    stamp = _stamp(data.get("muted_until"))
    moment = _stamp(at) or datetime.now(timezone.utc)
    if stamp is None or stamp <= moment:
        return {"muted": False, "remaining_min": 0.0, "streak": int(data["streak"]),
                "muted_until": None}
    left = round((stamp - moment).total_seconds() / 60.0, 1)
    return {"muted": True, "remaining_min": left, "streak": int(data["streak"]),
            "muted_until": model.iso(stamp)}


# ---- 对外：四条写读口 + 一条总览 -----------------------------------------


def sent(root: Any = None, now: Optional[str] = None, *, context=None) -> Dict[str, Any]:
    """她**主动**开口了：记一条 pending（不碰 streak，结果还没出来）。

    幂等：已经有一条未结算的 pending，就**不重复记**——一次开口只长一行 history，
    返回 {"ok": False, "code": "already-pending"}（不静默，也不覆盖上一条的时刻）。
    调用方（嘴 / 日子）只管在"她开口"那一刻调一次；被判成被动回复的说话**不该**调它。
    """
    at = _at(now)
    data = _load(root)
    if _pending_index(data["history"]) is not None:
        return {"ok": False, "code": "already-pending", "at": at,
                "streak": int(data["streak"]), "muted_until": data["muted_until"],
                "history": len(data["history"])}
    data["history"].append({"at": at, "outcome": OUTCOME_PENDING,
                            **({"context": dict(context)} if context else {})})
    data["last_sent_at"] = at
    _save(root, data, at=at, reason='主动开口已登记，等待回应', source='proactive_sent')
    return {"ok": True, "at": at, "outcome": OUTCOME_PENDING,
            "streak": int(data["streak"]), "muted_until": data["muted_until"],
            "history": len(data["history"])}


def outcome(result: str, root: Any = None, now: Optional[str] = None) -> Dict[str, Any]:
    """结算最新那条 pending：replied → streak 清零；ignored → streak + 1。

    streak 到 config.mute_after → 记 muted_until = now + config.mute_hours 小时，
    并 warnings.warn 一行（谁静的音、安静到什么时候，写清楚）。
    replied 除了清零，还顺手清 muted_until——有人回她，就没有静音的理由了。
    没有 pending 还来结账 → {"ok": False, "code": "no-pending"}：不静默、不改任何数。
    坏 result（不是 replied / ignored）→ ValueError：这是调用方的 bug，要响。
    """
    if result not in SETTLE_OUTCOMES:
        raise ValueError("outcome 只认 %s，收到 %r"
                         % ("、".join(SETTLE_OUTCOMES), result))
    at = _at(now)
    data = _load(root)
    index = _pending_index(data["history"])
    if index is None:
        return {"ok": False, "code": "no-pending", "at": at, "outcome": result}
    cfg = _config(data)
    data["history"][index]["outcome"] = result
    muted = False
    if result == OUTCOME_IGNORED:
        data["streak"] = int(data["streak"]) + 1
    else:
        data["streak"] = 0
        data["muted_until"] = None
    if int(data["streak"]) >= int(cfg["mute_after"]):
        moment = _stamp(at) or datetime.now(timezone.utc)
        until = model.iso(moment + timedelta(hours=_num(cfg["mute_hours"], 0.0)))
        data["muted_until"] = until
        muted = True
        warnings.warn(
            "心·proactive：连续 %d 次主动开口没人回，安静到 %s（有人主动找她就提前结束）"
            % (int(data["streak"]), until),
            RuntimeWarning, stacklevel=2)
    _save(root, data, at=at, reason='主动消息未获回应' if result == OUTCOME_IGNORED else '主动消息收到回应',
          source='proactive_' + result)
    return {"ok": True, "at": at, "outcome": result, "streak": int(data["streak"]),
            "muted": muted, "muted_until": data["muted_until"],
            "mute_after": int(cfg["mute_after"]), "mute_hours": cfg["mute_hours"],
            "history": len(data["history"])}


def receive_reply(who: str, session_id: str, *, root=None, now=None):
    """同对象同会话接话：结算等待，或在已记未回应后恢复；不改旧结论。"""
    at = _at(now)
    data = _load(root)
    index = _pending_index(data["history"])
    waiting = index is not None
    if index is None:
        if (not data['history'] or data['history'][-1]['outcome'] != OUTCOME_IGNORED
                or not (data['streak'] or data['muted_until'])):
            return {"ok": False, "code": "no-pending"}
        index = len(data['history']) - 1
    previous = data["history"][index]
    context = previous.get("context") or {}
    if not who or not session_id or context.get("who") != who or context.get("session_id") != session_id:
        return {"ok": False, "code": "different-context"}
    sent_at, reply_at = _stamp(previous["at"]), _stamp(at)
    if sent_at is None or reply_at is None or reply_at < sent_at:
        return {"ok": False, "code": "before-sent"}
    return dict(outcome(OUTCOME_REPLIED, root=root, now=at) if waiting else
                the_other_side_spoke(root=root, now=at), context=context)


def the_other_side_spoke(root: Any = None, now: Optional[str] = None) -> Dict[str, Any]:
    """**对方主动找她**：立刻清 streak 与 muted_until（恢复条件之一，不等时间到）。

    返回清之前的两个值（streak_before / muted_until_before）——"刚才她是什么状态"
    只能从这里拿到，清完以后可读的已经是 0 / null 了。
    一个字节都不用清时（本来就是 0 / null）尽量不落盘：文件还不存在才写一份空账。
    """
    at = _at(now)
    data = _load(root)
    before_streak = int(data["streak"])
    before_until = data["muted_until"]
    was_muted = _muted_view(data, at)["muted"]
    data["streak"] = 0
    data["muted_until"] = None
    if before_streak or before_until is not None:
        _save(root, data, at=at, reason='对方主动开口，恢复主动交流', source='proactive_incoming')
    return {"ok": True, "at": at, "streak_before": before_streak,
            "muted_until_before": before_until, "was_muted": was_muted,
            "streak": 0, "muted_until": None, "muted": False}


def is_muted(root: Any = None, now: Optional[str] = None) -> Dict[str, Any]:
    """此刻要不要安静：{"muted", "remaining_min", "streak", "muted_until"}。

    **纯读**：到期就地算解除（muted=False、remaining_min=0.0、muted_until=null），
    不写盘、不挂定时器、不清 streak。调用方把这份返回原样塞进 engage 的
    EngageInput.muted，门那边只看 muted 是否为 True。
    """
    return _muted_view(_load(root), _at(now))


def overview(root: Any = None, now: Optional[str] = None) -> Dict[str, Any]:
    """面板 / 接口用的只读汇总：config + streak + muted + 最近 5 条 history。

    纯读：不落笔。没有文件就是出厂默认（history=[]、muted=False），不凭空造一份。
    """
    at = _at(now)
    data = _load(root)
    view = _muted_view(data, at)
    history = data["history"][-HISTORY_SHOWN:] if HISTORY_SHOWN else []
    return {
        "version": data["version"],
        "at": at,
        "config": _config(data),
        "streak": view["streak"],
        "muted": view["muted"],
        "remaining_min": view["remaining_min"],
        "muted_until": view["muted_until"],
        "last_sent_at": data["last_sent_at"],
        "pending": _pending_index(data["history"]) is not None,
        "history": [dict(row) for row in history],
    }
