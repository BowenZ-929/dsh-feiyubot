"""说话端点：POST /chat —— 核上唯一一条写，把一轮真对话从入口说出去。

**只做 HTTP 映射，不装第二遍。** 装配（魂 → 脑 → 心 → 嘴 → 模型 → 过嘴 → 落脑）全在
organs/mind/conversation.py 的 answer()：那一处是全仓唯一的装配点，QQ 通道将来也直接调它。
本文件只做三件 HTTP 的事：认 body、把模型失败翻成状态码、把结果原样交出去。

失败一律 **非 200 + ok:false**，body 固定三键 {ok:false, kind, message}：
kind 给机器分岔，message 是模型那边的人话原文（str(LLMError) == message），一个字节都不改。
模型回了空内容也不算成功——那是 empty，不是「她轻轻没说话」；把空串当 200 发出去，页面就会
画出一个「她说了一句空话」的假成功。

**投递点只有这里，干跑端点不写。** 一轮说过之后（conversation.answer() 返回之后）判一次拒绝：
她的答话把话转开了（refusal.soft_scan）或就是那三句标准拒绝之一（refusal.enforce），就在本文件
投一次——heart.refuse.ingest 写心的 refusals.json，refusal.record 写我们自己的 refuse.jsonl。
/mouth/gate 那个干跑端点只判不投（它的 delivered 恒为 false、would_deliver 只算不写）：一个拒绝
要么落在这一层（真说过话），要么谁都不写（只是干跑），没有第三种。

**闸在 /chat 生效（这一轮加的）**：answer() 返回之后、组装响应之前，逐泡再过一遍闸
（条款 → 动作括注 → 内容重复 → 复述审计），命中就改一次、改不了就弃那一泡；弃掉的泡从 chunks 里
删掉，剩下的重算 send_at，全被弃掉就是 chunks=[]、reply=""（每一泡弃言各记一行弃言账）。所以
gate 节除了既有的 decision / hit / drops，还逐泡交代 parts / in_parts / out_parts / blocked / note。
判据一个都不在本文件：条款、复述审计、动作括注、内容重复全部经 warehouse.load 读；
「她最近几条回话」只读 mind 的 recent_turns.jsonl，取最近几轮的条数同样从 repeat.json 里读。
**动作配额也从装配传进闸**：answer() 回包的 action.quota.blocked 为真就传 forbid、为假传 allow；
装配里没有这一项就是 None（闸记 quota_status=not-checked）——没标不是 allow，不许猜。

**重写真的接到模型上（这一轮加的老账）**：以前命中即弃，一句口癖让她整泡消失。现在命中先改一次——
「她的原话 + 命中的那一条 + 一句人话原因」交给模型（走 answer() 用的**同一条出口** llm.complete，
不另开 HTTP 客户端），带 data/rewrite.json 的 budget_ms 硬时限；拿回来先过 rewrite.accept
（空 / 逐字相同 / 长度出区间 / 丢了数字、英文串、引号里的原话都算不合规），过了再用新稿跑一遍闸，
**二次仍命中才弃**。改不动（超时 / 空 / 不合规 / 出口抛错）就**保留原话发出**、账本记一行
rewrite-failed，绝不输出空——宁可发一句略有瑕疵的，也不许静默变哑。每一泡的重写读数挂在
parts[*].rewrite（{status, why, from, to}，from / to 只放一小截）。

**对方要她再说一遍时内容去重不判废（这一轮加的）**：asked（入站那句）原样传给闸，撞上重复而
对方明说要她重复时，那一泡只记一行账（reason 带 skipped-by-request）、照原样发出，
repeat_status 记成跳过那个状态——**她重复是对的，别吞**。这条豁免只在真撞上重复、且 asked 命中
请求标记时成立，asked 为空一律不豁免。

成功体里多一节 refusal（**不破坏既有键**）：{kind, shape, verdict_reason, delivered{delivered,
topic, until, remaining_min, reason}}。没判出拒绝时**这一节不出现**——不加一个空壳，免得人把
「判过了、不是拒绝」与「没判」读成一件事；正面答了的那句本来也不该挂个 refusal。

不加鉴权、不改绑地址：这一层的入口由 devserver / 宿主决定，端点只认 POST /chat。
每次失败写一行 WARNING 日志（logger 名 qqagent.chat，含 kind 与 message）——失败要留痕，
但日志不是给页面的东西，页面只读返回体。
"""
from __future__ import annotations

import concurrent.futures
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional

from organs.heart import refuse as heart_refuse
from organs.mind import conversation
from organs.mind import llm
from organs.mind import recent as mind_recent
from organs.mind.llm import LLMError
from organs.mouth import action_note, audit, drops, gate, refusal, repeat, warehouse
from organs.mouth import quota as quota_rules          # 参数名就叫 quota，模块用别名进来免得被挡住
from organs.mouth import rewrite as rewrite_rules      # 参数名就叫 rewrite，模块用别名进来免得被挡住
from organs.mouth.gate import ClauseBroken

