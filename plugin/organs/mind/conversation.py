"""核的装配入口：一句话进来 → 装配 → 发模型 → 过嘴 → 落脑，出去一整轮。

这是**全仓唯一的装配点**：QQ 通道将来直接调 answer()，不再自己拼第二遍。装配的规矩
一个数都不写在本文件里，全在 organs/mind/data/assembly.json：

  order      四段拼装的先后（soul / memory / heart / mouth）
  sections   {key: {"label": 段落标题, "cap": 字数上限}}
  prompt     system / user 两段模板（占位符见下）
  reply      budget_by_tier（五档词 ⟷ 回复字数）+ default + instruction
读法同 llm.json：实盘 <仓库>/home/organs/mind/assembly.json 优先（存在才读），否则读
出厂 organs/mind/data/assembly.json。

prompt 的两段模板各认几个占位符（花括号，装配时逐键**纯替换**，不做 format——
数据里多一个花括号不该把这一轮炸掉）：
  system  {sections}          四段拼好的正文，每段带 sections[key].label 当标题
          {reply_budget}      这一档的回复字数上限（来自 reply.budget_by_tier）
          {reply_instruction} 回复口径那句话（来自 reply.instruction）
  user    {text}              本人这一次的原话

三拍的顺序是钉死的：**先回想、再发模型、最后写记忆**。
先回想，是因为这一轮该想起什么要在写提示词之前定下来；写记忆放在最后，是因为
只有真说出口的那一句才配当亲历——被闸弃掉的话不进脑（它压根没说出口）。

各段只搬已有器官的读数，一个判据都不自己造：
  soul    store.render()（魂六件）
  memory  brain.recall(query=text, who=who or None)：既进提示词，也原样进返回体
          recalled[]（页面要显示「想起了什么 + 证据指针」）
  heart   effective.effective_state（全仓唯一合成点）+ engage.decide（只 decide，
          **不 decide_and_log**）；提示词与返回体都**只给档位词**，一个数都不出门
  mouth   address.resolve + tone.from_store（口径行进提示词），过闸用 gate.enforce
          （重写器传 None：本轮命中即弃，不进第二次模型调用），节奏用 split + pace.fill

坏卡不是崩溃：mood 照读，effective_state 抛 CardError 就降级成只要 mood 那一块——
与 api/heart.py 的既有口径同一条（面板能显示「拒载」，这一轮照旧说得出话）。

LLMError 不在这里翻译：原样上抛，由 api/chat.py 按 kind 翻成状态码与人话。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .. import paths
from ..brain import model as brain_model
from ..heart import card as card_module
from ..heart import effective as effective_module
from ..heart import energy as energy_module
from ..heart import engage as engage_module
from ..heart import model as heart_model
from ..heart import mood as mood_module
from ..heart import offset as offset_module
from ..heart import ports as ports_module
from ..mouth import address as address_module
from ..mouth import drops as drops_module
from ..mouth import gate as gate_module
from ..mouth import pace as pace_module
from ..mouth import tone as tone_module
from ..mouth import warehouse
from ..mouth.split import split as split_text
from . import llm

ASSEMBLY_NAME = "assembly.json"          # 装配说明书的文件名（实盘 / 出厂各一份）
HUMAN_WHO = "人"                          # 没给 who 时，人的原话记在谁名下
DEFAULT_WHO = "主人"                      # 门的读数要一个名字：没给就按主人算（与心·api 同一个兜底）
PENDING_WORD = "待定"                     # 待定轴（耐心 / 记性）没有档位词，照 effective.render_text 的说法
REPLY_KEY = "reply"                       # reply_budget.key：这笔预算出自 assembly.json 的哪一节
NO_MEMORY = "这次没想起什么。"
SEED_SEP = "|"                            # 节奏的种子：who 与时刻拼起来，同一轮两次调用得到同一组间隔


# —— 装配说明书 ———————————————————————————————————


def load_assembly(path: Optional[Any] = None) -> Dict[str, Any]:
    """读装配说明书：实盘覆盖（存在才读）→ 出厂数据；path 给定时只读那一份。

    两份都没有是错，不是「用默认值悄悄跑起来」——装配顺序与字数上限没有默认值。
    """
    if path is not None:
        return _read_assembly(Path(path))
    live = paths.organ_dir("mind") / ASSEMBLY_NAME
    if live.is_file():
        return _read_assembly(live)
    factory = paths.factory_dir("mind") / ASSEMBLY_NAME
    if factory.is_file():
        return _read_assembly(factory)
    raise ValueError("找不到装配说明：%s 与 %s 都不存在" % (live, factory))


def _read_assembly(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("读不了装配说明 %s：%s" % (path, exc)) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError("装配说明不是 JSON：%s：%s" % (path, exc)) from exc
    return _check_assembly(data, str(path))


def _check_assembly(data: Any, where: str) -> Dict[str, Any]:
    """校验并归一化：缺节、缺键、类型不对一律当场报——数据里没有的规矩不许在代码里补。"""
    if not isinstance(data, dict):
        raise ValueError("装配说明不是一份对象：%s" % where)
    order = data.get("order")
    if not isinstance(order, list) or not order:
        raise ValueError("装配说明缺 order（四段的先后）：%s" % where)
    sections = data.get("sections")
    if not isinstance(sections, dict) or not sections:
        raise ValueError("装配说明缺 sections（每段的名字与字数上限）：%s" % where)
    checked: Dict[str, Dict[str, Any]] = {}
    for key in order:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("order 里有一项不是段落名：%r（%s）" % (key, where))
        spec = sections.get(key)
        if not isinstance(spec, dict):
            raise ValueError("sections 里缺 %r 这一节：%s" % (key, where))
        label = spec.get("label")
        cap = spec.get("cap")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("sections.%s 缺 label（段落标题）：%s" % (key, where))
        if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
            raise ValueError("sections.%s 的 cap 要正整数：%s" % (key, where))
        checked[key] = {"label": label.strip(), "cap": cap}
    prompt = data.get("prompt")
    if not isinstance(prompt, dict):
        raise ValueError("装配说明缺 prompt（system / user 两段模板）：%s" % where)
    for name in ("system", "user"):
        if not isinstance(prompt.get(name), str) or not prompt.get(name).strip():
            raise ValueError("prompt.%s 不能为空：%s" % (name, where))
    reply = data.get("reply")
    if not isinstance(reply, dict):
        raise ValueError("装配说明缺 reply（回复预算）：%s" % where)
    budgets = reply.get("budget_by_tier")
    if not isinstance(budgets, dict) or not budgets:
        raise ValueError("reply.budget_by_tier 要一份非空的档位表：%s" % where)
    clean_budgets: Dict[str, int] = {}
    for tier, chars in budgets.items():
        if isinstance(chars, bool) or not isinstance(chars, int) or chars <= 0:
            raise ValueError("reply.budget_by_tier.%s 要正整数：%s" % (tier, where))
        clean_budgets[str(tier)] = chars
    default = reply.get("default")
    if isinstance(default, bool) or not isinstance(default, int) or default <= 0:
        raise ValueError("reply.default 要正整数：%s" % where)
    instruction = reply.get("instruction")
    if not isinstance(instruction, str):
        raise ValueError("reply.instruction 要一段字：%s" % where)
    return {
        "order": [key.strip() for key in order],
        "sections": checked,
        "prompt": {"system": prompt["system"], "user": prompt["user"]},
        "reply": {"budget_by_tier": clean_budgets, "default": default,
                  "instruction": instruction},
    }


# —— 唯一入口 ————————————————————————————————————


def answer(text: str, who: str = "", *, store: Any = None, brain: Any = None,
           heart_root: Any = None, now: Optional[str] = None,
           transport: Optional[Any] = None, config: Optional[Mapping[str, Any]] = None,
           env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """装配一轮并说出口：返回页面要的那份成功体；模型这边的失败原样上抛（由 api 翻译）。

    store / brain 是底线：没有魂就没有底色，没有记忆就不开口——这里当场报，不假装说了一轮。
    now 给了就按它算（测试把时间往前推靠它），没给就用器官自己的钟（brain.model.now_iso）。
    transport / config / env 三个注入点原样转给 llm.complete：测试一律注入假 transport，
    绝不发真网络请求；本文件不写死任何模型名、地址与 key。

    返回（键名对外冻结，pages/chat.html 照它渲染）：
      ok / reply / chunks[{text,gap_before,send_at}] / recalled[{id,text,source,
      source_type,evidence_key,score,who}] / heart{mood{tier,direction},
      voice{tone,direction,bans}, axes{轴: 档位词}, engage{state,reason}}
      / gate{decision, hit{clause_id,reason}|null, drops{today}} /
      assembly{order, sections[{key,chars,cap}], reply_budget{key,tier,chars}} /
      memory{wrote[{id,who}]}
    """
    if store is None:
        raise ValueError("魂未载入（store=None）：没有魂就没有装配的底色")
    if brain is None:
        raise ValueError("脑未载入（brain=None）：没有记忆就不开口")
    said = str(text or "").strip()
    if not said:
        raise ValueError("text 不能为空：没有一句话就没有这一轮")
    conf = load_assembly()
    at = now if isinstance(now, str) and now else brain_model.now_iso()

    # 1) 魂与脑：先回想（这一轮该想起什么，要在写提示词之前定下来）
    soul_text = store.render()
    recalled, memory_text = _recall(brain, said, who)

    # 2) 心：唯一合成点 + 一次判决；坏卡降级成只要 mood 那一块
    mood_read = mood_module.read(heart_root, at)
    offsets = offset_module.overview(root=heart_root, now=at)
    eff = _effective(heart_root, at, mood_read, offsets)
    energy_read = energy_module.read(heart_root, at)
    judgement = _engage(heart_root, at, eff, energy_read, brain, who)
    heart_words = _heart_words(eff, judgement)
    heart_text = _heart_text(eff, heart_words)

    # 3) 嘴：称呼 → 语气档（口径行进提示词）
    voice = _voice(store, brain, who, mood_read, energy_read)
    mouth_text = _mouth_text(voice)

    # 4) 提示词与回复预算
    tier = heart_words["mood"]["tier"]
    budget = conf["reply"]["budget_by_tier"].get(tier, conf["reply"]["default"])
    contents = {"soul": soul_text, "memory": memory_text,
                "heart": heart_text, "mouth": mouth_text}
    blocks: List[str] = []
    sections: List[Dict[str, Any]] = []
    for key in conf["order"]:
        spec = conf["sections"][key]
        body = contents.get(key, "")
        clipped = body if len(body) <= spec["cap"] else body[:spec["cap"]]
        blocks.append("[%s]\n%s" % (spec["label"], clipped))
        sections.append({"key": key, "chars": len(clipped), "cap": spec["cap"]})
    system = _fill(conf["prompt"]["system"], {
        "sections": "\n\n".join(blocks),
        "reply_budget": str(budget),
        "reply_instruction": conf["reply"]["instruction"],
    })
    user = _fill(conf["prompt"]["user"], {"text": said})
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    # 5) 发模型（LLMError 原样上抛）
    said_back = llm.complete(messages, config=config, env=env, transport=transport)

    # 6) 过嘴：切分 + 节奏 + 闸（重写器 None：本轮命中即弃）
    batch, verdict, ledger = _pass_mouth(said_back, who, at)
    dropped = verdict.decision == "dropped"
    reply = "" if dropped else verdict.text
    chunks = [] if dropped else [{"text": part.text, "gap_before": part.gap_before,
                                  "send_at": part.send_at} for part in batch.parts]
    hit = verdict.hits[0] if verdict.hits else None

    # 7) 落脑：人的原话先记，她真说出口的那句再记（被弃的那句没说出口，不进脑）
    wrote = _remember(brain, store, who, said, reply, dropped, at)

    return {
        "ok": True,
        "reply": reply,
        "chunks": chunks,
        "recalled": recalled,
        "heart": heart_words,
        "gate": {
            "decision": verdict.decision,
            "hit": {"clause_id": hit.clause_id, "reason": hit.reason} if hit else None,
            "drops": {"today": int(ledger.get("today") or 0)},
        },
        "assembly": {
            "order": list(conf["order"]),
            "sections": sections,
            "reply_budget": {"key": REPLY_KEY, "tier": tier, "chars": budget},
        },
        "memory": {"wrote": wrote},
    }


# —— 各段 ————————————————————————————————————


def _recall(brain: Any, said: str, who: str) -> Tuple[List[Dict[str, Any]], str]:
    """脑装包：既进提示词，也原样进返回体（页面要显示「想起了什么 + 证据指针」）。"""
    result = brain.recall(query=said, who=who or None)
    items = result.get("items") or []
    recalled = [{"id": item.get("id", ""),
                 "text": item.get("text", ""),
                 "source": item.get("source", ""),
                 "source_type": item.get("source_type", ""),
                 "evidence_key": item.get("evidence_key", ""),
                 "score": item.get("score", 0.0),
                 "who": item.get("who", "")} for item in items]
    if not recalled:
        return recalled, NO_MEMORY
    lines = ["- %s（来源：%s｜来源类型：%s｜证据：%s）"
             % (row["text"], row["source"], row["source_type"],
                row["evidence_key"] or "无") for row in recalled]
    return recalled, "\n".join(lines)


def _effective(heart_root: Any, at: str, mood_read: Mapping[str, Any],
               offsets: Any) -> Dict[str, Any]:
    """心·唯一合成点；坏卡降级成只要 mood 一块（与 api/heart.py 的既有口径一致）。"""
    try:
        return effective_module.effective_state(
            mood_read["baseline"], mood_read["now"],
            offset=offsets, root=heart_root, now=at)
    except card_module.CardError:
        return {"mood": effective_module.mood_block(mood_read["baseline"],
                                                    mood_read["now"])}


def _engage(heart_root: Any, at: str, eff: Mapping[str, Any],
            energy_read: Mapping[str, Any], brain: Any, who: str) -> Dict[str, Any]:
    """一次判决：只 decide，不 decide_and_log（核不写门的日志，那是心的账）。"""
    readout = ports_module.from_brain(brain)
    name = who or DEFAULT_WHO
    judgement = engage_module.EngageInput.from_effective(
        dict(eff),
        window=engage_module.window_signal(heart_root, at),
        energy=energy_read["value"],
        familiarity=readout.familiarity(name, at),
        today_count=readout.today_count(name, at),
        intent=heart_model.INTENT_PASSIVE,
        who=name,
        now=at,
    )
    return engage_module.decide(judgement)


def _heart_words(eff: Mapping[str, Any], judgement: Mapping[str, Any]) -> Dict[str, Any]:
    """心这一块**只给档位词**：两个数（now / baseline）一律不出门。

    坏卡降级时没有 voice 那一块：tone 就取 mood 的档位词——effective.py 里 voice.tone
    本来就是 mood 的档，两者同源，这一格不是第二套换算。
    """
    mood_read = eff.get("mood") or {}
    voice = eff.get("voice") or {}
    mood_tier = mood_read.get("tier")
    mood_direction = mood_read.get("direction")
    return {
        "mood": {"tier": mood_tier, "direction": mood_direction},
        "voice": {"tone": voice.get("tone", mood_tier),
                  "direction": voice.get("direction", mood_direction),
                  "bans": [str(ban) for ban in (voice.get("bans") or [])]},
        "axes": _axis_words(eff.get("axes") or {}),
        "engage": {"state": judgement.get("state"), "reason": judgement.get("reason")},
    }


def _axis_words(rows: Mapping[str, Any]) -> Dict[str, str]:
    """六轴各一个档位词：成对轴取面上那一档，待定轴照实写「待定」（不写数、不写 0）。"""
    words: Dict[str, str] = {}
    for axis, row in rows.items():
        if not isinstance(row, Mapping) or row.get("greyed"):
            words[str(axis)] = PENDING_WORD
            continue
        words[str(axis)] = str(row.get("shown_word") or PENDING_WORD)
    return words


def _heart_text(eff: Mapping[str, Any], heart_words: Mapping[str, Any]) -> str:
    """心这一段进提示词的样子：还是只有词——心里/面上两个档位词也是词，不是数。"""
    lines = ["心境：%s（%s）" % (heart_words["mood"]["tier"], heart_words["mood"]["direction"]),
             "语气：%s（%s）" % (heart_words["voice"]["tone"], heart_words["voice"]["direction"])]
    bans = heart_words["voice"]["bans"]
    lines.append("禁令：%s" % ("；".join(bans) if bans else "没有"))
    axes = eff.get("axes") or {}
    words: List[str] = []
    for axis, row in axes.items():
        if not isinstance(row, Mapping) or row.get("greyed"):
            words.append("%s：%s" % (axis, PENDING_WORD))
            continue
        words.append("%s：心里%s，面上%s"
                     % (axis, row.get("inner_word") or PENDING_WORD,
                        row.get("shown_word") or PENDING_WORD))
    lines.append("轴：%s" % ("；".join(words) if words else "没有"))
    engage = heart_words["engage"]
    lines.append("参与：%s（%s）" % (engage["state"] or "没说参与态",
                                    engage["reason"] or "没有拦下的理由"))
    return "\n".join(lines)


def _voice(store: Any, brain: Any, who: str, mood_read: Mapping[str, Any],
           energy_read: Mapping[str, Any]) -> Any:
    """嘴的语气档：称呼走人名录（与 api/mouth.py 同一个用法），心的读数原样交过去。"""
    table = address_module.load_table()
    person = brain.lookup(who) if who else None
    called = address_module.resolve(person, table)
    heart = {"mood": mood_read["now"], "energy": energy_read["value"],
             "offset": None, "engage": None}
    return tone_module.from_store(store, called, heart=heart, table=table)


def _mouth_text(voice: Any) -> str:
    """嘴这一段进提示词的样子：称呼、语气档、口径行、禁不禁开玩笑（都是词）。"""
    lines = ["称呼：%s（关系档：%s）" % (voice.address, voice.relation),
             "语气档：%s" % voice.label,
             "口径行：%s" % voice.line,
             "禁玩笑：%s" % ("禁" if voice.ban_joke else "不禁")]
    return "\n".join(lines)


def _pass_mouth(reply: str, who: str, at: str) -> Tuple[Any, Any, Dict[str, Any]]:
    """过嘴：切分 + 节奏（seed 稳定）+ 八股闸（重写器 None：本轮命中即弃）。

    弃言记进 organs/mouth/drops.py 的账本：闸只报「哪条条款、为什么」，时刻与落盘都在
    这一层给（照 api/mouth.py 的 _sink / _day 写法）。
    """
    policy = warehouse.load("policy")
    clauses = warehouse.load("clauses").get("clauses")
    batch = pace_module.fill(split_text(reply, policy), policy,
                             "%s%s%s" % (who or "", SEED_SEP, at))
    trace = drops_module.path_of()
    verdict = gate_module.enforce(reply, clauses, None, _sink(trace))
    ledger = drops_module.report(trace, _day())
    return batch, verdict, ledger


def _sink(trace: Any) -> Any:
    """把闸的弃言接到账本上：闸只报「哪条条款、哪句原因」，时刻在这里给。"""
    def note(clause_id: str, reason: str) -> None:
        drops_module.record(trace, clause_id, reason, datetime.now())
    return note


def _day() -> str:
    """今天的日期（ISO，十位）：账本按这个前缀认「今天」。"""
    return datetime.now().date().isoformat()


def _remember(brain: Any, store: Any, who: str, said: str, reply: str,
              dropped: bool, at: str) -> List[Dict[str, str]]:
    """落脑：人的原话先记；她真说出口的那句再记（被弃的那句没说出口，不进脑）。

    时刻与 schema 全走脑自己的 ingest：这里不新开表、不另造一行形状。
    id 用的是 ingest 回来的 evidence_key——亲历这一条的 id 就是它自己的证据指针。
    """
    wrote: List[Dict[str, str]] = []
    human = brain.ingest(who=who or HUMAN_WHO, text=said, ts=at)
    if human.get("ok"):
        wrote.append(_wrote_row(human))
    if dropped or not reply:
        return wrote
    hers = brain.ingest(who=store.snapshot().identity.display, text=reply, ts=at)
    if hers.get("ok"):
        wrote.append(_wrote_row(hers))
    return wrote


def _wrote_row(result: Mapping[str, Any]) -> Dict[str, str]:
    """一行「落了谁的哪条」：id 取证据指针，带上这条记在谁名下。"""
    entry = result.get("entry") or {}
    return {"id": str(entry.get("evidence_key") or entry.get("id") or ""),
            "who": str(entry.get("who") or "")}


def _fill(template: str, values: Mapping[str, str]) -> str:
    """模板里逐键纯替换：只换这次给了的那几个花括号，别的字符一个都不动。"""
    out = str(template)
    for key, value in values.items():
        out = out.replace("{%s}" % key, str(value))
    return out
