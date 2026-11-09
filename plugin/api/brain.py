"""脑的六个只读端点：原样搬自 api/api.py，逻辑一个字没改。

脑的写入口是程序内的 ingest()，面板没有、也不许有写脑的路由（architecture §9.2）。
brain=None（脑没载入）时 /brain 保持"未接入"，其余如实报未载入。
"""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import unquote

from organs.brain.model import PENDING
from organs.brain.store import BrainReadError

from .core import Result

BRAIN_ROUTES = {
    "/api/graph/snapshot": "GET",
    "/brain": "GET",
    "/brain/overview": "GET",
    "/brain/today": "GET",
    "/brain/people": "GET",
    "/brain/recall": "GET",
    "/brain/selfnarration": "GET",
    "/brain/proposals": "GET",
    "/brain/tiers": "GET",
    "/brain/cold": "GET",
    "/brain/judge": "POST",
}


def _overview(brain: Any) -> Result:
    """脑的只读总览；/brain/overview 供面板页拉取（devserver 的 GET /brain 是静态页）。"""
    today = brain.today_view()
    return 200, {
        "status": "已接入",
        "readonly": True,
        "agent": "brain",
        "today_entries": len(today["entries"]),
        "people": len(brain.people_list()),
        "longterm_entries": len(brain.longterm_entries()),
        "pending_proposals": len(brain.proposals().get("proposals", [])),
        "selfnarration_version": (brain.selfnarration_latest() or {}).get("version", 0),
        "pending": ["热记忆的形状", "遗忘的力度", "记忆滤镜"],
    }


def dispatch(route: str, params: Mapping[str, str], body: Any, brain: Any) -> Result:
    """脑的六个只读端点。brain=None 时 /brain 保持「未接入」，其余如实报未载入。"""
    if brain is None:
        if route == "/brain":
            return 200, {"status": "未接入", "readonly": True, "agent": "brain"}
        return 503, {"ok": False, "message": "脑未载入：先起脑的数据目录（devserver --brain-root）"}
    if route in ("/brain", "/brain/overview"):
        return _overview(brain)
    if route == "/api/graph/snapshot":
        try:
            return 200, brain.graph_snapshot()
        except (BrainReadError, OSError, ValueError, KeyError, TypeError):
            return 503, {"ok": False, "message": "记忆星图暂时读不到，请检查脑的数据；没有用空图替代。"}
    if route == "/brain/today":
        return 200, brain.today_view()
    if route == "/brain/people":
        return 200, {"people": brain.people_list()}
    if route == "/brain/recall":
        q = params.get("q", "").strip()
        if not q:
            return 400, {"ok": False, "message": "回想必须带 q（查询串）"}
        return 200, brain.recall(q, who=params.get("who") or None)
    if route == "/brain/selfnarration":
        return 200, {"latest": brain.selfnarration_latest(),
                     "versions": brain.selfnarration_versions()}
    if route == "/brain/judge":
        if not isinstance(body, dict):
            return 400, {"ok": False, "message": "body 必须是 JSON 对象"}
        try:
            from organs.brain.judge import JudgeInput, decide
        except Exception as exc:  # noqa: BLE001
            return 500, {"ok": False, "message": "judge 载入失败：%s" % exc}
        inp = JudgeInput(
            text=body.get("text", ""),
            heart_state=body.get("heart_state") or {},
            familiarity=float(body.get("familiarity", 0)),
            material=body.get("material") or {},
            daily_quota=body.get("daily_quota") or {"used": 0, "limit": 0},
            named=bool(body.get("named")),
            direct_question=bool(body.get("direct_question")),
            redline_hits=body.get("redline_hits") or [],
            ignore_hits=body.get("ignore_hits") or [],
            who=body.get("who", "主人"),
            intent=body.get("intent", "passive"),
        )
        return 200, decide(inp)
    if route == "/brain/tiers":
        try:
            return 200, brain.tiers_summary()
        except Exception as exc:  # noqa: BLE001 - 读不到要报错，不许折叠成 0 条
            return 500, {"ok": False, "message": "脑数据读不到：%s" % exc}
    if route == "/brain/cold":
        cursor = params.get("cursor") or None
        try:
            limit = int(params["limit"]) if params.get("limit") else 20
        except ValueError:
            return 400, {"ok": False, "message": "limit 必须是整数"}
        try:
            return 200, brain.cold_page(cursor=cursor, limit=limit)
        except Exception as exc:  # noqa: BLE001 - 读不到要报错
            return 500, {"ok": False, "message": "冷仓读不到：%s" % exc}
    return 200, brain.proposals()


