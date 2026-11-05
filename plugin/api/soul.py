"""魂的十二个端点：读三块、自检、变更史、注入面、改值、迁形状、接受提案、pin、版本、回滚、
红线增减、验收探针。

只读端点只调 store 的读方法，不落盘；写端点直接转调 store 的方法，不改写它们的返回结构。
GET /soul 并排给两个视图：归一化四块（面板展示）+ raw（磁盘形状，编辑 path 按它拼）。
GET /soul/render 给"她此刻会被注入的样子"：改完下一回合生效的证据面，不重启。

本轮新增的四条都为了「回得去、拦得住」：
- GET /soul/pin：压缩前要 pin 的那一份（条目 id + 禁令首句，≤1200 字节）；
- GET /soul/revisions 与 POST /soul/rollback：整档归档的版本清单与回滚（history 解释、
  归档恢复，分工写进响应里的 note）；
- POST /soul/redline：红线逐条加（add_redline）/ 逐条撤（retire_redline），整块替换一律拒；
- POST /soul/acceptance：验收探针，一次调用写值再写回原值，两行都记 kind=probe。

探针不是第三条手：它只转调 store.acceptance()，而 edit 的 probe 形参在这层是**关掉**的
（传了就 400，PROBE_VIA_EDIT_FORBIDDEN）——探针不许绕过日限额，也不许碰红线。
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Mapping, Optional

from organs.brain.model import now_iso
from organs.soul import SoulLoadError, model
from organs.soul import pin as soul_pin
from organs.soul import store as soul_store

from .core import Result, _failed, _issues

SOUL_ROUTES = {
    "/soul": "GET",
    "/soul/check": "GET",
    "/soul/history": "GET",
    "/soul/pin": "GET",
    "/soul/render": "GET",
    "/soul/revisions": "GET",
    "/soul/edit": "POST",
    "/soul/migrate": "POST",
    "/soul/proposals": "GET",
    "/soul/proposal": "POST",
    "/soul/proposal/reject": "POST",
    "/soul/redline": "POST",
    "/soul/rollback": "POST",
    "/soul/acceptance": "POST",
}
EDIT_FIELDS = ("actor", "reason", "block", "path", "value")
MIGRATE_FIELDS = ("actor", "reason", "block", "value", "expect_old_digest")
PROPOSAL_FIELDS = ("actor", "proposal")
PROPOSAL_REJECT_FIELDS = ("actor", "proposal_id", "reason")
ROLLBACK_FIELDS = ("actor", "reason", "revision")
REDLINE_FIELDS = ("actor", "reason", "op")
ACCEPTANCE_FIELDS = ("actor", "reason", "block", "path", "value", "restore_value")


def dispatch(route: str, params: Mapping[str, str], body: Optional[Dict[str, Any]],
             store: Any, brain: Any = None) -> Result:
    if route == "/soul":
        return _soul(store)
    if route == "/soul/check":
        return 200, {"issues": _issues(store.check())}
    if route == "/soul/history":
        return _history(store)
    if route == "/soul/render":
        return _render(params, store)
    if route == "/soul/edit":
        return _edit(body, store)
    if route == "/soul/migrate":
        return _migrate(body, store)
    if route == "/soul/pin":
        return _pin(store)
    if route == "/soul/revisions":
        return _revisions(store)
    if route == "/soul/rollback":
        return _rollback(body, store)
    if route == "/soul/redline":
        return _redline(body, store)
    if route == "/soul/acceptance":
        return _acceptance(body, store)
    if route == "/soul/proposals":
        return _proposals_query(params, brain)
    if route == "/soul/proposal/reject":
        return _proposal_reject(body, brain)
    return _proposal(body, store, brain)


def _soul(store: Any) -> Result:
    """三块现状（三张卡 + 变更史）加 digest 与载入时的 Issue。

    三张卡给两份：归一化形状（展示用）与 raw（磁盘原样，POST /soul/edit 的 path 按它拼）。
    """
    try:
        snapshot = store.snapshot()
        history = store.history()
        revision = store.revision()
    except SoulLoadError as exc:
        return _failed(500, "魂载入失败，先修卡", exc.issues)
    return 200, {
        "identity": asdict(snapshot.identity),
        "manner": asdict(snapshot.manner),
        "bounds": asdict(snapshot.bounds),
        "raw": {block: store.raw(block) for block in model.BLOCK_NAMES},
        "history": history,
        "digest": snapshot.digest,
        "loaded_at": snapshot.loaded_at,
        # 活着这一版的号：与 GET /soul/revisions 的清单、POST /soul/rollback 的入参同一口径
        "revision": revision,
        "issues": _issues(snapshot.issues),
    }


def _history(store: Any) -> Result:
    try:
        return 200, {"history": store.history()}
    except SoulLoadError as exc:
        return _failed(500, "history 读不出来", exc.issues)


def _chars(snapshot: Any) -> Dict[str, Any]:
    """七件字数：limit 是这一件的线，hard 说明它是硬线还是建议值。

    parts 一律四个键 {"name", "chars", "limit", "hard"}，不再按硬/软换键名：
    硬线那件的 limit 就是硬线值，其余是建议值（超了只 WARN，不拦）。合计硬线另给 limit。
    """
    return {
        "used": model.body_chars(snapshot.identity, snapshot.manner, snapshot.bounds),
        "limit": model.limits.body_total,
        "parts": [{"name": name, "chars": chars, "limit": limit, "hard": hard}
                  for name, chars, limit, hard in model.body_parts(
                      snapshot.identity, snapshot.manner, snapshot.bounds)],
    }


def _render(query: Mapping[str, str], store: Any) -> Result:
    """她此刻会被注入的样子。

    audience 只为旧链接留着（面板不再有对象档）：矩阵归脑，三档文本现在完全一样。
    """
    audience = query.get("audience", "")
    try:
        snapshot = store.snapshot()
        text = store.render(audience)
    except SoulLoadError as exc:
        return _failed(500, "魂载入失败，先修卡", exc.issues)
    return 200, {"audience": audience, "text": text, "chars": _chars(snapshot)}


def _edit(body: Optional[Dict[str, Any]], store: Any) -> Result:
    if not isinstance(body, dict):
        return 400, {"ok": False, "message": "body 必须是 JSON 对象"}
    if "probe" in body:
        # 后门就堵在这里：probe 只有 POST /soul/acceptance 一个入口。带了这个键就是 400，
        # 不看它的值是 true 还是 "true"——探针没有「顺手从 edit 走一趟」这条路。
        return _failed(400, "probe 不许走 /soul/edit：验收探针只有 POST /soul/acceptance 一个入口",
                       [model.Issue(soul_store.PROBE_VIA_EDIT_FORBIDDEN, model.ERROR,
                                    "edit 不接受 probe（无论值是什么）：探针要写值再写回原值、"
                                    "每天 ≤3 对、且不许碰红线，这些都只有 acceptance 端点做得到")])
    missing = [field for field in EDIT_FIELDS if field not in body]
    if missing:
        return 400, {"ok": False, "message": "缺字段：%s" % "/".join(missing)}
    result = store.edit(
        actor=body["actor"],
        reason=body["reason"],
        block=body["block"],
        path=body["path"],
        value=body["value"],
        # 结构变更必须显式声明；只认 JSON true，字符串 "false" 不算点头
        structural=body.get("structural") is True,
    )
    return (200 if result.get("ok") else 400), result


def _migrate(body: Optional[Dict[str, Any]], store: Any) -> Result:
    """形状变化走这里：坏卡也能迁，但必须带当前块的 digest。"""
    if not isinstance(body, dict):
        return 400, {"ok": False, "message": "body 必须是 JSON 对象"}
    missing = [field for field in MIGRATE_FIELDS if field not in body]
    if missing:
        return 400, {"ok": False, "message": "缺字段：%s" % "/".join(missing)}
    result = soul_store.migrate(
        root=store.root,
        actor=body["actor"],
        reason=body["reason"],
        block=body["block"],
        new_value=body["value"],
        expect_old_digest=body["expect_old_digest"],
    )
    return (200 if result.get("ok") else 400), result


def _mark(brain: Any, proposal_id: str, status: str, actor: str, ts: str,
          reason: str = "") -> Dict[str, Any]:
    """把处理结果回写进脑的 proposals.json：回写失败如实返回，不许假装成功。

    reason 只对 rejected 有意义，落盘键是 decision_reason（她原本的 reason 不动）。
    """
    if brain is None:
        return {"ok": False, "message": "脑未载入：提案状态没处落（devserver --brain-root）"}
    try:
        return brain.mark_proposal(proposal_id, status, actor, ts, reason)
    except Exception as exc:  # noqa: BLE001 - 回写失败要原样报给面板
        return {"ok": False, "message": "提案状态回写失败：%s" % exc}


def _proposal_handled(brain: Any, proposal_id: str) -> Optional[Dict[str, Any]]:
    """这条提案在脑里是不是已经有终态了：重复点头时要看得见「已经处理过」。"""
    if brain is None or not proposal_id:
        return None
    try:
        rows = brain.proposals().get("proposals") or []
    except Exception:  # noqa: BLE001 - 读不到就不多说一句
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("id") == proposal_id \
                and row.get("status") in ("accepted", "rejected"):
            return {"status": row["status"], "actor": row.get("actor", ""),
                    "ts": row.get("ts", ""), "reason": row.get("reason", ""),
                    "decision_reason": row.get("decision_reason", "")}
    return None


def _proposal(body: Optional[Dict[str, Any]], store: Any, brain: Any = None) -> Result:
    """接受一份提案：校验、限额、写盘、追账全在 store 里，这里只搬字段 + 回写脑。

    魂的写入是既成事实：脑那边 id 对不上或写不进去，接受照样算数，但响应里必须带
    mark 的失败说明——不回写就等于面板永远显示「等她点头」。
    """
    if not isinstance(body, dict):
        return 400, {"ok": False, "message": "body 必须是 JSON 对象"}
    missing = [field for field in PROPOSAL_FIELDS if field not in body]
    if missing:
        return 400, {"ok": False, "message": "缺字段：%s" % "/".join(missing)}
    limit = body.get("monthly_limit", model.PROPOSAL_MONTHLY_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool):
        return 400, {"ok": False, "message": "monthly_limit 必须是整数"}
    proposal = body["proposal"]
    proposal_id = str(proposal.get("id") or "").strip() if isinstance(proposal, dict) else ""
    try:
        result = store.accept_proposal(actor=body["actor"], proposal=proposal,
                                       monthly_limit=limit)
    except SoulLoadError as exc:
        return _failed(400, "魂载入失败，先修卡", exc.issues)
    if not result.get("ok"):
        handled = _proposal_handled(brain, proposal_id)
        if handled:
            # 重复点头大多先撞演化五闸（同一格 7 天内动过 = GATE_COOLDOWN）：
            # 拒因照旧带 issues，另外点名「这条已经处理过」，别让人以为点了没反应。
            result["handled"] = handled
            result["message"] = "%s（这条提案已经处理过：%s）" % (
                result.get("message", ""), handled["status"])
        return 400, result
    ts = ((result.get("history_line") or {}).get("ts") or "")
    result["mark"] = _mark(brain, proposal_id, "accepted", body["actor"], ts, "")
    return 200, result


def _proposal_reject(body: Optional[Dict[str, Any]], brain: Any) -> Result:
    """拒绝一条提案：只落脑里的终态，不写魂、不产生 revision。"""
    if not isinstance(body, dict):
        return 400, {"ok": False, "message": "body 必须是 JSON 对象"}
    missing = [field for field in PROPOSAL_REJECT_FIELDS if field not in body]
    if missing:
        return 400, {"ok": False, "message": "缺字段：%s" % "/".join(missing)}
    actor = body["actor"] if isinstance(body["actor"], str) else ""
    if not actor.strip():
        return 400, {"ok": False, "message": "actor 不能为空：拒绝的是人的手，提案要记清是谁"}
    proposal_id = str(body["proposal_id"] or "").strip()
    if not proposal_id:
        return 400, {"ok": False, "message": "proposal_id 不能为空：要拒哪一条"}
    decision_reason = body["reason"] if isinstance(body["reason"], str) else ""
    if not decision_reason.strip():
        return 400, {"ok": False, "message": "reason 不能为空：拒绝要写清为什么"}
    if brain is None:
        return 503, {"ok": False, "message": "脑未载入：提案状态没处落（devserver --brain-root）"}
    # 人的理由落 decision_reason；条目里原有的 reason 是「她为什么提」，一个字不动。
    mark = _mark(brain, proposal_id, "rejected", actor.strip(), now_iso(),
                 decision_reason.strip())
    return (200 if mark.get("ok") else 400), mark


def _proposals_query(params: Mapping[str, str], brain: Any) -> Result:
    """提案清单：默认只看 pending（没有 status 或 status=='pending'），?status=all 看全部。"""
    want = (params.get("status") or "pending").strip() or "pending"
    if want not in ("pending", "all"):
        return 400, {"ok": False, "message": "status 只许 pending / all，实际 %r" % want}
    if brain is None:
        return 503, {"ok": False, "message": "脑未载入：提案在脑里（devserver --brain-root）"}
    try:
        rows = brain.proposals().get("proposals") or []
    except Exception as exc:  # noqa: BLE001 - 读不到要如实报，不许折叠成 0 条
        return 500, {"ok": False, "message": "提案读不出来：%s" % exc}
    rows = [row for row in rows if isinstance(row, dict)]
    if want == "pending":
        rows = [row for row in rows if row.get("status") in (None, "", "pending")]
    return 200, {"proposals": rows}


def _pin(store: Any) -> Result:
    """M4：压缩前要 pin 的那一份——每条红线「条目 id ｜ 禁令首句」，≤1200 字节。"""
    try:
        snapshot = store.snapshot()
        text = store.pin()
    except ValueError as exc:
        return 400, {"ok": False, "message": str(exc)}
    except SoulLoadError as exc:
        return _failed(500, "魂载入失败，先修卡", exc.issues)
    return 200, {
        "pin": text,
        "bytes": soul_pin.pin_bytes(text),
        "limit": soul_pin.PIN_LIMIT,
        "items": len([item for item in snapshot.bounds.items if item.active]),
        "note": "压缩产物首段必须逐字回填这一段；admit_compression 会核，不符即拒",
    }


def _revisions(store: Any) -> Result:
    """环形里还留着的整档版本（最新在前）。history 解释谁为什么改，这里回答回到哪一版。"""
    try:
        revisions = store.revisions()
        revision = store.revision()
    except SoulLoadError as exc:
        return _failed(500, "魂载入失败，先修卡", exc.issues)
    return 200, {
        "current_revision": revision,
        "revisions": revisions,
        "note": "history 解释谁为什么改；快照负责回到哪一版，两者分工不混"
                  "（POST /soul/rollback {revision, actor, reason}）",
    }

def _rollback_revision(value: Any) -> Optional[int]:
    """revision 认整数；面板输入框给的是字符串，数字串也收，别让人先自己转类型。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _rollback(body: Optional[Dict[str, Any]], store: Any) -> Result:
    """回到某一版：回滚本身也要记账、也要先存整档（都在 store.rollback 里）。"""
    if not isinstance(body, dict):
        return 400, {"ok": False, "message": "body 必须是 JSON 对象"}
    missing = [field for field in ROLLBACK_FIELDS if field not in body]
    if missing:
        return 400, {"ok": False, "message": "缺字段：%s" % "/".join(missing)}
    revision = _rollback_revision(body["revision"])
    if revision is None:
        return 400, {"ok": False, "message": "revision 必须是整数版本号"}
    result = store.rollback(actor=body["actor"], reason=body["reason"], revision=revision)
    return (200 if result.get("ok") else 400), result

