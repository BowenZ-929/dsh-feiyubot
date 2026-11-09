"""五个动作 + 一个只读查询：把「做成一件事」做成能断言的回执。

统一信封（api 与面板就按这个读，**八键与三态冻结**）：
    {"state": "done"|"refused"|"failed", "action", "code", "say", "who",
     "card_id": str|None, "detail": {...}, "ledger_id": str|None}

码表冻结：refused ∈ {ACTION_NOT_ALLOWED, GRANT_REQUIRED, QUOTA_EXCEEDED, MISSING_DUE,
          STYLE_NOT_UNDERSTOOD, IMAGE_NOT_UNDERSTOOD, KEY_NOT_ALLOWED, VALUE_OUT_OF_RANGE}；
failed ∈ {HOME_WRITE_FAILED, HOME_READ_FAILED, LEDGER_WRITE_FAILED,
          IMAGE_NOT_FOUND, DRAW_UNAVAILABLE, DRAW_FAILED,
          CHANNEL_UNREACHABLE, SEND_FAILED, CHANNEL_BLOCKED}；成功 code="OK"。
（本批新增的三个 refused 码都归 `set_style`：没听懂／改的不是「怎么说话」／数越界。三件事
面板要分得开——「没听懂」再问一句就行，「不在这里」是换个说法也没用，「越界」是把范围说给她。
另加一个 failed 码 HOME_READ_FAILED：改口吻之前要先读出旧值，**读不出来就不动手**——
拿不到旧值还硬写，账上那句「原来是什么」就成了编的。）
本批再加一个 refused 码 IMAGE_NOT_UNDERSTOOD：句子里认不出「想看哪张图」就反问——它是**没听懂**，
不是**图没找着**（`IMAGE_NOT_FOUND` 是「听懂了、家里没有」）：两件事面板要分得开。）
**「她拒绝」不许写成 failed**——拒绝是她的判断，失败是系统没办成，两件事面板要分得开。
**「没发出去」也不许写成 done**：发图与画图都只认通道的 ok && sent>0，人话里带通道给的数与理由码，
手自己从不声明「发过了」。

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
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from organs.home import access as home_access
from organs.home import add_preference as home_add_preference
from organs.home import cards as home_cards
from organs.home import preference_of as home_preference_of
from organs.home import residents as home_residents
from organs.home import store as home_store

from . import assets, channel, imgphrase, ledger, permit, quota, store, stylelang, timelang, whitelist
from . import draw as draw_client

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


# ---- 图：发出去（send_image）与画一张（draw） ----------------------------
# 两个动作共用同一套：三层闸 → 先落 reserved 再动手 → 结果行写不进就改判 failed。
# 不一样的只有「图从哪来」：一个从家·仓库取指针，一个去外服要一张、落自己的盘、再登记进仓库。

IMAGE_NOT_FOUND_SAY = "图没找着"
DRAW_UNAVAILABLE_SAY = "这个我画不出来"
DRAW_FAILED_SAY = "画是画了，但一张能用的都没有，我没落盘"
DRAW_LAND_FAILED_SAY = "画是画了，但没落到我的盘上，这次不算做成"
DRAW_REGISTER_FAILED_SAY = "画出来了、也落盘了，但没登记进仓库，这次不算做成"
SEND_BOTH_SAY = "给了一个路径又给了一个名字：二选一，我不替你挑"
SEND_NEITHER_SAY = "既没给图在哪、也没给名字、也没说想看哪张：说一个我才知道发哪张"
SEND_MIXED_SAY = "给了话又直接给了图或名字：说一样就够了，我不替你挑"
SEND_WHICH_SAY = "你想看哪张图？说个名字给我（比如「自画像」）"
TARGET_SAY = "发给谁得是群号或 QQ 号（数字）"
SCENE_SAY = "场景只认 group（群）或 private（私聊）"

#: 结果行额外记的账目。账的 FIELDS 不加第九格：这几格并进 params，账仍是「一行一动作」。
SEND_ACCOUNTING: Tuple[str, ...] = ("scene", "target", "path", "sha256", "channel_id", "sent",
                                    "reason_code")
DRAW_ACCOUNTING: Tuple[str, ...] = ("scene", "target", "path", "sha256", "marked", "source",
                                    "channel_id", "sent", "reason_code", "landed", "card_id")


def _accounting(params: Dict[str, Any], detail: Dict[str, Any],
                keys: Tuple[str, ...]) -> Dict[str, Any]:
    """把这次的账目并进结果行的 params：账是索引，但也得能指着这一笔说清。"""
    merged = dict(params)
    for key in keys:
        merged[key] = detail.get(key)
    return merged


def _send_tail(outcome: Dict[str, Any]) -> str:
    """一句人话，把**通道自己的数**摆出来——手不替它说「发过了」，也不替它编理由。"""
    if outcome.get("local"):
        # 本地闸拦下的：通道什么都没收到，别把话说成「通道不认」。
        return "我这边就没发出去（%s），通道什么都没收到" % (outcome.get("error") or "参数没过闸")
    code = outcome.get("code")
    if code == channel.CHANNEL_BLOCKED:
        return "通道没发（sent=0），它给的理由码是 %s" % (outcome.get("reason_code") or "（没给）")
    if code == channel.CHANNEL_UNREACHABLE:
        return "通道没连上（%s），所以没有发出去" % (outcome.get("error") or "没说原因")
    return "通道回的东西不对劲（%s），我没把它算成发出去" % (outcome.get("error") or "没说原因")


def _send_done_say(outcome: Dict[str, Any]) -> str:
    """done 的人话必须带通道给的 sent 与理由码：少了这两个数，就等于这些话是手自己说的。"""
    return "通道说发出去了：sent=%d、理由码 %s、id %s" % (
        int(outcome.get("sent") or 0), outcome.get("reason_code") or "（没给）",
        outcome.get("channel_id") or "（没给）")


def _send_origin(outcome: Dict[str, Any]) -> str:
    """失败时记的原始码：通道拦下就用**它自己的** reason_code，其余用网络层说的话。"""
    if outcome.get("code") == channel.CHANNEL_BLOCKED:
        return str(outcome.get("reason_code") or "")
    return str(outcome.get("error") or outcome.get("code") or "")


def _bad_argument(say: str, origin: str) -> Dict[str, Any]:
    return {"ok": False, "code": "BAD_ARGUMENT", "say": say, "origin": origin, "path": None}


def _not_found(why: str, origin: str, path: Any = None) -> Dict[str, Any]:
    """「图没找着」是固定的那句话，后面接一句**具体**是哪一种找不到。"""
    return {"ok": False, "code": "IMAGE_NOT_FOUND", "say": "%s——%s" % (IMAGE_NOT_FOUND_SAY, why),
            "origin": origin, "path": None if path is None else str(path)}


def _channel_scene(scene: Any) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """场景收口 → (给通道的值, 问题)。通道只认 group／private，别的值它一律 400。"""
    text = "" if scene is None else str(scene).strip()
    if text in channel.SCENES:
        return text, None
    return None, _bad_argument("%s——收到 %r" % (SCENE_SAY, scene), "BAD_SCENE")


def _channel_target(target: Any) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """发给谁收口 → (正整数, 问题)。**在本地就归一，不发给通道再让它报错。**

    本来就是 int 的过；纯数字串（"100000001"）转成 int——真通道要求 JSON 里是数字，
    字符串会被它 400（实测）。空、0、负数、带小数点、中文，一律 BAD_ARGUMENT。
    """
    value: Optional[int] = None
    if isinstance(target, bool):
        value = None
    elif isinstance(target, int):
        value = target
    else:
        text = "" if target is None else str(target).strip()
        try:
            value = int(text) if text.isdigit() else None
        except ValueError:          # 全角／上标数字之类：isdigit 为真但 int() 不认
            value = None
    if value is None or value <= 0:
        return None, _bad_argument("%s——收到 %r（例：100000001）" % (TARGET_SAY, target),
                                   "BAD_TARGET")
    return value, None


def _channel_fields(scene: Any, target: Any,
                    ) -> Tuple[Optional[str], Optional[int], Optional[Dict[str, Any]]]:
    """两个都验一遍：哪个不对说哪个（先场景，后发给谁）。"""
    scene_value, scene_problem = _channel_scene(scene)
    target_value, target_problem = _channel_target(target)
    return scene_value, target_value, (scene_problem or target_problem)


def _fields_failed(*, who: str, action: str, params: Dict[str, Any], now: Any, root,
                   started: float, ledger_id: str, problem: Dict[str, Any],
                   scene: Any, target: Any) -> Dict[str, Any]:
    """场景／发给谁不过闸：**一次网络都不发**，账上记一条 failed（stage 说清卡在哪一段）。"""
    return _settle(who=who, action=action, params=params, now=now, root=root, started=started,
                   ledger_id=ledger_id, state="failed", code=str(problem["code"]),
                   say=str(problem["say"]), origin=str(problem["origin"]),
                   detail={"stage": "channel_fields", "reason": problem["origin"],
                           "scene": scene, "target": target})


def _resolve_image(*, path: Any, name: Any, utterance: Any, root) -> Dict[str, Any]:
    """图从哪来：给绝对路径、给名字、给她那句话解出来的名字——**三样只许一样**，不自作主张。

    从家里取时**只取指针**（路径与指纹），图的本体一个字节都不复制；`source` 报的是
    **命中的那一间房**（房名从 `find_asset` 来，手里不写房名）。
    """
    given_text = "" if path is None else str(path).strip()
    named = "" if name is None else str(name).strip()
    said = "" if utterance is None else str(utterance).strip()
    if given_text and named:
        return _bad_argument(SEND_BOTH_SAY, "BOTH_PATH_AND_NAME")
    if said and (given_text or named):
        # 话与直给不是一回事：两个都给就等于让我替她挑，所以码另起一个，别混进 BOTH_PATH_AND_NAME。
        return _bad_argument(SEND_MIXED_SAY, "UTTERANCE_AND_SOURCE")
    if not (given_text or named or said):
        return _bad_argument(SEND_NEITHER_SAY, "NEITHER_PATH_NOR_NAME")

    named = named or said          # 话已经由 imgphrase 解成了一个名字，往下走同一条路
    if named:
        try:
            found = assets.find_asset(name=str(named), root=root)
        except (home_cards.CardError, home_store.StoreError, OSError) as exc:
            origin = _origin_of(exc)
            return _not_found("家里没读出来（%s），图也就没找着" % origin, origin)
        if not found["found"]:
            return _not_found("家里没有叫「%s」的图" % named, str(found["reason"]))
        if not found["exists"]:
            return _not_found("家里那张「%s」记的路径不在了：%s"
                              % (named, found["path"]), str(found["reason"]), found["path"])
        return {"ok": True, "code": "OK", "path": found["path"], "sha256": found["sha256"],
                "name": named, "card_id": found["card_id"], "kind": found["kind"],
                "bytes": found["bytes"], "source": found["room"],
                "picked_reason": found["picked_reason"], "room": found["room"]}

    given = Path(given_text)
    if not given.is_absolute():
        return _bad_argument("path 要给绝对路径（收到 %r）：相对路径我不敢替你猜是哪个目录"
                             % given_text, "PATH_NOT_ABSOLUTE")
    if not given.is_file():
        return _not_found("这个路径上没有文件：%s" % given_text, "PATH_MISSING", given_text)
    try:
        digest = store.sha256_file(given)
    except OSError as exc:
        origin = _origin_of(exc)
        return _not_found("这个文件读不出来：%s（%s）" % (given_text, origin), origin, given_text)
    return {"ok": True, "code": "OK", "path": str(given), "sha256": digest,
            "name": given.name, "card_id": None, "kind": None, "source": "路径",
            "bytes": given.stat().st_size, "picked_reason": None, "room": None}


def send_image(*, who: str, scene: Any, target: Any, path: Any = None, name: Any = None,
               utterance: Any = None, now: Any = None, root=None,
               defer: bool = False) -> Dict[str, Any]:
    """把一张图交给通道发出去：图从家里取（只取指针），或直接给绝对路径。

    `utterance` 是**可选**的名字来源：给了就走 imgphrase 那张短语表，解出名字再照旧去家里找。
    解不出就**反问**（refused/IMAGE_NOT_UNDERSTOOD，零写入），不猜、不拿整句话当文件名——
    「没听懂」和「图没找着」是两件事（同 stylelang 那条口径）。

    **手自己绝不声明「发过了」**：只有通道回 `ok == true` 且 `sent > 0` 才记 done，
    人话里带通道给的 sent 与理由码；sent=0、连不上、回的东西不对劲，一律 failed。
    defer=True 只准备图片，随聊天回复统一发送；reserved 等实际发送回执再结算。
    """
    started = time.monotonic()
    params: Dict[str, Any] = {"scene": scene, "target": target, "path": path, "name": name,
                              "utterance": utterance}
    permitted = permit.check("send_image", who=who, now=now, root=root)
    if not permitted["allowed"]:
        return _refuse(who=who, action="send_image", params=params, now=now, root=root,
                       started=started, code=str(permitted["code"]), say=permitted["say"],
                       detail=permitted["detail"])

    said = "" if utterance is None else str(utterance).strip()
    wanted_name = None
    if said:
        wanted_name = imgphrase.parse(utterance)
        if wanted_name is None:
            # 认不出就反问：这是「没听懂」，不是「图没找着」，所以走 refused、一个字都不写。
            return _refuse(who=who, action="send_image", params=params, now=now, root=root,
                           started=started, code="IMAGE_NOT_UNDERSTOOD", say=SEND_WHICH_SAY,
                           detail={"stage": "understand", "utterance": utterance})

    ledger_id, failure = _begin(who=who, action="send_image", params=params, now=now,
                                root=root, started=started)
    if failure is not None:
        return failure

    scene_value, target_value, problem = _channel_fields(scene, target)
    if problem is not None:
        return _fields_failed(who=who, action="send_image", params=params, now=now, root=root,
                              started=started, ledger_id=ledger_id, problem=problem,
                              scene=scene, target=target)

    resolved = _resolve_image(path=path, name=name, utterance=wanted_name, root=root)
    if not resolved["ok"]:
        return _settle(who=who, action="send_image", params=params, now=now, root=root,
                       started=started, ledger_id=ledger_id, state="failed",
                       code=str(resolved["code"]), say=str(resolved["say"]),
                       origin=str(resolved["origin"]),
                       detail={"stage": "resolve", "reason": resolved["origin"],
                               "path": resolved.get("path"), "name": name,
                               "utterance": utterance, "wanted": wanted_name})

    if defer:
        detail = dict(resolved, scene=scene_value, target=target_value)
        out = _envelope("reserved", "send_image", "IMAGE_PREPARED", "图片已准备，等待通道发送",
                        who=who, detail=detail, ledger_id=ledger_id)
        out["receipt"] = {"ledger_id": ledger_id, "who": who,
                          "root": str(store.hand_root(root)),
                          "params": _accounting(params, detail, SEND_ACCOUNTING)}
        return out

    outcome = channel.say_image(scene=scene_value, target=target_value, path=resolved["path"],
                                name=resolved["name"])
    detail: Dict[str, Any] = {"scene": scene_value, "target": target_value,
                              "path": resolved["path"],
                              "sha256": resolved["sha256"],
                              "channel_id": outcome.get("channel_id"),
                              "sent": outcome.get("sent"),
                              "reason_code": outcome.get("reason_code"),
                              "source": resolved["source"],
                              "source_room": resolved["room"],
                              "picked_reason": resolved["picked_reason"],
                              "source_card_id": resolved["card_id"],
                              "channel_url": outcome.get("url"),
                              "http": outcome.get("http"), "error": outcome.get("error")}
    params = _accounting(params, detail, SEND_ACCOUNTING)

    if outcome["code"] == channel.OK:
        return _attach_quota(
            _settle(who=who, action="send_image", params=params, now=now, root=root,
                    started=started, ledger_id=ledger_id, state="done", code="OK",
                    say="%s（这张：%s）" % (_send_done_say(outcome), resolved["name"]),
                    detail=detail),
            who=who, now=now, root=root)
    return _settle(who=who, action="send_image", params=params, now=now, root=root,
                   started=started, ledger_id=ledger_id, state="failed",
                   code=str(outcome["code"]), say="没发出去：%s" % _send_tail(outcome),
                   origin=_send_origin(outcome), detail=detail)


def draw(*, who: str, description: Any, scene: Any, target: Any, now: Any = None,
         root=None) -> Dict[str, Any]:
    """画一张图：外服给字节 → 落到自己的盘（经 store）→ 登记进家·仓库（只记指针）→ 发出去。

    发送与 send_image 是**同一条通道、同一条判据**（`ok && sent > 0` 才算 done）。
    发送失败时图已经落了盘、仓库也有卡了：账上记 `landed=true`，
    人话要说清「画出来了、但没发出去」——不装作没画过，也不装作发过了。
    """
    started = time.monotonic()
    params: Dict[str, Any] = {"description": description, "scene": scene, "target": target}
    permitted = permit.check("draw", who=who, now=now, root=root)
    if not permitted["allowed"]:
        return _refuse(who=who, action="draw", params=params, now=now, root=root,
                       started=started, code=str(permitted["code"]), say=permitted["say"],
                       detail=permitted["detail"])

    ledger_id, failure = _begin(who=who, action="draw", params=params, now=now, root=root,
                                started=started)
    if failure is not None:
        return failure

    scene_value, target_value, problem = _channel_fields(scene, target)
    if problem is not None:
        # 场景／发给谁不成立就**连画都不画**：画了也发不出去，白落一张图、白占一次额度。
        return _fields_failed(who=who, action="draw", params=params, now=now, root=root,
                              started=started, ledger_id=ledger_id, problem=problem,
                              scene=scene, target=target)

    got = draw_client.painting(description=description)
    if got["code"] != draw_client.OK:
        say = (DRAW_UNAVAILABLE_SAY if got["code"] == draw_client.DRAW_UNAVAILABLE
               else DRAW_FAILED_SAY)
        return _settle(who=who, action="draw", params=params, now=now, root=root,
                       started=started, ledger_id=ledger_id, state="failed",
                       code=str(got["code"]), say=say, origin=str(got["origin"]),
                       detail={"stage": "generate", "reason": got["origin"],
                               "error": got.get("error"), "url": got.get("url"),
                               "landed": False, "marked": got.get("marked")})

    try:
        landed = store.land_draw(got["data"], root=root, ext=got["ext"])
        digest = store.sha256_file(landed)
    except (OSError, ValueError) as exc:
        origin = _origin_of(exc)
        return _settle(who=who, action="draw", params=params, now=now, root=root,
                       started=started, ledger_id=ledger_id, state="failed",
                       code=draw_client.DRAW_FAILED, say=DRAW_LAND_FAILED_SAY, origin=origin,
                       detail={"stage": "land", "error": origin, "landed": False,
                               "marked": got["marked"], "ext": got["ext"]})

    marked = got["marked"] is True
    note = "来源=draw; marked=%s" % ("true" if marked else "false")
    try:
        registered = home_residents.register_asset(root=store.home_root(root), by="hand",
                                                   path=str(landed), note=note)
        card = dict(registered.get("card") or {})
    except _HOME_ERRORS as exc:
        origin = _origin_of(exc)
        return _settle(who=who, action="draw", params=params, now=now, root=root,
                       started=started, ledger_id=ledger_id, state="failed",
                       code="HOME_WRITE_FAILED", say=DRAW_REGISTER_FAILED_SAY, origin=origin,
                       detail={"stage": "register", "home": origin, "landed": True,
                               "path": str(landed), "sha256": digest, "marked": marked})

    card_id = str(card.get("card_id") or "") or None
    outcome = channel.say_image(scene=scene_value, target=target_value, path=str(landed),
                                name=str(card.get("name") or landed.name))
    detail = {"scene": scene_value, "target": target_value, "path": str(landed), "sha256": digest,
              "marked": marked, "source": got["source"], "card_id": card_id,
              "channel_id": outcome.get("channel_id"), "sent": outcome.get("sent"),
              "reason_code": outcome.get("reason_code"), "landed": True,
              "channel_url": outcome.get("url"), "http": outcome.get("http"),
              "error": outcome.get("error"), "note": note}
    params = _accounting(params, detail, DRAW_ACCOUNTING)

    if outcome["code"] == channel.OK:
        say = "画好了，也发出去了：%s" % _send_done_say(outcome)
        if marked:
            say += "（回来的这张带标记，账上如实记着 marked=true）"
        return _attach_quota(
            _settle(who=who, action="draw", params=params, now=now, root=root,
                    started=started, ledger_id=ledger_id, state="done", code="OK", say=say,
                    card_id=card_id, detail=dict(detail, card=_card_brief(card))),
            who=who, now=now, root=root)
    return _settle(who=who, action="draw", params=params, now=now, root=root,
                   started=started, ledger_id=ledger_id, state="failed",
                   code=str(outcome["code"]),
                   say="画出来了、但没发出去：%s" % _send_tail(outcome),
                   origin=_send_origin(outcome), card_id=card_id,
                   detail=dict(detail, card=_card_brief(card)))


# ---- 怎么说话：只改旋钮，不改她是谁（set_style） ---------------------------
# 与其它动作同一条流：三层闸 → 先落 reserved 再动手 → 结果行写不进就改判 failed。
# 不一样的只有「改什么」：不落新卡、不发明文，只往家·名片盒的偏好卡上再追加一行。
# 还有一条只有它有的顺序：**动手前先读出旧值**——账上那句「原来是什么」得是真读来的。

STYLE_NOT_UNDERSTOOD_SAY = "你想让我怎么说话？短一点，还是一句话说完？"
STYLE_READ_FAILED_SAY = "没读出你现在的口径，我就没动它"
#: 每个旋钮的人话叫法。与 whitelist.STYLE_KEYS 一一对应（加旋钮忘了改这里，用例会红）。
STYLE_LABELS = {"chunks_per_reply": "一泡几句", "max_chars": "一次几个字"}
#: 结果行额外记的账目（并进 params：账仍是「一行一动作」，不另加第九格）。
STYLE_ACCOUNTING: Tuple[str, ...] = ("key", "value", "old", "old_source", "card_id",
                                     "who", "target_who", "source")


def _style_keys_say(key: Any) -> str:
    """KEY_NOT_ALLOWED 的人话：点名能改的那两样，并**明说「改她是谁」不在这里**。

    范围从 whitelist.STYLE_KEYS 里取，不在这里抄第二份——加旋钮时这句话跟着变。
    """
    parts = ["%s（%s，%d–%d）" % (name, STYLE_LABELS.get(name, name), low, high)
             for name, (low, high) in whitelist.STYLE_KEYS.items()]
    return ("只能改这两样：%s——「改她是谁」（身份／人设／名字／魂）不在这里，收到 %r"
            % ("、".join(parts), key))


def _style_done_say(key: str, value: int, old: Any, old_source: str) -> str:
    """改成什么了、原来是什么：原来没有就直说「以前没设过」，不拿默认值冒充旧值。"""
    label = STYLE_LABELS.get(key, key)
    if old_source == "live" and old == value:
        return "记住了：%s 还是 %s（你要的就是现在这样，我又记了一行）" % (label, value)
    if old_source == "live" and old is not None:
        return "记住了：%s 从 %s 改成 %s（想改回去再说一句就行）" % (label, old, value)
    return "记住了：%s 是 %s（以前没设过，这是第一次）" % (label, value)


def _style_int(value: Any) -> Optional[int]:
    """旋钮的数值收口：int 收、纯数字串转 int、**其余一律 None（不猜）**。

    为什么收纯数字串：命令行 `--value` 天生是字符串（`set-style --value 20`），面板给的是数字，
    同一个数有两种形状——无损转换不算猜，不转就等于「命令行永远改不了口径」。
    为什么拒 bool 与浮点：bool 是 int 的子类（True 会悄悄变成 1），20.5 收成 20 是替她把话说窄了；
    这两种都是「看起来像数」的猜测，宁可回一句范围让她再说一遍。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            try:
                return int(text)
            except ValueError:      # 全角／上标数字：isdigit 为真但 int() 不认
                return None
    return None


