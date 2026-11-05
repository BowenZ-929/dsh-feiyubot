"""家的十二条路由：把十间房、存取接口、只增的审计、三样住户与台词卡接到 HTTP 上。

形状照 api/mouth.py：一张 HOME_ROUTES 表 + dispatch(route, params, store, brain, body=None)。
store 是**家的根**（一个路径）；本层的每一项都要读盘，所以它没载入时一律回 200 +
status="未接入"，不 500、也不假装有数据。

四条不越界的规矩写在这里，因为它们最容易在这一层被"顺手补一下"：

1. **零文件权限判断**。能不能写全在 organs/home/access.py 的两道门：门一是结构门（只读五间对
   三个角色一律拒），门二是白名单门（只管 role="agent"）。本文件不重判一次，只把 AccessDenied
   原样翻成 HTTP——**detail.door 要跟出去**，人才分得清「这间房从不放东西（structure）」和
   「她今天被收回这间（whitelist）」。白名单只在 /home/access（与 /home/rooms 的"她现在可写"
   那一栏）里**展示**：拿 access 的解析结果原样答，不拿它判任何一次读写，也不自己算一遍。
   判据：docs/plan.md:45「能不能写看路由表，不看 UI 灰化」。
2. **审计只增不补**。verify 说 AUDIT_SHRUNK 就回 409 明说只报不修；不补行、不重写高水位
   （docs/modules/home.md:110）。补回去就把事故抹平了。
3. **读侧不看白名单**。/home/list 与 /home/read 只按 access 的读上限（200 行／6000 字）截断，
   截了就如实回 truncated 与 total；她今天能写哪几间，与她／别人能读什么无关。
4. **未写就不报写了**：纯说明卡如实回 body=null, external=true；缺件回 status="未接入"，
   不许报 0 字、不许编内容。

错误码映射一张表在下面（_REFUSED）：码与话都照搬库里的原话，这一层只决定用哪个状态码说；
门挡下来的（detail.door）一律 403，不按码再判一次。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from organs.home import access, anchors, audit, cards, residents, rooms
from organs.home import store as home_store

from .core import Result

HOME_ROUTES = {
    "/home/rooms": "GET",
    "/home/access": "GET",
    "/home/list": "GET",
    "/home/read": "GET",
    "/home/audit": "GET",
    "/home/audit/verify": "GET",
    "/home/drawer": "GET",
    "/home/archive/borrow": "GET",
    "/home/warehouse": "GET",
    "/home/anchors": "GET",
    "/home/anchors/append": "POST",
    "/home/write": "POST",
}
AGENT = "home"
AUDIT_TAIL_DEFAULT = 20
AUDIT_TAIL_MAX = 200

# 门给出的两个 door：一个是「这间房从不放东西」，一个是「她今天被收回这间」。
_DOORS = ("structure", "whitelist")

# 库里的拒绝码 → HTTP。认不得的算 500：宁可报错，也不把「读不出来」说成 4xx 的入参问题。
_REFUSED: Dict[str, int] = {
    # 400：入参或路径形状不对
    "ROOM_MISS": 400, "BAD_ARGUMENT": 400, "BODY_NOT_SELF": 400,
    "PATH_EMPTY": 400, "PATH_CONTROL_CHAR": 400, "PATH_ABSOLUTE": 400, "PATH_SHAPE": 400,
    "PATH_TRAVERSAL": 400, "PATH_SEGMENT_TOO_LONG": 400, "PATH_TOO_DEEP": 400,
    "CHARS_OVER": 400, "FILES_OVER": 400, "READ_LINES_OVER": 400, "READ_CHARS_OVER": 400,
    "BAD_CARD": 400, "BAD_KIND": 400, "BAD_NAME": 400, "BAD_CHARS": 400,
    "NOT_HOME_BUSINESS": 400, "ACCESS_ROOM_UNKNOWN": 400,
    # 403：角色不认识（门挡下来的那一类由 _status_of 按 detail.door 判，不在这张表里）
    "ROLE_UNKNOWN": 403,
    # 404：卡不在（房名对、件不在）
    "CARD_NOT_FOUND": 404,
    # 409：盘上的东西跟卡／账／高水位对不上——只报，不自动修
    "CARD_DISK_MISMATCH": 409, "AUDIT_SHRUNK": 409, "POINTER_STALE": 409,
    "ROOM_CAP_REACHED": 409, "DAY_QUOTA_REACHED": 409,
}


# 库里有两条线各报一次「房名不在十间」：access 报 ROOM_MISS、cards 报 ROOM_UNKNOWN。
# 对外同一个意思就用同一个码——/home/* 一律回 ROOM_MISS，库的原码留在 detail.code_from 里。
_ALIASES = {"ROOM_UNKNOWN": "ROOM_MISS"}


class _BadArgument(Exception):
    """本层自己的入参问题（缺 room、limit 不是数）：与门挡下来的拒绝分开，回 400。"""


def dispatch(route: str, params: Mapping[str, str], store: Any, brain: Any,
             body: Optional[Dict[str, Any]] = None) -> Result:
    """家的十条路由；十项都要读盘，所以 store（家的根）为 None 时一律如实报未接入。

    入参一律来自查询串，写的那一条来自 body（body 只在这里读，GET 不看它）。
    brain 不参与：家不反读任何人，也没有第二套人名录。
    库抛的这些异常在这里统一翻成 HTTP，**一个都不吞**：AccessDenied 是门的拒绝，
    CardError 是卡本身或卡与盘的关系，StoreError 是落盘失败。
    """
    if store is None:
        return _unconnected(route, "家未载入（store=None），十间房都读不到",
                            wrote=(HOME_ROUTES.get(route) == "POST"))
    root = _root(store)
    try:
        return _handle(route, params, root, body)
    except _BadArgument as exc:
        return _failed("BAD_ARGUMENT", str(exc), {})
    except access.AccessDenied as exc:
        return _failed(exc.code, exc.message, exc.detail)
    except cards.CardError as exc:
        return _failed(exc.code, exc.message, exc.detail)
    except anchors.AnchorError as exc:
        return _failed(exc.code, exc.message, exc.detail)
    except home_store.StoreError as exc:
        return _failed(exc.code, str(exc.detail), {"code": exc.code})
    except (OSError, ValueError) as exc:
        return _failed("HOME_IO_ERROR", "家这边没能完成这次请求（读盘／写盘／落账失败）：%s" % exc, {})


def _handle(route: str, params: Mapping[str, str], root: Any,
            body: Optional[Dict[str, Any]]) -> Result:
    if route == "/home/write":
        return _write(root, body)
    if route == "/home/rooms":
        return _rooms_table(root)
    if route == "/home/access":
        return _access_now(root)
    if route == "/home/list":
        return _list(root, params)
    if route == "/home/read":
        return _read(root, params)
    if route == "/home/audit":
        return _audit_tail(root, params)
    if route == "/home/audit/verify":
        return _audit_verify(root)
    if route == "/home/drawer":
        return _drawer(root)
    if route == "/home/archive/borrow":
        return _borrow(root)
    if route == "/home/anchors":
        return _anchors_list(root, params)
    if route == "/home/anchors/append":
        return _anchors_append(root, body)
    return _warehouse(root)


# —— 十条端点 ————————————————————————————————————————

def _rooms_table(root: Any) -> Result:
    """十间房的表：结构可写与她此刻可写**分两栏**答，另报件数与余量。

    白名单读不出来时不把整张表打回 500——房名、slug、放什么、结构可写都是房子本身的形状，
    答得出就先答；只有"她现在可写"那一栏如实记 None，并在 whitelist_error 里说清为什么。
    """
    whitelist, why = _whitelist(root)
    rows: List[Dict[str, Any]] = []
    for room in rooms.ROOMS:
        cap = access.ROOM_CAP.get(room.name)
        count = _card_count(root, room.name)
        rows.append({
            "name": room.name,
            "slug": room.slug,
            "purpose": room.purpose,
            "writable_by_structure": room.writable_default,
            "writable_now": _now_writable(room, whitelist),
            "count": count,
            "anchors": anchors.count(root, room.name),
            "cap": cap,
            "headroom": None if cap is None else cap - count,
        })
    payload: Dict[str, Any] = {
        "ok": True, "readonly": True, "agent": AGENT,
        "root": str(home_store.home_root(root)),
        "rooms": rows,
        "total": len(rows),
        "writable_now": None if whitelist is None else sorted(whitelist.writable),
        "writable_now_source": None if whitelist is None else whitelist.source,
        "note": "结构可写（这间房放不放东西，改它等于改建房）与她此刻可写（白名单，改一份配置）"
                "是两栏；count 是折过之后的卡数（一件一张卡），cap／headroom 只仓库、卧室、橱柜"
                "三间有，其余 null＝没有件数上限。读侧不看白名单：这一栏只用来给人看。",
    }
    if whitelist is None:
        payload["whitelist_error"] = why
    return 200, payload


def _access_now(root: Any) -> Result:
    """白名单现状：哪几间、哪一级解析出来的、改哪份文件——「她写哪几间现在怎么配置」的对外答案。

    白名单文件读不出来就照实回 500（在 _whitelist 里拿不到值、这里原话往上抛）：
    一份读不出来的配置既不是"谁都能写"，也不是"谁都不能写"，不许折叠成空表。
    """
    whitelist, why = _whitelist(root)
    if whitelist is None:
        raise access.AccessDenied(why["code"], why["message"], why["detail"])
    kind, _, path = whitelist.source.partition(":")
    structural = [room.name for room in rooms.ROOMS if room.writable_default]
    readonly = [room.name for room in rooms.ROOMS if not room.writable_default]
    return 200, {
        "ok": True, "readonly": True, "agent": AGENT,
        "writable": sorted(whitelist.writable),
        "source": whitelist.source,
        "source_kind": kind,          # explicit＝构造参数 / env＝环境变量 / root＝home_root()
        "file": path or None,
        "env": access.ENV_ACCESS,
        "default": sorted(structural),
        "readonly_rooms": sorted(readonly),
        "root": str(home_store.home_root(root)),
        "note": "白名单只记她（role=agent）今天能写哪几间；只读五间是结构决定的，不进这份配置。"
                "file 就是现在生效的那一份：改它的 writable 数组，下一次请求立刻照新的算（不缓存）。",
    }


def _list(root: Any, params: Mapping[str, str]) -> Result:
    """读一间房的索引卡（折叠后视角）：截了就说截了，total 照实报。

    READ_MAX_LINES／READ_MAX_CHARS 的截断在 cards.list_room 里做——本层不另判一次。
    """
    room = _text(params, "room")
    if room is None:
        raise _BadArgument("/home/list 要给 room（房名或 slug）")
    offset = _int_param(params, "offset", 0)
    limit = _int_param(params, "limit", access.READ_MAX_LINES)
    payload: Dict[str, Any] = {"ok": True, "readonly": True, "agent": AGENT}
    payload.update(cards.list_room(root, room=room, offset=offset, limit=limit))
    payload["note"] = "一次最多读 %d 行／%d 字：截了就 truncated=true，total 是折叠后的件数" \
                      "（不是 index.jsonl 的行数）" % (access.READ_MAX_LINES, access.READ_MAX_CHARS)
    return 200, payload


def _read(root: Any, params: Mapping[str, str]) -> Result:
    """读一件东西：卡与盘对不上回 409；纯说明卡如实回 body=null、external=true，不编内容。"""
    room = _text(params, "room")
    name = _text(params, "name")
    if room is None or name is None:
        raise _BadArgument("/home/read 要给 room 与 name（name 可以给卡名或 card_id）")
    payload: Dict[str, Any] = {"ok": True, "readonly": True, "agent": AGENT}
    payload.update(cards.read_item(root, room=room, name=name))
    if payload.get("body") is None:
        payload["note"] = "body=null：这张卡没有可读的正文（纯说明卡，或原件不是 md／jsonl）。" \
                          "家只记路径与指纹，不搬本体——这里不编一段内容出来。"
    return 200, payload


def _audit_tail(root: Any, params: Mapping[str, str]) -> Result:
    """审计尾：默认 20 条、上限 200 条。读账不算写账，不碰高水位。"""
    limit = min(_int_param(params, "limit", AUDIT_TAIL_DEFAULT), AUDIT_TAIL_MAX)
    entries = audit.read_tail(root, limit=limit)
    return 200, {
        "ok": True, "readonly": True, "agent": AGENT,
        "limit": limit, "count": len(entries),
        "entries": [entry.as_row() for entry in entries],
        "note": "只取最后 %d 条；读账不写账，也不动 %s" % (limit, audit.HIGH_WATER_NAME),
    }


def _audit_verify(root: Any) -> Result:
    """行数／序号 vs 高水位；变少回 409 AUDIT_SHRUNK，正文里明说只报不修。

    这里绝不为「看起来正常」去补行或重写高水位：账少了要么盘坏了、要么有人在改账，
    两种都得先被人看见（docs/modules/home.md:110）。
    """
    report = audit.verify(root)
    mark = report.get("mark") or {}
    if report.get("code") == audit.CODE_SHRUNK:
        return 409, {
            "ok": False, "code": audit.CODE_SHRUNK, "agent": AGENT,
            "message": "审计账比高水位短：现在 %d 行／序号 %d，高水位记的是 %s 行／序号 %s。"
                       "只报不修——不补行、不重写高水位；补回去就把事故抹平了。"
                       % (report["lines"], report["seq"], mark.get("lines"), mark.get("seq")),
            "lines": report["lines"], "seq": report["seq"], "mark": mark or None,
            "repaired": False,
            "detail": {"lines": report["lines"], "seq": report["seq"], "mark": mark or None,
                       "bad": report.get("bad", 0), "repaired": False},
        }
    payload: Dict[str, Any] = {"agent": AGENT, "readonly": True, "repaired": False}
    payload.update(report)
    payload["note"] = "只比对不修补：ok=false 只在账与高水位对不上时出现（原因看 code），" \
                      "文件一个字节都没改写"
    return 200, payload


def _drawer(root: Any) -> Result:
    """抽屉资料卡：只读、缺件如实报未接入 + 模板路径，不许报 0 字、不许编内容。"""
    data = residents.read_drawer(root)
    payload: Dict[str, Any] = {"ok": True, "agent": AGENT}
    payload.update(data)
    if data.get("status") != "已接入":
        payload["message"] = ("抽屉里还没有那张资料卡（%s）：缺件就报未接入，不报 0 字、不编内容。"
                              "抽屉只读，程序写不进去——照 template 那份由人直接在盘上写。"
                              % data.get("path"))
        payload.pop("chars", None)
    return 200, payload


def _borrow(root: Any) -> Result:
    """借阅条账（落名片盒）+ 读数合并：缺读数一律 learned=null、readout_status=未接入。"""
    data = residents.borrow_ledger(root)
    payload: Dict[str, Any] = {"ok": True, "readonly": True, "agent": AGENT}
    payload.update(data)
    # 库只在账不存在时给 status="未接入"；缺这个键就等于账在——把这一栏补成常驻，别让调用方猜
    payload["status"] = payload.get("status") or "已接入"
    payload["note"] = ("借阅条落在 %s（十间里的一间，不是第十一间房）：一条一借，只借不搬。"
                       "读数由人改文件，程序只在读的时候合并——答不出就是 learned=null + "
                       "readout_status=未接入，不报 0。" % data.get("room"))
    return 200, payload


def _warehouse(root: Any) -> Result:
    """仓库指针卡 + 现场重算 sha256：对不上回 409，只报不自动更新卡。"""
    data = residents.warehouse_pointers(root)
    payload: Dict[str, Any] = {"ok": True, "readonly": True, "agent": AGENT}
    payload.update(data)
    if not data["total"]:
        payload["status"] = "未接入"
        payload["message"] = ("仓库里还没有指针卡：跑 python3 -m organs.home.cli seed 登记嘴那三份的"
                              "指针（%s）。本端点只读，不替你登记。" % residents.MOUTH_ENV)
        return 200, payload
    if data["stale"]:
        stale_rows = [item for item in data["pointers"] if item.get("code") == "POINTER_STALE"]
        return 409, {
            "ok": False, "code": "POINTER_STALE", "agent": AGENT,
            "message": "现算的 sha256／字节数跟卡上对不上：%s。改了就是改了——本端点只报，"
                       "不自动更新卡。" % "；".join(
                           "%s（现算 %s… / 卡上 %s…，%s 字节 / 卡上 %s 字节）"
                           % (item.get("name"), str(item.get("actual_sha256") or "")[:12],
                              str(item.get("card_sha256") or "")[:12],
                              item.get("actual_bytes"), item.get("card_bytes"))
                           for item in stale_rows),
            "aligned": False,
            "stale": list(data["stale"]), "missing": list(data["missing"]),
            "detail": {"pointers": data["pointers"], "data_root": data["data_root"],
                       "bad_lines": data["bad_lines"]},
        }
    payload["note"] = "aligned 是现场重算的结果（卡上记的 vs 盘上现算）；missing 非空说明" \
                      "卡在、文件不在了——同样只报不修"
    return 200, payload


def _write(root: Any, body: Optional[Dict[str, Any]]) -> Result:
    """POST /home/write：整批登记一批卡，然后由 cards.append_cards 逐张追审计。

    **本层不判一次写权**：两道门（结构门＋白名单门）与五道上限全在 access 里，
    cards.append_cards 按顺序过门——这里的 try/except 只把拒绝翻成 HTTP，
    不许兜底成允许，也不许失败后重试一次更宽的写法。
    """
    if not isinstance(body, dict):
        raise _BadArgument("POST /home/write 的 body 要是一个对象："
                           "{\"role\",\"by\",\"room\",\"cards\"}")
    role = _clean(body.get("role"))
    by = _clean(body.get("by"))
    batch = body.get("cards")
    if role is None:
        raise _BadArgument("body 要给 role（agent／organ／human）")
    if by is None:
        raise _BadArgument("body 要给 by（谁写的，会记进审计）")
    if not isinstance(batch, list) or not batch:
        raise _BadArgument("body 要给 cards（一张以上的卡；空批等于什么都没登记）")
    room = _clean(body.get("room"))
    if room is not None:
        batch = [_with_room(card, room) for card in batch]
    outcome = cards.append_cards(root, role=role, by=by, cards=batch)
    written = outcome.get("cards") or []
    return 200, {
        "ok": True, "readonly": False, "agent": AGENT,
        "count": len(written),
        "rooms": sorted({str(card.get("room")) for card in written}),
        "cards": written,
        "audit": _rows(outcome.get("audit") or []),
        "note": "门在 access 里：结构门看房、白名单门只管 role=agent。本层没有第二处判定，"
                "也没有兜底——拒了就是拒了（403 带 detail.door）。",
    }


# —— 库的调用与小工具 ————————————————————————————————

def _root(store: Any) -> Any:
    """把核传进来的 store 读成家的根：是路径就用它（构造参数），别的对象就当没给。

    核里 store 是魂的 store（devserver 传的是它），那不是家的根；家不借它、也不猜它，
    落回 home_root() 的三级解析（构造参数 > HOME_ROOT > 包内 data/）。
    """
    if isinstance(store, (str, os.PathLike)):
        return Path(store)
    return None


def _whitelist(root: Any) -> Tuple[Any, Dict[str, Any]]:
    """白名单的**原样返回值**（本文件只在这一处拿它），读不到时给 (None, 原因)。

    拿它只为了展示、不为了判读写：门的判定全在 access 里，本层没有第二次判定。
    """
    try:
        return access.load_whitelist(root), {}
    except access.AccessDenied as exc:
        return None, {"code": exc.code, "message": exc.message, "detail": dict(exc.detail)}


def _now_writable(room: Any, whitelist: Optional[Any]) -> Optional[bool]:
    """她此刻能不能写这一间：只读房永远 false（结构），可写房看白名单；白名单读不到就是 None。"""
    if not room.writable_default:
        return False
    if whitelist is None:
        return None
    return room.name in whitelist.writable


def _card_count(root: Any, room_name: str) -> int:
    """这一间有几件东西：折过之后的卡数（一件一张卡），不是 index.jsonl 的行数。"""
    return cards.list_room(root, room=room_name, offset=0, limit=0).get("total", 0)


def _text(params: Mapping[str, str], key: str) -> Optional[str]:
    """查询串里的一个文本参数：没给或全是空白都算没给（返回 None）。"""
    raw = params.get(key)
    if raw is None:
        return None
    body = raw if isinstance(raw, str) else str(raw)
    return body.strip() or None


def _clean(raw: Any) -> Optional[str]:
    """body 里的一个文本字段：非空白的字符串才算给了。"""
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def _int_param(params: Mapping[str, str], key: str, default: int) -> int:
    """查询串里的一个整数：没给就是默认值；给了读不成数或负数就当场 400。"""
    raw = _text(params, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _BadArgument("%s 要给一个整数，给了 %r" % (key, raw))
    if value < 0:
        raise _BadArgument("%s 不能是负数：%d" % (key, value))
    return value


def _with_room(card: Any, room: str) -> Any:
    """顶层的 room 落到每张卡上：卡自带 room 就照卡的（一卡一房，批量不等于同房）。"""
    if not isinstance(card, dict) or card.get("room"):
        return card
    return dict(card, room=room)


def _rows(entries: Any) -> List[Any]:
    """审计条目 → JSON 行：库给的是 Entry，别在返回里塞对象。"""
    return [entry.as_row() if hasattr(entry, "as_row") else entry for entry in entries]


def _status_of(code: str, detail: Mapping[str, Any]) -> int:
    """状态码怎么选：门挡下来的回 403，其余按码归类，认不得的算 500。

    两条都算「门挡下来的」：① 门在 detail 里给了 door（structure＝这间房从不放东西，
    whitelist＝她今天被收回这间）；② 白名单配置自己点名了一间结构只读的房——那是配置与房子
    打架，人得去改那份文件，与结构门的拒绝同属「这间不该放东西」。
    """
    if detail.get("door") in _DOORS:
        return 403
    if code.startswith("ACCESS_") and "readonly" in detail:
        return 403
    return _REFUSED.get(code, 500)


def _failed(code: str, message: str, detail: Optional[Mapping[str, Any]] = None) -> Result:
    """把库里的拒绝原样翻成 HTTP：码照搬、话照搬、detail 照搬。

    detail.door 与 detail.nearest 另在顶层各露一份：人要能一眼分清两道门（structure／whitelist），
    房名给错时也要能直接点开最接近的那一间（docs/modules/home.md:122）。
    """
    original = code
    code = _ALIASES.get(code, code)
    body: Dict[str, Any] = {"ok": False, "agent": AGENT, "code": code,
                            "message": message, "detail": dict(detail or {})}
    if code != original:
        body["detail"]["code_from"] = original
    for key in ("door", "nearest"):
        if key in body["detail"]:
            body[key] = body["detail"][key]
    return _status_of(code, body["detail"]), body


def _unconnected(route: str, why: str, wrote: bool = False) -> Result:
    """未接入：200 + status="未接入"。不 500，也不给一个看起来像数据／像写成功的答案。"""
    payload: Dict[str, Any] = {"ok": True, "status": "未接入", "readonly": not wrote,
                               "agent": AGENT, "route": route,
                               "message": "%s：%s，只能如实报「未接入」" % (route, why)}
    if wrote:
        payload["wrote"] = 0
        payload["message"] += "；这一批一张卡都没写，也没有审计行"
    return 200, payload

def _anchors_list(root: Any, params: Mapping[str, str]) -> Result:
    """GET /home/anchors：仓库里的台词卡，按 scene 取最新一条。

    读侧不看白名单（她能写哪几间与她或别人能读什么无关）；文件不在不是错误，
    如实分报 file_exists 与 lines，不把「还没人给她定台词」说成「0 条台词」。
    """
    offset = max(_int_param(params, "offset", 0), 0)
    limit = min(max(_int_param(params, "limit", 200), 1), access.READ_MAX_LINES)
    got = anchors.read_all(root, offset=offset, limit=limit)
    return 200, {
        "ok": True, "readonly": True, "agent": AGENT,
        "room": got["room"], "file": got["file"], "file_exists": got["file_exists"],
        "seed_file": str(anchors.seed_path()),
        "source": "live",
        "total": got["total"], "lines": got["lines"], "bad": got["bad"],
        "truncated": got["truncated"], "anchors": got["anchors"],
    }


def _anchors_append(root: Any, body: Optional[Dict[str, Any]]) -> Result:
    """POST /home/anchors/append：登记一张以上的台词卡。只追加，改一句就追加一行新的。

    **本层不判一次写权**：门与五道上限全在 access（房间固定是仓库），
    这里只把 AnchorError 翻成 HTTP，不许兜底成允许。
    """
    if not isinstance(body, dict):
        raise _BadArgument("POST /home/anchors/append 的 body 要是一个对象："
                           "{\"role\",\"by\",\"anchors\":[{\"scene\",\"say\",\"body\"}]}")
    role = _clean(body.get("role"))
    by = _clean(body.get("by"))
    batch = body.get("anchors")
    if role is None:
        raise _BadArgument("body 要给 role（agent／organ／human）")
    if by is None:
        raise _BadArgument("body 要给 by（谁定的，会记进审计）")
    if not isinstance(batch, list) or not batch:
        raise _BadArgument("body 要给 anchors（一张以上；空批等于什么都没登记）")
    outcome = anchors.append_anchors(root, role=role, by=by, anchors=batch)
    return 200, {
        "ok": True, "readonly": False, "agent": AGENT,
        "room": outcome["room"], "file": outcome["file"],
        "count": outcome["count"], "anchors": outcome["anchors"],
        "audit": _rows(outcome["audit"]),
    }