from .core import Result

CHAT_ROUTES = {"/chat": "POST"}

LOGGER_NAME = "qqagent.chat"
MISSING_TEXT = "POST /chat 要给 text（一句话）"
STORE_MISSING = "魂未载入：说话前先起核"
BRAIN_MISSING = "脑未载入：没有记忆就不开口"

# 模型失败 → HTTP：拿不到 key 是配置问题（503，去补），鉴权/服务端是上游的脸，
# 超时与限流是「等会儿再来」，空内容是上游没给话（502）而不是这一轮的错。
STATUS_BY_KIND = {
    "no-key": 503,
    "auth": 502,
    "timeout": 504,
    "rate_limit": 429,
    "empty": 502,
    "http": 502,
    "config": 500,
}
DEFAULT_STATUS = 502

LOG = logging.getLogger(LOGGER_NAME)

# 重写那一次模型调用的注入点：与装配（answer() → llm.complete）走的是同一条出口，本文件不另开
# HTTP 客户端。生产三样都是 None（配置从 data/llm.json 读、key 从环境读、transport 走 urllib）；
# 测试把假 transport / 假配置塞进这一格，一个字节都不发真网络。
TRANSPORT: Optional[Any] = None
CONFIG: Optional[Mapping[str, Any]] = None
ENV: Optional[Mapping[str, str]] = None

GATE_NOTE = ("闸在 /chat 生效：answer() 返回之后逐泡过一遍闸（条款 → 动作括注 → 内容重复 → 复述审计），"
             "命中就交给模型改一次（同一个 transport、带 budget_ms 硬时限）；改完不命中就用新稿，"
             "二次仍命中才弃那一泡，改不动（超时 / 空 / 不合规 / 出口抛错）则保留原话发出并记账；"
             "弃掉的泡从 chunks 里删掉，send_at 按剩下的重算。"
             "in_parts 是进闸的泡数、out_parts 是出闸的泡数，blocked 是这次拦下过她的 clause_id"
             "（去重、按出现顺序），parts 逐泡交代判词与这一泡过闸前后的原文，"
             "另带这一泡的几样「判没判」读数：audit 是复述审计（status 不是 ok 就是没比过）、"
             "action_status 是动作括注、repeat_status 是内容重复（skipped-by-request 是「对方要她"
             "再说一遍、这一关只记账没判废」）、quota_status 是动作配额（not-checked 都是这一关"
             "没跑，不是通过），以及 rewrite 是这一泡的重写读数"
             "（{status: ok|failed|not-tried, why, from, to}，from / to 只是一小截）；"
             "顶层的 quota 是这一轮交给闸的配额写法"
             "（forbid / allow / null），取自装配 action.quota.blocked，装配没标就是 null。"
             "每一泡弃言都记一行账（organs/mouth/drops.py）。")


def dispatch(route: str, body: Optional[Dict[str, Any]],
             store: Any, brain: Any) -> Result:
    """POST /chat：一句话进来，一整轮出去；判得出拒绝就在这儿投一次心。

    body 非 dict、text 缺失或全空白 → 400；store / brain 未载入 → 503（说不出话的两种原因
    分开报：没有魂是没底色，没有脑是没记忆）。LLMError 之后一律非 200 + ok:false。

    成功之后先过闸：拿 answer() 装配进去的那几段原文（魂 / 锚句 / 记忆原文 / 心档位词 / 规矩句 /
    对方原话）与装配体逐泡过一遍闸，结果就地落回 payload（chunks / reply / gate）。闸坏了不 fail-open：
    判据读不动就整轮弃掉，并在 gate 里如实说为什么。

    再过一步：拿对方那句（text）与她那句（payload["reply"]）判一次拒绝，判出来就投——
    **全仓唯一的投递点就在这一处**。判据坏了（拒绝数据读不动）不改这一轮的成功：她那句话已经
    说出口了，写一行 WARNING、并在 refusal 那一节里如实说这次没判成、没投心。
    """
    text = _text(body)
    if text is None:
        return 400, {"ok": False, "message": MISSING_TEXT}
    if store is None:
        return 503, {"ok": False, "message": STORE_MISSING}
    if brain is None:
        return 503, {"ok": False, "message": BRAIN_MISSING}
    # 闸要的「她最近几条回话」在答题之前读：answer() 最后一步会把这一轮追加进同一份账，
    # 之后再读就把本轮当成了历史——她这句会跟自己比一次，于是每一句都成复读。
    history = _her_recent()
    try:
        payload = conversation.answer(text, who=_who(body), store=store, brain=brain)
    except LLMError as exc:
        status = STATUS_BY_KIND.get(exc.kind, DEFAULT_STATUS)
        LOG.warning("POST /chat 失败：kind=%s message=%s", exc.kind, exc.message)
        return status, {"ok": False, "kind": exc.kind, "message": exc.message}
    _gate_round(payload, text, store, history)
    section = _judge(text, payload.get("reply"))
    if section is not None:
        payload["refusal"] = section
    return 200, payload


