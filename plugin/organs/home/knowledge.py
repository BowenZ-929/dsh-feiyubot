"""带来源的外部知识放在家·仓库，独立于人物事实与亲历记忆。

只留一个容器：字幕任务与网页任务同构（`source` + `cards[]`），顶层 `kind` 说明是谁。
分成两种容器就等于给"这张卡算不算事实"留两个答案——同一句 query 同时命中两种来源时，
会按两套规矩排序、说成两种话；而"她是不是亲历过"这件事必须只有一个答案。
"""
import json
import re
from datetime import datetime

from . import residents, store

KIND_SUBTITLE = "subtitle"
KIND_WEB = "web"
SOURCE_LABELS = {KIND_SUBTITLE: "字幕", KIND_WEB: "网页"}


def library_root(root=None):
    return store.home_root(root) / residents.WAREHOUSE_ROOM / "外部知识"


def _kind(job):
    """旧记录没有 `kind`：一律当字幕。

    缺省读在入口而不是靠一次性迁移，是因为实盘的历史文件不许改——改了就分不清
    "本来就是字幕"和"被程序判成字幕"，而这两种在排查来源时是相反的证据。
    """
    return job.get("kind") or KIND_SUBTITLE


def _normalized(job):
    return dict(job, kind=_kind(job))


def jobs(root=None):
    # 排序键缺省给空串：网页卡走另一条写入路径，一个缺 `created_at` 的文件不该让整库召回炸掉。
    return sorted((_normalized(json.loads(p.read_text())) for p in library_root(root).glob("*.json")),
                  key=lambda row: row.get("created_at") or "", reverse=True)


def read(ident, root=None):
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
        raise ValueError("学习记录编号不正确")
    path = library_root(root) / (ident + ".json")
    if not path.is_file():
        raise ValueError("没有这条学习记录")
    return _normalized(json.loads(path.read_text()))


def save(job, root=None):
    # 写侧显式落 `kind`：读侧靠缺省兼容旧件，新件就不必再依赖那份兼容。
    job = _normalized(job)
    store.atomic_write_text(library_root(root) / (job["id"] + ".json"), json.dumps(job, ensure_ascii=False, indent=2))


def save_web(job, root=None):
    """网页任务入库前的唯一一道门：缺 `fetched_at` 的卡一张也不收。

    没有抓取时刻，"这是哪天的"和新鲜度都无从判断，卡进库后只会在聊天里被当成
    事实更新时刻使用——那正是"把归属说错"的起点（任务书 §5 第 2 条）。
    """
    if not isinstance(job, dict):
        raise ValueError("网页知识必须是对象")
    ident = job.get("id")
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
        raise ValueError("网页知识编号不正确")
    if not isinstance(job.get("fetched_at"), str) or not job["fetched_at"].strip():
        raise ValueError("网页知识没有抓取时间，不入库")
    source = job.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("url"), str) or not source["url"].strip():
        raise ValueError("网页知识没有来源网址，不入库")
    cards = job.get("cards")
    if not isinstance(cards, list) or not cards:
        raise ValueError("网页知识没有可用的卡，不入库")
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get("claim"), str) or not card["claim"].strip():
            raise ValueError("网页卡没有给人看的正文，不入库")
        if not isinstance(card.get("id"), str) or not card["id"].strip():
            raise ValueError("网页卡没有编号，不入库")
    job = dict(job, kind=KIND_WEB, source=dict(source, kind=KIND_WEB))
    save(job, root)
    return job


def _dedupe_key(job):
    """同一份外部内容只认最新那次任务（jobs 已按 created_at 倒序）。

    字幕按 (bvid, page) 去重：重做同一集就该盖掉旧结论；网页按 url 去重，
    同一页反复抓也只留最新一张卡，否则一次失败重试会让同一张卡在聊天里排两遍。
    """
    if _kind(job) == KIND_WEB:
        return (KIND_WEB, job["source"].get("url"))
    meta = job["source"]["meta"]
    return (meta.get("bvid"), meta.get("page"))


def _match(card, tokens):
    """标题/标签权重高于正文，英文词与中文二元词一视同仁。

    网页卡不一定有 `title`（它只有一段给人看的 `claim`），所以这里一律按缺省读，
    否则同一句 query 会因为"来源不同"而在这个函数里报错而不是被召回。
    """
    head = ((card.get("title") or "") + " " + " ".join(card.get("tags") or [])).lower()
    claim = (card.get("claim") or "").lower()
    return head, [term for term in tokens if term in head or term in claim]


