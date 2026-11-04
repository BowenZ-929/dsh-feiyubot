"""嘴的四个只读干跑端点：把三批的库（切分+节奏、称呼、语气档、闸）接到 HTTP 上。

四条全是 GET，而且**只干跑**：算一遍给人看，不发出去、不落盘。本文件没有任何写盘路径，
也没有 POST/PUT/DELETE 分支——非 GET 由 core.handle 统一回 405，措辞说明「嘴的干跑端点只读」。

/mouth/address 与 /mouth/tone 还要认一个新查询参数 rename=<新叫法>：这一轮本人要求换个叫法，
称呼**当场就换**（退到中性等于没听人话），返回里加 renamed 与 memory_draft 那一节——
memory_draft 就是 address.rename_memory 的产物，供出口投给脑（GET 自己不投、不写盘）。
**端点不 remember**：会话级的临时覆盖（SessionAddressBook）只在库层提供用法，这里一个都不调，
所以同一个请求两次得到的答案一样，也不会因为查过一次就改了下一次。没给 rename 时返回与从前一致。

干跑的意思是「入参就是全部输入」。心还没做，所以心的读数**只能从查询参数来**：
mood/energy/engage/offset 不给就是 None，返回里的 heart_source 会如实写「heart:未接入」。
这一条是「心还没做、接口留白」的对外证明，不是默认值。

数值一个都不在本文件：切分与节奏的阈值来自 organs/mouth/data/policy.json，
四档档界、称呼词、频次说明、语气档与心的折算来自 organs/mouth/data/address_table.json，
条款一条不进代码，全部来自 organs/mouth/data/clauses.json（三份都经 warehouse.load 读）。

本轮**没有重写器**：/mouth/gate 命中就弃（decision="dropped"），api 层不伪造一次改写，
也不把命中的句子当 pass 放出去。

弃言不再是「发了个空串就没了」：闸把「谁拦的、为什么」交给 api 注入的 sink，api 把它记进
organs/mouth/drops.py 的账本（时刻在这里给，写盘只在 drops 里），返回里固定带一节 drops。
**pass 的那次也带**——人要能看见「今天已经弃过几次」，而不是只有被弃那次才看得见；
文件不存在、坏行几行也都如实分开放（读不到 ≠ 0 次）。这一轮不加路由：弃言那一行就挂在
/mouth/gate 的返回里，外加那个 trace 文件。

未载入如实报：brain=None 时 /mouth/address 与 /mouth/tone 回 200 + status="未接入"，
store=None 时 /mouth/tone 也回未接入——不 500，也不假装有读数。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from organs.mouth import address, drops, gate, pace, tone, warehouse
from organs.mouth import PolicyMiss, WarehouseBroken, WarehouseMiss
from organs.mouth.split import split as split_text
from organs.soul import SoulLoadError

from .core import Result

MOUTH_ROUTES = {
    "/mouth/split": "GET",
    "/mouth/address": "GET",
    "/mouth/tone": "GET",
    "/mouth/gate": "GET",
}
AGENT = "mouth"
SEED_FALLBACK = "dry-run"   # 没给 seed 时的兜底种子：只为「同一请求两次拿到同一组间隔」，不是数值


def dispatch(route: str, params: Mapping[str, str], store: Any, brain: Any) -> Result:
    """嘴的四条只读端点；四条都不读 body（GET 没有 body），入参一律来自查询串。"""
    if route == "/mouth/split":
        return _split(params)
    if route == "/mouth/address":
        return _address(params, brain)
    if route == "/mouth/tone":
        return _tone(params, store, brain)
    return _gate(params)


# —— 四条端点 ————————————————————————————————————————

def _split(params: Mapping[str, str]) -> Result:
    """切分 + 节奏干跑：几泡、每泡隔多久、什么时候发。"""
    text = _text(params)
    if text is None:
        return _missing_text("/mouth/split")
    seed = (params.get("seed") or "").strip() or SEED_FALLBACK
    try:
        policy = warehouse.load("policy")
        batch = pace.fill(split_text(text, policy), policy, seed)
        visible = pace.visible_chars(text, policy)
    except (WarehouseMiss, WarehouseBroken, PolicyMiss) as exc:
        return _broken(exc)
    return 200, {
        "ok": True,
        "readonly": True,
        "agent": AGENT,
        "parts": [{"text": part.text, "gap_before": part.gap_before,
                   "send_at": part.send_at, "degraded": part.degraded,
                   "reason": part.reason} for part in batch.parts],
        "summary": batch.summary,
        "chars": len(text),
        "visible_chars": visible,
        "seed": seed,
        "note": "只干跑：不落盘、不发出去；间隔是按 seed 算出来的确定值，同一个 seed 两次一样",
    }


def _address(params: Mapping[str, str], brain: Any) -> Result:
    """一次称呼：人名录读数 → 关系档 → 一条叫法；查不到人就按生人档回退。

    给了 rename 就当场换（并在返回里带一份投给脑的记忆草稿）；**这里不 remember**——
    查一次不改下一次，会话覆盖只在库层提供用法。
    """
    who, reading, failed = _readings(params)
    if failed is not None:
        return failed
    person, note, blocked = _person(who, reading, brain, "/mouth/address")
    if blocked is not None:
        return blocked
    rename = _rename(params)
    try:
        table = address.load_table()
        called = address.resolve(_asked(person, rename, who), table)
    except (WarehouseMiss, WarehouseBroken, PolicyMiss) as exc:
        return _broken(exc)
    payload = {"ok": True, "readonly": True, "agent": AGENT}
    payload.update(_addressee(called, note))
    payload.update(_renamed_draft(called, who, table))
    return 200, payload


def _tone(params: Mapping[str, str], store: Any, brain: Any) -> Result:
    """称呼 + 语气档干跑：魂的姿态是底色，关系档定档位，心的读数只从查询参数来。

    rename 走的是同一条路：称呼当场换，语气口径行里「对谁」跟着换，档位不动；
    换了就把记忆草稿一并交出去，这里不投、不写盘、不 remember。
    """
    if store is None:
        return _unconnected("/mouth/tone", "魂未载入（store=None），读不到姿态与自称")
    if brain is None:
        return _unconnected("/mouth/tone", "脑未载入（brain=None），读不到人名录")
    who, reading, failed = _readings(params)
    if failed is not None:
        return failed
    person, note, blocked = _person(who, reading, brain, "/mouth/tone")
    if blocked is not None:
        return blocked
    try:
        heart = _heart(params)
    except ValueError as exc:
        return 400, {"ok": False, "readonly": True, "agent": AGENT,
                     "message": "心的读数要给数：%s" % exc}
    rename = _rename(params)
    try:
        table = address.load_table()
        called = address.resolve(_asked(person, rename, who), table)
        voice = tone.from_store(store, called, heart=heart, table=table)
    except (WarehouseMiss, WarehouseBroken, PolicyMiss) as exc:
        return _broken(exc)
    except SoulLoadError as exc:
        return 500, {"ok": False, "readonly": True, "agent": AGENT,
                     "message": "魂载入失败，先修卡：%s" % exc}
    payload = {
        "ok": True,
        "readonly": True,
        "agent": AGENT,
        "addressee": _addressee(called, note),
        "tone": {"tier": voice.tier, "label": voice.label, "line": voice.line,
                 "stance": voice.stance, "self_name": voice.self_name,
                 "heart_source": voice.heart_source, "softness": voice.softness,
                 "ban_joke": voice.ban_joke},
        "heart": heart,
        "note": "心的读数只从查询参数来：没给就是 None（heart_source 写「未接入」）；"
                "给了哪几项就只带哪几项，不补 0",
    }
    payload.update(_renamed_draft(called, who, table))
    return 200, payload


def _gate(params: Mapping[str, str]) -> Result:
    """过闸干跑：命中返回那条条款与一句人话原因；这一轮没有重写器，命中就弃。

    弃掉的那一句会留在账本里：闸只叫注入的 sink 报「谁拦的、为什么」，时刻与写盘都在这一层
    （核里不取时钟）。所以返回里固定带一节 drops，**pass 的那次也带**——今天已经弃过几次、
    最近一次命中哪条、账本在不在、有没有读不出来的坏行，四件事分开报。
    账本写不进去就照实回 500（记账失败也是失败，不许当成「今天 0 次」）。
    """
    text = _text(params)
    if text is None:
        return _missing_text("/mouth/gate")
    trace = drops.path_of()
    today = _day()
    try:
        clauses = warehouse.load("clauses").get("clauses")
        verdict = gate.enforce(text, clauses, None, _sink(trace))   # rewrite=None：这一轮没有重写器
        ledger = drops.report(trace, today)
    except (WarehouseMiss, WarehouseBroken, gate.ClauseBroken, OSError) as exc:
        return _broken(exc)
    hit = verdict.hits[0] if verdict.hits else None
    return 200, {
        "ok": True,
        "readonly": True,
        "agent": AGENT,
        "hit": {"clause_id": hit.clause_id, "kind": hit.kind,
                "reason": hit.reason, "matched": hit.matched} if hit else None,
        "decision": verdict.decision,
        "reason": verdict.reason,
        "text": verdict.text,
        "rewriter": False,
        "disabled_ids": verdict.disabled,
        "drops": ledger,
        "note": "这一轮没有重写器：命中就弃（decision=dropped），api 层不代写一次；"
                "弃掉的 text 是空串，不会被当成 pass 发出去。drops 这一节是弃言账本的读数："
                "today 是今天弃了几次（pass 的那次也照报），exists=false 表示账本还没建、"
                "errors 是读不出来的坏行数；disabled_ids 是这次被停用、没参与匹配的条款。",
    }


def _sink(trace: Any) -> Callable[[str, str], None]:
    """把闸的弃言接到账本上：闸只报「哪条条款、哪句原因」，时刻与落盘都在 api 层给。"""
    def note(clause_id: str, reason: str) -> None:
        drops.record(trace, clause_id, reason, datetime.now())
    return note


def _day() -> str:
    """今天的日期（ISO，十位）：账本按这个前缀认「今天」，不收时区。"""
    return datetime.now().date().isoformat()


# —— 入参 ——————————————————————————————————————————

def _text(params: Mapping[str, str]) -> Optional[str]:
    """查询串里的原文：没给、或全是空白，都算没给（返回 None，调用方回 400）。"""
    raw = params.get("text")
    if raw is None:
        return None
    body = raw if isinstance(raw, str) else str(raw)
    return body if body.strip() else None


def _missing_text(route: str) -> Result:
    """缺原文：400，说清要给 text（不拿空串往下算，也不给默认话）。"""
    return 400, {"ok": False, "readonly": True, "agent": AGENT,
                 "message": "%s 要给 text（原文）：缺了或全是空白，等于没话可以干跑" % route}


def _readings(params: Mapping[str, str]) -> Tuple[str, Optional[float], Optional[Result]]:
    """who 与 familiarity：who 不带空白，familiarity 读不出数就当场 400。"""
    who = (params.get("who") or "").strip()
    try:
        reading = _number(params.get("familiarity"))
    except ValueError:
        return who, None, (400, {"ok": False, "readonly": True, "agent": AGENT,
                                 "message": "familiarity 要给一个数（人名录的熟稔读数），"
                                            "给了 %r 读不出数" % params.get("familiarity")})
    return who, reading, None


def _rename(params: Mapping[str, str]) -> str:
    """查询串里的 rename（本人这一轮要求的新叫法）：没给、或全是空白，都算没要求。"""
    return (params.get("rename") or "").strip()


def _asked(person: Optional[Dict[str, Any]], rename: str, who: str) -> Optional[Dict[str, Any]]:
    """把这一轮本人要求的叫法塞进人名录读数的一份**副本**里：不改名录、不落盘。

    人名录取不到这个人时也给他一份只有 id 与 rename_request 的读数——改名是他说的，
    不因为他没进过名录就不算数（档位照旧按读数 0 走生人档）。没给 rename 时原样返回，
    这样「不给 rename 的返回」与从前逐字一致。
    """
    if not rename:
        return person
    reading: Dict[str, Any] = dict(person) if isinstance(person, Mapping) else {}
    if who and not reading.get("id"):
        reading["id"] = who
    reading["rename_request"] = rename
    return reading


def _number(raw: Any) -> Optional[float]:
    """查询串里的一个数：没给或空串就是 None（没读数）；给了但不是数就抛 ValueError。"""
    if raw is None or not str(raw).strip():
        return None
    return float(str(raw).strip())


def _heart(params: Mapping[str, str]) -> Optional[Dict[str, Any]]:
    """心的四样读数：**只从查询参数来**。

    四项全没给就是 None（心未接入，tone.render 会写「heart:未接入」）；给了哪几项就只带哪几项，
    其余保持 None——不补 0，也不替心猜一个读数。offset 是那份折算，这里按 softness 一项接。
    """
    mood = _number(params.get("mood"))
    energy = _number(params.get("energy"))
    engage = _number(params.get("engage"))
    offset = _number(params.get("offset"))
    if mood is None and energy is None and engage is None and offset is None:
        return None
    return {"mood": mood, "energy": energy, "engage": engage,
            "offset": None if offset is None else {"softness": offset}}


def _person(who: str, reading: Optional[float], brain: Any,
            route: str) -> Tuple[Optional[Dict[str, Any]], str, Optional[Result]]:
    """人名录读数：有 who 就去脑那边查一次。

    查不到人不是错、更不是 0：那是「没有这个人」，按生人档回退，并在 note 里说一句实话
    （与「读不到库」分开——读不到库是 warehouse 那边抛错，走 _broken）。
    """
    if not who:
        if reading is not None:
            return ({"id": "", "display": "", "familiarity": reading},
                    "没给 who：这是只有读数、没有人的干跑", None)
        return None, "没给 who：没有记录，按生人档回退", None
    if brain is None:
        return None, "", _unconnected(route, "脑未载入（brain=None），人名录读不到")
    found = brain.lookup(who)
    if found is None:
        return None, "人名录取不到这个人，按生人档回退（没有这个人不等于读数是 0，" \
                     "familiarity 也不参与：没有这个人，读数无从谈起）", None
    if reading is not None:
        return (dict(found, familiarity=reading),
                "familiarity 按查询参数覆盖这一次（不写回人名录）", None)
    return found, "", None


# —— 出参 ——————————————————————————————————————————

def _addressee(called: Any, note: str) -> Dict[str, Any]:
    """一次称呼的十项；address 那边自己的那句实话（彩蛋改口／当场换／红线退词）与这里的实话并成一条 note。"""
    return {
        "who": called.who,
        "display": called.display,
        "tier": called.tier,
        "relation": called.relation,
        "address": called.address,
        "frequency": called.frequency,
        "banned_self": called.banned_self,
        "note": "；".join(part for part in (called.note, note) if part),
        "renamed": called.renamed,
        "source": called.source,
    }


def _renamed_draft(called: Any, who: str, table: dict) -> Dict[str, Any]:
    """当场换了称呼时，把那条要投给脑的记忆草稿一并交出去（端点自己不投、不落盘）。

    没换（没给 rename、或换过去被自称红线拦下）就是空的一节：宁可什么都不加，
    也不给一份「看起来换成了」的草稿让人误投。who 取名录里的 id，名录没这个人就用查询串的 who。
    """
    if not called.renamed:
        return {}
    return {"renamed": True,
            "memory_draft": address.rename_memory(called.who or who, called.address, table=table)}


def _unconnected(route: str, why: str) -> Result:
    """未接入：200 + status="未接入" + readonly。不 500，也不给一个看起来像读数的 0。"""
    return 200, {"ok": True, "status": "未接入", "readonly": True, "agent": AGENT,
                 "route": route, "message": "%s：%s，只能如实报「未接入」" % (route, why)}


def _broken(exc: Exception) -> Result:
    """数据读不到／读不动：如实报 500，把仓库或政策那句原话（带路径）原样带出去。"""
    return 500, {"ok": False, "readonly": True, "agent": AGENT, "message": str(exc)}
