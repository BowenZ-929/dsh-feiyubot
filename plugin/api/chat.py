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

成功体里多一节 refusal（**不破坏既有键**）：{kind, shape, verdict_reason, delivered{delivered,
topic, until, remaining_min, reason}}。没判出拒绝时**这一节不出现**——不加一个空壳，免得人把
「判过了、不是拒绝」与「没判」读成一件事；正面答了的那句本来也不该挂个 refusal。

不加鉴权、不改绑地址：这一层的入口由 devserver / 宿主决定，端点只认 POST /chat。
每次失败写一行 WARNING 日志（logger 名 qqagent.chat，含 kind 与 message）——失败要留痕，
但日志不是给页面的东西，页面只读返回体。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from organs.heart import refuse as heart_refuse
from organs.mind import conversation
from organs.mind.llm import LLMError
from organs.mouth import refusal, warehouse
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


def dispatch(route: str, body: Optional[Dict[str, Any]],
             store: Any, brain: Any) -> Result:
    """POST /chat：一句话进来，一整轮出去；判得出拒绝就在这儿投一次心。

    body 非 dict、text 缺失或全空白 → 400；store / brain 未载入 → 503（说不出话的两种原因
    分开报：没有魂是没底色，没有脑是没记忆）。LLMError 之后一律非 200 + ok:false。

    成功之后多做一步：拿对方那句（text）与她那句（payload["reply"]）判一次拒绝，判出来就投——
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
    try:
        payload = conversation.answer(text, who=_who(body), store=store, brain=brain)
    except LLMError as exc:
        status = STATUS_BY_KIND.get(exc.kind, DEFAULT_STATUS)
        LOG.warning("POST /chat 失败：kind=%s message=%s", exc.kind, exc.message)
        return status, {"ok": False, "kind": exc.kind, "message": exc.message}
    section = _judge(text, payload.get("reply"))
    if section is not None:
        payload["refusal"] = section
    return 200, payload


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
