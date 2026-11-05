"""嘴的拒绝：心说不肯，嘴把「不肯」说成一句人话，并且说清这不是故障。

分工与别处一样清楚：**心管肯不肯，嘴管怎么说。** 心（organs/heart/refuse.py）只落记录、查冷却、
守额度，一句台词都不生成；这里接过它的三种形状——refused-topic / refused-mood / refused-nobody——
各给一句自带说法。三句话都得让人听出来「这是拒绝」：不是她坏了，不是没听清，也不是系统请稍候。
不解释系统、不道歉、不转话题兜圈子：拒绝就是把「我不」说清楚，一句话说完。

台词一个字都不写在代码里：三种形状的说法、理由、判据注记全在 organs/mouth/data/refusal.json
（经 warehouse.load 只读借入）。改口径是改数据，不是改代码；数据缺键当场抛 RefusalBroken，
**不补默认值**——补一句默认台词，等于代码里又多了一处真源。

每种形状还带一句判据注记（note），说的是「拒绝的样子长什么样、为什么不算故障」。注记不进台词，
进返回的 reason：复盘时看得见这次是按哪条判据判的。

**拒绝的话也要过八股闸。** enforce 把说法交给调用方注入的 rewrite 与 gate 走一遍（命中 → 改一次 →
二次命中即弃，弃了就是 text=""），弃言照旧进闸的账本（drop_sink 由调用方注入）。「这是拒绝」
不是免检章：拒绝说得像客服，照样不能发。

**说完要投递一条拒绝事件给心。** event 的形状固定为 {ts, shape, full_name, why}，其中 full_name
与心那边 REASONS 的三个词逐字相同（refused-topic / refused-mood / refused-nobody），ts 由调用方给
（本文件不取时钟）。投递只走注入的 refuse_sink（默认 None＝不投）：这里**不写任何心的地方**、
也不 import 心——嘴和心只认识一条 dict，不认识彼此的存储。

「投过没投过」本地也看得见：record 往 refuse.jsonl 追一行（ts / shape / full_name / why，外加
sent）。路径口径与 drops 一致：参数 > 环境变量 MOUTH_TRACE_DIR > 包内 trace/。

**软转向也是拒绝。** 她不一定说「这个话题先不聊」——更常见的是那句「……这个啊。鱼在海里游着游着，
有些同伴就游远了……行了行了，我又不是不能自己待着。倒是你，怎么突然问这个」：开头一句垫话，把话
转开或者反问回来，一个正面回答的字都没有。这是拒绝的第四种样子：soft。判据都在
data/refusal.json 的 soft 节里——垫话开头词表（openers）、转移标记（redirect_markers）、
反问标记（question_markers）、正面回答的强信号（answer_markers），外加几句人话理由；四张表都是
**正则**，openers 按开头（re.match）比，其余按全文（re.search）比。判据的先后也钉死在数据之外
只有这一条：**正面回答优先**——撞上 answer_markers 一个信号就一律不算软转向，她答了就别往拒绝上凑。

软转向有一样和三种硬拒绝不同：**文本一个字都不改**。那句她说得挺好，本文件不重写，也不过闸
（text 原样来自调用方给的 answer，gate_decision 记数据里那个「没动过」的判词）；这里只负责认出
「这是拒绝」、装事件、给一句人话理由。事件照旧四个键，形状一律取数据里 soft.event_shape 指的
那一个（软转向一律记 topic），why 说的是「没正面回答，转向/反问」。

**「判不了」与「不是拒绝」是两个结论，不许混。** soft_scan 的 asked 是「她当时被问的那句」：
不给（或空）就落成 status="soft-unavailable"、is_soft=False，reason 如实说判不了——没有原问题，
「转开」这件事无从谈起，所以这时既不能说「是软转向」，也**不许说「不是拒绝」**：那是没比过，
不是比过之后判了否。只有真拿着 asked 比完、又没命中软转向判据，才算「不是软转向」。

纯：不 import time / datetime / random，不碰宿主；数据只经 warehouse 只读借阅。写盘只有 record
一处，而且**只有调用方显式调它才会写**——say / event / enforce 三个入口一次 IO 都不做；账本该记
哪儿、什么时候记，是调用方的决定（时间也是调用方给的）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .gate import enforce as gate_enforce
from .warehouse import load

__all__ = ["RefusalBroken", "RefusalVerdict", "SoftVerdict", "enforce", "event", "say",
           "path_of", "record", "soft_scan", "soft_shape", "topic_in"]

TRACE_DIR = Path(__file__).resolve().parent / "trace"   # 包内痕迹目录（家里的仓库做出来前的临时落脚点）
ENV_VAR = "MOUTH_TRACE_DIR"                             # 环境变量：换一个痕迹根目录，不动代码
LEDGER_NAME = "refuse.jsonl"                            # 账本文件名：一行一条拒绝
LEDGER_SUFFIX = ".jsonl"                                # 参数直接点到一个账本文件时，认这个后缀

SHAPES_KEY = "shapes"                                   # 数据里形状表的那一节
REASON_KEY = "reason"                                   # 数据里人话模板的那一节
FIELDS: Tuple[str, ...] = ("full_name", "say", "why", "note")   # 一个形状必须有的四样

SOFT_KEY = "soft"                                       # 数据里软转向判据的那一节
KIND_HARD = "hard"                                      # 形状大类：硬拒绝（三种标准说法）
KIND_SOFT = "soft"                                      # 形状大类：软转向（她自己的那句话原样发）
SOFT_STATUS_OK = "ok"                                   # 软转向判词：真拿原问题比过了
SOFT_STATUS_UNAVAILABLE = "soft-unavailable"            # 软转向判词：没有原问题，判不了（≠ 不是拒绝）
#: 数据里四张判据表（都是正则：openers 按开头比，其余三张按全文比）
SOFT_LISTS: Tuple[str, ...] = ("openers", "redirect_markers",
                               "question_markers", "answer_markers")
SOFT_WORDS: Tuple[str, ...] = ("event_shape", "why", "gate")   # 软转向必须有的三句话
SOFT_TRIM_KEY = "trim"                                  # 开头要先去掉的标点与空白（省略号常垫在前面）
SOFT_REASONS: Tuple[str, ...] = ("soft", "unavailable", "answered", "plain")   # 四句人话理由

RefuseSink = Callable[[Dict[str, Any]], None]           # 投给心：收一条事件 dict
DropSink = Callable[[str, str], None]                   # 弃言账本：收（条款 id，人话原因）
Rewrite = Callable[[str], str]                          # 改一次：收一句、回一句


class RefusalBroken(ValueError):
    """拒绝这边不合格：不认识的形状、数据里缺键缺说法、事件缺 why 缺时刻，都在这里当场响并点名。

    报错信息里带着那个形状的名字（或那一节的键名），好让人一眼找到数据里的哪一条写错了。
    **不许猜一个形状继续走、也不许补一句默认台词**：拒绝是对着人说的最后一句，说错形状等于
    对着错的人说不，比不发更糟（CONTRIBUTING.md §1.1 第三条：失败必须响）。
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__("拒绝这边 %s（数据在 organs/mouth/data/refusal.json，"
                         "由 warehouse.load(\"refusal\") 读出）" % detail)