def _style_target(*, key: Any, value: Any,
                  utterance: Any) -> Tuple[Optional[Dict[str, Any]],
                                           Optional[Dict[str, Any]]]:
    """这次要改哪一格 → (目标, 问题)。两条入口二选一，认不出就反问、**一个字都不写**。

    两条都给时按 utterance 办：她说出口的那句话就是这次请求，面板上的旋钮是上一次的读数。
    """
    said = utterance if isinstance(utterance, str) else ""
    if said.strip():
        parsed = stylelang.parse(said)
        if parsed is None:
            return None, {"code": "STYLE_NOT_UNDERSTOOD", "say": STYLE_NOT_UNDERSTOOD_SAY,
                          "reason": "unreadable"}
        got_key, got_value = parsed
        return {"key": got_key, "value": got_value, "source": "utterance"}, None
    if key is None and value is None:
        return None, {"code": "STYLE_NOT_UNDERSTOOD", "say": STYLE_NOT_UNDERSTOOD_SAY,
                      "reason": "nothing_given"}
    return {"key": key, "value": value, "source": "direct"}, None


def _pref_read(who: str, home) -> Dict[str, Any]:
    """读这个人的偏好卡：**没有卡就是 {}**（不补 0、不补默认值，家那边同一条口径）。

    这是与家之间的一处薄适配：家换参数形状时只改这一行，动作层不看它怎么拼。
    """
    return dict(home_preference_of(root=home, who=who) or {})


