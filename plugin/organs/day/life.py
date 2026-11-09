"""连续的角色生活计划；借鉴 dsh-daily-schedule，来源见 REFERENCE-LICENSE.txt。

沿用日子器官 04:00 换日和绝对时间。计划只供角色背景，不落真实经历、提醒或发送队列。
生成失败保留状态，自动 tick 每个目标日只尝试一次；重试由人明确触发。
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import model
from ..mind import llm
from ..heart import rest, sleep

# HTTP 手动调整与已有节律线程确实会同时写同一天，写入期间拒绝另一次操作。
_WRITE = threading.Lock()


class LifeError(ValueError):
    pass


def _read(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def config(root=None):
    return _read(model.day_root(root) / "life.json", {"enabled": False})


def _path(day, root):
    # HTTP 的日期只在这个边界校验，路径不能带任意目录。
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise LifeError("日期需要 YYYY-MM-DD")
    return model.day_root(root) / "life-days" / (day + ".json")


def bounds(day, root=None):
    start = datetime.combine(date.fromisoformat(day), datetime.min.time()).replace(
        tzinfo=model.local_tz(), hour=model.limits(root)["day_cutover_hour"])
    return start, start + timedelta(days=1)


def view(now=None, *, day=None, root=None, heart_root=None):
    stamp = model.parse_ts(model.iso(now or model.now_iso()))
    key = day or model.day_key(stamp, model.limits(root)["day_cutover_hour"])
    plan = _read(_path(key, root))
    current = None
    following = None
    if plan:
        for item in plan["items"]:
            if model.parse_ts(item["start"]) <= stamp < model.parse_ts(item["end"]):
                current = item
            elif model.parse_ts(item["start"]) > stamp and following is None:
                following = item
    return {"enabled": config(root)["enabled"], "day": key, "now": stamp.isoformat(),
            "plan": plan, "current": current, "next": following,
            "rest": rest.state(heart_root if heart_root is not None else
                               (Path(root)/'heart' if root is not None else None)),
            "cutover_hour": model.limits(root)["day_cutover_hour"],
            "settings_changes": config(root).get("changes", [])}


def configure(enabled, *, root=None, now=None):
    if type(enabled) is not bool:
        raise LifeError("enabled 需要 true 或 false")
    if not _WRITE.acquire(blocking=False):
        raise LifeError("正在生成或调整日程，请稍后再试")
    try:
        cfg = config(root)
        stamp = model.iso(now or model.now_iso())
        if cfg["enabled"] != enabled:
            cfg.setdefault("changes", []).append({"at": stamp, "before": cfg["enabled"], "after": enabled})
        cfg.update(enabled=enabled, updated_at=stamp)
        _save(model.day_root(root) / "life.json", cfg)
        return cfg
    finally:
        _WRITE.release()


def validate(raw, day, root=None):
    """模型输出一次性校验：分钟偏移映射到绝对时刻，不悄悄补洞或改写冲突。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("title"), str):
        raise LifeError("日程缺少标题")
    items = raw.get("items")
    if not isinstance(items, list) or not 2 <= len(items) <= 40:
        raise LifeError("日程需要 2–40 个连续时间段")
    start, end = bounds(day, root)
    cursor = 0
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            raise LifeError("日程条目需要对象")
        a, b = item.get("start"), item.get("end")
        if type(a) is not int or type(b) is not int or a != cursor or not a < b <= 1440:
            raise LifeError("时间段必须从 0 到 1440 分钟连续排列，不能重叠或留空")
        row = {"start": (start + timedelta(minutes=a)).isoformat(),
               "end": (start + timedelta(minutes=b)).isoformat()}
        for key, limit in (("activity", 80), ("location", 80), ("detail", 300), ("outfit", 120)):
            value = item.get(key, "")
            if not isinstance(value, str) or len(value) > limit or (key == "activity" and not value.strip()):
                raise LifeError("%s 内容为空或过长" % key)
            row[key] = value.strip()
        row["source"] = "model"
        normalized.append(row)
        cursor = b
    if cursor != 1440 or not raw["title"].strip() or len(raw["title"]) > 120:
        raise LifeError("日程需要覆盖完整一天，标题限 120 字")
    return {"title": raw["title"].strip(), "items": normalized,
            "start": start.isoformat(), "end": end.isoformat()}