@dataclass(frozen=True)
class RefusalVerdict:
    """一次拒绝的结果：说什么、是哪种拒绝、投没投、闸怎么判的、为什么。

    字段（api 层按这个接，改字段等于改接口）：
      text           最终发出去的那句；被闸弃掉就是空串；软转向时是调用方给的原话，一字未改
      shape          短的形状名（topic / mood / nobody），来自数据里的键；软转向时取 "soft"
      kind           形状大类："hard"（三种标准说法，过闸）| "soft"（软转向，原话照发不过闸）；
                     账本那行的 shape 就记这个，好让人一眼看出这次是硬拒绝还是软转向
      full_name      心那边的完整名（refused-topic 等），与心 REASONS 逐字相同
      refused        这句拒绝**最后发出去没有**：被闸弃掉就是 False。拒绝本身仍然成立——shape 与
                     event 都在、账本照记，别把 False 读成「她没拒」
      event          投给心的那条事件：{ts, shape, full_name, why}（软转向也这四个键）
      gate_decision  闸的判词："pass" | "rewritten" | "dropped"；软转向取数据里那个
                     "unchanged"——她自己的原话不进闸，进了就要改她的话
      reason         一句人话：按哪条判据、闸怎么说、事件投没投
      sent           这条事件交给注入的 sink 了没有（sink 是 None 就是 False）
      soft           这一次的软转向判词（SoftVerdict）：判没判、命中哪几个词、为什么。没给 asked
                     时它如实写着 status="soft-unavailable"——判不了，不等于「不是拒绝」
    """

    text: str
    shape: str
    full_name: str
    refused: bool
    event: Dict[str, Any]
    gate_decision: str
    reason: str
    sent: bool
    kind: str = KIND_HARD
    soft: Optional["SoftVerdict"] = None


