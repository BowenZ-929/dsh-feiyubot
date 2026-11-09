"""回想：第一档原文 + 字面检索（instr 子串 + 自建 bigram），装包交 pack.py。

字面引擎不用 SQLite FTS5：trigram 索引不了单字 CJK，unicode61 把连续 CJK 当一个 token，
只能前缀查——instr 子串 + 自建 bigram 是中文上正确的引擎（docs/reference/memory.md §3）。
向量只做重排、永不 gate：接口见 vector_rerank，输入是字面检索已取回的候选，输出只改顺序。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import pack as pack_mod
from .model import (ALPHA_DEFAULT, BETA_DEFAULT, SIDE_OTHER,
                    SOURCE_LIVED, SOURCE_MIGRATED, SOURCE_SCHEDULED, SOURCE_TYPES,
                    pack_limits, parse_ts, valid_who)

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


def _candidate(entry: Dict[str, Any], source: str) -> Dict[str, Any]:
    source_type = entry.get("source_type") or SOURCE_LIVED
    if source_type not in SOURCE_TYPES:
        source_type = SOURCE_LIVED   # 读取侧兜底；写入侧 _normalize_row 会直接报错
    beta = entry.get("beta", BETA_DEFAULT)
    testimony = source_type == SOURCE_LIVED and beta >= 0   # 证词只认 quote（原话）
    note = entry.get("note", "")
    side = entry.get("side", SIDE_OTHER)
    quote = entry.get("quote", entry.get("text", ""))
    if source_type == SOURCE_MIGRATED:
        display_text = (quote + "（旧家记录）") if quote else "（无原话，来自旧家记录）"
    else:
        display_text = quote
    return {
        "id": entry.get("id", ""),
        "turn_id": entry.get("turn_id", ""),
        "context_refs": entry.get("context_refs", []),
        "source": source,
        "kind": entry.get("kind", "fact"),
        "ts": entry.get("ts", ""),
        # 同一时刻可写成UTC或本地时间；仅排序用，不覆盖原始证据时间。
        "sort_time": parse_ts(entry.get("ts") or "0001-01-01T00:00:00+00:00"),
        "who": entry.get("who", ""),
        "text": entry.get("text", ""),
        "count": entry.get("count", 1),
        "evidence_key": entry.get("evidence_key", ""),
        "source_type": source_type,
        "alpha": entry.get("alpha", ALPHA_DEFAULT),
        "beta": beta,
        "testimony": testimony,
        "side": side,
        "scene": entry.get("scene", "unknown"),
        "audience": entry.get("audience", "当事人"),
        "display_text": display_text,
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
    """回想：在役三层候选 → 字面打分 → 装包（分桶配额 + 预算 + 每项 cap）。"""
    q = (query or "").strip()
    if not q:
        return {"query": query, "items": [], "has_material": False,
                "slots_used": 0, "budget": {"slots": pack_limits.slots},
                "skipped": [], "empty_reason": "没材料：query 为空"}
    # 问句中的时间不等于记录写入时间：今天的偏好可能记在昨天，今天也能纠正上周的事。
    candidates = [_candidate(entry, source) for source, entries in (
        ("today", today_entries), ("longterm", longterm_entries), ("hot", hot_entries or []))
        for entry in entries]

    scored: List[Dict[str, Any]] = []
    for entry in candidates:
        matches = {field: literal_score(q, entry.get(field, "")) for field in ("text", "note")}
        score = max(matches.values())
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
        item["matched_by"] = [field for field, value in matches.items() if value > 0]
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
            item["matched_by"] = ["recent"]
        recent.sort(key=lambda pair: pair["sort_time"], reverse=True)
        scored.extend(recent)
        fallback = "recent"

    # 引文只在同人的指定回合中定位；重复候选层先按实际记忆编号合一。
    by_id = {entry['id']: entry for entry in reversed(candidates) if entry['id']}
    turns = {}
    for entry in by_id.values():
        if entry['turn_id'] and entry['testimony'] and entry['side'] == SIDE_OTHER:
            turns.setdefault((entry['who'], entry['turn_id']), []).append(entry)
    links = {}
    for entry in by_id.values():
        if not entry['note'] or not entry['testimony'] or entry['side'] != SIDE_OTHER or not valid_who(entry['who']):
            continue
        for ref in entry['context_refs']:
            sources = [old for old in turns.get((entry['who'], ref['turn_id']), [])
                       if ref['quote'] in old['text']]
            if len(sources) == 1 and sources[0]['sort_time'] < entry['sort_time']:
                links.setdefault(sources[0]['id'], []).append(entry)
                # 合批理解挂在本人末条；后续引文可能指向同回合的首条。
                carriers = [old for old in turns[(entry['who'], ref['turn_id'])]
                            if old['note']]
                if (len(carriers) == 1 and carriers[0]['id'] != sources[0]['id']
                        and carriers[0]['sort_time'] < entry['sort_time']):
                    links.setdefault(carriers[0]['id'], []).append(entry)
    ranked = {entry['id']: entry for entry in reversed(scored) if entry['id']}
    # 边只从早到晚；按时间走一次即可把查询相关度传给连续的后续更正/承接。
    for source in sorted(by_id.values(), key=lambda item: item['sort_time']):
        origin = ranked.get(source['id'])
        if origin is None:
            continue
        for entry in links.get(source['id'], []):
            item = ranked.get(entry['id'])
            if item is None:
                item = dict(entry, score=origin['score'], matched_by=[])
                ranked[entry['id']] = item
                scored.append(item)
            item['score'] = max(item['score'], origin['score'])
            if 'context_link' not in item['matched_by']:
                item['matched_by'].append('context_link')
            if source['id'] not in item.setdefault('context_from', []):
                item['context_from'].append(source['id'])

    scored.sort(key=lambda pair: (pair.get("score", 0), bool(pair.get('context_from')), pair["sort_time"]), reverse=True)

    # 同一原话可能承接不同前文；理解也相同才去重，避免吞掉后来的更正。
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in scored:
        key = (item.get("who"), item.get("text"), item.get("note"))
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
