"""嘴的四个只读干跑端点：把这几批的库（切分+节奏、称呼、语气档、闸、拒绝、长度）接到 HTTP 上。

四条全是 GET，而且**只干跑**：算一遍给人看，不发出去。没有 POST/PUT/DELETE 分支——
非 GET 由 core.handle 统一回 405，措辞说明「嘴的干跑端点只读」。

**干跑只记两本账**（写在明处，别让下一个人以为 GET 一个字节都不写）：弃言与长度两本账的写盘
全在 organs/mouth 的 record 里（drops.record / style.record），本文件只是那股「时刻 + 路径」的
供应方——路径口径几处一致（MOUTH_TRACE_DIR > 包内 trace/），时刻一律取 datetime.now()（库层一个
都不取时钟）。本文件自己不碰文件：没有 open 调用、没有 mkdir、没有 JSON 写盘，所以账本写不进去
时的 500 一定是那两处 record 抛上来的。

**拒绝这一路一个字节都不写**（本轮改的口径，写在这里因为它曾经是个反例）：/mouth/gate 判出拒绝
（硬拒绝或软转向）时**只判不投**——不调 heart.refuse.ingest，所以心的 refusals.json
（HEART_ROOT > <仓库>/home/organs/heart，心自己的口径，不认 MOUTH_TRACE_DIR）一个字节都不动；
也不调 refusal.record，我们自己的 refuse.jsonl 同样不动。返回里把两件事分开写：verdict 说这次判成
了什么，delivered 恒为「没投」，另给一个 would_deliver（真投的话投不投得进去）。算 would_deliver
只走 heart.refuse.check（只读），所以算得出、写不进。**投递点只有一个**：真实对话 /chat
（api/chat.py）在 conversation.answer() 之后拿她那句与对方那句判一次，判出来才写心、才记拒绝账。

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

/mouth/gate 后来又扩了**可选**参数（inject / refuse / tier / hard_cap，第三轮 C 再加
asked / answer），**从头到尾没有新端点**：一个都不给就与从前逐字一致（只多一节 audit）。
判的顺序钉死并写进 _gate 的 docstring：**拒绝（含软转向）→ 闸（条款 → 动作括注 → 复述审计）→
目标长度**——先决定说不说、再决定这么说行不行、最后才管长短。各节各自交代：audit 恒在
（status 为 audit-unavailable 时原样透出，不许显示成通过）、verdict 只在给了 refuse 或 asked 时
有、delivered 恒在（干跑一律没投）、style 只在给了 tier 时有（这一轮没有重写器，超目标只能如实报
「改不了、放行并记账」，rewrites 恒为 0）。

弃言不再是「发了个空串就没了」：闸把「谁拦的、为什么」交给 api 注入的 sink，api 把它记进
organs/mouth/drops.py 的账本（时刻在这里给，写盘只在 drops 里），返回里固定带一节 drops。
**pass 的那次也带**——人要能看见「今天已经弃过几次」，而不是只有被弃那次才看得见；
文件不存在、坏行几行也都如实分开放（读不到 ≠ 0 次）。这一轮不加路由：弃言那一行就挂在
/mouth/gate 的返回里，外加那个 trace 文件。

**嘴这一轮把「不肯」接上（第三轮 C 判，第五轮把投递搬去真实对话；仍没有新端点）**：
/mouth/gate 多认两个可选参数 asked（对方那句问话）与 answer（她说的话，缺省用 text）。给了 asked
就先 refusal.soft_scan 判一次「她是不是把话转开了」：判成软转向 → **她的原话一个字都不改**，闸照跑
但只报不改（读数在 gate_readonly 里）；判不了（soft-unavailable）就如实写在 verdict.soft 那一节，
**不当成「不是拒绝」**。给了 refuse（hard 三选一）走既有的 refusal.enforce。**判出来只判不投**：
verdict 说判成了什么（kind hard|soft、shape、判定理由、软转向三要素、命中条款），delivered 恒为
没投，另给 would_deliver（认得出话题名、今天还够额度）——那一笔真投在 /chat 里，这一层不写心、
不写拒绝账。话题名只从心那份共用词表里认（refuse.vocabulary），认不出就 would_deliver=false 并说
为什么：心不收词表以外的话题，这一层也不替它造分类。

动作括注这一关也接上了：/mouth/gate 读 organs/mouth/data/action_note.json 传进 gate.enforce(action=)，
返回里固定带一节 action={checked, where, env_word, reason}——checked=false 是「这一关没跑」，
不是「查过没问题」。/mouth/gate 的 verdict 那一节同时给 shape 与 kind（hard|soft）：软的硬的分得开。

未载入如实报：brain=None 时 /mouth/address 与 /mouth/tone 回 200 + status="未接入"，
store=None 时 /mouth/tone 也回未接入——不 500，也不假装有读数。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from organs.heart import refuse as heart_refuse
from organs.mouth import action_note, address, audit, drops, gate, pace, refusal, style, tone, warehouse
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

INJECT_HINT = "inject 要一个 JSON 数组（URL 编码），形如 [{\"kind\": \"魂\", \"text\": \"…\"}]"

# 干跑那一路的 delivered.reason：写死一句人话。这一句是「GET 一定没投」的对外证明，
# 不是给人猜的变量——真投的那一句在 /chat 里（api/chat.py 是唯一的投递点）。
DRY_RUN_REASON = "干跑只判不投；投递发生在真实对话里"

# /mouth/gate 的那句 note：第一段与从前逐字相同（不给新参数时返回照旧），第二段只在给了新参数时追加。
GATE_NOTE = ("这一轮没有重写器：命中就弃（decision=dropped），api 层不代写一次；"
             "弃掉的 text 是空串，不会被当成 pass 发出去。drops 这一节是弃言账本的读数："
             "today 是今天弃了几次（pass 的那次也照报），exists=false 表示账本还没建、"
             "errors 是读不出来的坏行数；disabled_ids 是这次被停用、没参与匹配的条款。")
GATE_NOTE_MORE = ("另外几节按「拒绝 → 闸（含照抄审计）→ 目标长度」的顺序判：audit 是照抄审计的读数"
                  "（status 不是 ok 就是没真的比过，audit-unavailable 不许当成通过）；verdict 只在"
                  "给了 refuse 或 asked 时有，说的是这次判成了什么拒绝；delivered 恒在，说的是有没有"
                  "真投出去（这一路恒为没投）；style 只在给了 tier 时有，这一轮没有重写器，超目标只能"
                  "如实报改不了。干跑只记两本账（drops / style），都吃 MOUTH_TRACE_DIR；拒绝的账等"
                  "真实投递（/chat）时再记。")
GATE_NOTE_HEART = ("这一轮把「不肯」拆成两件事：verdict 是判了什么——给了 asked/refuse 就先判「她是不是"
                   "把话转开了」，判成软转向（soft）时她的原话一个字都不改，闸照跑但只报不改（读数在"
                   "gate_readonly 里），kind 是 hard / soft，soft.status 不是 ok 就是没真的比过"
                   "（soft-unavailable≠不是拒绝）；delivered 是投没投——GET 一律不投"
                   "（delivered=false，reason 写死那句干跑人话），would_deliver 按真实投递的判法算"
                   "（只认心那份词表里的话题名、今天还够额度），why 说清这一次为什么算成这样。"
                   "action 那一节恒在：checked=false 是这一关没跑，不是查过没问题。")


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
    """过闸干跑：拒绝（含软转向）→ 闸（条款 → 动作括注 → 照抄审计）→ 目标长度；命中就弃，账照记。

    **顺序钉死**：先决定说不说（refuse / 软转向）、再决定这么说行不行（闸 + 动作括注 + 照抄审计）、
    最后才管长短（style）。拒绝排在闸前面，是因为「这句要不要说」与「这样说合不合格」是两件事；
    长度垫在最后，是因为一句被弃掉的话再短也没意义。

    六个新参数都**可选**，一个都不给时返回与从前逐字一致（只多一节 audit）：

    - inject：URL 编码的 JSON 数组 [{"kind": "魂", "text": "…"}]，交给照抄审计去比。
      解析失败不是 500、也不静默当成「没给」：400 说清「inject 要一个 JSON 数组」；
      数组里某一项的 kind 不认识由 audit 抛 AuditBroken，400 把库的报错原文带出来
      （未知 kind 不许当成设定类静默从严）。
    - refuse：topic / mood / nobody（数据里那三种形状；完整名 refused-topic 也认）。给了就走
      拒绝这条路，说法取自 organs/mouth/data/refusal.json，**这句同样过闸**：命中即弃（弃了
      text 是空串，照旧进弃言账本）。拒绝不要求 text——拒了就没有原文可言，那时 text 一概不看。
    - asked：对方那句问话；answer：她说的话（缺省用 text）。给了 asked 就先 refusal.soft_scan 判一次
      软转向。判成 soft 时**她的原话一个字都不改**：闸照跑但只报不改（读数在 gate_readonly），
      verdict.text 与返回的 text 都是那句原话；判出来也**只判不投**（不写心、不写拒绝账）。
      判不了（asked 给了却是空白）时 verdict.soft.status 是 soft-unavailable，**这不是「不是拒绝」**。
    - tier：1..4，嘴的目标档位（与 /mouth/tone 的 tone.tier 同一套档）；hard_cap：核给的字数上限。
      两个可以只给一个：只给 tier 就是「核没给上限」，上限取这句自己的字数（于是 over_hard_cap
      恒为 false——不拿嘴的目标区间冒充核的上限）；**只给 hard_cap 不给 tier 是 400**：
      没档位就没有目标区间，别猜。

    各节各自交代：

    - audit **恒在**：status 为 "audit-unavailable" 时原样透出，**不许显示成通过**——没拿到
      注入原文就是没比过，「没比过」与「比过了、没在复述」是两件事，混一起就等于替她背了一次
      没人做过的检查。
    - verdict 只在给了 refuse 或 asked 时有：{kind, shape, full_name, reason, text, event,
      gate_decision, hit, soft, refused, fatal_status}。这一节只说**判了什么**——kind 是形状大类
      （hard|soft），shape 是那一节的名字（topic / soft…），reason 是按哪条判据判的，hit 是闸在
      这一句上命中的条款，soft 是软转向
      那四样 {is_soft, status, opener, redirect, question, reason}（没给 asked 就是 null）。
      refused 是「她拒了、这句最后发出去没有」；**fatal_status 是红线那一关判成什么**
      （ok / not-checked / fatal，原样取 RefusalVerdict，本层不另判）——红线命中时 refused=false 而
      fatal_status="fatal"，说的是「这句不是拒绝，是不许发」，而 gate_decision 仍是闸的判词
      （dropped）：三个读数各占一格，谁也不顶替谁。没判成拒绝
      （她正面答了、只是没转开、或者判不了）时 kind/shape/full_name 都是 null、event 是 null：
      这时 verdict 说的是「判过了、不是拒绝」，理由取软转向那句人话。status 不是 "ok" 就是没真的
      比过，**不许当成「不是拒绝」**。
    - delivered **恒在**：{delivered, would_deliver, reason, why}。delivered 恒为 false——干跑只判
      不投，投递只在真实对话 /chat 里；reason 是写死的那句人话（DRY_RUN_REASON）；would_deliver
      按真实投递的判法算（只认心那份共用词表里的话题名 refuse.vocabulary、今天还够额度
      heart.refuse.check），但它只读不算写：心那边的 refusals.json 与我们的 refuse.jsonl 一个字节
      都不动；why 说清这一次为什么算成 true/false（认不出话题 / 额度用完 / 真投会落盘）。
    - action 在这段字**像动作括注**时才有（数据里的括号、身体部位词命中一个）：{checked, where,
      env_word, reason}。只在这时把判据表交给闸，是为了不给别的调用改口径——没有括注的样子时，
      闸的 reason 与从前逐字一致。checked=false 表示这一关没跑（判据表没给），不是「查过没问题」；
      判据表与那两个词全来自 organs/mouth/data/action_note.json。
    - gate_readonly 只在软转向时有：{decision, hits, reason}——闸照跑、照判，只是不许改她的话。
    - style 只在给了 tier 时有：{decision, target_lo, target_hi, chars, rewrites, over_hard_cap,
      reason}。**这一轮没有重写器**（rewrite=None），所以超目标只能如实报「改不了、放行并记账」，
      api 层不伪造一次改写（rewrites 恒为 0）；超硬上限也不截断，只把 over_hard_cap 举起来交给核。

    弃言那一节 drops 与从前一样，**pass 的那次也带**（今天弃过几次、最近一次命中哪条、账本在不在、
    有没有读不出来的坏行，四件事分开报）。

    **干跑不写心、不写拒绝账**，这是本轮钉死的口径：判出拒绝（硬或软）只判不投，heart.refuse.ingest
    与 refusal.record 在 GET 这一路一次都不调；我们自己的 refuse.jsonl 与心那边的 refusals.json 都
    等真实投递（api/chat.py）时再写。**只写 drop 与 style 两本账**（drops 走闸的 sink、style 走
    style.record），都吃 MOUTH_TRACE_DIR；写不进去照实回 500——记账失败也是失败，不许当成「今天 0 次」。
    drops 保留是有意的：它是闸的可见性，与「拒绝投递」是两件事。

    长度判的是**最后要发出去的那句**：闸弃掉时它已经是空串，长度那一节就按空串判（chars=0、
    decision="pass" 是「空串没有超目标」的意思，不是「这句合格」）——长度这一层不替闸改口径。
    """
    refuse = (params.get("refuse") or "").strip()
    text = _text(params)
    asked = _asked_text(params)
    answer = _answer_text(params, text)
    if text is None and not refuse and answer is None:
        return _missing_text("/mouth/gate")
    try:
        inject = _inject(params)
    except ValueError as exc:
        return _bad(str(exc))
    tier = (params.get("tier") or "").strip()
    try:
        hard_cap = _whole(params.get("hard_cap"), "hard_cap")
    except ValueError as exc:
        return _bad(str(exc))
    if hard_cap is not None and not tier:
        return _bad("hard_cap 给了却没给 tier：没档位就没有目标区间，给 hard_cap 必须连 tier 一起给"
                    "（两个都给，或者只给 tier）——档位不许猜")
    trace = drops.path_of()
    today = _day()
    now = datetime.now()
    soft = None
    refused = None
    source = None
    try:
        clauses = warehouse.load("clauses").get("clauses")
        book = warehouse.load("refusal")
        marks = warehouse.load("action_note")
        fatal = warehouse.load("fatal")           # 红线表：干跑也按同一份判据判（红线命中就 dropped）
        if asked is not None:
            soft = refusal.soft_scan(answer, asked, table=book)
        source = asked if (asked or "").strip() else answer

        if soft is not None and soft.is_soft:
            # 软转向：她自己的那句原话照发，这里只负责认出来、装事件（sink 是 None：干跑不投）
            refused = _soft_refusal(book, clauses, asked, answer, now)
            spoken = refused.text
        elif refuse:
            # 这一遍只取说法、事件与那句人话：不给 drop_sink，所以不记账（记账归下面那一遍闸）
            refused = refusal.enforce(refuse, table=book, clauses=clauses, rewrite=None,
                                      refuse_sink=None, drop_sink=None, now=now,
                                      asked=asked, answer=answer, fatal=fatal)
            spoken = refusal.say(refuse, table=book)
        else:
            spoken = answer
        notebook = marks if _looks_noted(spoken, marks) else None
        if refused is not None and refused.kind == refusal.KIND_SOFT:
            # 闸照跑，但只报不改：不给 sink、也不拿它的 text，弃言账本因此不添行
            gated = gate.enforce(spoken, clauses, None, None, inject=inject, action=notebook,
                                 fatal=fatal)
        else:
            # 唯一一遍闸：条款 + 动作括注 + 照抄审计都在这，弃言也只在这里记一次
            gated = gate.enforce(spoken, clauses, None, _sink(trace), inject=inject,
                                 action=notebook, fatal=fatal)
        # 干跑到此为止：拒绝只判不投——不调 heart.refuse.ingest，也不调 refusal.record，
        # 心的 refusals.json 与我们自己的 refuse.jsonl 因此一个字节都不动。
        delivered = _delivered(source, refused)
        ledger = drops.report(trace, today)
        noted = action_note.check(spoken, marks) if notebook is not None else None
        cleared = action_note.clear_reason(marks) if notebook is not None else ""
    except (audit.AuditBroken, refusal.RefusalBroken, action_note.ActionBroken) as exc:
        return _bad(str(exc))                 # 注入段、拒绝形状、判据表坏掉是调用方的事：400
    except (WarehouseMiss, WarehouseBroken, gate.ClauseBroken, OSError) as exc:
        return _broken(exc)
    except ValueError as exc:
        return _broken(exc)                   # 判话题时的 ValueError 是我们这边的错，不许咽
    soft_here = refused is not None and refused.kind == refusal.KIND_SOFT
    final = refused.text if soft_here else gated.text
    counted = None
    if tier:
        try:
            target = warehouse.load("style_target")
            lo, hi = style.target_range(tier, target)
            counted = style.apply(final, tier,
                                  len(final) if hard_cap is None else hard_cap,
                                  table=target, rewrite=None, sink=_style_sink(lo, hi))
        except style.StyleBroken as exc:
            return _bad(str(exc))             # 档位不认识、上限不是数：同样是调用方的事
        except (WarehouseMiss, WarehouseBroken, OSError) as exc:
            return _broken(exc)
    hit = gated.hits[0] if gated.hits else None
    clause = {"clause_id": hit.clause_id, "kind": hit.kind,
              "reason": hit.reason, "matched": hit.matched} if hit else None
    payload = {
        "ok": True,
        "readonly": True,
        "agent": AGENT,
        "hit": None if soft_here else clause,
        "decision": refused.gate_decision if soft_here else gated.decision,
        "reason": refused.reason if soft_here else gated.reason,
        "text": final,
        "rewriter": False,
        "disabled_ids": gated.disabled,
        "drops": ledger,
        "delivered": delivered,
        "note": GATE_NOTE,
        "audit": _audit_section(gated.audit),
    }
    if notebook is not None:
        payload["action"] = _action_section(noted, gated.action_status, cleared)
    if soft_here:
        payload["gate_readonly"] = _readonly_section(gated)
    if refused is not None or soft is not None:
        payload["verdict"] = _verdict_section(refused, gated, soft, final, clause)
    if counted is not None:
        payload["style"] = _style_section(counted)
    if inject is not None or refuse or tier or asked is not None:
        payload["note"] = GATE_NOTE + GATE_NOTE_MORE + (
            GATE_NOTE_HEART if (refused is not None or soft is not None) else "")
    return 200, payload


def _sink(trace: Any) -> Callable[[str, str], None]:
    """把闸的弃言接到账本上：闸只报「哪条条款、哪句原因」，时刻与落盘都在 api 层给。"""
    def note(clause_id: str, reason: str) -> None:
        drops.record(trace, clause_id, reason, datetime.now())
    return note


def _style_sink(lo: int, hi: int) -> Callable[[str, str], None]:
    """把长度这一层的记账接到 style.jsonl 上：判词与那句人话由库给，时刻、路径、目标区间在这一层。

    只在「记账」那几次被叫一遍（超目标、改不了、超硬上限）；pass 与 rewritten 不叫——
    没超的事不必进账本。落进账本的那一列 target（"lo-hi"）是这一档的目标区间：区间收窄过一次，
    没有这一列，回头看见一行 over_target 就分不清是拿旧区间还是新区间判的。
    跟弃言账本同一个口径：写不进去照实抛，由 _gate 回 500。
    """
    trace = style.path_of()

    def note(decision: str, reason: str) -> None:
        style.record(trace, decision, reason, datetime.now(), target=(lo, hi))
    return note


def _looks_noted(text: Any, marks: Mapping[str, Any]) -> bool:
    """这段字里有没有「动作括注」的样子：数据里的某个括号，或某个身体部位词。

    判据表只在真有可能命中时才交给闸，是为了不给别的调用改口径：没有动作括注的样子时，闸的
    reason 与从前逐字一致（不会被多缀一句「这一关查过了」）。这个前置检查不会漏判——两条路
    要么认括号（括号内从紧），要么要身体部位词（括号外从松），两样都没有，命中就无从谈起。
    括号表与身体部位词表都来自 data/action_note.json，本文件不留一个字。
    """
    body = text if isinstance(text, str) else ""
    for pair in marks.get("brackets") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            if any(isinstance(side, str) and side and side in body for side in pair):
                return True
    for word in marks.get("body_parts") or []:
        if isinstance(word, str) and word and word in body:
            return True
    return False


def _soft_refusal(book: Mapping[str, Any], clauses: Any, asked: Optional[str],
                  answer: Optional[str], now: Any, sink: Any = None) -> Any:
    """软转向走 refusal.enforce 那条路取事件与那句人话：形状取自数据 soft.event_shape。

    不在这层自己装事件，是因为台词、判据、事件形状与 why 全在 data/refusal.json 里；api 只负责
    把 asked / answer 交进去。它给回来的 text 就是她自己的那句原话，本文件一个字节都不改。
    数据里那个形状要是认不得，refusal.enforce 会当场抛 RefusalBroken（400），不猜。
    """
    return refusal.enforce(refusal.soft_shape(book), table=book, clauses=clauses, rewrite=None,
                           refuse_sink=sink, drop_sink=None, now=now,
                           asked=asked, answer=answer, fatal=warehouse.load("fatal"))


def _delivered(source: Any, refused: Any) -> Dict[str, Any]:
    """这一路真投了没有：GET 恒为没投，另算一遍「真投的话投不投得进去」（would_deliver）。

    判法与真实投递一模一样（/chat 那一笔先认话题、再看今天还剩不剩额度），但这里只走
    heart.refuse.check（只读）：**心那边的 refusals.json 一个字节都不写**，我们自己的
    refuse.jsonl 也不写——拒绝的账等真实投递时再记。reason 是那句固定的干跑人话，why 是这一次
    would_deliver 算成这样的理由（没判出拒绝 / 认不出话题 / 额度用完 / 真投会落盘）。

    话是这么来的：would_deliver 必须**看起来跟真投一样**才有参考价值，可它一旦真投，GET 就有了
    写盘的副作用——所以算与投在这一层被拆成两件事，算的这半边只读。
    """
    if refused is None:
        return {"delivered": False, "would_deliver": False, "reason": DRY_RUN_REASON,
                "why": "这次没判出拒绝：没有要投的事件"}
    topic = refusal.topic_in(source, heart_refuse.vocabulary())
    if topic is None:
        return {"delivered": False, "would_deliver": False, "reason": DRY_RUN_REASON,
                "why": "认不出拒的是哪个话题：对方那句问话里没有心认可的话题名"
                        "（词表见 organs/brain/topics.json），真投也投不进去"}
    quota = heart_refuse.check([topic]).get("quota") or {}
    left = int(quota.get("left") or 0)
    if left < 1:
        return {"delivered": False, "would_deliver": False, "reason": DRY_RUN_REASON,
                "why": "今天额度用完了（%s/%s），真投也投不进去"
                        % (quota.get("used_today"), quota.get("limit"))}
    return {"delivered": False, "would_deliver": True, "reason": DRY_RUN_REASON,
            "why": "认出了话题 %r、今天还剩 %d 次额度：真对话（/chat）里这一笔会落进 refusals.json"
                   % (topic, left)}


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


def _asked_text(params: Mapping[str, str]) -> Optional[str]:
    """查询串里对方那句问话：没给是 None（这一次不判软转向），给了就给原文（空白也算给了）。

    空白也算给了，是因为「给了却没内容」与「根本没给」不一样：前者要如实落成 soft-unavailable
    （没比过），后者才是什么都不用判。这个区别由 refusal.soft_scan 的 status 说出来。
    """
    raw = params.get("asked")
    if raw is None:
        return None
    return raw if isinstance(raw, str) else str(raw)


def _answer_text(params: Mapping[str, str], text: Optional[str]) -> Optional[str]:
    """查询串里她说的话：给了 answer 就用它，没给就沿用 text（软转向判的是她那句话）。"""
    raw = params.get("answer")
    body = raw if isinstance(raw, str) else ""
    return body if body.strip() else text


def _missing_text(route: str) -> Result:
    """缺原文：400，说清要给 text（不拿空串往下算，也不给默认话）。"""
    return 400, {"ok": False, "readonly": True, "agent": AGENT,
                 "message": "%s 要给 text（原文）：缺了或全是空白，等于没话可以干跑" % route}


def _bad(message: str) -> Result:
    """入参不合格：400 并把那句人话原样带出去。

    坏入参是调用方的事，不是服务器的事：不许 500 蒙混（500 说明「我们这边坏了」，
    可这次坏的是请求），也不许静默当成「没给」往下算——那等于替调用方猜了一个意思。
    """
    return 400, {"ok": False, "readonly": True, "agent": AGENT, "message": message}


def _inject(params: Mapping[str, str]) -> Optional[list]:
    """查询串里的 inject：URL 编码的 JSON 数组；没给返回 None（交给审计报 audit-unavailable）。

    解析失败、或解出来不是数组，都当场抛 ValueError 并说清「inject 要一个 JSON 数组」。
    **不许静默当成没给**：当成没给，审计就永远显示 unavailable，调用方还以为是自己没送对料。
    数组里每一项的形状（kind 认不认识、text 空不空）不在这里判——那是 audit 的判据，
    它会抛 AuditBroken 并在报错里指名是哪一段，由 _gate 原样翻成 400。
    """
    raw = params.get("inject")
    if raw is None or not str(raw).strip():
        return None
    try:
        loaded = json.loads(raw)
    except ValueError as exc:            # JSONDecodeError 是 ValueError 的一种
        raise ValueError("%s；这一段读不出 JSON：%s" % (INJECT_HINT, exc)) from None
    if not isinstance(loaded, list):
        raise ValueError("%s；解出来是 %s，不是数组（一段好端端的话也请包进数组里）"
                         % (INJECT_HINT, type(loaded).__name__))
    return loaded


def _whole(raw: Any, name: str) -> Optional[int]:
    """查询串里的一个整数（hard_cap）：没给或空串就是 None，给了读不出整数就抛 ValueError。"""
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(str(raw).strip(), 10)
    except ValueError:
        raise ValueError("%s 要给一个整数（核给的字数上限），读到 %r 读不出整数"
                         % (name, raw)) from None


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


def _audit_section(seen: Any) -> Dict[str, Any]:
    """照抄审计那一节：status / hit / 最长撞了几个字 / 阈值 / 一句人话。

    **status 原样透出**，包括 "audit-unavailable"：没拿到注入原文就是没比过，把它写成
    「通过」等于替她背了一次没人做过的检查。hit 只有在 status 为 "ok" 且真的撞够阈值时才非 null。
    """
    one = seen.hit
    return {
        "status": seen.status,
        "hit": {"kind": one.kind, "matched": one.matched,
                "seg_kind": one.seg_kind, "reason": one.reason} if one else None,
        "lcs_len": seen.lcs_len,
        "threshold": seen.threshold,
        "reason": seen.reason,
    }


def _verdict_section(refused: Any, gated: Any, soft: Any, final: str,
                    clause: Any) -> Dict[str, Any]:
    """判了什么：形状与大类、按哪条判据、装出来的事件、闸在这一句上命中的条款，外加软转向那几样。

    这一节**只说判了什么**，不说投没投——那是 delivered 那一节的事。两件事分开写，是因为这一轮
    它们本来就分开了：GET 判得出（verdict 有内容）、投不了（delivered 恒为 false）。

    没判成拒绝时 kind / shape / full_name / event 都是 None，text 与 gate_decision 是「这句照发」的
    读数，理由取软转向那句人话：这时 verdict 说的是「判过了、不是拒绝」（或「判不了」），不是「没判」。
    kind 是形状大类（hard|soft），shape 是那一节的名字（topic / soft…）：少了 kind，人就得靠猜
    shape 是不是 "soft" 才知道这次是软转向还是硬拒绝。

    refused 与 fatal_status 是这一轮新透出的两格，**各有各的意思、不许互相顶替**：
    refused 说「她拒了、这句最后发出去没有」（红线命中时是 false：这话不许发，不是她没拒）；
    fatal_status 说「红线那一关判成什么」（ok / not-checked / fatal，原样取 RefusalVerdict）；
    gate_decision 说「闸最后怎么判的」（红线命中时必然是 dropped）。没判成拒绝时 refused 是 null
    （没有「拒绝成立不成立」这回事），fatal_status 照旧取闸的读数——干跑这一路闸是跑过的。
    """
    if refused is None:
        return {"kind": None, "shape": None, "full_name": None, "event": None,
                "reason": soft.reason if soft is not None else "",
                "text": final, "gate_decision": gated.decision, "hit": clause,
                "refused": None, "fatal_status": gated.fatal_status,
                "soft": _soft_section(soft) if soft is not None else None}
    hard = refused.kind != refusal.KIND_SOFT
    return {"kind": refused.kind, "shape": refused.shape, "full_name": refused.full_name,
            "event": refused.event, "reason": refused.reason, "text": final,
            "gate_decision": gated.decision if hard else refused.gate_decision,
            "hit": clause,
            "refused": refused.refused, "fatal_status": refused.fatal_status,
            "soft": _soft_section(soft) if soft is not None else None}
def _readonly_section(verdict: Any) -> Dict[str, Any]:
    """软转向时闸那一遍的读数：照跑、照判，只是不许改她的话（所以这里只报不改）。

    hits 是闸看出问题的那些条（可能是空的），decision 是它本来想怎么处理——都留着给人看：
    「照发」不等于「闸没意见」，两件事分开报，比把闸的判词抹掉诚实。
    """
    return {
        "decision": verdict.decision,
        "hits": [{"clause_id": one.clause_id, "kind": one.kind,
                  "matched": one.matched, "reason": one.reason} for one in verdict.hits],
        "reason": verdict.reason,
    }


def _soft_section(soft: Any) -> Dict[str, Any]:
    """软转向判词那一节：是不是、比没比过、命中哪几个词、为什么这么判。

    status 原样透出：不是 "ok" 就是没真的比过（soft-unavailable），**这时既不是「是软转向」，
    也不是「不是拒绝」**——没比过而已，不许把它读成清白。
    """
    return {
        "is_soft": soft.is_soft,
        "status": soft.status,
        "opener": soft.opener,
        "redirect": soft.redirect,
        "question": soft.question,
        "reason": soft.reason,
    }


def _action_section(found: Any, status: str, cleared: str) -> Dict[str, Any]:
    """动作括注那一节：判没判、哪一路命中、踩到哪个场景词、一句人话。

    checked 取闸的 action_status（"ok" 才算真判过）：false 是「这一关没跑」，不是「查过没问题」，
    两者必须分得开。where / env_word 都取自判据表（括号内 / 身体句、场景词表里的那个词）；
    没命中时给的是 data/action_note.json 里那句「查过了」的人话——放行也要有据。
    """
    return {
        "checked": status == action_note.STATUS_OK,
        "where": found.where if found is not None else None,
        "env_word": found.env_word if found is not None else None,
        "reason": found.reason if found is not None else cleared,
    }


def _style_section(counted: Any) -> Dict[str, Any]:
    """长度那一节：判词、目标区间、最后多少字、重写了几次、有没有超核的上限、一句人话。

    rewrites 恒为 0：这一轮没有重写器，超目标只能如实报「改不了、放行并记账」，
    api 层不伪造一次改写。over_hard_cap 为 true 时 text 也没被截断——截断是核的事。
    """
    return {
        "decision": counted.decision,
        "target_lo": counted.target_lo,
        "target_hi": counted.target_hi,
        "chars": counted.chars,
        "rewrites": counted.rewrites,
        "over_hard_cap": counted.over_hard_cap,
        "reason": counted.reason,
    }


def _unconnected(route: str, why: str) -> Result:
    """未接入：200 + status="未接入" + readonly。不 500，也不给一个看起来像读数的 0。"""
    return 200, {"ok": True, "status": "未接入", "readonly": True, "agent": AGENT,
                 "route": route, "message": "%s：%s，只能如实报「未接入」" % (route, why)}


def _broken(exc: Exception) -> Result:
    """数据读不到／读不动：如实报 500，把仓库或政策那句原话（带路径）原样带出去。"""
    return 500, {"ok": False, "readonly": True, "agent": AGENT, "message": str(exc)}