# —— 闸：逐泡过一遍，结果落回返回体 ————————————————

def _gate_round(payload: Dict[str, Any], asked: str, store: Any,
                history: Optional[List[str]]) -> None:
    """answer() 之后、交出去之前：把 chunks 里的每一泡单独过一遍闸，结论就地写回 payload。

    六件事：

    - **逐泡过闸**：每一泡各调一次 gate.enforce（条款 → 动作括注 → 内容重复 → 复述审计）。注入的
      原文、最近几轮、判据表都是同一份——同一轮里的几泡是同一句话切出来的，凭什么两样看待。
    - **命中先改一次，不再命中即弃**（这一轮接上的）：命中之后把「她的原话 + 命中的那一条 + 一句
      人话原因」交给模型改一次（走 answer() 同一个 transport，带 data/rewrite.json 的 budget_ms
      硬时限），拿回来先过 rewrite.accept，过了再用新稿跑第二遍闸；二次仍命中才弃那一泡。
      **改不动（超时 / 空 / 不合规 / 出口抛错）就保留原话发出**并记一笔账，绝不输出空——
      一句略有瑕疵的话也比她突然变哑强。每一泡的重写读数挂在 parts[*].rewrite 上。
    - **对方要她重复时不判废**：asked（入站那句）原样转给闸，撞上重复而对方明说要她再说一遍时，
      这一泡照发、账本记一行 skipped-by-request，repeat_status 记成跳过那个状态。
    - **真的落到返回体**：pass 原样留着；rewritten 换 Verdict.text（gap 沿用那一泡的）；dropped 从
      chunks 里删掉。**一处都没被改时不碰 chunks**——不接闸时逐字相同，这条是被测过的。
    - **重算 send_at**：删掉几泡之后，剩下的按原来的 gap_before 累加重排，不留洞（小数位与
      pace.fill 同一个数，取自 policy 的 pace 段）。
    - **全被弃掉**：chunks=[]、reply=""，gate.decision 记 dropped；每一泡弃言由 sink 记一行账。
    - **判据读不动**：整轮弃掉（_not_gated），并在 gate.note 里说清是哪一处坏——出口不许 fail-open：
      改不了就不发这句，也不拿她的沉默去兜一次没人做过的检查之外的东西。

    动作配额（这一轮补的）也在这条路上：装配标了这一轮不许写动作就给闸 forbid、标了放行给 allow、
    没标给 None（闸记 not-checked，**没标不是 allow**）。配额命中是闸里唯一一条不丢整句的路——
    它把动作行剥掉、正文接着过其余判据，于是这一泡是 rewritten 而不是 dropped，text 换成剥完的正文。

    gate 节既不新增顶层键，也不丢既有键：decision / hit / drops 是 answer() 给的那一份（页面与既有
    断言都在读它），这里追加 parts / in_parts / out_parts / blocked / quota / note；被闸改过时
    decision 与 hit 按最终结果改写——她一句都没出去，就不能还挂着 pass。
    """
    chunks = [dict(one) for one in (payload.get("chunks") or [])]
    quota = _quota(payload)
    section = dict(payload.get("gate") or {})
    section.update({"parts": [], "in_parts": len(chunks), "out_parts": 0,
                    "blocked": [], "quota": quota, "note": GATE_NOTE})
    payload["gate"] = section
    if not chunks:
        section["note"] = (GATE_NOTE + "这一轮没有泡可过闸：reply 是空串，"
                                    "装配那一步的闸已经把它整句弃掉了。")
        return
    try:
        books = _books()
        inject = _inject(asked, payload, store, books["audit"])
    except (warehouse.WarehouseMiss, warehouse.WarehouseBroken, gate.ClauseBroken,
            audit.AuditBroken, action_note.ActionBroken, repeat.RepeatBroken,
            OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("POST /chat 过闸没成：%s", exc)
        _not_gated(payload, exc)
        return

    trace = drops.path_of()
    today = _day()
    recorded: List[str] = []

    def sink(clause_id: str, reason: str) -> None:
        """闸叫的那一声（只在弃掉那一泡时叫）：时刻在这一层给，落盘只在 drops 里。"""
        drops.record(trace, clause_id, reason, datetime.now())
        recorded.append(clause_id)

    parts: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    blocked: List[str] = []
    changed = False
    for index, chunk in enumerate(chunks):
        rewritten = _outcome()
        verdict = gate.enforce(
            chunk["text"], books["clauses"],
            _rewriter(chunk["text"], asked=asked, books=books, inject=inject,
                      recent=history, quota=quota, outcome=rewritten),
            sink, inject=inject, policy=books["audit"],
            action=books["action"], recent=history,
            repeat_table=books["repeat"], quota=quota,
            asked=asked, keep_unrewritten=True)
        parts.append({"i": index, "decision": verdict.decision,
                      "hits": [_hit_row(one) for one in verdict.hits],
                      "reason": verdict.reason,
                      "before": chunk["text"], "after": verdict.text,
                      "audit": _audit_row(verdict.audit),
                      "action_status": verdict.action_status,
                      "repeat_status": verdict.repeat_status,
                      "quota_status": verdict.quota_status,
                      "rewrite": rewritten})
        if verdict.decision == "pass":
            kept.append({"text": chunk["text"], "gap_before": chunk["gap_before"]})
            continue
        changed = True
        if verdict.decision == "dropped":
            blocked.append(verdict.hits[-1].clause_id if verdict.hits else "")
            continue
        kept.append({"text": verdict.text, "gap_before": chunk["gap_before"]})

    section.update({"parts": parts, "out_parts": len(kept), "blocked": _unique(blocked)})
    if changed:
        _resend(payload, kept, books["digits"])
        section["decision"] = "dropped" if not kept else "rewritten"
        first = next((row for row in parts if row["hits"]), None)
        if first is not None:
            section["hit"] = {"clause_id": first["hits"][-1]["clause_id"],
                              "reason": first["hits"][-1]["reason"]}
    if recorded:
        section["drops"] = {"today": int(drops.report(trace, today).get("today") or 0)}


def _not_gated(payload: Dict[str, Any], exc: Exception) -> None:
    """判据读不动：这一轮整句弃掉，并在 gate 里说清为什么（不 fail-open，也不假装过了闸）。"""
    payload["chunks"] = []
    payload["reply"] = ""
    section = dict(payload.get("gate") or {})
    section.update({"decision": "dropped", "out_parts": 0, "blocked": [],
                    "note": GATE_NOTE + "这一次判据读不动（%s：%s），按「改不了就不许发」整轮弃掉，"
                                        "一句都没出去。" % (type(exc).__name__, exc)})
    payload["gate"] = section


# —— 重写：命中之后改一次，不改就留原话 ————————————————————

def _outcome() -> Dict[str, Any]:
    """这一泡的重写读数：{status, why, from, to}，进 gate.parts[*].rewrite。

    默认 not-tried：这一泡没命中，一次请求都没发。why 用数据里那句「没命中，不用改」——
    空白字段会让人以为「没看见」，而这里其实是「不需要」。
    """
    return {"status": rewrite_rules.STATUS_NOT_TRIED, "why": rewrite_rules.clear_reason(),
            "from": "", "to": ""}


def _rewriter(original: str, *, asked: str, books: Mapping[str, Any], inject: Any,
              recent: Optional[List[str]], quota: Optional[str],
              outcome: Dict[str, Any]) -> Callable[[str], str]:
    """造这一泡的 rewrite（text -> text）：把命中的那一条交给模型改一次，改不动就抛 Unrewritable。

    闸只在命中之后才调它，所以没命中的那泡一次模型请求都不发。要改的是哪一条由 gate.check 现问：
    入参与紧接着的 enforce 一模一样（同一份 clauses / inject / recent / 判据表 / asked），
    而两道判据都是纯函数、顺序也钉死了，所以两处指的必然是同一条——问一次不会分岔。

    三件事按顺序做，缺一件都不算改成：**发**（_ask_model，带硬时限）→ **验**（rewrite.accept，
    六条判据全在数据里）→ **交**（把新稿还给闸，由闸拿它再判一遍）。任何一步不过就抛
    Unrewritable，把「为什么改不动」原样带出去——闸据此保留原话、记一行账，绝不输出空。
    from / to 只放必要的一小截（截多少取自数据）：面板要看的是「改的是哪一句」，不是整段再抄一遍。
    """
    table = books["rewrite"]

    def rewrite(body: str) -> str:
        hit = gate.check(body, books["clauses"], inject=inject, policy=books["audit"],
                         action=books["action"], recent=recent,
                         repeat_table=books["repeat"], quota=quota, asked=asked)
        before = rewrite_rules.excerpt(body, table)
        try:
            said = _ask_model(rewrite_rules.prompt(body, hit, table), table)
            ok, why = rewrite_rules.accept(body, said, table)
        except rewrite_rules.Unrewritable as exc:
            outcome.update({"status": rewrite_rules.STATUS_FAILED, "why": str(exc),
                            "from": before, "to": ""})
            raise
        if not ok:
            outcome.update({"status": rewrite_rules.STATUS_FAILED, "why": why,
                            "from": before, "to": rewrite_rules.excerpt(said, table)})
            raise rewrite_rules.Unrewritable(why)
        outcome.update({"status": rewrite_rules.STATUS_OK, "why": why, "from": before,
                        "to": rewrite_rules.excerpt(said, table)})
        return said

    return rewrite


def _ask_model(prompt_text: str, table: Mapping[str, Any]) -> str:
    """把重写那一段指令发出去、把新稿拿回来：走 answer() 用的同一条出口，带 budget_ms 硬时限。

    **同一个 transport、同一份配置。** 出口就是 organs/mind/llm.complete（模型名 / 地址 / key /
    重试都在 data/llm.json 与那份配置里，与装配那一问完全一样），本文件不另开 HTTP 客户端、
    不自己拼地址；测试把假 transport 塞进模块顶上那三个注入点，一个字节都不发真网络。

    **两个时限一起收：** transport 那一侧把 config 的 timeout_ms 换成 budget_ms（一次读写别拖过
    整件事的预算），attempts 按数据算（1 就是不重试，免得「一次重写」变成两三次）；外面再用
    线程等一个硬时限，超了就抛——**等不到就当改不动**，原话留着发。

    失败一律翻成 Unrewritable（为什么取自数据）：超时、出口抛错、配置读不动都算「改不动」，
    绝不往上抛裸异常——闸那边只认这一种「改不了」的说法。
    """
    budget = rewrite_rules.budget_ms(table)
    try:
        conf = dict(llm.resolve_config(CONFIG), timeout_ms=budget,
                    retries=max(0, rewrite_rules.attempts(table) - 1))
        messages = [{"role": "user", "content": prompt_text}]
        return _bounded(lambda: llm.complete(messages, config=conf, env=ENV,
                                             transport=TRANSPORT),
                        budget / 1000.0)
    except concurrent.futures.TimeoutError:
        raise rewrite_rules.Unrewritable(rewrite_rules.timeout_why(table)) from None
    except LLMError as exc:
        raise rewrite_rules.Unrewritable(rewrite_rules.error_why(exc, table)) from None
    except Exception as exc:          # 配置读不动、出口抛别的：一样算「改不动」，不往上抛裸异常
        raise rewrite_rules.Unrewritable(rewrite_rules.error_why(exc, table)) from None


def _bounded(call: Callable[[], str], seconds: float) -> str:
    """在 seconds 秒内等这一次调用的结果；到点就抛 concurrent.futures.TimeoutError。

    为什么用线程、而不是只靠 transport 自己的超时：那一层管的是「一次 HTTP 读写等多久」，重试、
    连接、读响应各算各的，凑起来能把一次重写拖到三四倍。这里要的是一个「整件事最多等多久」的
    硬时限，所以拿线程等：submitted 之后 result(timeout) 到点就抛。

    **不 join**：超时之后那次请求可能还在路上，等它就把这一轮 HTTP 请求挂死了。线程自己不清理
    也没关系——它不持有本文件的任何状态，回来也只会被丢掉。
    """
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(call).result(timeout=seconds)
    finally:
        pool.shutdown(wait=False)


def _books() -> Dict[str, Any]:
    """这一轮过闸用的六份判据，全部经 warehouse 读——api 里不写死一条条款、一个数。

    条数（rounds）与小数位（round_digits）也在数据里：前者是她最近几条回话的窗口，后者与
    pace.fill 算 send_at 用的是同一个数，重排时才不会跟切分那一处对不上。缺件坏件照实抛，
    由 _gate_round 翻成「整轮弃掉」——判据读不动时不猜一个默认值顶上。
    """
    policy = warehouse.load("policy")
    repeat_book = warehouse.load("repeat")
    return {
        "clauses": warehouse.load("clauses").get("clauses"),
        "audit": warehouse.load("audit"),
        "action": warehouse.load("action_note"),
        "repeat": repeat_book,
        "rewrite": warehouse.load("rewrite"),
        "digits": int(policy["pace"]["round_digits"]),
    }


def _quota(payload: Mapping[str, Any]) -> Optional[str]:
    """装配这一轮给不给写动作的资格，翻成闸认的那两种写法；没标就是 None。

    取值路径只有一条，用的是 answer() 回包里现成的那一份（装配那边按
    organs/mind/data/assembly.json 的 action.quota 数最近几轮算出来的 blocked）：

    - blocked 为真 → "forbid"（本轮一句动作行都不许写）；
    - blocked 为假 → "allow"（装配给了配额配置、这一轮放行）；
    - action / action.quota / blocked 缺了、或 blocked 不是布尔 → None：**装配没标这一轮**，
      闸记 quota_status="not-checked"。没标不是 allow——猜成放行，这一关就等于白设。

    两个写法取自 organs/mouth/quota.py 的常量（数据里 quota_values 的别名也认，但这里不必自己拼词）。
    """
    action = payload.get("action")
    if not isinstance(action, Mapping):
        return None
    book = action.get("quota")
    if not isinstance(book, Mapping):
        return None
    blocked = book.get("blocked")
    if not isinstance(blocked, bool):
        return None
    return quota_rules.FORBID if blocked else quota_rules.ALLOW


def _inject(asked: str, payload: Mapping[str, Any], store: Any,
            table: Mapping[str, Any]) -> Optional[List[Dict[str, str]]]:
    """这一轮装配进提示词的原文，按 type 分段交给复述审计；一段都拿不到就是 None。

    六段各按本来的来源取，取不到就**如实少一段**，不拿别的东西凑：

    - 魂：store.raw 那几张卡的正文（identity / manner）；
    - 锚句：answer() 真的注进去的半句（回包 anchors.items[*].injected），未配就是空；
    - 记忆原文：回包 recalled[*].text；
    - 心档位词：回包 heart 里那几档词（轴上的档位词、心境档、语气档）；
    - 规矩句：装配说明里的 reply.instruction 与那几条格式要求；
    - 对方原话：入站那句 text。

    None（一段都没取到）交给 audit 报 audit-unavailable——**不许伪造通过**：没料可比就是没比过。
    """
    kinds = _kinds(table)
    rows: List[Dict[str, str]] = []
    for kind, text in ((kinds["soul"], _soul_text(store)),
                       (kinds["anchor"], _anchor_text(payload)),
                       (kinds["memory"], _memory_text(payload)),
                       (kinds["heart"], _heart_text(payload)),
                       (kinds["rule"], _rule_text()),
                       (kinds["quote"], asked)):
        body = (text or "").strip()
        if body:
            rows.append({"kind": kind, "text": body})
    return rows or None


def _kinds(table: Mapping[str, Any]) -> Dict[str, str]:
    """六个 kind 的原词全部取自 data/audit.json：五类设定段按数据里的顺序，对方原话取 quote_kind。

    五类设定段在审计那边是同一档（照搬一律拦），所以这里的顺序只决定报出来的来源段叫什么；
    数量不对就当场抛（改数据改坏了要响，不许在这儿补一个默认名——那就是第二处真源）。
    """
    setting = table.get("setting_kinds")
    quote = table.get("quote_kind")
    if not isinstance(setting, (list, tuple)) or len(setting) != 5:
        raise ValueError("audit.json 的 setting_kinds 要是五类设定段，读到 %r" % (setting,))
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("audit.json 的 quote_kind 要是一个非空字符串，读到 %r" % (quote,))
    soul, anchor, memory, heart, rule = setting
    return {"soul": str(soul), "anchor": str(anchor), "memory": str(memory),
            "heart": str(heart), "rule": str(rule), "quote": quote.strip()}


def _soul_text(store: Any) -> str:
    """魂那几张卡的正文（identity / manner），摊平拼成一段；读不动的那张就当少一段。"""
    out: List[str] = []
    for block in ("identity", "manner"):
        try:
            raw = store.raw(block)
        except (ValueError, OSError) as exc:
            LOG.warning("POST /chat 读魂的 %s 卡没成：%s", block, exc)
            continue
        out.extend(_strings(raw))
    return "\n".join(out)


def _strings(value: Any) -> List[str]:
    """把一张卡里所有非空字符串（含嵌套的清单与键值表）摊平；空着的一律不取。"""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Mapping):
        out: List[str] = []
        for one in value.values():
            out.extend(_strings(one))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for one in value:
            out.extend(_strings(one))
        return out
    return []