def _redline(body: Optional[Dict[str, Any]], store: Any) -> Result:
    """红线只许逐条加、逐条撤：两条窄口，整块替换没有入口。

    op=add 走 store.add_redline、op=retire 走 store.retire_redline（动作名专用，旧的
    add_bounds_item / retire_bounds_item 已删）。两个 op 都不占那 2 次月限额，但都要 reason；
    同一条线撤了 7 天内不许加回（REDLINE_RERELEASE_COOLDOWN）。
    """
    if not isinstance(body, dict):
        return 400, {"ok": False, "message": "body 必须是 JSON 对象"}
    if "probe" in body:
        # 探针不许碰红线：这里不看值，带了这个键就拒。
        return _failed(400, "红线不许探：加线走 op=add、撤线走 op=retire",
                       [model.Issue(soul_store.PROBE_ON_BOUNDS_FORBIDDEN, model.ERROR,
                                    "红线没有 probe 路径：红线只许人逐条加（add_redline）/逐条撤"
                                    "（retire_redline），探针一律不许碰红线段")])
    missing = [field for field in REDLINE_FIELDS if field not in body]
    if missing:
        return 400, {"ok": False, "message": "缺字段：%s" % "/".join(missing)}
    op = body["op"]
    if op == "add":
        if "item" not in body:
            return 400, {"ok": False, "message": "op=add 要给 item（id/rule/match/action）"}
        result = store.add_redline(actor=body["actor"], reason=body["reason"],
                                   item=body["item"])
    elif op == "retire":
        if "id" not in body:
            return 400, {"ok": False, "message": "op=retire 要给 id（撤哪一条）"}
        result = store.retire_redline(actor=body["actor"], reason=body["reason"],
                                      item_id=body["id"])
    else:
        return 400, {
            "ok": False,
            "message": "op 只许 add / retire：红线块不许整块替换"
                       "（BOUNDS_BLOCK_REPLACE_FORBIDDEN）",
        }
    return (200 if result.get("ok") else 400), result


def _acceptance(body: Optional[Dict[str, Any]], store: Any) -> Result:
    """验收探针：一次调用写值、再写回原值，返回两条 probe 账行（细节在 store.acceptance）。

    body: {actor, reason, block, path, value, restore_value}。block="bounds" 一律拒。
    """
    if not isinstance(body, dict):
        return 400, {"ok": False, "message": "body 必须是 JSON 对象"}
    missing = [field for field in ACCEPTANCE_FIELDS if field not in body]
    if missing:
        return 400, {"ok": False, "message": "缺字段：%s" % "/".join(missing)}
    result = store.acceptance(actor=body["actor"], reason=body["reason"], block=body["block"],
                              path=body["path"], value=body["value"],
                              restore_value=body["restore_value"])
    return (200 if result.get("ok") else 400), result
