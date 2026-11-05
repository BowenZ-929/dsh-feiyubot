"""拒绝记录：她拒过哪个话题、还冷却多久、今天还剩几次。

产品立场（比代码重要）：拒绝**不是**"整档不回"——那是回避。拒绝的形状是
**只拒这个话题、其余照常、说一句人话、而且她记得自己拒过**。那句人话是嘴的活，
心这一层一个字的台词都不生成：心只做三件——落记录、查冷却、守额度。

边界（硬规则，写在这里就是给自己看的）：
- 冷却只能由时间解除。本模块不 import mood、不 import energy，也没有任何
  "心情好了就提前解除"的路径：mood 升两档、energy 回满，都不改这里一个字节。
- 额度是刹车，不是许可：一天最多拒 daily_quota 次，用完就照常说；用完这件事
  必须 WARN 一行，不许静默吞。
- 冷却随次数变长（growth），但封顶在 max_cooldown_h，不许无限延长。
  "放弃/过期必须与寿命同量级"的机器形态就是 validate_config 那几条：冷却不许
  越过保留期、封顶不许越过保留期、额度至少 1。
- 配置落盘、人可改、改文件即生效；读入永不抛——坏值夹回安全值并 WARN 一行
  （照 heart.md 的 M8：缺字段补默认、越界夹回、解析失败退回内置默认）。

关于话题词表（唯一的例外，钉在明处）：
  拒绝要有话题名，而话题名取自**共用词表** <实盘>/organs/brain/topics.json
  （读不到再看出厂那份 plugin/organs/brain/data/topics.json；HEART_TOPICS_FILE
  可以指向另一份）。词表只取顶层键，只经本文档下方那个单独的限制加载器读这一个
  文件，不碰脑的任何别的路径、文件名、内部 dict：**这是共用词表，不是脑的记忆；
  除此之外心仍不碰脑存储。**

投递方式：只走进程内函数 refuse.ingest(...)。本轮不开任何 HTTP 路由，尤其不加
POST——查询侧（GET）下一轮再做。
"""
from __future__ import annotations

import json
import os
import warnings
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from organs import paths
from organs.day import model as day_model
from organs.heart import model, store

REFUSALS_FILE = "refusals.json"
VERSION = 1

#: 允许的理由：拒绝了话题本身 / 拒绝了某种心情下的对话 / 拒绝了"没人"的场合。
REASONS = ("refused-topic", "refused-mood", "refused-nobody")

ENV_TOPICS_FILE = "HEART_TOPICS_FILE"
TOPICS_FILE = "topics.json"

#: 出厂默认（落盘、人可改；改文件即生效，这些只是"没有文件时的值"）。
DEFAULT_CONFIG: Dict[str, Any] = {
    "cooldown_h": 12.0,
    "retention_days": 7,
    "daily_quota": 3,
    "growth": 1.5,
    "max_cooldown_h": 168.0,
}

#: validate_config 的违规码：冷却与封顶不许越过保留期，额度至少 1，增长至少 1。
BAD_COOLDOWN_OVER_RETENTION = "cooldown-exceeds-retention"
BAD_MAX_COOLDOWN_OVER_RETENTION = "max-cooldown-exceeds-retention"
BAD_QUOTA = "quota-below-one"
BAD_GROWTH = "growth-below-one"
BAD_RETENTION = "retention-below-one-day"
BAD_COOLDOWN = "cooldown-not-positive"


# ---- 内部小工具 ----------------------------------------------------------


def _path(root: Any = None) -> Path:
    """状态文件路径：root 给了就用 root，没给就用 store.default_root()（实盘）。"""
    base = Path(root) if root is not None else store.default_root()
    return base / REFUSALS_FILE


