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
    "/soul/proposal": "POST",
    "/soul/redline": "POST",
    "/soul/rollback": "POST",
    "/soul/acceptance": "POST",
}
EDIT_FIELDS = ("actor", "reason", "block", "path", "value")
MIGRATE_FIELDS = ("actor", "reason", "block", "value", "expect_old_digest")
PROPOSAL_FIELDS = ("actor", "proposal")
ROLLBACK_FIELDS = ("actor", "reason", "revision")
REDLINE_FIELDS = ("actor", "reason", "op")
ACCEPTANCE_FIELDS = ("actor", "reason", "block", "path", "value", "restore_value")


def dispatch(route: str, params: Mapping[str, str], body: Optional[Dict[str, Any]],
             store: Any) -> Result:
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
    return _proposal(body, store)


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
    """六件字数：limit 是这一件的线，hard 说明它是硬线还是建议值。

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


def _proposal(body: Optional[Dict[str, Any]], store: Any) -> Result:
    """接受一份提案：校验、限额、写盘、追账全在 store 里，这里只搬字段。"""
    if not isinstance(body, dict):
        return 400, {"ok": False, "message": "body 必须是 JSON 对象"}
    missing = [field for field in PROPOSAL_FIELDS if field not in body]
    if missing:
        return 400, {"ok": False, "message": "缺字段：%s" % "/".join(missing)}
    limit = body.get("monthly_limit", model.PROPOSAL_MONTHLY_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool):
        return 400, {"ok": False, "message": "monthly_limit 必须是整数"}
    try:
        result = store.accept_proposal(actor=body["actor"], proposal=body["proposal"],
                                       monthly_limit=limit)
    except SoulLoadError as exc:
        return _failed(400, "魂载入失败，先修卡", exc.issues)
    return (200 if result.get("ok") else 400), result

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
