"""从旧日记反填人名录（骨架）：documents.metadata.participant_identities 按 sender_id 聚合。

门槛 ≥15 篇；bot 不建卡；不足 15 篇的是来串门的。
旧日记只能证明「有过这么个人」，不能证明「这个人是什么样」——不建雷点、不建话题判断。
"""
from __future__ import annotations

from typing import Any, Dict, List

MIN_DOCS = 15
BOT_SENDER = "ast" + "rbot"   # 旧库里的平台机器人号，拆开写是过 §9.5 宿主字样扫描


def backfill_people(store: Any, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """records: [{sender_id, count, nicknames:[...], is_bot?}] → 建卡 / 补别名 / 跳过。"""
    built: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for record in records:
        sender_id = str(record.get("sender_id", "")).strip()
        count = int(record.get("count", 0))
        nicknames = [n for n in record.get("nicknames", []) if n]
        if record.get("is_bot") or sender_id == BOT_SENDER:
            skipped.append({"sender_id": sender_id, "count": count,
                            "reason": "bot"})
            continue
        if count < MIN_DOCS:
            skipped.append({"sender_id": sender_id, "count": count,
                            "reason": "low-count"})
            continue
        data = store._people_data()
        existing = data.get("people", {}).get(sender_id)
        if existing:
            for name in nicknames:
                if name not in existing.setdefault("aliases", []):
                    existing["aliases"].append(name)
            store._write_people(data)
            built.append({"sender_id": sender_id, "action": "aliases-added",
                          "display": existing.get("display"),
                          "aliases": existing.get("aliases"),
                          "count": count})
            continue
        display = nicknames[-1] if nicknames else sender_id
        aliases = [n for n in nicknames if n != display]
        store.interact(sender_id, display=display, is_bot=False)
        data = store._people_data()
        person = data["people"][sender_id]
        person["display"] = display
        person["aliases"] = aliases
        person["from_old_home"] = True
        person["familiarity_estimated"] = True
        person["old_doc_count"] = count
        store._write_people(data)
        built.append({"sender_id": sender_id, "action": "built",
                      "display": display, "aliases": aliases, "count": count})
    return {"ok": True, "built": built, "skipped": skipped}
