"""召回装包：按类别配额分桶 → 预算内装填 → 每项 cap → 超预算跳过。

名额沿用 docs/modules/brain.md：一条纲占 1 个名额、顶 3–6 条事实且必须带 2 个目；
普通条目 1 条占 1 个名额。名额上限将来由嘴的注入预算反推，这里先写成默认数。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .model import PackLimits, pack_limits

BUCKETS = ("today", "hot", "longterm", "outline")


def _bucket_of(candidate: Dict[str, Any]) -> str:
    if candidate.get("kind") == "outline":
        return "outline"
    source = candidate.get("source", "longterm")
    return source if source in ("today", "hot", "longterm") else "longterm"


def _cap_text(text: str, cap: int) -> str:
    text = text or ""
    if len(text) <= cap:
        return text
    return text[: max(0, cap - 1)] + "…"


def pack(candidates: Sequence[Dict[str, Any]],
         limits: Optional[PackLimits] = None,
         query: str = "") -> Dict[str, Any]:
    """候选 → 装好的包：quota 分桶、预算内装填、每项 cap、超预算跳过并记录。"""
    limits = limits or pack_limits
    quotas = {"today": limits.today_quota, "hot": limits.hot_quota,
              "longterm": limits.longterm_quota, "outline": limits.outline_quota}
    used: Dict[str, int] = {bucket: 0 for bucket in BUCKETS}
    items: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    slots_used = 0

    for candidate in candidates:
        bucket = _bucket_of(candidate)
        reason = ""
        if slots_used >= limits.slots:
            reason = "budget_full"
        elif used[bucket] >= quotas[bucket]:
            reason = "quota_full"
        if reason:
            # dropped(remaining/need)：被挤掉时记录"还剩多少 / 它需要多少"，不许静默丢
            skipped.append({
                "id": candidate.get("id", ""),
                "bucket": bucket,
                "reason": reason,
                "remaining": {"slots": limits.slots - slots_used,
                              "quota": quotas[bucket] - used[bucket]},
                "need": {"slots": 1},
            })
            continue
        raw_text = candidate.get("text", "")
        text = _cap_text(raw_text, limits.per_item_cap)
        items.append({
            "id": candidate.get("id", ""),
            "source": candidate.get("source", "longterm"),
            "kind": candidate.get("kind", "fact"),
            "bucket": bucket,
            "who": candidate.get("who", ""),
            "ts": candidate.get("ts", ""),
            "text": text,
            "full_text": raw_text,   # 判定用完整正文；cap 只截注入文本，不参与判定
            "capped": len(candidate.get("text", "")) > limits.per_item_cap,
            "score": candidate.get("score"),
            "slot": 1,   # 纲与普通条目都占 1 个名额
            "evidence_key": candidate.get("evidence_key", ""),
            "backed": bool(candidate.get("evidence_key", "")),
            "source_type": candidate.get("source_type", ""),
            "alpha": candidate.get("alpha"),
            "beta": candidate.get("beta"),
            "testimony": candidate.get("testimony", False),
            "backing_note": "无记录背书" if not candidate.get("evidence_key", "") else "",
            "note": _cap_text(candidate.get("note", ""), limits.per_item_cap),
            "note_full": candidate.get("note_full", candidate.get("note", "")),
            "unclear": bool(candidate.get("unclear", False)),
            "beta_note": candidate.get("beta_note"),
            "note_testimony": False,
            "note_display": candidate.get("note_display", ""),
        })
        used[bucket] += 1
        slots_used += 1

    return {
        "query": query,
        "items": items,
        "has_material": bool(items),
        "slots_used": slots_used,
        "budget": {"slots": limits.slots, "per_item_cap": limits.per_item_cap,
                   "quotas": quotas},
        "skipped": skipped,
        "empty_reason": "" if items else "没材料：字面检索没命中，不硬凑",
    }
