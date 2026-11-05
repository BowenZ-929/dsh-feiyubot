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

纯：不 import time / datetime / random，不碰宿主；数据只经 warehouse 只读借阅。写盘只有 record
一处，而且**只有调用方显式调它才会写**——say / event / enforce 三个入口一次 IO 都不做；账本该记
哪儿、什么时候记，是调用方的决定（时间也是调用方给的）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .gate import enforce as gate_enforce
from .warehouse import load

__all__ = ["RefusalBroken", "RefusalVerdict", "enforce", "event", "say",
           "path_of", "record"]

TRACE_DIR = Path(__file__).resolve().parent / "trace"   # 包内痕迹目录（家里的仓库做出来前的临时落脚点）
ENV_VAR = "MOUTH_TRACE_DIR"                             # 环境变量：换一个痕迹根目录，不动代码
LEDGER_NAME = "refuse.jsonl"                            # 账本文件名：一行一条拒绝
LEDGER_SUFFIX = ".jsonl"                                # 参数直接点到一个账本文件时，认这个后缀

SHAPES_KEY = "shapes"                                   # 数据里形状表的那一节
REASON_KEY = "reason"                                   # 数据里人话模板的那一节
FIELDS: Tuple[str, ...] = ("full_name", "say", "why", "note")   # 一个形状必须有的四样

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
      text           最终发出去的那句；被闸弃掉就是空串
      shape          短的形状名（topic / mood / nobody），来自数据里的键
      full_name      心那边的完整名（refused-topic 等），与心 REASONS 逐字相同
      refused        这句拒绝**最后发出去没有**：被闸弃掉就是 False。拒绝本身仍然成立——shape 与
                     event 都在、账本照记，别把 False 读成「她没拒」
      event          投给心的那条事件：{ts, shape, full_name, why}
      gate_decision  闸的判词："pass" | "rewritten" | "dropped"
      reason         一句人话：按哪条判据、闸怎么说、事件投没投
      sent           这条事件交给注入的 sink 了没有（sink 是 None 就是 False）
    """

    text: str
    shape: str
    full_name: str
    refused: bool
    event: Dict[str, Any]
    gate_decision: str
    reason: str
    sent: bool


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
            now: Any = None) -> RefusalVerdict:
    """说一次拒绝：取数据里的说法 → 过闸 → 装事件 → 投给心 → 拼一句人话。

    顺序钉死（别调换）：取形状 → 取说法 → 过闸 → 装事件并投递。闸先走一步，是因为句子先要过检
    才谈得上「说过」；事件后投，则是因为「拒了」这件事与「这句能不能发」是两件事——句子被闸弃掉，
    心照样要知道她拒了。

    - text 来自 gate.enforce：命中就改一次、二次命中即弃（text=""）；rewrite 为 None 时按闸的规矩
      直接弃掉，出口不许 fail-open。
    - event 恒有（弃了也有）：拒绝发生没发生，不取决于这句发不发得出去。
    - refuse_sink 为 None 就不投，sent=False；drop_sink 交给闸，只在弃言那一次被叫。
    - now 必须有（事件带 ts）；不给就抛——本文件不替调用方取时钟。
    """
    book = table if isinstance(table, Mapping) else load("refusal")
    key, entry = _lookup(shape, book)
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
                          gate_decision=gated.decision, reason=reason, sent=sent)


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


def record(path: Optional[Any], event: Mapping[str, Any], sent: bool) -> Dict[str, Any]:
    """往拒绝账本追一行：event 原样照抄，另加 sent（这次投给心了吗）。

    时间不在这里取：event 里的 ts 由调用方给。写不进去就照实抛——账本记不上是失败，
    不是「今天没拒过」。sent 落进这一行，所以「投过没投过」翻账本就能看见，不必猜。
    """
    row = dict(event)
    for name in ("ts", "shape", "full_name", "why"):
        value = row.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("拒绝账本这一行缺 %r（读到的 %r）：账本记不清是哪一次拒、为什么拒，"
                             "这一行写了也没用" % (name, value))
    row["sent"] = bool(sent)
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
