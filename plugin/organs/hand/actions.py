"""两个动作 + 一个只读查询：把「记下一件事」做成能断言的回执。

统一信封（下一波的 api 与面板就按这个读）：
    {"state": "done"|"refused"|"failed", "action", "code", "say", "who",
     "card_id": str|None, "detail": {...}, "ledger_id": str|None}

码表冻结：refused ∈ {ACTION_NOT_ALLOWED, GRANT_REQUIRED, QUOTA_EXCEEDED, MISSING_DUE}；
failed ∈ {HOME_WRITE_FAILED, LEDGER_WRITE_FAILED}；成功 code="OK"。
**「她拒绝」不许写成 failed**——拒绝是她的判断，失败是系统没办成，两件事面板要分得开。

动作流（每个动作都一样的三步）：
    1. permit.check：三层闸，不过就拒（拒绝也记进手账，好让面板看得见被拦了什么）；
    2. 起手先落一行 state="reserved"——**落不下去就不动手**；
    3. 动手（调家）→ 追加一行 done / failed。

顺序为什么是「先动手后记结果」：两个动作都可撤销，做到一半崩了，账上的 reserved 一眼能看出来，
人还能去家那边补一笔；**不可逆动作将来必须先记账再动手**（先占位、再发），那时的顺序要改，
改的只有这一处。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from organs.home import access as home_access
from organs.home import cards as home_cards
from organs.home import store as home_store

from . import ledger, permit, quota, store, timelang

#: 手替她动家：家的口径里 organ 不过白名单门（那是给 agent 的），但仍过路径形状与日额度。
ROLE = "organ"

HOME_FAILED_SAY = "这个我没记上，你再说一遍"
MISSING_DUE_SAY = "什么时候提醒你？"
LEDGER_RESERVED_SAY = "账没记上，我就没动手"
LEDGER_RESULT_SAY = "账没记上，这次不算做成"

_LEDGER_ERRORS = (ledger.LedgerWriteFailed, OSError)
#: 家的错一律带 code：CardError 是卡本身的规矩，AccessDenied 是门与上限，
#: StoreError 是落盘底座。三种都翻成同一条人话，但 origin 留着原始码。
_HOME_ERRORS = (home_cards.CardError, home_access.AccessDenied, home_store.StoreError, OSError)


def _envelope(state: str, action: str, code: str, say: str, *, who: str,
              card_id: Optional[str] = None, detail: Optional[Dict[str, Any]] = None,
              ledger_id: Optional[str] = None) -> Dict[str, Any]:
    return {"state": state, "action": action, "code": code, "say": say, "who": who,
            "card_id": card_id, "detail": dict(detail or {}), "ledger_id": ledger_id}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _origin_of(exc: BaseException) -> str:
    """原始码：家的三个错误类都带 code，其它的按类名记，不吞成空串。"""
    code = getattr(exc, "code", None)
    return str(code) if code else type(exc).__name__


def _human(stamp: str) -> str:
    """给人看的一行时刻（本地时区）。读不出来就原样给，不编。"""
    try:
        return store.parse_ts(stamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(stamp)


def _card_brief(card: Dict[str, Any]) -> Dict[str, Any]:
    """回执里带的那点卡信息：够面板显示，不把整张卡塞进信封。"""
    params = card.get("params") or {}
    return {"card_id": card.get("card_id"), "name": card.get("name"),
            "due": params.get("due"), "done": bool(card.get("done")),
            "done_count": int(card.get("done_count") or 0)}


def _begin(*, who: str, action: str, params: Dict[str, Any], now: Any, root,
           started: float) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """落 reserved 那一行。写不进去 → (None, 失败信封)：**这次动作不许动手**。"""
    entry = ledger.make_entry(who=who, action=action, params=params, state="reserved",
                              at=store.now_iso(now), elapsed_ms=_elapsed_ms(started))
    try:
        row = ledger.append(entry, root=root)
    except _LEDGER_ERRORS as exc:
        return None, _envelope("failed", action, "LEDGER_WRITE_FAILED", LEDGER_RESERVED_SAY,
                               who=who, detail={"stage": "reserved", "gate": "ledger",
                                                "error": _origin_of(exc)})
    return str(row.get("ledger_id")), None


def _settle(*, who: str, action: str, params: Dict[str, Any], now: Any, root,
            started: float, ledger_id: str, state: str, code: str, say: str,
            detail: Optional[Dict[str, Any]] = None, origin: str = "OK",
            card_id: Optional[str] = None) -> Dict[str, Any]:
    """追加结果行，并给出信封。结果行写不进去 → 改判 failed，**绝不说成功**。"""
    entry = ledger.make_entry(ledger_id=ledger_id, who=who, action=action, params=params,
                              state=state, code=code, elapsed_ms=_elapsed_ms(started),
                              origin=origin, at=store.now_iso(now))
    try:
        ledger.append(entry, root=root)
    except _LEDGER_ERRORS as exc:
        return _envelope("failed", action, "LEDGER_WRITE_FAILED", LEDGER_RESULT_SAY, who=who,
                         detail=dict(detail or {}, stage="result", gate="ledger",
                                     wanted={"state": state, "code": code},
                                     error=_origin_of(exc)),
                         ledger_id=ledger_id)
    return _envelope(state, action, code, say, who=who, card_id=card_id, detail=detail,
                     ledger_id=ledger_id)


def _attach_quota(out: Dict[str, Any], *, who: str, now: Any, root) -> Dict[str, Any]:
    """成功之后补上额度：**必须在结果行落账之后现算**，否则回执里的「还剩几次」是动手前的旧数。"""
    if out.get("state") == "done":
        out["detail"]["quota"] = quota.remaining(who, now=now, root=root)
    return out


def _refuse(*, who: str, action: str, params: Dict[str, Any], now: Any, root,
            started: float, code: str, say: str,
            detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """拒绝也走一遍手账（reserved + refused）：被拦下的动作同样是证据。"""
    ledger_id, failure = _begin(who=who, action=action, params=params, now=now, root=root,
                                started=started)
    if failure is not None:
        return failure
    return _settle(who=who, action=action, params=params, now=now, root=root, started=started,
                   ledger_id=ledger_id, state="refused", code=code, say=say, detail=detail)


def _resolve_due(due: Any, when: Any, now: Any) -> Tuple[Optional[str], Optional[str]]:
    """→ (交出去的 due, 问题)。due 原样交给家，不重写她给的串。

    问题有三种：「什么都没给」「when 认不出」「due 没带时区」。三种都不猜，一律反问。
    """
    due_text = str(due).strip() if due is not None else ""
    bad_due = None
    if due_text:
        try:
            store.parse_ts(due_text)
            return due_text, None
        except (TypeError, ValueError):
            bad_due = due_text
    when_text = str(when).strip() if when is not None else ""
    if when_text:
        got = timelang.parse(when_text, now=now)
        if got is not None:
            return got, None
        return None, "when_unreadable"
    return None, ("due_without_timezone" if bad_due else "missing")


def _due_question(problem: Optional[str], due: Any, when: Any) -> str:
    """反问一句。三种问题各说各的，最后永远落回同一个问题。"""
    if problem == "when_unreadable":
        return "「%s」我听不出是哪一刻，要 明天中午 / 下午3点半 / 8点 这样。%s" % (when, MISSING_DUE_SAY)
    if problem == "due_without_timezone":
        return "「%s」没带时区，我不敢替它猜。%s" % (due, MISSING_DUE_SAY)
    return MISSING_DUE_SAY


def add_task(*, who: str, title: Any, due: Any = None, when: Any = None, note: str = "",
             now: Any = None, root=None) -> Dict[str, Any]:
    """记下一件事。没有能用的时间就反问、不落卡。"""
    started = time.monotonic()
    params = {"title": title, "when": when, "due": due, "note": note}
    permitted = permit.check("add_task", who=who, now=now, root=root)
    if not permitted["allowed"]:
        return _refuse(who=who, action="add_task", params=params, now=now, root=root,
                       started=started, code=str(permitted["code"]), say=permitted["say"],
                       detail=permitted["detail"])

    resolved, problem = _resolve_due(due=due, when=when, now=now)
    if resolved is None:
        return _refuse(who=who, action="add_task", params=params, now=now, root=root,
                       started=started, code="MISSING_DUE", say=_due_question(problem, due, when),
                       detail={"need": "due", "reason": problem, "when": when, "due": due})

    ledger_id, failure = _begin(who=who, action="add_task", params=params, now=now, root=root,
                                started=started)
    if failure is not None:
        return failure

    # 卡上那格「这是一件事」（params.task）由手写：日子线判「快到点该提醒了」只认这一格
    # （organs/day/proactive.py 的 pending_task），不写它，卡就只是躺在工作台上的一张纸。
    # 走家的公开写口 append_cards，房间与种类的名字仍从家取——不在手里抄第二份常量。
    size = len(title) + len(note) if isinstance(title, str) and isinstance(note, str) else 0
    try:
        written = home_cards.append_cards(root=store.home_root(root), role=ROLE, by=who, cards=[
            {"room": home_cards.TASK_ROOM, "kind": home_cards.TASK_KIND, "name": title,
             "chars": size, "at": store.now_iso(now),
             "params": {"due": resolved, "done": False, "note": note, "task": True}}])
        card = written["cards"][0]
    except _HOME_ERRORS as exc:
        origin = _origin_of(exc)
        return _settle(who=who, action="add_task", params=params, now=now, root=root,
                       started=started, ledger_id=ledger_id, state="failed",
                       code="HOME_WRITE_FAILED", say=HOME_FAILED_SAY, origin=origin,
                       detail={"home": origin, "due": resolved, "title": title})

    return _attach_quota(
        _settle(who=who, action="add_task", params=params, now=now, root=root,
                started=started, ledger_id=ledger_id, state="done", code="OK",
                say="记下了：%s（%s）" % (card.get("name"), _human(resolved)),
                card_id=str(card.get("card_id") or "") or None,
                detail={"card": _card_brief(card), "due": resolved}),
        who=who, now=now, root=root)


def complete_task(*, who: str, card_id: Any, now: Any = None, root=None) -> Dict[str, Any]:
    """把一张任务卡标成做完了。卡上的规矩归家（空 id / 不存在的 id 都由家判），这里不重写一遍。"""
    started = time.monotonic()
    params = {"card_id": card_id}
    permitted = permit.check("complete_task", who=who, now=now, root=root)
    if not permitted["allowed"]:
        return _refuse(who=who, action="complete_task", params=params, now=now, root=root,
                       started=started, code=str(permitted["code"]), say=permitted["say"],
                       detail=permitted["detail"])

    ledger_id, failure = _begin(who=who, action="complete_task", params=params, now=now,
                                root=root, started=started)
    if failure is not None:
        return failure

    try:
        written = home_cards.complete_task(root=store.home_root(root), role=ROLE, by=who,
                                           card_id=card_id, at=store.now_iso(now))
    except _HOME_ERRORS as exc:
        origin = _origin_of(exc)
        return _settle(who=who, action="complete_task", params=params, now=now, root=root,
                       started=started, ledger_id=ledger_id, state="failed",
                       code="HOME_WRITE_FAILED", say=HOME_FAILED_SAY, origin=origin,
                       detail={"home": origin, "card_id": card_id})

    card = dict(written.get("card") or {})
    count = int(card.get("done_count") or 1)
    say = "这件标成做完了：%s" % (card.get("name") or card_id)
    if count > 1:
        # 家对同一张卡再完成一次是**幂等追加**（done_count +1），这里如实说第几次，不装作没发生。
        say += "（这是第 %d 次记完成，家的流水只增不改）" % count
    return _attach_quota(
        _settle(who=who, action="complete_task", params=params, now=now, root=root,
                started=started, ledger_id=ledger_id, state="done", code="OK", say=say,
                card_id=str(card.get("card_id") or "") or None,
                detail={"card": _card_brief(card), "done_count": count}),
        who=who, now=now, root=root)


def due(now: Any = None, root=None) -> list:
    """**只读**：未完成且 `due <= now` 的任务卡。不接时钟、不 sleep、不起线程、不写盘。

    到点没做的判据只在这里：家那边只管卡，手这边只管「现在该催哪几张」。
    """
    moment = store.now_dt(now)
    ready = []
    for card in home_cards.list_tasks(root=store.home_root(root)):
        if card.get("done"):
            continue
        stamp = (card.get("params") or {}).get("due")
        try:
            if store.parse_ts(stamp) <= moment:
                ready.append(card)
        except (TypeError, ValueError):
            continue            # 形状坏了的卡不催：催错了比不催更坏
    ready.sort(key=lambda card: str((card.get("params") or {}).get("due") or ""))
    return ready
