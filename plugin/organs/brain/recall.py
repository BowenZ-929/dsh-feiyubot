"""回想：第一档原文 + 字面检索（instr 子串 + 自建 bigram），装包交 pack.py。

字面引擎不用 SQLite FTS5：trigram 索引不了单字 CJK，unicode61 把连续 CJK 当一个 token，
只能前缀查——instr 子串 + 自建 bigram 是中文上正确的引擎（docs/reference/memory.md §3）。
向量只做重排、永不 gate：接口见 vector_rerank，输入是字面检索已取回的候选，输出只改顺序。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import pack as pack_mod
from .model import (ALPHA_DEFAULT, BETA_DEFAULT, SOURCE_LIVED,
                    SOURCE_SCHEDULED, pack_limits)

TIME_TODAY = ("今天", "刚才", "刚刚", "这会儿")
# 只放硬过去词：说过/记得/原来不是时间词，走 both（今天和长期都查）
TIME_PAST = ("以前", "之前", "昨天", "前几天", "上周", "上个月", "上次")


def compact(text: str) -> str:
    return "".join((text or "").split())


def instr_hit(query: str, text: str) -> bool:
    """instr 子串：等价 SQLite instr(text, query) > 0，中文上唯一正确的内置引擎。"""
    q = compact(query)
    t = compact(text)
    return bool(q) and q in t


def char_bigrams(text: str) -> set:
    text = compact(text)
    if len(text) < 2:
        return set()
    return {text[index:index + 2] for index in range(len(text) - 1)}


def literal_score(query: str, text: str) -> float:
    """字面相关度：instr 子串命中最重，其次字二元组重合。"""
    q = compact(query)
    t = compact(text)
    if not q or not t:
        return 0.0
    if q in t:
        return 6.0
    if t in q:
        return 3.0
    qb = char_bigrams(q)
    if not qb:
        return 0.0
    overlap = len(qb & char_bigrams(t))
    if overlap == 0:
        return 0.0
    return overlap * 3.0 / max(1, len(qb))


def _route(query: str) -> str:
    if any(word in query for word in TIME_TODAY):
        return "today"
    if any(word in query for word in TIME_PAST):
        return "longterm"
    return "both"


def _candidate(entry: Dict[str, Any], source: str) -> Dict[str, Any]:
    source_type = entry.get("source_type") or SOURCE_LIVED
    if source_type not in (SOURCE_LIVED, SOURCE_SCHEDULED):
        source_type = SOURCE_LIVED
    beta = entry.get("beta", BETA_DEFAULT)
    testimony = source_type == SOURCE_LIVED and beta >= 0   # 证词只认 quote（原话）
    note = entry.get("note", "")
    return {
        "id": entry.get("id", ""),
        "source": source,
        "kind": entry.get("kind", "fact"),
        "ts": entry.get("ts", ""),
        "who": entry.get("who", ""),
        "text": entry.get("text", ""),
        "count": entry.get("count", 1),
        "evidence_key": entry.get("evidence_key", ""),
        "source_type": source_type,
        "alpha": entry.get("alpha", ALPHA_DEFAULT),
        "beta": beta,
        "testimony": testimony,
        "note": note,
        "note_full": note,
        "unclear": bool(entry.get("unclear", False)),
        "beta_note": entry.get("beta_note"),
        "note_testimony": False,   # note 永不作证词
        "note_display": ("她理解：" + note) if note else ("（unclear，未理解）" if entry.get("unclear") else "（无理解）"),
    }


def recall(query: str, today_entries: Sequence[Dict[str, Any]],
           longterm_entries: Sequence[Dict[str, Any]],
           hot_entries: Optional[Sequence[Dict[str, Any]]] = None,
           who: Optional[str] = None) -> Dict[str, Any]:
    """回想：时间词路由 → 字面打分 → 装包（分桶配额 + 预算 + 每项 cap）。"""
    q = (query or "").strip()
    if not q:
        return {"query": query, "items": [], "has_material": False,
                "slots_used": 0, "budget": {"slots": pack_limits.slots},
                "skipped": [], "empty_reason": "没材料：query 为空"}
    route = _route(q)
    candidates: List[Dict[str, Any]] = []
    if route in ("today", "both"):
        candidates.extend(_candidate(entry, "today") for entry in today_entries)
    if route in ("longterm", "both"):
        candidates.extend(_candidate(entry, "longterm") for entry in longterm_entries)
    if route in ("hot", "both"):
        candidates.extend(_candidate(entry, "hot") for entry in (hot_entries or []))

    scored: List[Dict[str, Any]] = []
    for entry in candidates:
        score = literal_score(q, entry.get("text", ""))
        if score <= 0:
            continue
        if who and entry.get("who") == who:
            score += 2.0
        if entry.get("source") == "today":
            score += 0.5
        if entry.get("source_type") == SOURCE_SCHEDULED:
            score -= 3.0     # 日程模拟排在亲历之后；只作谈资，不作证词
        item = dict(entry)
        item["score"] = round(score, 3)
        scored.append(item)
    fallback = ""
    if any(word in q for word in ("说过", "记得", "上次", "以前", "之前", "聊过")):
        # 泛回忆问句（"你上次说过什么"）：字面命中之外，把"最近在役"并进候选——
        # 这是"给我看最近说过什么"的语义，不是给具体问题硬凑答案。
        scored_ids = {item.get("id") for item in scored}
        recent = [dict(candidate) for candidate in candidates
                  if candidate.get("id") not in scored_ids]
        for item in recent:
            item["score"] = 0.0
        recent.sort(key=lambda pair: pair.get("ts", ""), reverse=True)
        scored.extend(recent)
        fallback = "recent"

    scored.sort(key=lambda pair: (pair.get("score", 0), pair.get("ts", "")), reverse=True)

    # 去重：同一人同一句只留一条（装包前）
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in scored:
        key = (item.get("who"), item.get("text"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    packed = pack_mod.pack(deduped, limits=pack_limits, query=query)
    packed["fallback"] = fallback
    return packed


def vector_rerank(query: str, candidates: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """向量只做重排、永不 gate：输入是字面检索已取回的候选，输出只改顺序、不删任何一项。

    未接入时原样返回候选。接入后也绝不因为向量分低而把字面候选丢掉——
    向量替换主路的实测是 hit@10 69%→53%（docs/reference/memory.md §2 A）。
    """
    return {"connected": False,
            "candidates": list(candidates),
            "note": "向量重排接口留位，未接入；只做重排、永不 gate"}
