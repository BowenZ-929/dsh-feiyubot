"""核·识别：这一句话对她意味着什么（只出事件名，不出一个 mood 数值）。

五类事件的名字与心的枚举是同一份东西（organs/heart/model.py 的 model.EVENTS）：
praise（夸）/ care（关心）/ cold（冷落）/ insult（被骂）/ rejected（被拒绝）。
这里只回答"是哪一类"，推多少由心按契约权重与冷却自己拍——数值一个都不许从本模块出去。

输入只有三样（外加两个可选窗口）：
- text          这一段的原话（可以是发言段拼起来的几行）；
- last_reply    上一轮她自己的回话（没有人说过话就空串），rejected 的问答关系要用它；
- scene         group / private / unknown；
- recent_replies（可选）最近几轮她自己的回话，老的在前、新的在后——cold 的窗口判据要用它；
  不给就退化成 [last_reply]，窗口凑不满 N 轮 → 不判 cold（**不拿一轮当三轮**）。

输出 {"kind", "who", "note", "evidence"}：kind 是五类之一或 None；note 是一句给账看的
依据（哪条规则命中、命中的词），evidence 是命中的原话片段。

三条铁律：
1. **识别不出来就 kind=None，一个字都不许猜**。text 为空或全是标点 → 一票否决；
   某个 kind 的场景开关里没有这一场的名字 → 就当它不存在（unknown 不在任何开关里，
   所以"场合说不清"就只认那些把 unknown 写进开关的 kind，而现在一个都没有）。
2. 所有词表、阈值、问句规则、场景开关、连给账看的 note 模板都在 data/events.json，
   本文件里没有任何中文词表：改数据就改行为，代码不跟着动。
3. cold 是**窗口判据，不是单词**：最近 N 轮里她的回话都是"极短应答"（长度阈值 + 词表）
   才判 cold。窗口 N、长度阈值、应答词表三样都在数据里。

is_insult(text) 是**全仓唯一的「被骂」判据**：心的 engage 硬门只看调用方传进来的
insulted_today（organs/heart/engage.py:139，理由是 model.REASON_INSULTED），接那一门
与投 insult 事件用的必须是同一个布尔，否则"她今天被骂了"会出现两份结论。它与
recognize 里 insult 那一路走的是同一个 _insult_hits（同一份词表、同一票否决），
两条路同结论；scene 只决定"认不认"，is_insult 不接 scene——被骂这件事本身与场合无关。

账本（本模块另一半职责）：每轮一行落 <mind 器官目录>/event_log.jsonl，
{"at", "kind", "who", "scene", "mode", "applied", "reason", "note"}。
- **不识别也要写一行**（kind 写空串、applied=false、reason="没识别出事件"）——
  "没识别出"与"被心拒了"是两件事，账上必须分得开；
- mode：这一轮是人发消息触发的 = passive，她自己开口 = proactive（调用方传，缺省 passive）；
- 投递优先走 heart.report_event(kind, who, at, note)；**入口不存在（AttributeError /
  没有这个属性）时不许假装成功**：applied=false、reason="heart.report_event 未落地"，
  照样落一行。心回 {"applied": false, "reason": ...} 时照实写它的 applied 与 reason；
- 账本只追加、不改写；读的时候坏行跳过（rows）。
"""
from __future__ import annotations

import json
import re
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import paths

# 两份文件的落点：规则读出厂/实盘，账本只写实盘。
DATA_NAME = "events.json"
LOG_NAME = "event_log.jsonl"

# 五类事件名：与 model.EVENTS 同一份枚举（名字只在数据文件里再出现一次，做校验）。
KIND_PRAISE = "praise"
KIND_CARE = "care"
KIND_COLD = "cold"
KIND_INSULT = "insult"
KIND_REJECTED = "rejected"

SCENE_GROUP = "group"
SCENE_PRIVATE = "private"
SCENE_UNKNOWN = "unknown"

MODE_PASSIVE = "passive"
MODE_PROACTIVE = "proactive"
MODES = (MODE_PASSIVE, MODE_PROACTIVE)

