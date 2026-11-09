"""把三块连成一条链：判据说该查 → 手去查 → 家落卡 → 这一轮的答案用得着。

一件事只在这一层做：**顺序**。六道门不在这里（`search.decide` 判），外发与手账不在这里
（`hand.actions.lookup` 做），卡与引用视图不在这里（`home.knowledge` 管）。本模块只把它们
按对的顺序叫一遍，并保证下面三件不许走样的事：

一、**没有回合编号就不查**。判据层「每轮最多一次」的封顶要靠 `turn_id` 记账，缺了它那条
   判据就退化成不限次——把一条已经定死的规矩悄悄关掉，是这类接线最容易犯的错。所以缺
   `turn_id` 直接不查，并在 `reason` 里写明；**不降级成「照样查」**。

二、**落卡只经 `knowledge.save_web`**。那是唯一带 `fetched_at` 闸的入库门；`knowledge.save`
   是字幕流程的未校验入口，网页卡走它就等于绕过「没有抓取时刻不许入库」。
   `tests/test_lookup_wiring.py` 有一道源码断言：本文件里不许出现那一调用的字样。

三、**落卡失败不许吞**。查到却没落盘与根本没查是两回事（前者可能已经计费），所以状态分四格
   报出去：`not_requested / failed / searched_not_saved / saved`，而**这一轮仍用内存里的结果
   回答**——钱已经花了，再把结果藏起来是双输。

视图键与 `knowledge.search()` 同一套（直接借它自己的 `_view`）：下游把本轮结果并进 `learned`
就够了，不必为「这张卡从哪来」写第二套分支。两套分支迟早有一支没人维护，而那一支会把网页卡
说成她的记忆（任务书 §5 第 1 条）。

出处与说法只在两处，本模块一句都不另写：没查到是 `actions.LOOKUP_EMPTY_SAY`，查不了是
`actions` 按失败类别给的那句，门的拒绝话是 `search.decide` 的 `refuse_say`。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
import time

from ..hand import actions as hand_actions
from ..hand import store as hand_store
from ..home import knowledge
from ..home import store as home_store
from . import llm, rice
from ..hand import ledger
from . import search

#: 四态（任务书 §4）：未请求／已请求失败／已查到未落盘／已落盘。账上必须分得开。
NOT_REQUESTED = "not_requested"
FAILED = "failed"
UNSAVED = "searched_not_saved"
SAVED = "saved"

#: 一张卡的正文预算：够回答，不把整页塞进提示词。
CARD_CHARS = 600


def _home_root(root):
    """手根 → 家的根（兄弟目录）；不给 root 就让两家各自解析自己的默认根（生产就是这样）。"""
    return None if root is None else hand_store.home_root(root)


def _result(action: str, gate: str, reason: str, *, items=(), say: Optional[str] = None,
            requested: bool = False, state: str = NOT_REQUESTED) -> Dict[str, Any]:
    """固定七格。`state` 是四态里的一格（给账与面板看），`requested` 是「发过请求吗」。"""
    return {"action": action, "gate": gate, "reason": reason, "items": list(items),
            "say": say, "requested": requested, "state": state}


def _credentials(conf, env=None) -> Tuple[Any, Any, Any]:
    """→ (base_url, key, 超时秒)。读不出来就给空，让手记一条「未请求」的拒绝账。

    不在这里自己编「查不了」那句话：缺 key 的那句说法在 `actions` 里只有一份，而且只有它
    写得出手账那一行。手那边拿到空地址／空 key 会就地拒掉——calls=0 就是「一次都没发」的证据。
    """
    try:
        config = llm.resolve_config(conf)
    except llm.LLMError:
        return None, None, None
    try:
        key = llm.read_key(config, env=env)
    except llm.LLMError:
        key = None
    budget = config.get("timeout_ms")
    timeout = min(20, budget / 1000.0) if isinstance(budget, (int, float)) and budget > 0 else 20
    return config.get("base_url"), key, timeout


def _usable(row: Any) -> bool:
    """这一条能不能成一张卡：没有网址就答不出「哪看的」，没有正文就没有给人看的话。"""
    return (isinstance(row, dict) and str(row.get("url") or "").strip() != ""
            and str(row.get("summary") or row.get("snippet") or "").strip() != "")


def _job(query: str, row: Dict[str, Any], now: Any) -> Dict[str, Any]:
    """一条服务端结果 → 一份网页任务。**一页一份**：家的去重键是 url，页面时间也各归各的；
    合成一份多卡的 job 会让一条 url 与一个日期冒充整批结果的时间（任务书 §5 第 2 条）。
    """
    stamp = hand_store.now_iso(now)
    claim = str(row.get("summary") or row.get("snippet") or "")[:CARD_CHARS].strip()
    snippet = str(row.get("snippet") or "")[:240].strip()
    return {"id": uuid4().hex, "kind": knowledge.KIND_WEB, "created_at": stamp, "fetched_at": stamp,
            "source": {"kind": knowledge.KIND_WEB, "url": str(row.get("url")).strip(),
                       "site_name": row.get("site_name") or "",
                       "published_at": row.get("published_at"), "query": query},
            # tags 里放 query：下一次同一问题由门 5 判成复用时，靠它把这张卡按原名召回。
            "cards": [{"id": uuid4().hex, "title": row.get("title") or "", "claim": claim,
                       "tags": [query], "evidence": [snippet] if snippet else []}]}


def _store(query: str, results: List[Dict[str, Any]], root, now: Any, session_id):
    """→ (items, 四态里的一格, 未落盘的原因)。**落卡只走 save_web，失败不抛给聊天。**

    落不下去也照样把内存里的结果交出去：这一轮已经花过钱，"查到但没存上"仍然能回答，
    只是要如实记账，不许说成「没有这件事」。
    """
    home = _home_root(root)
    items: List[Dict[str, Any]] = []
    trouble = ""
    for row in results:
        if not _usable(row):
            continue
        job = _job(query, row, now)
        job["session_id"] = session_id
        try:
            job = knowledge.save_web(job, root=home)
        except (ValueError, OSError, home_store.StoreError):
            trouble = "落卡失败，本轮仍使用已返回的资料"
        # 借家的视图函数，不在这里抄第二份键表与「哪天的」说法。
        items.append(knowledge._view(job, job["cards"][0], 0, []))
    if not items:
        return [], UNSAVED, "查到 %d 条，但没有一条同时带网址与正文，落不了卡" % len(results)
    return items, (UNSAVED if trouble else SAVED), trouble


def run(*, text, who, scene, session_id, turn_id, now=None, root=None, transport=None, conf=None,
        memory_hit: bool = False, intent=None, env=None) -> Dict[str, Any]:
    """这一轮要不要查、查到了什么、卡落在哪。**只编排，不判断、不自己说话。**

    `root` 是**手的实盘根**（与 `remind.handle` 同一个口径）：家的根由它换算成兄弟目录。
    `transport` 只为测试注入——生产走标准库那一个，本模块自己不碰网络。
    `memory_hit` 由调用方从记忆召回结果给：给 True 就是「这轮召回到了记忆，先按记忆答」
    （任务书 §3 门2）。默认 False 不等于「没有记忆」，只等于「调用方没判」。
    """
    if not turn_id:
        return _result("deny", "turn", "这一轮没有回合编号，不查（fail-closed：没有编号就记不了每轮封顶）")
    now = hand_store.now_iso(now)
    started = time.perf_counter()

    if intent is None:
        return _result("deny", "intent", "整段理解没有当前公共检索请求")
    elif not intent["question"]:
        verdict = {"action": "deny", "gate": "clarify", "query": None,
                   "reason": "公共问题缺少必要限定", "refuse_say": "先确认要查的对象和时间，不能猜。"}
    else:
        verdict = search.decide(text=text, who=who, scene=scene, session_id=session_id,
                                memory_hit=memory_hit, root=_home_root(root), turn_id=turn_id,
                                question=intent["question"], freshness=intent["freshness"])

    action, gate, query = verdict["action"], verdict["gate"], verdict["query"]
    decision = {"turn_id": turn_id, "action": action, "gate": gate, "reason": verdict["reason"]}
    billing = {}

    def audit(result, receipt=None):
        params = dict(decision, reason=result["reason"], calls=int(result["requested"]), landed=result["state"] == SAVED,
                      state="ok" if result["items"] else "error",
                      card_id=[item["card_id"] for item in result["items"]])
        receipt = receipt or {}
        detail = receipt.get("detail") or {}
        params.update({k: detail[k] for k in ("calls", "http", "state", "results_count", "query_sha256") if k in detail})
        if query:
            params["query"] = query
        params.update({k: billing[k] for k in ("billed_calls", "billing_status", "currency", "cost", "price_url") if k in billing})
        if billing.get("error"):
            result["billing_error"] = billing["error"]
        entry = ledger.make_entry(who=who, action="lookup", params=params, at=hand_store.now_iso(now),
                                  ledger_id=receipt.get("ledger_id"), code=receipt.get("code", "OK"),
                                  elapsed_ms=round((time.perf_counter() - started) * 1000),
                                  state=receipt.get("state", "done" if result["items"] else "refused"))
        try:
            ledger.append(entry, root=root)
        except ledger.LedgerWriteFailed:
            result["audit_error"] = "检索结果的最终记录未写入"
        return result

    if action == "reuse":
        items = knowledge.reuse_web(query, session_id=session_id, root=_home_root(root), now=now)
        return audit(_result("reuse", gate, verdict["reason"], items=items,
                             say=None if items else "此前的查询结果已不可用，本轮没有重新查询。"))

    if action != "search":
        # off / deny：零 HTTP，话用门给的那句（开门 3 是「这个我这儿看不到」，不是「不知道」）。
        return audit(_result(action, gate, verdict["reason"], say=verdict.get("refuse_say")))

    base_url, key, timeout = _credentials(conf, env)
    receipt = hand_actions.lookup(who=who, query=query, base_url=base_url, key=key,
                                 timeout=timeout, now=now, root=root, transport=transport)
    detail = receipt.get("detail") or {}
    results = [row for row in (detail.get("results") or []) if isinstance(row, dict)]
    sent = int(detail.get("calls") or 0) > 0
    if sent:
        from datetime import datetime
        try:
            billing = rice.record_search(detail, now=datetime.fromisoformat(hand_store.now_iso(now))) or {}
        except OSError:
            billing = {"error": "费用记录未写入，实际请求已发出"}
    ok = detail.get("state") == "ok" and bool(results)

    if not ok:
        search.note_outcome(session_id=session_id, query=query, ok=False)
        return audit(_result("search", gate, "没有可用的结果（%s）" % receipt.get("code"),
                       say=receipt.get("say"), requested=sent,
                       state=FAILED if sent else NOT_REQUESTED), receipt)

    items, state, trouble = _store(query, results, root, now, session_id)
    search.note_outcome(session_id=session_id, query=query, ok=state == SAVED)
    reason = "查到 %d 条，可用的 %d 条" % (len(results), len(items))
    if trouble:
        reason += "；" + trouble
    return audit(_result("search", gate, reason, items=items, requested=sent, state=state,
                         say=None if items else hand_actions.LOOKUP_EMPTY_SAY), receipt)
