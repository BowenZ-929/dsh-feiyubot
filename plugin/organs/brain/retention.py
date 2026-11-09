"""遗忘曲线：读时结算。参数全部来自 data/retention.json，代码里不写 magic number。

retention = 重要度(α) + min(想起次数×2, 上限) + 近度奖励 − 衰减率/天 × 距写入天数。
主人相关用衰减率 0.12，普通 0.35（默认值，可改数据文件）。
清晰度四档 vivid → clear → fuzzy → trace；fuzzy 只留前 N 字，trace 只留标题，原文在冷仓可捞。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def parse_ts(text: str) -> datetime:
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def days_between(now_ts: str, then_ts: Optional[str]) -> Optional[float]:
    if not then_ts:
        return None
    try:
        return max(0.0, (parse_ts(now_ts) - parse_ts(then_ts)).total_seconds() / 86400.0)
    except (TypeError, ValueError):
        return None


def should_archive(entry: Dict[str, Any], params: Dict[str, Any], now_ts: str) -> bool:
    """同一遗忘判据供只读材料筛选与实际归档使用。"""
    last = entry.get("last_recalled_at") or entry.get("last_accessed_at") or entry.get("ts")
    idle = days_between(now_ts, last)
    return (retention(entry, params, now_ts) < params.get("threshold", 0.08)
            and (idle is None or idle >= params.get("idle_days", 30)))


def load_params(root: Path) -> Dict[str, Any]:
    path = Path(root) / "retention.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def retention(entry: Dict[str, Any], params: Dict[str, Any], now_ts: str) -> float:
    """读时现算，不写回；低于阈值且闲置够久由调用方决定落冷仓。"""
    alpha = entry.get("alpha", entry.get("importance", 1))
    recall_count = entry.get("recall_count", 0)
    recall_cap = params.get("recall_cap", 12)
    base = float(alpha) + min(recall_cap, int(recall_count) * 2)

    last = entry.get("last_recalled_at") or entry.get("last_accessed_at")
    idle = days_between(now_ts, last)
    bonus = 0.0
    if idle is not None:
        if idle <= params.get("recency_bonus_mentioned_days", 2):
            bonus += params.get("recency_bonus_mentioned", 8)
        if idle <= params.get("recency_bonus_recent_days", 7):
            bonus += params.get("recency_bonus_recent", 18)

    age_days = days_between(now_ts, entry.get("ts") or entry.get("created_at")) or 0.0
    who = entry.get("who", "")
    owner = params.get("owner_ids", ["主人"])
    if who in owner:
        rate = params.get("decay_rate_owner", 0.12)
    else:
        rate = params.get("decay_rate_normal", 0.35)
    return round(base + bonus - float(rate) * age_days, 4)


def clarity(value: float, params: Dict[str, Any]) -> str:
    if value >= params.get("clarity_vivid_above", 1.0):
        return "vivid"
    if value >= params.get("clarity_clear_above", 0.5):
        return "clear"
    if value >= params.get("clarity_fuzzy_above", 0.2):
        return "fuzzy"
    return "trace"


def title_of(text: str, params: Dict[str, Any]) -> str:
    text = text or ""
    cap = params.get("title_chars", 12)
    return text[:cap]


def render(entry: Dict[str, Any], value: float, params: Dict[str, Any]) -> str:
    """按清晰度渲染：vivid/clear 给原文；fuzzy 留前 N 字；trace 只留标题。"""
    text = entry.get("text", "")
    stage = clarity(value, params)
    if stage in ("vivid", "clear"):
        return text
    if stage == "fuzzy":
        cap = params.get("fuzzy_chars", 42)
        return text[:cap] + "（细节记不清了）"
    return title_of(text, params) + "（想不起来了）"