def _pref_write(who: str, key: str, value: int, *, before: Dict[str, Any], home,
                at: Any) -> Dict[str, Any]:
    """改一个旋钮，**把另一个旋钮现在的值一起带上**——一行就是「这个人此刻的整份口径」。

    为什么不是只写这一格：家的 fold 取追加顺序**最新那一行**、不再把两行合并
    （organs/home/residents.py:_fold_prefs）。只写一格的话，上一次设的另一个旋钮会被这一行
    整份盖没——她说「说短点」再「一句话说完」，前者就白说了。所以另一格从刚读到的旧值里带上；
    没设过就是 None（家把 None 当「这次没说」，不补 0、不补默认值）。

    写法照本模块既有的动作：手替她动手（role=organ），by 记让她改的那个人。第二处薄适配。
    """
    knobs: Dict[str, Any] = {name: before.get(name) for name in whitelist.STYLE_KEYS
                             if name != key}
    knobs[key] = value
    return dict(home_add_preference(root=home, role=ROLE, by=who, who=who, at=at,
                                   **knobs) or {})


def set_style(*, who: str, key: Any = None, value: Any = None, utterance: Any = None,
              now: Any = None, root=None) -> Dict[str, Any]:
    """改「她怎么说话」：两个旋钮写进家·名片盒的偏好卡，账上记清谁改的、改成什么、原来是什么。

    三条判据：
    1. 可写的 key **恰两个**（whitelist.STYLE_KEYS）。别的 key——尤其「身份／人设／名字／魂」
       这一类——一律 KEY_NOT_ALLOWED，人话里明说「改她是谁不在这里」。
    2. utterance 走 stylelang 那张短语表，认不出就 STYLE_NOT_UNDERSTOOD 反问，**零写入**。
    3. 先落 reserved → 读旧值 → 让家追加一行 → 结果行写不进就改判 failed（照旧口径）。
    """
    started = time.monotonic()
    params: Dict[str, Any] = {"key": key, "value": value, "utterance": utterance}
    permitted = permit.check("set_style", who=who, now=now, root=root)
    if not permitted["allowed"]:
        return _refuse(who=who, action="set_style", params=params, now=now, root=root,
                       started=started, code=str(permitted["code"]), say=permitted["say"],
                       detail=permitted["detail"])

    target, problem = _style_target(key=key, value=value, utterance=utterance)
    if problem is not None:
        return _refuse(who=who, action="set_style", params=params, now=now, root=root,
                       started=started, code=str(problem["code"]), say=str(problem["say"]),
                       detail={"stage": "understand", "reason": problem["reason"],
                               "key": key, "value": value, "utterance": utterance})

    style_key = target["key"]
    bounds = whitelist.style_range(style_key)
    if bounds is None:
        # 身份类不是「没听懂」：换个说法也没用，所以码要落在 KEY_NOT_ALLOWED 上。
        return _refuse(who=who, action="set_style", params=params, now=now, root=root,
                       started=started, code="KEY_NOT_ALLOWED", say=_style_keys_say(style_key),
                       detail={"stage": "understand",
                               "reason": "identity" if stylelang.is_identity(style_key)
                                         else "key_not_allowed",
                               "key": style_key, "allowed": sorted(whitelist.STYLE_KEYS)})

    number = _style_int(target["value"])
    low, high = bounds
    if number is None or not low <= number <= high:
        return _refuse(who=who, action="set_style", params=params, now=now, root=root,
                       started=started, code="VALUE_OUT_OF_RANGE",
                       say="%s 只收 %d–%d 的整数（收到 %r）——今天没改"
                           % (style_key, low, high, target["value"]),
                       detail={"stage": "understand",
                               "reason": "not_an_int" if number is None else "out_of_range",
                               "key": style_key, "value": target["value"],
                               "range": [low, high]})

    ledger_id, failure = _begin(who=who, action="set_style", params=params, now=now, root=root,
                                started=started)
    if failure is not None:
        return failure

    home = store.home_root(root)
    try:
        before = _pref_read(who, home)
    except _HOME_ERRORS as exc:
        # 旧值读不出来就**不写**：账上「原来是什么」绝不能是编的。
        origin = _origin_of(exc)
        return _settle(who=who, action="set_style", params=params, now=now, root=root,
                       started=started, ledger_id=ledger_id, state="failed",
                       code="HOME_READ_FAILED", say=STYLE_READ_FAILED_SAY, origin=origin,
                       detail={"stage": "read_preference", "home": origin, "key": style_key})

    old = before.get(style_key)
    old_source = "live" if old is not None else "default"
    card_id = str(before.get("card_id") or "") or None

    try:
        written = _pref_write(who, style_key, number, before=before, home=home,
                              at=store.now_iso(now))
    except _HOME_ERRORS as exc:
        origin = _origin_of(exc)
        return _settle(who=who, action="set_style", params=params, now=now, root=root,
                       started=started, ledger_id=ledger_id, state="failed",
                       code="HOME_WRITE_FAILED", say=HOME_FAILED_SAY, origin=origin,
                       detail={"stage": "write_preference", "home": origin, "key": style_key,
                               "value": number, "old": old, "old_source": old_source,
                               "card_id": card_id})

    written_card_id = str(written.get("card_id") or "") or card_id
    detail: Dict[str, Any] = {"key": style_key, "value": number, "old": old,
                              "old_source": old_source, "card_id": written_card_id,
                              "who": who, "target_who": who, "source": target["source"],
                              "utterance": utterance, "range": [low, high]}
    params = _accounting(params, detail, STYLE_ACCOUNTING)
    return _attach_quota(
        _settle(who=who, action="set_style", params=params, now=now, root=root,
                started=started, ledger_id=ledger_id, state="done", code="OK",
                say=_style_done_say(style_key, number, old, old_source),
                card_id=written_card_id, detail=detail),
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