def _as_float(value: Any, default: float) -> float:
    """转 float；bool / None / 坏串一律回默认。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    """转 int；bool / None / 坏串一律回默认。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _config_of(raw: Any) -> Dict[str, Any]:
    """任意形状的 config → 一份完整数值：缺的补默认，**不夹**（夹在 _safe_config）。"""
    data = raw if isinstance(raw, dict) else {}
    return {
        "cooldown_h": _as_float(data.get("cooldown_h"), DEFAULT_CONFIG["cooldown_h"]),
        "retention_days": _as_float(
            data.get("retention_days"), DEFAULT_CONFIG["retention_days"]),
        "daily_quota": _as_int(data.get("daily_quota"), DEFAULT_CONFIG["daily_quota"]),
        "growth": _as_float(data.get("growth"), DEFAULT_CONFIG["growth"]),
        "max_cooldown_h": _as_float(
            data.get("max_cooldown_h"), DEFAULT_CONFIG["max_cooldown_h"]),
    }


def validate_config(config: Any) -> List[str]:
    """配置违规清单（空表 = 合规）。

    这三条就是"放弃/过期必须与寿命同量级"的机器形态：冷却期比保留期还长，
    等于这条记录还没到期就已经被清掉了——那时冷却还在，凭据却没了。
    """
    cfg = _config_of(config)
    retention_h = cfg["retention_days"] * 24.0
    problems: List[str] = []
    if cfg["retention_days"] < 1:
        problems.append(BAD_RETENTION)
    if cfg["cooldown_h"] <= 0:
        problems.append(BAD_COOLDOWN)
    elif cfg["cooldown_h"] > retention_h:
        problems.append(BAD_COOLDOWN_OVER_RETENTION)
    if cfg["max_cooldown_h"] > retention_h:
        problems.append(BAD_MAX_COOLDOWN_OVER_RETENTION)
    if cfg["growth"] < 1.0:
        problems.append(BAD_GROWTH)
    if cfg["daily_quota"] < 1:
        problems.append(BAD_QUOTA)
    return problems


def _safe_config(raw: Any) -> Dict[str, Any]:
    """读进来的配置夹回安全值：冷却与封顶都不越过保留期，额度至少 1，增长至少 1。"""
    cfg = _config_of(raw)
    days = max(1, _as_int(cfg["retention_days"], int(DEFAULT_CONFIG["retention_days"])))
    retention_h = days * 24.0
    cooldown = cfg["cooldown_h"]
    if cooldown <= 0:
        cooldown = min(float(DEFAULT_CONFIG["cooldown_h"]), retention_h)
    cooldown = min(cooldown, retention_h)
    max_cool = max(min(cfg["max_cooldown_h"], retention_h), cooldown)
    return {
        "cooldown_h": cooldown,
        "retention_days": days,
        "daily_quota": max(1, cfg["daily_quota"]),
        "growth": max(1.0, cfg["growth"]),
        "max_cooldown_h": max_cool,
    }


# ---- 共用词表：唯一的那一个例外 ------------------------------------------


def _topic_paths() -> List[Path]:
    """词表候选路径：HEART_TOPICS_FILE > <实盘>/organs/brain > 出厂 data/。

    环境变量一旦设了就只认它：指错了就空表 + WARN，不偷偷回落到别的文件——
    否则人永远说不清当下读到的是哪一份词表。
    """
    raw = os.environ.get(ENV_TOPICS_FILE, "").strip()
    if raw:
        return [Path(raw).expanduser()]
    return [paths.organ_dir("brain") / TOPICS_FILE,
            paths.factory_dir("brain") / TOPICS_FILE]


def vocabulary(root: Any = None) -> List[str]:
    """共用词表的话题名：顶层键，按文件里出现的顺序，去重。

    只读本模块被允许的那一个文件，只取顶层键（关键词表是脑的事，心一个都不看）；
    读不到 / 坏 JSON / 顶层不是对象 → 空表 + 一行 WARN，**永不抛**。
    没有词表就不许拒——那会变成"自己造分类去拒绝别人"。

    root 只为与其余公开函数同形收着：选哪份词表只由 HEART_TOPICS_FILE 与上面两个
    固定路径决定（心的根与脑的词表不是一件事）。
    """
    tried: List[str] = []
    for path in _topic_paths():
        tried.append(str(path))
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        names: List[str] = []
        for key in data:
            if isinstance(key, str) and key and key not in names:
                names.append(key)
        return names
    warnings.warn(
        "心·refuse：读不到话题词表（试过 %s），本次按空表处理——没有词表就不许拒，"
        "别自己造分类" % "；".join(tried),
        RuntimeWarning, stacklevel=2)
    return []