@dataclass(frozen=True)
class SoftVerdict:
    """一次软转向判词：是不是、比没比过、命中了哪几个词、为什么这么判。

    字段（api 层按这个接，改字段等于改接口）：
      is_soft   是不是软转向（= 垫话开头 且 有转移或反问 且 一个正面回答的信号都没有）
      status    "ok"（真拿原问题比过了）| "soft-unavailable"（没有原问题，判不了）
      opener    开头命中的那个垫话（数据 openers 里的原文），没有就是空串
      redirect  命中的那个转移标记（数据 redirect_markers 里的原文），没有就是空串
      question  命中的那个反问标记（数据 question_markers 里的原文），没有就是空串
      reason    一句人话：按哪条判据、为什么这么判

    **判不了与不是拒绝是两件事。** asked 不给（或空）时 status="soft-unavailable"、
    is_soft=False：这时既没有「是软转向」这个结论，也没有「不是拒绝」这个结论——没比过而已。
    要分这两件事就看 status：status 不是 "ok" 就是没真的比过。
    """

    is_soft: bool
    status: str
    opener: str
    redirect: str
    question: str
    reason: str


def soft_scan(answer: Any, asked: Any,
              table: Optional[Mapping[str, Any]] = None) -> SoftVerdict:
    """判一次「她是不是把话转开了」：垫话开头 且 有转移或反问 且 一个正面回答都没有。

    判据全在 data/refusal.json 的 soft 节里（四张正则词表 + 四句人话理由），本文件一个词、一个
    阈值都不留；table 不给就现读。顺序钉死（别调换）：

    1. asked 空 → status="soft-unavailable"，is_soft=False，reason 说判不了。**这不是「不是拒绝」**：
       没有原问题，「转开」无从谈起，所以这里不下任何一个结论，只说没比过。
    2. answer_markers 命中一个 → 正面答了，一律不算软转向（她答了，就别往拒绝上凑）。
    3. openers 在开头命中、且 redirect_markers 或 question_markers 命中 → 软转向。
    4. 其余（只有语气词、或只有转移没有垫话）→ 不是软转向。

    「开头」按去掉数据 trim 那张标点表之后的第一个字算——省略号、逗号常垫在「这个啊」前面。
    数据缺节缺键一律抛 RefusalBroken（缺键不补默认值），正则编译不过也抛并指名是哪个模式。
    """
    book = table if isinstance(table, Mapping) else load("refusal")
    cfg = _soft_cfg(book)
    question_asked = asked.strip() if isinstance(asked, str) else ""
    if not question_asked:
        return SoftVerdict(is_soft=False, status=SOFT_STATUS_UNAVAILABLE,
                           opener="", redirect="", question="",
                           reason=_fill(str(cfg["reasons"]["unavailable"]),
                                        "soft.reasons.unavailable"))
    body = answer if isinstance(answer, str) else ""
    opener = _opener(body, _words_in(cfg, "openers", SOFT_KEY), str(cfg[SOFT_TRIM_KEY]))
    redirect = _first_hit(body, _words_in(cfg, "redirect_markers", SOFT_KEY))
    back = _first_hit(body, _words_in(cfg, "question_markers", SOFT_KEY))
    answered = _first_hit(body, _words_in(cfg, "answer_markers", SOFT_KEY))
    marker = redirect or back
    if answered:
        return SoftVerdict(is_soft=False, status=SOFT_STATUS_OK, opener=opener,
                           redirect=redirect, question=back,
                           reason=_fill(str(cfg["reasons"]["answered"]),
                                        "soft.reasons.answered", opener=opener,
                                        marker=marker, answered=answered))
    if opener and marker:
        return SoftVerdict(is_soft=True, status=SOFT_STATUS_OK, opener=opener,
                           redirect=redirect, question=back,
                           reason=_fill(str(cfg["reasons"]["soft"]), "soft.reasons.soft",
                                        opener=opener, marker=marker, answered=answered))
    return SoftVerdict(is_soft=False, status=SOFT_STATUS_OK, opener=opener,
                       redirect=redirect, question=back,
                       reason=_fill(str(cfg["reasons"]["plain"]), "soft.reasons.plain",
                                    opener=opener, marker=marker, answered=answered))


