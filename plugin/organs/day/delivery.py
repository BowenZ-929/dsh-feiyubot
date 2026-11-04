"""送达：判据是「这一轮真的开始了」，不是「调用没抛错」。

判据（先写在这里，别处不许再判一次；出处 docs/reference/repos/dsh-world.md §3.4）：

1. **发出前先落盘**。intake 先把一条 state=expect 的记录写进 pending.json，再返回
   send_request：落盘是「发之前」的动作，不是「发之后」的补记。崩在发之前，账上仍是
   一条 expect，重启后能对账；崩在发之后也不谎报送达。
2. **发出不等于送达**。send_fn 返回/不抛错只证明「请求递出去了」，不证明对方那一轮
   走到了第一步。所以 intake 之后 state 仍是 expect，只有通道 ack 的 retcode==0 才转
   delivered。retcode!=0 时记 ack、退回 expect 并立刻记一条 reason——**不许**把它洗成
   送达（通道回了非 0 就是这一轮没开始）。
3. **重启后对账三路**（reconcile）：
   - delivered → 计入 delivered，永不 requeue；
   - expect 且最后尝试未超 stuck_minutes → watching，一个字节不动；
   - expect 且超时（且已过 next_try_at）→ requeue：attempts+1、next_try_at = now +
     退避（基数 × 2^(n-1)，封顶 backoff_cap_minutes）、reason 记在条目里。
   卡住判据从 **last_try_at**（最后一次真的发出）算，不从 created_at 算、也不被 ack
   或 requeue 刷新——刷新它等于把「重送成功」写进账里，那是谎话。
4. **不收窄成单飞**。原作者「同时只允许一件待投递」是被他自己列进「代价」的一条：
   一个 pending 就堵住整条投递链（dsh-world.md §3.4「代价」）。我们这边节律线程与夜跑
   都可能到点想开口，单飞会让第二句一直等到第一句确认，而确认可能整个重启窗口都等不到。
   所以本模块允许多件同时在途；代价是 ack 必须带着 item_id 回（不能只有一个全局 slot），
   reconcile 必须逐条分流，不能只看一个位置。
5. **不接真发**。默认 sink 只记日志（logging）；send_fn 可注入，日后接嘴/手时从外面塞。
   send_fn 抛错也只是「没发出去」：记 reason、留在 expect，不把异常穿给调用方。
6. 阈值默认值写在 DEFAULTS 兜底，实际取值读 data/delivery.json（与 limits.json 同一套
   规矩：改参数不改代码）。时间一律可注入 now_ts；root 可注入；测试只写临时目录。
7. 四条入口（pending / intake / ack / reconcile）缺参、坏参、账坏了都回
   {"ok": false, "message": ...}，不许抛未捕获异常、返回一律 json 安全。

pending.json（运行时文件，不提交）的形状：
    {"version": 1, "items": [行]}
    行：{id, text, to, created_at, attempts, state, next_try_at, last_try_at, ack, echo, reason}
    - id：这一条的唯一名字（ack 靠它对回来）
    - created_at：入队时刻（第一次落盘）
    - last_try_at：最后一次「真的发出」的时刻（卡住判据从它算）
    - next_try_at：允许重送的最早时刻（退避排出来的）
    - ack：通道回报原样 {"retcode": n, "at": iso}；None 表示还没 ack 过
    - reason：最近一次失败原因（ack 非 0 / send_fn 抛错 / requeue 的理由）
    - echo：发出去时带回来的回声（等于 id；日后接通道时比对用）
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import model

LOG = logging.getLogger(__name__)

PENDING_FILE = "pending.json"
CONFIG_FILE = "delivery.json"
VERSION = 1
STATE_EXPECT = "expect"
STATE_DELIVERED = "delivered"

# 兜底默认值：盘上的 delivery.json 读不到、写坏了，就按这一份跑
DEFAULTS: Dict[str, Any] = {
    "version": VERSION,
    "stuck_minutes": 5,          # 最后一次尝试之后多久还没 ack 算卡住（作者默认 5 分钟）
    "backoff_minutes": 2,        # 退避基数：第 n 次重送等 基数 × 2^(n-1) 分钟
    "backoff_cap_minutes": 32,   # 退避封顶
}
CONFIG_KEYS = ("stuck_minutes", "backoff_minutes", "backoff_cap_minutes")

SendFn = Callable[[Dict[str, Any]], Any]


class DeliveryError(ValueError):
    """待说队列坏了：调用方要看见，不许当空队列继续跑（空队列等于把没确认的那句丢掉）。"""


# ---- 阈值 -----------------------------------------------------------------


def config_path(root: Any = None) -> Path:
    """阈值配置：注入的根里有一份就用它，否则读包内 data/delivery.json（测试不必复制配置）。"""
    own = model.day_root(root) / CONFIG_FILE
    return own if own.exists() else model.DATA_DIR / CONFIG_FILE


def load_config(root: Any = None, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读阈值：整份缺就兜底 DEFAULTS；坏值按 DEFAULTS 那一项修，不抛。"""
    if isinstance(config, dict) and config:
        values = dict(DEFAULTS)
        values.update(config)
    else:
        values = dict(DEFAULTS)
        try:
            loaded = json.loads(config_path(root).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            values.update(loaded)
    for name in CONFIG_KEYS:
        try:
            values[name] = max(0, int(values[name]))
        except (TypeError, ValueError):
            values[name] = int(DEFAULTS[name])
    return values


def backoff_minutes(attempts: Any, config: Optional[Dict[str, Any]] = None) -> int:
    """第 n 次重送要等几分钟：基数 × 2^(n-1)，封顶。attempts 从 1 起算。"""
    cfg = config or load_config()
    base = int(cfg["backoff_minutes"])
    cap = int(cfg["backoff_cap_minutes"])
    try:
        delay = base * (2 ** max(0, int(attempts) - 1))
    except (OverflowError, ValueError):
        delay = cap
    return min(delay, cap) if cap > 0 else delay


# ---- 队列 -----------------------------------------------------------------


def pending_file(root: Any = None) -> Path:
    """待说队列的位置：日子线数据根下的 pending.json。"""
    return model.day_root(root) / PENDING_FILE


def _empty() -> Dict[str, Any]:
    return {"version": VERSION, "items": []}


def load(root: Any = None) -> Dict[str, Any]:
    """读队列：文件不在就是空队列（第一次跑没有文件是正常的），读坏了才抛 DeliveryError。"""
    target = pending_file(root)
    if not target.exists():
        return _empty()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeliveryError("待说队列读不出来：%s（%s）" % (target, exc))
    if not isinstance(raw, dict):
        raise DeliveryError("待说队列顶层要是对象：%s" % target)
    rows = raw.get("items")
    items = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    for row in items:
        if not isinstance(row.get("id"), str) or not row["id"]:
            raise DeliveryError("待说队列里有一行缺 id（%s）：行不齐就 ack 不回来" % target)
        if not isinstance(row.get("text"), str):
            raise DeliveryError("待说队列里 %s 那行缺 text（%s）" % (row["id"], target))
        if row.get("state") not in (STATE_EXPECT, STATE_DELIVERED):
            raise DeliveryError("待说队列里 %s 那行的 state 不认识：%r"
                                % (row["id"], row.get("state")))
        try:
            model.parse_ts(row.get("created_at"))
        except ValueError as exc:
            raise DeliveryError("待说队列里 %s 那行的 created_at 读不出来（%s）"
                                % (row["id"], exc))
    return {"version": VERSION, "items": items}


def save(data: Dict[str, Any], root: Any = None) -> Path:
    """整份换掉：先写同目录临时件再 os.replace（fsync 过），中途崩了不留半个队列。"""
    target = pending_file(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = target.with_name(".%s.tmp-%d-%s" % (target.name, os.getpid(), secrets.token_hex(4)))
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(target))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    return target


# ---- 小工具 ---------------------------------------------------------------


def _now_dt(now_ts: Any = None) -> datetime:
    """注入的时刻（没有就问真实时钟）；坏值抛 ValueError，由入口译成 ok:false。"""
    return model.parse_ts(now_ts) if now_ts is not None else model.parse_ts(model.now_iso())


def counts(data: Dict[str, Any]) -> Dict[str, int]:
    """按 state 分组计数（外加 total）；以后加了状态也照实报，不吞。"""
    tally = Counter(str(item.get("state") or STATE_EXPECT) for item in data.get("items", []))
    report: Dict[str, int] = {STATE_EXPECT: tally.get(STATE_EXPECT, 0),
                              STATE_DELIVERED: tally.get(STATE_DELIVERED, 0)}
    for state, number in tally.items():
        report.setdefault(state, number)
    report["total"] = len(data.get("items", []))
    return report


def _new_id(taken: Any) -> str:
    """队列里没出现过的 id（冲突了再抽一次；同一毫秒两条也不会撞）。"""
    seen = set(taken)
    while True:
        candidate = "dl-" + secrets.token_hex(6)
        if candidate not in seen:
            return candidate


def _trace(item: Dict[str, Any], action: str) -> Dict[str, Any]:
    """对账里一条的去向（拷贝一份，调用方改不动账）。"""
    return {"id": item.get("id"), "action": action, "state": item.get("state"),
            "attempts": item.get("attempts"), "next_try_at": item.get("next_try_at"),
            "last_try_at": item.get("last_try_at"), "reason": item.get("reason") or "",
            "ack": item.get("ack")}


def _log_sink(request: Dict[str, Any]) -> None:
    """默认 sink：不发网络，只记一行日志（真发由外面注入 send_fn）。"""
    LOG.info("送达·发出（未接真发）：id=%s to=%s 字数=%d",
             request.get("id"), request.get("to") or "-", len(str(request.get("text") or "")))


def _send(request: Dict[str, Any], send_fn: Optional[SendFn] = None) -> "tuple[bool, str]":
    """真的把请求递出去：成功 (True, "")；抛错 (False, reason)。异常不穿给调用方。"""
    sink = send_fn if send_fn is not None else _log_sink
    try:
        sink(dict(request))
    except Exception as exc:  # noqa: BLE001 - 通道/手 的错一律只记原因，不许炸掉这一轮
        return False, "send_failed: %s: %s" % (type(exc).__name__, exc)
    return True, ""


def _persist(data: Dict[str, Any], root: Any) -> Optional[str]:
    """落盘；写不进去回一句 message（入口拿去回 ok:false），成功回 None。"""
    try:
        save(data, root)
    except OSError as exc:
        return "待说队列写不进去：%s（%s）" % (pending_file(root), exc)
    return None


# ---- 四条入口 -------------------------------------------------------------


def intake(text: Any, *, to: Any = "", now_ts: Any = None, root: Any = None,
           send_fn: Optional[SendFn] = None) -> Dict[str, Any]:
    """入队一句待说：先落盘 expect，再递 send_request；返回的是「发出去了」，不是「送达了」。

    - to 是收件人标识（空串表示默认对象），今天只落账不解释。
    - send_fn 可注入（日后接嘴/手）；缺省 sink 只记日志，不发网络。
    - 发失败（send_fn 抛错）时账上仍是一条 expect，只把 reason 记下，不谎报送达。
    """
    body = str(text if text is not None else "").strip()
    if not body:
        return {"ok": False, "message": "intake 要一条非空的 text（这一轮要说什么）"}
    try:
        stamp = model.iso(_now_dt(now_ts))
    except ValueError as exc:
        return {"ok": False, "message": "now_ts 不是合法 ISO8601：%s" % exc}
    try:
        data = load(root)
    except DeliveryError as exc:
        return {"ok": False, "message": str(exc)}
    item: Dict[str, Any] = {
        "id": _new_id(row.get("id") for row in data["items"]),
        "text": body,
        "to": str(to if to is not None else ""),
        "created_at": stamp,
        "attempts": 0,
        "state": STATE_EXPECT,
        "next_try_at": stamp,
        "last_try_at": stamp,
        "ack": None,
        "echo": "",
        "reason": "",
    }
    item["echo"] = item["id"]
    # 先落盘再发：这一步之后才允许碰通道
    data["items"].append(item)
    problem = _persist(data, root)
    if problem:
        return {"ok": False, "message": problem}
    request = {"id": item["id"], "text": item["text"], "to": item["to"], "echo": item["echo"]}
    sent, reason = _send(request, send_fn)
    if not sent:
        item["reason"] = reason
        _persist(data, root)
    return {"ok": True, "now": stamp, "item": dict(item), "send_request": request,
            "sent": sent, "state": item["state"], "counts": counts(data),
            "root": str(model.day_root(root))}


def ack(item_id: Any, retcode: Any, *, now_ts: Any = None, root: Any = None) -> Dict[str, Any]:
    """记一条通道 ack：retcode==0 才 delivered；非 0 退回 expect 并记 reason（不算送达）。"""
    key = str(item_id if item_id is not None else "").strip()
    if not key:
        return {"ok": False, "message": "ack 要一个 item_id（哪一条的通道回执）"}
    try:
        code = int(str(retcode).strip())
    except (TypeError, ValueError):
        return {"ok": False, "message": "retcode 要一个整数（通道原样回报的码），收到 %r" % (retcode,)}
    try:
        stamp = model.iso(_now_dt(now_ts))
        data = load(root)
    except ValueError as exc:
        return {"ok": False, "message": "now_ts 不是合法 ISO8601：%s" % exc}
    except DeliveryError as exc:
        return {"ok": False, "message": str(exc)}
    for item in data["items"]:
        if item.get("id") != key:
            continue
        item["ack"] = {"retcode": code, "at": stamp}
        if code == 0:
            item["state"] = STATE_DELIVERED
            item["reason"] = ""
        else:
            item["state"] = STATE_EXPECT
            item["reason"] = ("ack_retcode_%d: 通道回了非 0，这一轮没开始，不算送达" % code)
        problem = _persist(data, root)
        if problem:
            return {"ok": False, "message": problem}
        return {"ok": True, "now": stamp, "delivered": code == 0, "item": dict(item),
                "counts": counts(data), "root": str(model.day_root(root))}
    return {"ok": False, "message": "队列里没有这一条待说：%s" % key, "item_id": key}


def pending(now_ts: Any = None, *, root: Any = None) -> Dict[str, Any]:
    """队列快照：按 state 分组计数 + 全部条目（只读，一个字节都不改）。"""
    try:
        stamp = model.iso(_now_dt(now_ts))
        data = load(root)
    except ValueError as exc:
        return {"ok": False, "message": "now_ts 不是合法 ISO8601：%s" % exc}
    except DeliveryError as exc:
        return {"ok": False, "message": str(exc)}
    items = [dict(item) for item in data["items"]]
    report = counts(data)
    report.pop("total", None)
    return {"ok": True, "now": stamp, "root": str(model.day_root(root)),
            "counts": counts(data), "by_state": report, "items": items}


def reconcile(now_ts: Any = None, *, root: Any = None,
              config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """对账一次：delivered / watching / requeued 三路逐条分流。

    - delivered：计入，永不 requeue；
    - expect 且「最后一次尝试」距今未超 stuck_minutes：watching，一个字节不动；
    - expect 且超时（且已过 next_try_at）：requeue —— attempts+1、
      next_try_at = now + 退避（指数增长、封顶）、reason 记在条目里。
    next_try_at 是重复调用不会把 attempts 刷上去的闸：退避期间再来一次只能算 watching。
    """
    cfg = load_config(root, config)
    stuck_seconds = int(cfg["stuck_minutes"]) * 60
    try:
        at = _now_dt(now_ts)
        data = load(root)
    except ValueError as exc:
        return {"ok": False, "message": "now_ts 不是合法 ISO8601：%s" % exc}
    except DeliveryError as exc:
        return {"ok": False, "message": str(exc)}

    tally = {"delivered": 0, "watching": 0, "requeued": 0}
    traces = []
    changed = False
    for item in data["items"]:
        if item.get("state") == STATE_DELIVERED:
            tally["delivered"] += 1
            traces.append(_trace(item, "delivered"))
            continue
        last = model.parse_ts(item.get("last_try_at") or item.get("created_at"))
        next_try = model.parse_ts(item["next_try_at"]) if item.get("next_try_at") else last
        elapsed = (at - last).total_seconds()
        if elapsed <= stuck_seconds or at < next_try:
            # 还在途（或退避中）：观察，不动。发出≠送达的核心就在这一步不乱写账。
            tally["watching"] += 1
            traces.append(_trace(item, "watching"))
            continue
        attempt = int(item.get("attempts") or 0) + 1
        delay = backoff_minutes(attempt, cfg)
        item["attempts"] = attempt
        item["next_try_at"] = model.iso(at + timedelta(minutes=delay))
        item["reason"] = ("requeue_%d: 超时未 ack（最后一次尝试已过 %d 秒，上限 %d 秒），"
                          "退避 %d 分钟后再送" % (attempt, int(elapsed), stuck_seconds, delay))
        changed = True
        tally["requeued"] += 1
        traces.append(_trace(item, "requeued"))
    if changed:
        problem = _persist(data, root)
        if problem:
            return {"ok": False, "message": problem}
    return {"ok": True, "now": model.iso(at), "root": str(model.day_root(root)),
            "delivered": tally["delivered"], "watching": tally["watching"],
            "requeued": tally["requeued"], "counts": counts(data), "items": traces}