def dispatch_why(method: str, route: str, brain: Any) -> Result:
    """只解释落盘的关联/命中记录，不调用 recall，不伪造一次召回的名次。"""
    if method != "GET":
        return 405, {"ok": False, "message": "星图详情只读，只收 GET"}
    status, snapshot = dispatch("/api/graph/snapshot", {}, None, brain)
    if status != 200:
        return status, snapshot
    node_id = route[len("/api/memory/"):-len("/why")]
    node = next((row for row in snapshot["nodes"] if row["id"] == node_id), None)
    if node is None:
        return 404, {"ok": False, "message": "这条记忆或节点已不存在"}
    return 200, {"node": node, "edges": [row for row in snapshot["edges"] if node_id in (row["from"], row["to"])],
                 "why": {"hitReason": None, "rank": None, "quota": None,
                         "source": node.get("source"), "ref": node.get("evidence_key"),
                         "note": "这里只展示已有关系和累计命中记录；没有这条记忆的逐轮召回排名。"},
                 "readonly": True, "revision": snapshot["revision"]}


def dispatch_note(method: str, route: str, body: Any, brain: Any) -> Result:
    """PATCH /brain/memory/<id>/note：人改理解，quote 逐字不动，历史留痕。"""
    if brain is None:
        return 503, {"ok": False, "message": "脑未载入：先起脑的数据目录（devserver --brain-root）"}
    entry_id = route[len("/brain/memory/"):-len("/note")]
    if not entry_id:
        return 404, {"ok": False, "message": "没有这个端点：%s" % route}
    if method != "PATCH":
        return 405, {"ok": False, "message": "%s 只收 PATCH（改 note 不覆盖 quote）" % route}
    if not isinstance(body, dict):
        return 400, {"ok": False, "message": "body 必须是 JSON 对象"}
    actor = body.get("actor", "").strip()
    if not actor:
        return 400, {"ok": False, "message": "需要 actor"}
    result = brain.edit_note(entry_id, actor, body.get("note", ""),
                             unclear=body.get("unclear"))
    return (200 if result.get("ok") else 400), result


def dispatch_card(method: str, route: str, body: Any, brain: Any) -> Result:
    """PATCH /brain/people/<id>/card：名片白名单字段人可写，熟稔不收；GET 读名片+历史。"""
    if brain is None:
        return 503, {"ok": False, "message": "脑未载入：先起脑的数据目录（devserver --brain-root）"}
    # 人名可以是中文：路由里是百分号编码，取出来要先还原，否则「小明」永远查不到。
    person_id = unquote(route[len("/brain/people/"):-len("/card")])
    if not person_id:
        return 404, {"ok": False, "message": "没有这个端点：%s" % route}
    if method == "GET":
        card = brain.card_view(person_id)
        if card is None:
            return 404, {"ok": False, "message": "人名录里没有 %s" % person_id}
        return 200, card
    if method == "PATCH":
        if not isinstance(body, dict):
            return 400, {"ok": False, "message": "body 必须是 JSON 对象"}
        actor = body.get("actor", "").strip()
        fields = body.get("fields")
        if not actor or not isinstance(fields, dict):
            return 400, {"ok": False, "message": "需要 actor 与 fields（JSON 对象）"}
        result = brain.patch_card(person_id, actor, fields)
        return (200 if result.get("ok") else 400), result
    return 405, {"ok": False,
                 "message": "%s 只收 GET/PATCH（名片字段人可写，熟稔只读）" % route}


def dispatch_preferences(method: str, route: str, body: Any, brain: Any) -> Result:
    """按人查看各会话偏好；面板可撤回，新增和更正从本人聊天学习。"""
    from organs.brain import preferences
    from organs.brain.model import now_iso
    if brain is None:
        return 503, {"ok": False, "message": "脑未载入"}
    who = route[len('/brain/people/'):-len('/preferences')]
    rows = preferences.history(brain.root, who)
    replaced = {key for row in rows for key in row['replaces']}
    active = [row for row in rows if row['text'] and row['id'] not in replaced]
    if method == 'GET':
        return 200, {'ok': True, 'current': active, 'history': rows}
    if method != 'POST':
        return 405, {'ok': False, 'message': '只收 GET 查看或 POST 撤回'}
    if not isinstance(body, dict) or not isinstance(body.get('id'), str):
        return 400, {'ok': False, 'message': '需要待撤回的偏好 id'}
    selected = next((row for row in active if row['id'] == body['id']), None)
    if selected is None:
        return 404, {'ok': False, 'message': '此人没有这条现行偏好'}
    event = preferences.record(brain.root, who=who, session_id=selected['session_id'],
        change={'text': '', 'evidence': [], 'replaces': [selected['id']]}, at=now_iso(), actor='panel')
    return 200, {'ok': True, 'event': event}
