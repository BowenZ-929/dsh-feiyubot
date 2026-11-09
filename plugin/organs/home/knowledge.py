"""带来源的外部知识放在家·仓库，独立于人物事实与亲历记忆。"""
import json
import re
from datetime import datetime

from . import residents, store


def library_root(root=None):
    return store.home_root(root) / residents.WAREHOUSE_ROOM / "外部知识"


def jobs(root=None):
    return sorted((json.loads(p.read_text()) for p in library_root(root).glob("*.json")),
                  key=lambda row: row["created_at"], reverse=True)


def read(ident, root=None):
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
        raise ValueError("学习记录编号不正确")
    path = library_root(root) / (ident + ".json")
    if not path.is_file():
        raise ValueError("没有这条学习记录")
    return json.loads(path.read_text())


def save(job, root=None):
    store.atomic_write_text(library_root(root) / (job["id"] + ".json"), json.dumps(job, ensure_ascii=False, indent=2))


def search(query, *, root=None, limit=3):
    # 适配 dsh-bili-learn text.js：英文词 + 中文二元词，标题/标签优先。
    query_lower = query.lower()
    tokens = set(re.findall(r"[a-z][a-z0-9_+-]+", query_lower))
    for run in re.findall(r"[\u3400-\u9fff]+", query):
        tokens.update(run[i:i+2] for i in range(len(run)-1))
    tokens -= {"这个", "那个", "什么", "怎么", "我们", "可以", "就是", "一个", "今天", "自己", "一下", "知道"}
    try:
        records = jobs(root)
    except store.StoreError:
        return []  # 家尚未接入时没有外部知识；不影响普通聊天。
    found, seen = [], set()
    for job in records:
        if not job.get("cards"):
            continue
        meta = job["source"]["meta"]
        video = (meta["bvid"], meta["page"])
        if video in seen:
            continue
        seen.add(video)
        for card in job["cards"]:
            head = (card["title"] + " " + " ".join(card["tags"])).lower()
            claim = card["claim"].lower()
            matches = [term for term in tokens if term in head or term in claim]
            exact = any(tag.lower() == query_lower.strip() or (len(tag) >= 2 and tag.lower() in query_lower)
                        for tag in card["tags"])
            if not exact and len(matches) < 2 and meta["bvid"] not in query:
                continue
            score = sum(3 if term in head else 1 for term in matches) + (5 if exact else 0)
            found.append({"job_id": job["id"], "video_title": meta["title"], "owner": meta["owner"],
                          "card": card, "score": score, "matches": matches,
                          "source": "B站字幕提炼，视频观点未独立核验", "auto_subtitle": meta.get("auto_subtitle", False)})
    return sorted(found, key=lambda row: -row["score"])[:limit]


def record_use(items, *, session_id, delivery_id, root=None):
    path = library_root(root) / "usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now().astimezone().isoformat(), "kind": "offered_to_model",
                                "session_id": session_id, "delivery_id": delivery_id,
                                "cards": [{"job_id": row["job_id"], "card_id": row["card"]["id"],
                                           "matches": row["matches"]} for row in items]}, ensure_ascii=False) + "\n")