def _anchor_text(payload: Mapping[str, Any]) -> str:
    """这一轮真的注进去的锚句（半句）：回包 anchors.items[*].injected，未配锚句就是空。

    不拿 say 全文替一份她没看见的原文——她抄不着没进提示词的东西，报成抄了就是冤枉她。
    """
    items = (payload.get("anchors") or {}).get("items") or []
    return "\n".join(str(one.get("injected") or "") for one in items
                      if isinstance(one, Mapping))


def _memory_text(payload: Mapping[str, Any]) -> str:
    """这一轮想起来的记忆原文：回包 recalled[*].text，一条都没有就是空。"""
    return "\n".join(str(one.get("text") or "") for one in (payload.get("recalled") or [])
                      if isinstance(one, Mapping))


def _heart_text(payload: Mapping[str, Any]) -> str:
    """这次心给的档位 / 摘要词：轴上的档位词加心境档、语气档（全是词，没有数）。"""
    heart = payload.get("heart") or {}
    out = _strings(heart.get("axes") or {})
    for part in (heart.get("mood") or {}, heart.get("voice") or {}):
        for key in ("tier", "tone"):
            value = part.get(key)
            if isinstance(value, str) and value.strip():
                out.append(value.strip())
    return "\n".join(out)


def _rule_text() -> str:
    """装配说明里的规矩句（reply.instruction 与那几条格式要求）；读不到就少这一段。"""
    try:
        conf = conversation.load_assembly()["reply"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("POST /chat 读装配说明没成：%s", exc)
        return ""
    rows = [str(conf.get("instruction") or "")]
    rows.extend(str(one) for one in (conf.get("format") or []))
    return "\n".join(one for one in rows if one.strip())


def _her_recent() -> Optional[List[str]]:
    """她最近几条自己的回话（旧的在前），只读 mind 的 recent_turns.jsonl；读不动返回 None。

    条数（最近几轮）不写死：从 repeat.json 的 rounds 读，与去重那一关看的是同一个窗口。
    三种情形必须分得开：账还没建 = 空清单（判过了，只是还没有历史）；账在、读出来空 = 空清单；
    **读不动 = None**——这一关没判，闸会把它记成 not-checked，不许当成「没重复」。
    """
    try:
        rounds = int(warehouse.load("repeat")["rounds"])
        path = mind_recent.path_of()
        if not path.is_file():
            return []
        rows = mind_recent.load(turns=rounds).get("turns") or []
    except (warehouse.WarehouseMiss, warehouse.WarehouseBroken, OSError,
            ValueError, KeyError, TypeError) as exc:
        LOG.warning("POST /chat 读不到她最近的回话：%s", exc)
        return None
    return [str(row.get("reply") or "") for row in rows]


def _resend(payload: Dict[str, Any], kept: List[Dict[str, Any]], digits: int) -> None:
    """把剩下的泡重排 send_at 后写回返回体：按原来的 gap_before 累加，删掉几泡也不留洞。

    reply 跟着一改写的是「她这一轮实际说出口的话」：被弃的那几泡没发出去，就不该还留在 reply 里。
    """
    clock = 0.0
    rows: List[Dict[str, Any]] = []
    for one in kept:
        clock = round(clock + float(one["gap_before"]), digits)
        rows.append({"text": one["text"], "gap_before": one["gap_before"], "send_at": clock})
    payload["chunks"] = rows
    payload["reply"] = "".join(str(one["text"]) for one in rows)


def _audit_row(seen: Any) -> Dict[str, Any]:
    """这一泡的复述审计读数：status / 撞上的那一条 / 最长撞了几个字 / 阈值。

    **status 原样透出**：不是 "ok" 就是没真的比过（audit-unavailable），这时既不是「通过」也不是
    「拦住」——把它写成通过，等于替她背了一次没人做过的检查。没拿到注入原文时就是这一态。
    """
    one = getattr(seen, "hit", None)
    return {
        "status": getattr(seen, "status", None),
        "hit": None if one is None else {"kind": one.kind, "seg_kind": one.seg_kind,
                                         "matched": one.matched},
        "lcs_len": getattr(seen, "lcs_len", None),
        "threshold": getattr(seen, "threshold", None),
    }


def _hit_row(one: Any) -> Dict[str, str]:
    """一条命中：哪条（条款 id 或违规类别）、什么标签、一句人话、命中的那段原文。"""
    return {"clause_id": one.clause_id, "kind": one.kind,
            "reason": one.reason, "matched": one.matched}


def _unique(items: List[str]) -> List[str]:
    """去重、按出现顺序（空串是「闸说弃了却没报条款」的缺口，不往 blocked 里塞）。"""
    out: List[str] = []
    for one in items:
        if one and one not in out:
            out.append(one)
    return out


def _day() -> str:
    """今天的日期（ISO，十位）：弃言账按这个前缀认「今天」。"""
    return datetime.now().date().isoformat()


# —— 拒绝：判一次、投一次（唯一的投递点） ————————————————————

def _judge(asked: str, answer: Any) -> Optional[Dict[str, Any]]:
    """一轮说完之后判一次拒绝：判出来就投给心；没判出来返回 None（成功体里不出现这一节）。

    两条路，顺序钉死：先问「她这句是不是把话转开了」（refusal.soft_scan），再看它是不是那三句
    标准拒绝之一（refusal.enforce：形状取自 data/refusal.json，代码里不留那三句话）。
    **软转向在前**是因为它更常见，而且硬拒绝那三句本来也不带垫话/转移——顺序换过来不会更准，
    只会把「转开了」这句话当成普通回答放过。

    asked 是对方那句原话，answer 是她那句（被闸弃掉的轮 reply 是空串，这里自然判不出什么）。
    """
    body = answer if isinstance(answer, str) else ""
    try:
        book = warehouse.load("refusal")
        soft = refusal.soft_scan(body, asked, table=book)
        shape = refusal.soft_shape(book) if soft.is_soft else _standard_shape(body, book)
        if shape is None:
            return None
        kind = refusal.KIND_SOFT if soft.is_soft else refusal.KIND_HARD
        delivered: Dict[str, Any] = {}

        def sink(found_event: Dict[str, Any]) -> None:
            """库约定的投递口：事件到手就投一次心，投没投成如实写进 delivered 那一节。"""
            delivered.update(_deliver(asked, found_event, kind))

        # 软转向那一遍不过闸（她的原话一个字都不改），所以只有硬拒绝才去取条款。
        found = refusal.enforce(shape, table=book, refuse_sink=sink, now=_stamp(),
                                clauses=None if soft.is_soft else _clauses(),
                                asked=asked if soft.is_soft else None, answer=body)
    except (refusal.RefusalBroken, ClauseBroken, warehouse.WarehouseMiss,
            warehouse.WarehouseBroken) as exc:
        LOG.warning("POST /chat 判拒绝没成：%s", exc)
        return {"kind": None, "shape": None,
                "verdict_reason": "拒绝判据读不动，这一轮没判成：%s" % exc,
                "delivered": {"delivered": False, "topic": None, "until": None,
                              "remaining_min": None, "reason": "判据读不动，没投心"}}
    return {"kind": found.kind, "shape": found.shape, "verdict_reason": found.reason,
            "delivered": delivered}


def _deliver(asked: str, found_event: Mapping[str, Any], kind: str) -> Dict[str, Any]:
    """把这一次拒绝投给心：认话题 → heart.refuse.ingest → 把心的账翻成五样，顺手记我们自己的账。

    事件由库（refusal.enforce 的 refuse_sink）交过来，这里只认 {ts, shape, full_name, why} 那四键：
    嘴与心只认识一条 dict，不认识彼此的存储；kind（hard|soft）只用来写我们账本那一列。

    话题名沿用现有认法：只在心自己那份词表里找 asked 里出现的那个词（refusal.topic_in，与
    /mouth/gate 的干跑同一个认法）；找不到就投不成（delivered=false）并说清为什么——心不收
    词表以外的话题，这一层也不替它造分类。投不进去（今日额度用完）也照实 false，不写成投过了。

    落盘两处：心的 refusals.json（ingest 那一笔，**全仓唯一写它的地方**）与我们自己的
    refuse.jsonl（refusal.record，一行一次拒绝，sent 记这次投没投进去）。**投不进去也照记那一行**
    ——她拒了这件事是真的，与「有没有落到心上」是两件事。写不进去也是失败：记一行 WARNING 并在
    reason 里说清楚，不把「账没记上」当成「没拒过」。
    """
    topic = refusal.topic_in(asked, heart_refuse.vocabulary())
    if topic is None:
        answer = {"ok": False,
                  "message": "认不出拒的是哪个话题：对方那句问话里没有心认可的话题名"
                             "（词表见 organs/brain/topics.json），这次没投心"}
    else:
        try:
            answer = heart_refuse.ingest(topic, str(found_event.get("full_name") or ""),
                                         now=_stamp())
        except (OSError, ValueError) as exc:
            LOG.warning("POST /chat 投拒绝没成：%s", exc)
            answer = {"ok": False, "message": "投心失败：%s" % exc}
    delivered = bool(answer.get("ok"))
    try:
        refusal.record(refusal.path_of(), found_event, delivered, shape=kind)
    except (OSError, ValueError) as exc:
        LOG.warning("POST /chat 记拒绝账没成：%s", exc)
        tail = "；但拒绝账没记上：%s" % exc
    else:
        tail = ""
    return {"delivered": delivered, "topic": topic, "until": answer.get("until"),
            "remaining_min": answer.get("remaining_min"),
            "reason": _delivered_word(topic, found_event, answer) + tail}


def _delivered_word(topic: str, found_event: Mapping[str, Any], out: Mapping[str, Any]) -> str:
    """投完之后那句人话：投进去了就说冷却到什么时候，没投进去就用心自己的原话（不代它编）。"""
    if out.get("ok"):
        return "投给心：%r 记一次「%s」，冷却到 %s。" % (
            topic, found_event.get("full_name"), out.get("until"))
    return str(out.get("message") or out.get("code") or "心没收这次拒绝")


def _standard_shape(body: str, book: Mapping[str, Any]) -> Optional[str]:
    """她说的是不是那三句标准拒绝之一：是就回那个形状，不是就回 None。

    比的是数据里那句 say 的原文（一字不差），代码里不写死任何一句台词；形状名同样从数据里取，
    认不出就一个都不算——不猜她「大概是在拒绝」。
    """
    said = body.strip()
    if not said:
        return None
    shapes = book.get("shapes") if isinstance(book, Mapping) else None
    for key in shapes if isinstance(shapes, Mapping) else ():
        if refusal.say(key, table=book).strip() == said:
            return key
    return None


def _clauses() -> Any:
    """闸的条款：refusal.enforce 过闸要用它——没有条款不是「没命中」，是「不许放行」（它会抛）。"""
    return warehouse.load("clauses").get("clauses")


def _stamp() -> str:
    """投心时给的那一刻（ISO，秒级）：api 层是时刻的供应方，库层一个都不取时钟。"""
    return datetime.now().replace(microsecond=0).isoformat()


def _text(body: Any) -> Optional[str]:
    """body 里的那句话：不是对象、缺 text、全空白都算没给（返回 None，调用方回 400）。"""
    if not isinstance(body, dict):
        return None
    raw = body.get("text")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def _who(body: Any) -> str:
    """这一轮是谁在说话：可选，没给就是空串（装配那边按生人档算称呼）。"""
    if not isinstance(body, dict):
        return ""
    raw = body.get("who")
    return raw.strip() if isinstance(raw, str) else ""