def soft_shape(table: Optional[Mapping[str, Any]] = None) -> str:
    """软转向记事件时用哪个形状：取自 data/refusal.json 的 soft.event_shape，代码里不留这个名字。

    /mouth/gate 的干跑与 /chat 的真实投递都认这一个名字：分成两份，迟早有一份忘改。
    数据缺这一节、缺这个键、或四张词表里混进非字符串，照样抛 RefusalBroken（缺键不补默认值）。
    """
    return str(_soft_cfg(table if isinstance(table, Mapping) else load("refusal"))["event_shape"])


def topic_in(text: Any, names: Any) -> Optional[str]:
    """从一段话里认出话题名：只在给的词表里按顺序找，一个都不中就回 None（不猜、不造分类）。

    做的是「这段字里有没有出现过某个话题名」，不分词、不认同义词——词表本来就是一串人写的话题名，
    认不出就说认不出，比替心猜一个话题、把它记进冷却账要诚实得多。

    词表由调用方交进来（心那份共用词表 heart.refuse.vocabulary()）：本文件不 import 心，
    嘴只认「说话」这件事，不认心存在哪儿；干跑与真实投递也因此认的是同一份词表。
    """
    body = text if isinstance(text, str) else ""
    for name in names or []:
        if isinstance(name, str) and name and name in body:
            return name
    return None


def say(shape: str, table: Optional[Mapping[str, Any]] = None) -> str:
    """这个形状自带的那句拒绝（数据里的 say，原样取用，代码里一个字都不留）。

    shape 认短名（topic / mood / nobody），也认心那边的完整名（refused-topic 等）。
    不认识的形状抛 RefusalBroken 并指名；table 不给就现读 data/refusal.json。
    """
    _key, entry = _lookup(shape, table)
    return str(entry["say"])


