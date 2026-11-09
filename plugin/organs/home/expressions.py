"""外部评论表达卡；候选默认关闭，与人物事实、魂和人工锚句分开。"""
import json
import re
from datetime import datetime
from difflib import SequenceMatcher
from . import residents, store


def library_root(root=None):
    return store.home_root(root) / residents.WAREHOUSE_ROOM / "评论表达"


def records(root=None):
    return sorted((json.loads(path.read_text()) for path in library_root(root).glob("*.json")),
                  key=lambda row: row["created_at"], reverse=True)


def save(record, root=None):
    store.atomic_write_text(library_root(root) / (record["id"] + ".json"),
                            json.dumps(record, ensure_ascii=False, indent=2))


def enable(ident, enabled, root=None, *, card_id=None):
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident) or type(enabled) is not bool:
        raise ValueError("表达记录编号或开关不正确")
    path = library_root(root) / (ident + ".json")
    record = json.loads(path.read_text())
    if enabled and record.get("source_rejected"):
        raise ValueError("这组表达的来源已判定有误，需要重新采集，不能采用")
    if enabled and not record["cards"]:
        raise ValueError("这次没有提炼出表达，不能采用")
    if card_id is not None and (not isinstance(card_id, str) or card_id not in {c['id'] for c in record['cards']}):
        raise ValueError("找不到这张表达卡")
    changes = []
    for card in record['cards']:
        card.setdefault('enabled', record['enabled'])
        if card_id is None or card['id'] == card_id:
            if card['enabled'] != enabled or card.get('selection_source') != 'user':
                changes.append({'id':card['id'], 'enabled_before':card['enabled'], 'enabled_after':enabled,
                                'source_before':card.get('selection_source'), 'source_after':'user'})
            card.update(enabled=enabled, selection_source='user')
    record["enabled"] = any(c.get('enabled', record['enabled']) for c in record['cards'])
    if changes:
        record.setdefault('selection_history', []).append({'at':datetime.now().astimezone().isoformat(), 'changes':changes})
    save(record, root)
    return record


def candidates(root=None, *, query=''):
    try:
        rows = records(root)
    except store.StoreError:
        return []  # 未接入家时没有外部语料。
    cards, seen = [], set()
    for row in rows:
        if not row["enabled"] or row.get("source_rejected"):
            continue
        for card in row["cards"]:
            if not card.get("enabled", row["enabled"]) or card["phrase"] in seen:
                continue
            seen.add(card["phrase"])
            cards.append(dict(card, context=row["source"]["context"], source_id=row["id"]))
    if query:
        scored = [(SequenceMatcher(None, query, '\n'.join(card[k] for k in ('phrase', 'meaning', 'when')),
                                   autojunk=False).find_longest_match().size, card) for card in cards]
        # 与自身作品回读同口径；不足三字的重合不优先，平分时保留原来的新近顺序。
        cards = [card for _, card in sorted(scored, key=lambda item: item[0] if item[0] >= 3 else 0,
                                            reverse=True)]
    return cards[:12]


def record_use(cards, *, session_id, delivery_id, root=None):
    from datetime import datetime
    path = library_root(root) / "usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now().astimezone().isoformat(), "kind": "offered_to_model",
                                "session_id": session_id, "delivery_id": delivery_id,
                                "card_ids": [card["id"] for card in cards]}, ensure_ascii=False) + "\n")