def _named_directly(job, query):
    """query 里直接贴了视频编号也算找得到；这条只属于字幕——网页没有编号可贴。"""
    if _kind(job) != KIND_SUBTITLE:
        return False
    bvid = job["source"]["meta"].get("bvid") or ""
    return bool(bvid) and bvid in query


def _web_note(published_at):
    """网页卡的对外说法：日期只报页面自己写的那个。

    `fetched_at` 只说明"我今天抓到这一页"，拿它当"这是某日的事实"就是把抓取时刻
    冒充事实更新时刻（任务书 §5 第 2 条）；页面没写日期就说没写，不拿抓取时刻顶上。
    """
    day = (published_at or "").strip() if isinstance(published_at, str) else ""
    return "网页公开资料（%s），不是她的记忆" % ("页面时间 " + day if day else "页面未注明时间")


def _view(job, card, score, matches):
    """两种来源解析成同一个引用视图：来源类型、url、两个时间、卡编号都在同一批键上。

    字段一样，是因为下游（聊天装配、观察页、账）不该为"这张卡从哪来"写第二套分支；
    不一样的部分（字幕的 video_title/owner、网页的 site_name/query）只是各自解析的附带信息。
    """
    kind = _kind(job)
    view = {"job_id": job["id"], "kind": kind, "source_type": SOURCE_LABELS.get(kind, kind),
            "url": None, "published_at": None, "fetched_at": job.get("fetched_at"),
            "card_id": card.get("id"), "card": card, "score": score, "matches": matches}
    if kind == KIND_WEB:
        source = job["source"]
        view.update(url=source["url"], site_name=source.get("site_name") or "",
                    published_at=source.get("published_at") or None, query=source.get("query") or "",
                    source=_web_note(source.get("published_at")))
        return view
    meta = job["source"]["meta"]
    view.update(url=meta.get("url"), published_at=meta.get("published_at") or None,
                video_title=meta["title"], owner=meta["owner"],
                auto_subtitle=meta.get("auto_subtitle", False),
                source="B站字幕提炼，视频观点未独立核验")
    return view


def reuse_web(query, *, session_id, root=None, now=None):
    """仅复用本会话、同一查询、五分钟内最新一批已保存结果。"""
    at = datetime.fromisoformat(now) if now else datetime.now().astimezone()
    found, batch = [], None
    for job in jobs(root):
        if (_kind(job) != KIND_WEB or job.get("session_id") != session_id
                or job["source"].get("query") != query):
            continue
        stamp = job.get("fetched_at")
        if not stamp or not 0 <= (at - datetime.fromisoformat(stamp)).total_seconds() <= 300:
            continue
        batch = batch or stamp
        if stamp == batch:
            found.extend(_view(job, card, 0, []) for card in job["cards"])
    return found


def search(query, *, root=None, limit=3, include_web=True):
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
        # 来源不成形的任务不进召回：字幕在建、网页在抓、文件被手改坏，都不该让整库召回跟着炸。
        # 网页缺 url 就连"哪看的"都答不出，宁可漏召回，也不给一张答不了出处的卡。
        source, kind = job.get("source"), _kind(job)
        if kind == KIND_WEB and not include_web:
            continue
        if not job.get("cards") or not isinstance(source, dict):
            continue
        if (not (source.get("url") or "").strip() if kind == KIND_WEB
                else not isinstance(source.get("meta"), dict)):
            continue
        key = _dedupe_key(job)
        if key in seen:
            continue
        seen.add(key)
        for card in job["cards"]:
            if not isinstance(card, dict):
                continue
            head, matches = _match(card, tokens)
            exact = any(tag.lower() == query_lower.strip() or (len(tag) >= 2 and tag.lower() in query_lower)
                        for tag in (card.get("tags") or []))
            if not exact and len(matches) < 2 and not _named_directly(job, query):
                continue
            score = sum(3 if term in head else 1 for term in matches) + (5 if exact else 0)
            found.append(_view(job, card, score, matches))
    return sorted(found, key=lambda row: -row["score"])[:limit]


def record_use(items, *, session_id, delivery_id, root=None, turn_id=""):
    path = library_root(root) / "usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now().astimezone().isoformat(), "kind": "offered_to_model",
                                "session_id": session_id, "delivery_id": delivery_id, "turn_id": turn_id,
                                # source_kind 只加不改：旧账行没有这一格照样读得懂，
                                # 但观察页要能分清"这句话靠的是字幕还是网页"。
                                "cards": [{"job_id": row["job_id"], "card_id": row["card"]["id"],
                                           "source_kind": row.get("kind", KIND_SUBTITLE),
                                           "matches": row["matches"]} for row in items]}, ensure_ascii=False) + "\n")