REASON_NO_EVENT = "没识别出事件"
REASON_NO_ENTRY = "heart.report_event 未落地"
REASON_NO_APPLIED = "心回的账里没有 applied"

HER = "她"  # evidence 里她那几句按谁的名下摆着（这里只是拼串，不落账）

_REQUIRED = ("punct", "clause_seps", "priority", "scenes", "kinds")
_RULE_TYPES = ("words", "window", "refusal")


# ---- 规则数据：出厂一份，实盘可覆盖（存在才读） --------------------------


def data_path_factory() -> Path:
    """出厂规则：plugin/organs/mind/data/events.json（只读参数）。"""
    return paths.factory_dir("mind") / DATA_NAME


def data_path_live() -> Path:
    """实盘规则：<home>/organs/mind/events.json（存在才读，坏了就报错）。"""
    return paths.organ_dir("mind") / DATA_NAME


def _read_config(path: Path) -> Dict[str, Any]:
    """读一份规则并校验；读不了 / 结构不对一律 ValueError（不悄悄用默认值跑起来）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            "读不了识别规则 %s（%s：%s）" % (path, type(exc).__name__, exc))
    return _check_config(data, str(path))


def _check_config(data: Any, where: str) -> Dict[str, Any]:
    """校验规则表：缺节、priority 里的 kind 没规则、kind 没场景开关，都是数据错。"""
    if not isinstance(data, dict):
        raise ValueError("识别规则不是一份对象：%s" % (where,))
    for key in _REQUIRED:
        if key not in data:
            raise ValueError("识别规则缺 %r 这一节：%s" % (key, where))
    priority = data["priority"]
    kinds = data["kinds"]
    scenes = data["scenes"]
    if not isinstance(priority, list) or not priority:
        raise ValueError("识别规则的 priority 必须是非空列表：%s" % (where,))
    if not isinstance(kinds, dict) or not isinstance(scenes, dict):
        raise ValueError("识别规则的 kinds / scenes 必须是对象：%s" % (where,))
    for kind in priority:
        rule = kinds.get(kind)
        if not isinstance(rule, dict):
            raise ValueError(
                "priority 里的 %r 在 kinds 里没有规则：%s" % (kind, where))
        if rule.get("rule") not in _RULE_TYPES:
            raise ValueError(
                "%r 的 rule 只认 %s，收到 %r：%s"
                % (kind, "、".join(_RULE_TYPES), rule.get("rule"), where))
        if not isinstance(scenes.get(kind), list):
            raise ValueError("scenes 里没有 %r 的场景开关：%s" % (kind, where))
        if not isinstance(rule.get("note"), str) or not rule["note"]:
            raise ValueError("%r 缺 note（给账看的一句依据）：%s" % (kind, where))
    return data


def load_config(path: Optional[Any] = None) -> Dict[str, Any]:
    """读规则表：path 给了只读那一份；否则实盘（存在才读）→ 出厂。

    两份都没有是配置错，不是"不识别"——静默返回空词表等于让她对什么都无感。
    """
    if path is not None:
        return _read_config(Path(path))
    live = data_path_live()
    if live.is_file():
        return _read_config(live)
    factory = data_path_factory()
    if factory.is_file():
        return _read_config(factory)
    raise ValueError("找不到识别规则：%s 与 %s 都不存在" % (live, factory))


def _config(data: Optional[Any] = None, data_path: Optional[Any] = None) -> Dict[str, Any]:
    """取这次要用的规则：注入的字典 > 注入的路径 > 实盘/出厂。"""
    if isinstance(data, dict):
        return _check_config(data, "传入的 data")
    if data is not None:
        raise ValueError("data 只收规则表（dict），收到 %r" % (type(data).__name__,))
    return load_config(data_path)


# ---- 小工具：否决、命中、片段 --------------------------------------------


def _core(text: str, punct: str) -> str:
    """去掉标点与空白之后剩下什么；空串就是"除了标点什么都没有"。"""
    return "".join(ch for ch in text if ch not in punct and not ch.isspace())


def _vetoed(text: Any, cfg: Dict[str, Any]) -> bool:
    """一票否决：text 不是字符串 / 空的 / 全是标点空白 → 这一句话什么都不算。"""
    if not isinstance(text, str) or not text.strip():
        return True
    return _core(text, cfg["punct"]) == ""


def _hits(text: Any, words: Any) -> List[str]:
    """词表命中：按数据里的次序返回命中的词（不重复、不做同义扩张）。"""
    if not isinstance(text, str) or not text or not words:
        return []
    low = text.lower()
    found: List[str] = []
    for word in words:
        if isinstance(word, str) and word and word.lower() in low and word not in found:
            found.append(word)
    return found


def _snippet(text: str, words: Sequence[str], seps: str) -> str:
    """命中的那句原话片段：按分隔符切句，取第一句含命中词的；找不到就整段。"""
    pattern = "[" + re.escape(seps) + "]"
    for clause in re.split(pattern, text):
        piece = clause.strip()
        if piece and any(word.lower() in piece.lower() for word in words):
            return piece
    return text.strip()


def _fmt(rule: Dict[str, Any], **kw: Any) -> str:
    """note 模板填空：模板在数据里，填进去的是命中的词/窗口里的应答。"""
    try:
        return str(rule["note"]).format(**kw)
    except (KeyError, IndexError, ValueError):
        return str(rule["note"])


# ---- 五类各自的判据（口径全在数据里） ------------------------------------


def _insult_hits(text: Any, cfg: Dict[str, Any]) -> List[str]:
    """被骂的命中词：**engage 的 insulted_today 与投 insult 事件共用这一个口径**。"""
    if _vetoed(text, cfg):
        return []
    return _hits(text, cfg["kinds"][KIND_INSULT].get("words"))


def _judge_words(rule: Dict[str, Any], text: str, last_reply: str,
                 replies: Optional[Sequence[Any]], cfg: Dict[str, Any]
                 ) -> Optional[Tuple[str, str]]:
    """词表类（praise / care / insult）：这一段原话里出现词表里的词就命中。"""
    words = _hits(text, rule.get("words"))
    if not words:
        return None
    return _fmt(rule, words="、".join(words)), _snippet(text, words, cfg["clause_seps"])


def _reply_pool(last_reply: Any, replies: Optional[Sequence[Any]]) -> List[str]:
    """她那几句回话的窗口：显式给了 recent_replies 就用它，否则退化成 [last_reply]。"""
    if replies is not None:
        pool = [item for item in replies if isinstance(item, str) and item.strip()]
        return pool
    if isinstance(last_reply, str) and last_reply.strip():
        return [last_reply]
    return []


def _judge_window(rule: Dict[str, Any], text: str, last_reply: str,
                  replies: Optional[Sequence[Any]], cfg: Dict[str, Any]
                  ) -> Optional[Tuple[str, str]]:
    """窗口类（cold）：最近 N 轮她的回话都短过长阈值且都在应答词表里，才判冷落。

    窗口凑不满 N 轮 → None（一轮"嗯"不是"三个嗯"）；有一轮是长句 → None
    （她说过话就不算冷落）；应答一个都不在词表里 → None。
    """
    n = int(rule.get("window") or 0)
    max_len = int(rule.get("max_len") or 0)
    words = rule.get("words") or []
    pool = _reply_pool(last_reply, replies)
    if n <= 0 or len(pool) < n:
        return None
    recent = pool[-n:]
    kept: List[str] = []
    for reply in recent:
        core = _core(reply, cfg["punct"])
        if len(core) > max_len or core not in words:
            return None
        kept.append(core)
    window_text = "/".join(kept)
    return _fmt(rule, n=n, replies=window_text), window_text


def _judge_refusal(rule: Dict[str, Any], text: str, last_reply: str,
                   replies: Optional[Sequence[Any]], cfg: Dict[str, Any]
                   ) -> Optional[Tuple[str, str]]:
    """拒绝类（rejected）：这一轮明确回绝，且她上一轮确实在问她/在给提议。

    两件都要有：text 里出现回绝词；last_reply 里出现问句/提议标志。缺一条就是
    "说不清"，一律 None——宁可这一轮不记，也不许替对方认一句"她拒绝了"。
    """
    refuse = _hits(text, rule.get("refuse"))
    if not refuse:
        return None
    offer = _hits(last_reply, rule.get("offer"))
    if not offer:
        return None
    note = _fmt(rule, refuse="、".join(refuse), offer="、".join(offer))
    return note, _snippet(text, refuse, cfg["clause_seps"])


_JUDGES = {
    "words": _judge_words,
    "window": _judge_window,
    "refusal": _judge_refusal,
}


# ---- 识别 ----------------------------------------------------------------


def recognize(
    text: Any,
    last_reply: Any = "",
    scene: Any = SCENE_UNKNOWN,
    who: Any = "主人",
    recent_replies: Optional[Sequence[Any]] = None,
    data: Optional[Any] = None,
    data_path: Optional[Any] = None,
) -> Dict[str, Any]:
    """这一段原话对她意味着什么：{kind, who, note, evidence}，认不出就 kind=None。

    判据的次序就是数据里 priority 的次序（谁先命中谁说了算，不并列、不投票）：
    先看被骂，再看被拒绝，再看冷落窗口，然后夸、关心。场景开关按 kind 逐条查
    （scenes[kind] 里没有这一场 → 这个 kind 这一轮不参与）；text 空或全标点 →
    一票否决，返回 kind=None，**不猜**。
    """
    cfg = _config(data, data_path)
    speaker = who if isinstance(who, str) and who else "主人"
    result: Dict[str, Any] = {"kind": None, "who": speaker, "note": "", "evidence": ""}
    if _vetoed(text, cfg):
        return result
    here = scene if isinstance(scene, str) else SCENE_UNKNOWN
    for kind in cfg["priority"]:
        if here not in cfg["scenes"][kind]:
            continue
        rule = cfg["kinds"][kind]
        judge = _JUDGES[rule["rule"]]
        verdict = judge(rule, text, last_reply if isinstance(last_reply, str) else "",
                        recent_replies, cfg)
        if not verdict:
            continue
        note, evidence = verdict
        result.update({"kind": kind, "note": note, "evidence": evidence})
        return result
    return result


def is_insult(text: Any, data: Optional[Any] = None,
              data_path: Optional[Any] = None) -> bool:
    """这句话算不算"被骂"——**全仓唯一的「被骂」判据**。

    心的 engage 硬门只看调用方传进来的 insulted_today（organs/heart/engage.py:139，
    理由是 model.REASON_INSULTED），投 insult 事件也认同一个事实：两处都必须调这里，
    谁也不许另写一张脏话表——两边分叉的那一刻，"她今天被骂了没有"就有两份答案。
    与 recognize 的 insult 一路共用 _insult_hits（同一份词表、同一票否决），所以
    同一句话两条路同结论；本函数不接 scene：被骂这件事本身与场合无关。
    """
    return bool(_insult_hits(text, _config(data, data_path)))


# ---- 账本：每轮一行，投递如实 --------------------------------------------


def log_path(root: Optional[Any] = None) -> Path:
    """账本落点：root 给了就用（测试一律给临时目录），否则 <home>/organs/mind/。"""
    base = Path(root) if root is not None else paths.organ_dir("mind")
    return base / LOG_NAME


def _stamp(at: Optional[Any]) -> str:
    """这一笔的时刻：调用方给了就用它，没给就是此刻（本地时区、秒精度）。"""
    if isinstance(at, str) and at:
        return at
    from datetime import datetime
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _default_report() -> Any:
    """心里的那个投递口：heart.report_event(kind, who, at, note)。

    拿不到（包级没有这个属性 / 整个包里没有这个模块）就 AttributeError 冒上去，
    由 _deliver 记成"未落地"——**不许拿一个假的成功糊过去**。
    """
    from .. import heart
    entry = getattr(heart, "report_event", None)
    if entry is None:
        raise AttributeError("organs.heart 没有 report_event")
    return entry


def _deliver(kind: str, who: str, at: str, note: str, report: Optional[Any]
             ) -> Tuple[bool, str]:
    """把事件名投给心，返回 (applied, reason) —— 三点都照实说，一个都不许编。

    - 投递口不存在（AttributeError）→ (False, "heart.report_event 未落地")；
    - 投递口抛异常 → (False, "heart.report_event 抛错：<类型>")，异常不外传；
    - 心回字典且带 applied → 照它写（applied=false 就写 false，reason 用它的）；
    - 心回字典但没有 applied → (False, "心回的账里没有 applied")；
    - 心回 None / 无返回体 → (True, "")：调用没炸就是接住了，不等于失败。
    """
    if report is not None:
        entry = report
    else:
        try:
            entry = _default_report()
        except AttributeError:
            return False, REASON_NO_ENTRY
    try:
        answer = entry(kind, who, at=at, note=note)
    except Exception as exc:  # 投递是旁路，不许把这一轮整死
        warnings.warn(
            "核·events：投递 %r 失败（%s：%s），账上照实记" % (kind, type(exc).__name__, exc),
            RuntimeWarning, stacklevel=3)
        return False, "heart.report_event 抛错：%s" % (type(exc).__name__,)
    if answer is None:
        return True, ""
    if isinstance(answer, dict):
        if "applied" not in answer:
            return False, REASON_NO_APPLIED
        reason = answer.get("reason")
        return bool(answer.get("applied")), reason if isinstance(reason, str) else ""
    return True, ""


def _append(row: Dict[str, Any], root: Optional[Any] = None) -> Path:
    """只追加一行；写失败只 WARN（账是旁证，不是这一轮的前提）。"""
    path = log_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        warnings.warn(
            "核·events：事件账写失败（%s：%s），这一轮不受影响"
            % (type(exc).__name__, exc),
            RuntimeWarning, stacklevel=3)
    return path


def record(
    text: Any,
    last_reply: Any = "",
    scene: Any = SCENE_UNKNOWN,
    who: Any = "主人",
    at: Optional[Any] = None,
    mode: Any = MODE_PASSIVE,
    recent_replies: Optional[Sequence[Any]] = None,
    root: Optional[Any] = None,
    report: Optional[Any] = None,
    data: Optional[Any] = None,
    data_path: Optional[Any] = None,
) -> Dict[str, Any]:
    """识别 + 投递 + 落一行账，返回写下去的那一行（形状固定八个字段）。

    **每轮都落一行**，不识别也落（kind="" / applied=false / reason="没识别出事件"）：
    "这一轮什么都没发生"和"这一轮被心拒了"在账上必须分得开。
    mode：人发消息触发的 = passive，她自己开口 = proactive（调用方传，别的一律按 passive）。
    report：投递口注入口（测试用它假造"心回 applied:false"那一支）；不给就走真入口。
    """
    cfg = _config(data, data_path)
    moment = _stamp(at)
    mode_value = mode if mode in MODES else MODE_PASSIVE
    where = scene if isinstance(scene, str) and scene else SCENE_UNKNOWN
    result = recognize(text, last_reply=last_reply, scene=where, who=who,
                       recent_replies=recent_replies, data=cfg)
    kind = result.get("kind") or ""
    note = result.get("note") or ""
    if not kind:
        applied, reason = False, REASON_NO_EVENT
    else:
        applied, reason = _deliver(kind, result["who"], moment, note, report)
    row = {
        "at": moment,
        "kind": kind,
        "who": result["who"],
        "scene": where,
        "mode": mode_value,
        "applied": bool(applied),
        "reason": reason,
        "note": note,
    }
    _append(row, root)
    return row


def rows(root: Optional[Any] = None) -> List[Dict[str, Any]]:
    """读回账本：只认 JSON 对象，坏行/空行跳过（读不下去的一行不该让她闭口）。"""
    path = log_path(root)
    if not path.is_file():
        return []
    kept: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            kept.append(item)
    return kept