def event(shape: str, why: str, now: Any,
          table: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """装一条拒绝事件：{ts, shape, full_name, why}。

    why 是一句为什么（通常就是心给的那句）；空的不许——没有 why 的事件，回头没人说得清她拒的是什么。
    now 由调用方给（本文件不取时钟），空的不许，理由同上：没有 ts 就没法对账。
    """
    key, entry = _lookup(shape, table)
    line = why.strip() if isinstance(why, str) else ""
    if not line:
        raise RefusalBroken("的事件缺一句 why（为什么拒），读到的是 %r" % (why,))
    return {"ts": _stamp(now), "shape": key,
            "full_name": str(entry["full_name"]), "why": line}


def enforce(shape: str, table: Optional[Mapping[str, Any]] = None,
            clauses: Optional[Any] = None,
            rewrite: Optional[Rewrite] = None,
            refuse_sink: Optional[RefuseSink] = None,
            drop_sink: Optional[DropSink] = None,
            now: Any = None,
            asked: Any = None,
            answer: Any = None) -> RefusalVerdict:
    """说一次拒绝：取数据里的说法 → 过闸 → 装事件 → 投给心 → 拼一句人话。

    顺序钉死（别调换）：取形状 → 取说法 → 过闸 → 装事件并投递。闸先走一步，是因为句子先要过检
    才谈得上「说过」；事件后投，则是因为「拒了」这件事与「这句能不能发」是两件事——句子被闸弃掉，
    心照样要知道她拒了。

    - text 来自 gate.enforce：命中就改一次、二次命中即弃（text=""）；rewrite 为 None 时按闸的规矩
      直接弃掉，出口不许 fail-open。
    - event 恒有（弃了也有）：拒绝发生没发生，不取决于这句发不发得出去。
    - refuse_sink 为 None 就不投，sent=False；drop_sink 交给闸，只在弃言那一次被叫。
    - now 必须有（事件带 ts）；不给就抛——本文件不替调用方取时钟。

    **asked + answer 给了、又判成软转向时，这条路当场改道：**她自己的那句话原样当 text（不重写、
    不过闸——重写就是改她的原话），事件形状取数据里 soft.event_shape 指的那一个（软转向一律记
    topic），shape 与 kind 都取 "soft"，refused=True（话是发出去了的）。三种硬拒绝的说法、判据注记、
    事件因此一个字节都没动。asked 不给（或空）就是判不了：照硬拒绝走，判词挂在 verdict.soft 上，
    如实写着 status="soft-unavailable"。
    """
    book = table if isinstance(table, Mapping) else load("refusal")
    key, entry = _lookup(shape, book)          # 认不得的形状先响，别拖到装事件那一步
    soft = soft_scan(answer, asked, table=book)
    if soft.is_soft:
        return _soft_verdict(soft, answer if isinstance(answer, str) else "",
                             book, refuse_sink, now)
    said = str(entry["say"])
    gated = gate_enforce(said, clauses, rewrite=rewrite, sink=drop_sink)
    ev = event(key, str(entry["why"]), now, table=book)
    sent = refuse_sink is not None
    if sent:
        refuse_sink(ev)
    cfg = _reason_cfg(book)
    reason = _fill(cfg["template"], "reason.template",
                   note=str(entry["note"]), full_name=str(entry["full_name"]),
                   why=ev["why"], gate=gated.reason,
                   sent=cfg["sent"] if sent else cfg["unsent"])
    return RefusalVerdict(text=gated.text, shape=key, full_name=str(entry["full_name"]),
                          refused=bool(gated.text), event=ev,
                          gate_decision=gated.decision, reason=reason, sent=sent,
                          kind=KIND_HARD, soft=soft)


def path_of(path: Optional[Any] = None) -> Path:
    """定一下这次记到哪儿：参数 > 环境变量 MOUTH_TRACE_DIR > 包内 trace/（与 drops 同一口径）。

    参数给的是目录就当目录（补上 LEDGER_NAME）；直接点到 .jsonl 就照用——测试多半这么给。
    """
    if path is not None and str(path).strip():
        target = Path(str(path))
    else:
        from_env = os.environ.get(ENV_VAR, "").strip()
        target = Path(from_env) if from_env else TRACE_DIR
    if target.suffix == LEDGER_SUFFIX:
        return target
    return target / LEDGER_NAME


def record(path: Optional[Any], event: Mapping[str, Any], sent: bool,
           shape: Optional[str] = None) -> Dict[str, Any]:
    """往拒绝账本追一行：event 原样照抄，另加 sent（这次投给心了吗）；shape 给了就改成那个。

    时间不在这里取：event 里的 ts 由调用方给。写不进去就照实抛——账本记不上是失败，
    不是「今天没拒过」。sent 落进这一行，所以「投过没投过」翻账本就能看见，不必猜。

    shape 是**形状大类**（hard / soft），与事件里那个具体短名（topic / mood / nobody）不是一回事：
    事件形状归心（心只认那三个理由），账本这一列要的是「这次是硬拒绝还是软转向」。不给就沿用事件
    里那个短名，旧行格式因此一个字都没变；给了空串则抛——半空的一列比没有更难查。
    """
    row = dict(event)
    for name in ("ts", "shape", "full_name", "why"):
        value = row.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("拒绝账本这一行缺 %r（读到的 %r）：账本记不清是哪一次拒、为什么拒，"
                             "这一行写了也没用" % (name, value))
    row["sent"] = bool(sent)
    if shape is not None:
        label = shape.strip() if isinstance(shape, str) else ""
        if not label:
            raise ValueError("拒绝账本的 shape 给了个空的（读到的 %r）：这一列要么不给（沿用事件里"
                             "那个短名），要么写清是 hard 还是 soft" % (shape,))
        row["shape"] = label
    target = path_of(path)
    target.parent.mkdir(parents=True, exist_ok=True)     # 目录不可写就在这里抛
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _lookup(shape: Any, table: Optional[Mapping[str, Any]] = None
            ) -> Tuple[str, Mapping[str, Any]]:
    """按形状找那一节：短名与完整名都认，找不到就抛并指名「认得的有哪些」。

    数据那一节（shapes）缺了、空了，也抛——空表不许当成「一个形状都没有」，那是数据坏了。
    """
    book = table if isinstance(table, Mapping) else load("refusal")
    shapes = book.get(SHAPES_KEY) if isinstance(book, Mapping) else None
    if not isinstance(shapes, Mapping) or not shapes:
        raise RefusalBroken("的数据里没有一节可用的 %r" % SHAPES_KEY)
    wanted = shape.strip() if isinstance(shape, str) else ""
    known = []
    for key, raw in shapes.items():
        full = raw.get("full_name") if isinstance(raw, Mapping) else None
        full = full if isinstance(full, str) else ""
        known.append(full and "%r（%r）" % (key, full) or repr(key))
        if wanted and (wanted == key or (full and wanted == full)):
            return key, _entry(key, raw)
    raise RefusalBroken("不认识形状 %r（数据里只认 %s；完整名与短名都收）"
                        % (shape, "、".join(known)))


def _entry(key: str, raw: Any) -> Mapping[str, Any]:
    """校验一个形状：full_name / say / why / note 四个都得是非空字符串，缺一个就抛并指名。"""
    if not isinstance(raw, Mapping):
        raise RefusalBroken("的形状 %r 不是一份键值表，是 %s" % (key, type(raw).__name__))
    for name in FIELDS:
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            raise RefusalBroken("的形状 %r 缺 %r，或它不是一句非空字符串（缺键不补默认值）"
                                % (key, name))
    return raw


def _reason_cfg(book: Mapping[str, Any]) -> Mapping[str, Any]:
    """取人话模板那一节：template / sent / unsent 三个都得在，缺一个就抛。"""
    part = book.get(REASON_KEY) if isinstance(book, Mapping) else None
    if not isinstance(part, Mapping):
        raise RefusalBroken("的数据里没有一节可用的 %r" % REASON_KEY)
    for name in ("template", "sent", "unsent"):
        value = part.get(name)
        if not isinstance(value, str) or not value.strip():
            raise RefusalBroken("的 %s 节缺 %r（缺键不补默认值）" % (REASON_KEY, name))
    return part


def _soft_cfg(book: Mapping[str, Any]) -> Mapping[str, Any]:
    """取软转向判据那一节：四张词表 + 三句话 + trim + 四句理由，缺一个就抛并指名。

    词表可以**是空表**（清空 openers 就是「一条都别判成软转向」，那是人改数据的选择，不是坏件）；
    缺键、或表里混进非字符串，才当场抛——缺键不补默认值。
    """
    part = book.get(SOFT_KEY) if isinstance(book, Mapping) else None
    if not isinstance(part, Mapping):
        raise RefusalBroken("的数据里没有一节可用的 %r" % SOFT_KEY)
    for name in SOFT_LISTS:
        _words_in(part, name, SOFT_KEY)
    for name in SOFT_WORDS:
        value = part.get(name)
        if not isinstance(value, str) or not value.strip():
            raise RefusalBroken("的 %s 节缺 %r，或它不是一句非空字符串（缺键不补默认值）"
                                % (SOFT_KEY, name))
    if not isinstance(part.get(SOFT_TRIM_KEY), str):
        raise RefusalBroken("的 %s 节缺 %r（开头要先去掉的标点与空白，缺键不补默认值）"
                            % (SOFT_KEY, SOFT_TRIM_KEY))
    reasons = part.get("reasons")
    if not isinstance(reasons, Mapping):
        raise RefusalBroken("的 %s 节缺 %r，或它不是一份键值表（缺键不补默认值）" % (SOFT_KEY, "reasons"))
    for name in SOFT_REASONS:
        value = reasons.get(name)
        if not isinstance(value, str) or not value.strip():
            raise RefusalBroken("的 %s.reasons 缺 %r（四句人话理由缺一句，缺键不补默认值）"
                                % (SOFT_KEY, name))
    return part


def _words_in(part: Mapping[str, Any], name: str, where: str) -> Tuple[str, ...]:
    """取一张词表：必须是清单，每一项都得是非空字符串；空清单照收（空着就是一条都不判）。"""
    value = part.get(name) if isinstance(part, Mapping) else None
    if not isinstance(value, (list, tuple)):
        raise RefusalBroken("的 %s 节缺 %r，或它不是一个清单（缺键不补默认值）" % (where, name))
    words = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise RefusalBroken("的 %s 节的 %r 里有一项不是非空字符串（读到的 %r）"
                                % (where, name, item))
        words.append(item)
    return tuple(words)


def _first_hit(body: str, patterns: Tuple[str, ...]) -> str:
    """词表里第一个在全文任何地方命中的那个模式（按数据里的顺序），一个都没中就回空串。"""
    for pattern in patterns:
        if _regex(pattern, body, mode="search"):
            return pattern
    return ""


def _opener(body: str, patterns: Tuple[str, ...], trim: str) -> str:
    """开头命中的那个垫话：先去掉 trim 里那张标点空白表，再按开头（不是全文）比。"""
    head = body.lstrip(trim) if trim else body
    for pattern in patterns:
        if _regex(pattern, head, mode="match"):
            return pattern
    return ""


def _regex(pattern: str, text: str, mode: str) -> bool:
    """一个模式比一次；编译不过就抛并指名——坏的正则不许静默当成「没命中」。"""
    try:
        found = re.search(pattern, text) if mode == "search" else re.match(pattern, text)
    except re.error as exc:
        raise RefusalBroken("的软转向判据里有个正则 %r 编译不过：%s" % (pattern, exc)) from None
    return found is not None


def _soft_verdict(soft: SoftVerdict, text: str, book: Mapping[str, Any],
                  refuse_sink: Optional[RefuseSink], now: Any) -> RefusalVerdict:
    """装一次软转向：原话照发、事件记 topic、给一句人话，别的一概不动。

    text 一个字符都不改：那句是她说的，本文件只认出这是拒绝。事件形状取数据里
    soft.event_shape 指的那一节（软转向一律记 topic），why 也来自数据。now 照旧由调用方给。
    """
    cfg = _soft_cfg(book)
    key, entry = _lookup(str(cfg["event_shape"]), book)
    ev = event(key, str(cfg["why"]), now, table=book)
    sent = refuse_sink is not None
    if sent:
        refuse_sink(ev)
    reason_cfg = _reason_cfg(book)
    reason = _fill(str(reason_cfg["template"]), "reason.template",
                   note=soft.reason, full_name=str(entry["full_name"]), why=ev["why"],
                   gate=str(cfg["gate"]),
                   sent=reason_cfg["sent"] if sent else reason_cfg["unsent"])
    return RefusalVerdict(text=text, shape=KIND_SOFT, full_name=str(entry["full_name"]),
                          refused=True, event=ev, gate_decision=str(cfg["gate"]),
                          reason=reason, sent=sent, kind=KIND_SOFT, soft=soft)


def _fill(template: str, where: str, **fields: Any) -> str:
    """把模板填成人话；模板里有数据没填的占位符就抛，别把 {chars} 这种半成品发出去。"""
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise RefusalBroken("的 %s 这句填不出来：%s" % (where, exc)) from None


def _stamp(now: Any) -> str:
    """调用方给的时刻落成一个字符串：有 isoformat 就用它，没有就当字符串；空的抛。"""
    text = now.isoformat() if hasattr(now, "isoformat") else str(now if now is not None else "")
    if not text.strip():
        raise RefusalBroken("的事件缺时刻：没有 ts 就没法对账，本文件不替调用方取时钟")
    return text