# ---- 状态读写 ------------------------------------------------------------


def _rows(value: Any) -> List[Dict[str, Any]]:
    """把 refusals 读成一撮能用的行；认不出的（缺 since/until、时间解析不了）丢掉。"""
    rows: List[Dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        topic = item.get("topic")
        since = item.get("since")
        until = item.get("until")
        if not isinstance(topic, str) or not topic:
            continue
        if not isinstance(since, str) or not isinstance(until, str):
            continue
        try:
            model.parse_ts(since)
            model.parse_ts(until)
        except ValueError:
            continue
        reason = item.get("reason")
        rows.append({
            "topic": topic,
            "since": since,
            "until": until,
            "reason": reason if reason in REASONS else REASONS[0],
            "count": max(1, _as_int(item.get("count"), 1)),
        })
    return rows


def _read(root: Any, at: str) -> Tuple[Dict[str, Any], List[str]]:
    """读一份可用状态 + 读入时发现的违规：缺的补默认、坏的夹回，永不抛。"""
    data = store.read_json(_path(root), {})
    raw = data.get("config")
    day = data.get("day")
    state = {
        "version": VERSION,
        "config": _safe_config(raw),
        "day": day if isinstance(day, str) and day else day_model.day_key(
            at, cutover_hour=0),
        "used_today": max(0, _as_int(data.get("used_today"), 0)),
        "refusals": _rows(data.get("refusals")),
    }
    return state, validate_config(raw)


# ---- 冷却计算 ------------------------------------------------------------


def _cooldown_hours(config: Dict[str, Any], count: int) -> float:
    """这一次的冷却小时数：cooldown × growth**(count-1)，夹到 max_cooldown_h（封顶即止）。"""
    hours = float(config["cooldown_h"]) * (
        float(config["growth"]) ** max(0, int(count) - 1))
    return min(hours, float(config["max_cooldown_h"]))


def _until_of(since: str, count: int, config: Dict[str, Any]) -> str:
    """解除时刻 = since + 这一次的冷却小时数（从头算，不是在上一次 until 上叠）。"""
    return model.iso(model.parse_ts(since) + timedelta(
        hours=_cooldown_hours(config, count)))


def _remaining_min(until: str, at: str) -> float:
    """离解除冷却还有几分钟（已经到期就是 0.0）：冷却只往时间那一头走。"""
    return round(model.hours_between(at, until) * 60.0, 1)


# ---- 两个公开动作 --------------------------------------------------------


def ingest(topic: str, reason: str, root: Any = None,
           now: Optional[str] = None) -> Dict[str, Any]:
    """记一次拒绝：落库、当日额度 +1，返回这一次的账；额度用完就不落库。

    顺序钉死（别调换）：理由校验 → 话题校验 → 换日 → 额度闸 → 计数 / 延长冷却 →
    清理过期 → 写盘。额度闸在写盘之前，所以"今天拒不动了"这句话一个字节都不写。

    - reason 不在 REASONS → ValueError（原样带出坏值）；
    - topic 不在共用词表 → ValueError：话题必须取自脑的话题词表，别自己造分类；
    - 额度用完 → 不落库，返回 {"ok": False, "code": "quota-exhausted", ...} 并 WARN；
    - 成功返回 {"ok": True, "topic", "reason", "count", "since", "until",
      "remaining_min", "used_today", "quota", "violations"}。
    """
    if reason not in REASONS:
        raise ValueError(
            "不认识的理由：%r（只认 %s）" % (reason, "、".join(REASONS)))
    names = vocabulary()
    if topic not in names:
        raise ValueError(
            "话题必须取自脑的话题词表，别自己造分类：%r 不在词表里（现读 %d 个：%s）"
            % (topic, len(names), "、".join(names) or "空表"))

    at = now if isinstance(now, str) and now else model.now_iso()
    state, violations = _read(root, at)
    if violations:
        warnings.warn(
            "心·refuse：refusals.json 的 config 违规（%s），已夹回安全值——"
            "冷却与封顶都不许越过保留期" % "、".join(violations),
            RuntimeWarning, stacklevel=2)
    config = state["config"]
    quota = int(config["daily_quota"])

    today = day_model.day_key(at, cutover_hour=0)
    if state["day"] != today:
        state["day"] = today
        state["used_today"] = 0
    used = state["used_today"]
    if used >= quota:
        warnings.warn(
            "心·refuse：今天的次数用完了（%d/%d），话题 %r 这次不落库——额度是刹车，"
            "不是一直拒的许可" % (used, quota, topic),
            RuntimeWarning, stacklevel=2)
        return {
            "ok": False,
            "code": "quota-exhausted",
            "message": "今天的次数用完了（%d/%d）" % (used, quota),
            "remaining_min": None,
        }

    cooling_row = None
    for item in state["refusals"]:
        if item["topic"] == topic:
            cooling_row = item
            break
    if cooling_row is not None and model.parse_ts(cooling_row["until"]) > model.parse_ts(at):
        cooling_row["count"] += 1
        cooling_row["reason"] = reason
        cooling_row["until"] = _until_of(cooling_row["since"], cooling_row["count"], config)
    else:
        cooling_row = {
            "topic": topic,
            "since": at,
            "until": _until_of(at, 1, config),
            "reason": reason,
            "count": 1,
        }
        state["refusals"] = [r for r in state["refusals"] if r["topic"] != topic]
        state["refusals"].append(cooling_row)

    state["used_today"] = used + 1
    keep_from = model.parse_ts(at) - timedelta(days=int(config["retention_days"]))
    state["refusals"] = [r for r in state["refusals"]
                         if model.parse_ts(r["since"]) >= keep_from]
    store.write_json(_path(root), state)

    return {
        "ok": True,
        "topic": cooling_row["topic"],
        "reason": cooling_row["reason"],
        "count": cooling_row["count"],
        "since": cooling_row["since"],
        "until": cooling_row["until"],
        "remaining_min": _remaining_min(cooling_row["until"], at),
        "used_today": state["used_today"],
        "quota": quota,
        "violations": violations,
    }


def check(topics: Sequence[str], root: Any = None,
          now: Optional[str] = None) -> Dict[str, Any]:
    """只读：这些话题冷不冷、还差几分钟、今天额度还剩几次。**一个字段都不写盘。**

    **冷却只能由时间解除**：这里只比 until 与 now 的先后，不读 mood、不读 energy，
    也不留"心情好了就提前解除"的参数。拒绝过就是拒绝过——时间到了才算完。

    返回 {"as_of", "items": [{"topic", "known", "cooling", "remaining_min",
    "reason", "count"}], "cooling": [...], "quota": {"used_today", "limit", "left"}}。
    """
    at = now if isinstance(now, str) and now else model.now_iso()
    state, _violations = _read(root, at)
    config = state["config"]
    quota = int(config["daily_quota"])
    today = day_model.day_key(at, cutover_hour=0)
    used = state["used_today"] if state["day"] == today else 0
    rows = {row["topic"]: row for row in state["refusals"]}
    names = set(vocabulary())
    items: List[Dict[str, Any]] = []
    cooling: List[str] = []
    for topic in topics or []:
        row = rows.get(topic)
        is_cooling = bool(row) and model.parse_ts(row["until"]) > model.parse_ts(at)
        items.append({
            "topic": topic,
            "known": topic in names,
            "cooling": is_cooling,
            "remaining_min": _remaining_min(row["until"], at) if is_cooling else 0.0,
            "reason": row["reason"] if row else None,
            "count": row["count"] if row else 0,
        })
        if is_cooling:
            cooling.append(topic)
    return {
        "as_of": at,
        "items": items,
        "cooling": cooling,
        "quota": {"used_today": used, "limit": quota,
                  "left": max(0, quota - used)},
    }