def generate(day, *, persona, root=None, heart_root=None, now=None, retry=False, complete=None):
    path = _path(day, root)
    stamp = model.iso(now or model.now_iso())
    current_day = model.day_key(stamp, model.limits(root)["day_cutover_hour"])
    tomorrow = (date.fromisoformat(current_day) + timedelta(days=1)).isoformat()
    if day not in (current_day, tomorrow):
        raise LifeError("只生成今天或明天的安排")
    if not _WRITE.acquire(blocking=False):
        raise LifeError("正在生成或调整日程，请稍后再试")
    try:
        old = _read(path)
        if old and (old["status"] == "ready" or not retry):
            return old
        if not persona.strip():
            raise LifeError("缺少角色设定，未调用模型")
        # 请求发出前落盘：重启后 pending 仍可查看，不能自动重复付费调用。
        record = {"day": day, "status": "pending", "source": "model", "items": [],
                  "revision": 0, "changes": [], "attempts": (old or {}).get("attempts", 0) + 1,
                  "attempted_at": stamp}
        run = {"turn_id": uuid4().hex, "attempt": record["attempts"], "at": stamp}
        record["model_runs"] = list((old or {}).get("model_runs", [])) + [run]
        _save(path, record)
        recent = []
        for offset in (3, 2, 1):
            previous = _read(_path((date.fromisoformat(day) - timedelta(days=offset)).isoformat(), root))
            if previous and previous["status"] == "ready":
                recent.append({"day": previous["day"], "planned_title": previous["title"],
                               "planned_activities": [row["activity"] for row in previous["items"]]})
        start, end = bounds(day, root)
        sleep_window = sleep.overview(heart_root if heart_root is not None else
                                      (Path(root)/'heart' if root is not None else None), stamp)['effective_window']
        prompt = (
            "为角色安排连续的一天，作为聊天中的虚构生活背景。符合性格和作息，留休息和发呆，"
            "有小变化，不把每一天都写成闹剧。不编与真实用户的约定、关系变化或用户做过的事。"
            "这些是安排，不是已发生的真实经历，也不是待执行的工具任务。\n"
            "角色设定：\n" + persona[:6000] + "\n最近安排（不代表做过）：" +
            json.dumps(recent, ensure_ascii=False) +
            "\n当前生效睡眠时段（本地钟点，跨午夜时延续到次日）：%02d:00–%02d:00。" % tuple(sleep_window) +
            "按这个时段安排睡眠，其间不要安排看视频、外出等清醒活动。实际休息状态始终优先，"
            "未来作息变化不代表这些计划已执行。" +
            "\n目标日期：" + day + "，覆盖 " + start.isoformat() + " 到 " + end.isoformat() +
            "。start/end 是从起点开始的整数分钟偏移（0–1440，不是钟点），"
            "例如起点 04:00 时，起床 08:00 的偏移是 240。排 8–16 段，连续无空档无重叠，"
            "第一段 start=0，最后一段 end=1440。activity 简短具体，location 地点，"
            "detail 一句状态，outfit 衣着（没有设定可留空）；不要凭空赋予职业或固定人际关系。"
            '\n仅输出 JSON，不要代码围栏：{"title":"一天的概括","items":'
            '[{"start":0,"end":240,"activity":"睡觉","location":"卧室","detail":"还没醒","outfit":""},...]}'
        )
        try:
            with llm.trace_round(run["turn_id"], "day:life:" + day):
                messages = [{"role": "user", "content": prompt}]
                text = complete(messages) if complete else llm.complete(messages, purpose="安排生活背景")
            parsed = validate(json.loads(text), day, root)
        except (llm.LLMError, ValueError, TypeError) as exc:
            record.update(status="failed", error="日程生成未通过：" + type(exc).__name__)
            _save(path, record)
            return record
        record.update(parsed, status="ready", revision=1, generated_at=model.now_iso())
        _save(path, record)
        return record
    finally:
        _WRITE.release()


def tick(*, persona, now=None, root=None, heart_root=None):
    if not config(root)["enabled"]:
        return {"enabled": False}
    stamp = model.parse_ts(model.iso(now or model.now_iso()))
    today = model.day_key(stamp, model.limits(root)["day_cutover_hour"])
    days = [today]
    if stamp.hour >= 22:
        days.append((date.fromisoformat(today) + timedelta(days=1)).isoformat())
    return {"enabled": True, "days": [
        {"day": key, "status": generate(key, persona=persona, now=stamp.isoformat(), root=root,
                                       heart_root=heart_root)["status"]}
        for key in days]}


def adjust(body, *, root=None, now=None):
    if not isinstance(body, dict):
        raise LifeError("调整需要一个对象")
    path = _path(body.get("day"), root)
    fields = {}
    for key, limit in (("activity", 80), ("location", 80), ("detail", 300), ("outfit", 120)):
        value = body.get(key, "")
        if not isinstance(value, str) or len(value) > limit or (key == "activity" and not value.strip()):
            raise LifeError("%s 内容为空或过长" % key)
        fields[key] = value.strip()
    if not _WRITE.acquire(blocking=False):
        raise LifeError("正在生成或调整日程，请稍后再试")
    try:
        plan = _read(path)
        if not plan or plan["status"] != "ready":
            raise LifeError("这一天还没有完整安排")
        if body.get("revision") != plan["revision"]:
            raise LifeError("页面里的日程已变化，请刷新后再修改")
        index = next((i for i, row in enumerate(plan["items"]) if row["start"] == body.get("start")), None)
        if index is None:
            raise LifeError("找不到这个时间段")
        original = plan["items"][index]
        stamp = model.parse_ts(model.iso(now or model.now_iso()))
        if model.parse_ts(original["end"]) <= stamp:
            raise LifeError("已结束的时间段保留原记录，只能修改现在或将来")
        changed = dict(original, **fields, source="manual")
        replacement = [changed]
        if model.parse_ts(original["start"]) < stamp:
            changed["start"] = stamp.isoformat()
            replacement.insert(0, dict(original, end=stamp.isoformat()))
        plan["items"][index:index + 1] = replacement
        plan["revision"] += 1
        plan["changes"].append({"at": stamp.isoformat(), "by": "user", "before": original,
                                "after": changed, "revision": plan["revision"]})
        _save(path, plan)
        return plan
    finally:
        _WRITE.release()


def context(now=None, *, root=None, heart_root=None):
    if not config(root)["enabled"]:
        return ""
    state = view(now, root=root, heart_root=heart_root)
    resting = state['rest']
    if resting['phase'] in ('asleep', 'holding', 'deep'):
        return ('\n[实际休息状态] ' + rest.PHASES[resting['phase']] +
                '。生活背景里的活动只是原安排，没有实际执行证据；不能据此说自己正在做或已经做过。')
    if not state["current"]:
        return ""
    return ("\n[角色生活背景·计划] 当前时间 " + state["now"] +
            "。以下是角色的生活安排，可自然影响闲聊状态；不代表真实执行过任何行为。"
            "不要每轮主动报日程；对方当前的话优先，不能用安排推断用户或编造共同经历。\n" +
            json.dumps({"current": state["current"], "next": state["next"]}, ensure_ascii=False))
