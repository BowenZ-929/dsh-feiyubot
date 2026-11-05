"""核的装配入口：一句话进来 → 装配 → 发模型 → 过嘴 → 落脑，出去一整轮。

这是**全仓唯一的装配点**：QQ 通道将来直接调 answer()，不再自己拼第二遍。装配的规矩
一个数都不写在本文件里，全在 organs/mind/data/assembly.json：

  order      各段拼装的先后（soul / anchors / recent / banned / action / memory / heart / mouth）
  sections   {key: {"label": 段落标题, "cap": 字数上限}}
  prompt     system / user 两段模板（占位符见下）
  reply      budget_by_tier（五档词 ⟷ 回复字数）+ default + instruction
  recent     turns（读几轮）/ cap（每侧截多少字）/ notice（「更早还有 N 轮」那句模板）
  banned     rounds（抽她最近几轮）/ opening_chars（开场取几个字）/ note（那句禁令怎么说）
  action     {"pool": 部位池, "rule": 写法规矩, "quota": 配额}：动作给池子，但配额说了算
  anchors    {"label", "cap", "room", "name"}：锚句从家·哪一间房的哪一张卡读
读法同 llm.json：实盘 <仓库>/home/organs/mind/assembly.json 优先（存在才读），否则读
出厂 organs/mind/data/assembly.json。

prompt 的两段模板各认几个占位符（花括号，装配时逐键**纯替换**，不做 format——
数据里多一个花括号不该把这一轮炸掉）：
  system  {sections}          各段拼好的正文，每段带 sections[key].label 当标题
          {reply_budget}      这一档的回复字数上限（来自 reply.budget_by_tier）
          {reply_instruction} 回复口径那句话（来自 reply.instruction）
  user    {text}              本人这一次的原话

三拍的顺序是钉死的：**先回想、再发模型、最后写记忆**。
先回想，是因为这一轮该想起什么要在写提示词之前定下来；写记忆放在最后，是因为
只有真说出口的那一句才配当亲历——被闸弃掉的话不进脑（它压根没说出口）。

**口癖护栏**（anchors.guard）也守同一条次序：装配之前先读 usage.report 的滚动窗口，
再按它的 injected 决定这一轮注入几条半句（命中率超阈值就降到 reduced_count 条）；
她真说出口之后才把这一轮的「开头」追进 organs/mind/usage.py 那本账——先记后算会自己踩
自己，被闸弃掉的轮（reply 空）不记：她没说出口，就没有开头。护栏的六个门槛全在
assembly.json 的 anchors.guard 里，本文件一个数都不写。

各段只搬已有器官的读数，一个判据都不自己造：
  soul    store.render()（魂六件）
  anchors 家·仓库里那张锚句卡（organs.home.cards.read_item 的公开 API，不绕过家的 access 闸）；
          没配 / 卡不在 / 读不出来一律写「未配锚句」，不静默跳过——那一格空着她会以为没有过
  recent  organs/mind/recent.py：最近几轮原文；omitted > 0 时把「更早还有 N 轮」放在段尾
  banned  organs/mind/repeat.py：只抽**她那一侧**最近几轮的动作行与开场，去重计次列成禁令——
          最近几轮那段原文一个字都不删，禁令另起一节；人的话一个字都不许进来
  action  部位池、写法规矩与配额（assembly.json 的 action，代码里没有部位词与本层的配额数）；
          配额按「最近 quota.window 轮里有几轮带动作」算（repeat.count_with_actions）——
          已达 quota.max 就只写 quota.blocked 那一句，池子与规矩一个字都不给
  memory  brain.recall(query=text, who=who or None)：既进提示词，也原样进返回体
          recalled[]（页面要显示「想起了什么 + 证据指针 + 她的理解」）。每条记忆进提示词是
          两行：原文一行（括号里是来源｜来源类型｜证据），她的理解 note_display 另起一行、
          缩进两格——原话与她的理解不许糊成一行，那是脑里分开写的两栏
  heart   effective.effective_state（全仓唯一合成点）+ engage.decide（只 decide，
          **不 decide_and_log**）；提示词与返回体都**只给档位词**，一个数都不出门
  mouth   address.resolve + tone.from_store（口径行进提示词），过闸用 gate.enforce
          （重写器传 None：本轮命中即弃，不进第二次模型调用），节奏用 split + pace.fill

坏卡不是崩溃：mood 照读，effective_state 抛 CardError 就降级成只要 mood 那一块——
与 api/heart.py 的既有口径同一条（面板能显示「拒载」，这一轮照旧说得出话）。

温度分档也在这里定：档位（默认 / 事实）与触发条件全在 data/llm.json 的 temperature_tiers，
本轮 recalled 非空且回复预算不超过阈值就降成事实档，实际用的那个温度传进 llm.complete，
并原样进返回体的 llm.temperature / llm.temperature_tier。

LLMError 不在这里翻译：原样上抛，由 api/chat.py 按 kind 翻成状态码与人话。
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .. import paths
from ..brain import model as brain_model
from ..home import access as home_access
from ..home import anchors as home_anchors
from ..home import cards
from ..home import store as home_store
from ..heart import card as card_module
from ..heart import effective as effective_module
from ..heart import energy as energy_module
from ..heart import engage as engage_module
from ..heart import intent as intent_module
from ..heart import model as heart_model
from ..heart import mood as mood_module
from ..heart import offset as offset_module
from ..heart import ports as ports_module
from ..heart import sleep as sleep_module
from ..mouth import address as address_module
from ..mouth import drops as drops_module
from ..mouth import gate as gate_module
from ..mouth import pace as pace_module
from ..mouth import tone as tone_module
from ..mouth import warehouse
from ..mouth.split import split as split_text
from . import llm
from . import recent
from . import repeat
from . import usage

ASSEMBLY_NAME = "assembly.json"          # 装配说明书的文件名（实盘 / 出厂各一份）
HUMAN_WHO = "人"                          # 没给 who 时，人的原话记在谁名下
DEFAULT_WHO = "主人"                      # 门的读数要一个名字：没给就按主人算（与心·api 同一个兜底）
PENDING_WORD = "待定"                     # 待定轴（耐心 / 记性）没有档位词，照 effective.render_text 的说法
REPLY_KEY = "reply"                       # reply_budget.key：这笔预算出自 assembly.json 的哪一节
NO_MEMORY = "这次没想起什么。"
NO_QUOTE = "（无原话，只有她的理解）"        # 原话空、只有她的理解那一行：第一行不能空着（空行会被读成「没想起」）
NOTE_INDENT = "  "                        # 她的理解另起一行、缩进两格：与原文那行一眼分开，又不另起一节
NO_TURNS = "还没有更早的对话"                # 盘上一条历史都没有时，recent 段不写空白
ANCHORS_MISSING = "未配锚句"                  # 没配 / 卡不在 / 读不出来，三态同一句话
ANCHORS_HINT = "锚句由人写，程序不替她造。"
SEED_SEP = "|"                            # 节奏的种子：who 与时刻拼起来，同一轮两次调用得到同一组间隔
POOL_SEP = "／"                            # 动作池进提示词时部位之间的分隔（全角斜杠，与标点同一套）
BANNED_EMPTY = "最近几轮没有可禁用的动作行与开场。"   # 一条都抽不出来时的实话（空白会被她读成「随便用」）


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
                  "instruction": instruction,
                  "format": _check_rules(reply.get("format"), "format", where),
                  "format_blocked": _check_rules(reply.get("format_blocked"),
                                                 "format_blocked", where)},
        "recent": _check_recent(data.get("recent"), where),
        "banned": _check_banned(data.get("banned"), where),
        "action": _check_action(data.get("action"), where),
        "anchors": _check_anchors(data.get("anchors"), where),
    }


def _check_rules(spec: Any, name: str, where: str) -> List[str]:
    """reply 那两份说话规矩（format / format_blocked）同形：字符串列表，条目不许空。

    两份都要在数据里：blocked 那一态换的是**整份规矩**（动作那条从「怎么写」改成「不许写」），
    不在这里兜底补一条——数据缺了就当场报，缺的那一句会自动补成默认的话，人改不动它。
    """
    if not isinstance(spec, list):
        raise ValueError("reply.%s 要是字符串列表（空列表 = 不加规矩）：%s" % (name, where))
    clean: List[str] = []
    for rule in spec:
        if not isinstance(rule, str) or not rule.strip():
            raise ValueError("reply.%s 里有空的规矩：%s" % (name, where))
        clean.append(rule.strip())
    return clean


def _check_recent(spec: Any, where: str) -> Dict[str, Any]:
    """最近几轮那一段的三条规矩：读几轮、每侧截多少字、段尾那句模板。"""
    if not isinstance(spec, dict):
        raise ValueError("装配说明缺 recent（读几轮 / 截多少字 / notice 模板）：%s" % where)
    clean: Dict[str, Any] = {}
    for name in ("turns", "cap"):
        value = spec.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("recent.%s 要正整数：%s" % (name, where))
        clean[name] = value
    template = spec.get("notice")
    if not isinstance(template, str) or not template.strip():
        raise ValueError("recent.notice 要一句非空的模板：%s" % where)
    if recent.NOTE_SLOT not in template:
        raise ValueError("recent.notice 里缺 %s（不写占位符，那句话就永远是同一句假话）：%s"
                         % (recent.NOTE_SLOT, where))
    clean["notice"] = template
    return clean


def _check_banned(spec: Any, where: str) -> Dict[str, Any]:
    """近轮禁用那一格：抽她最近几轮、开场取几个字、那句禁令怎么说——三个都在数据里。"""
    if not isinstance(spec, dict):
        raise ValueError("装配说明缺 banned（rounds / opening_chars / note）：%s" % where)
    clean: Dict[str, Any] = {}
    for name in ("rounds", "opening_chars"):
        value = spec.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("banned.%s 要正整数：%s" % (name, where))
        clean[name] = value
    note = spec.get("note")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("banned.note 要一句非空的禁令：%s" % where)
    clean["note"] = note.strip()
    return clean


def _check_action(spec: Any, where: str) -> Dict[str, Any]:
    """动作池那一格：池子、规矩、配额都在数据里，代码里一个部位词、一个配额数都不许写死。"""
    if not isinstance(spec, dict):
        raise ValueError("装配说明缺 action（pool / rule / quota）：%s" % where)
    pool = spec.get("pool")
    if not isinstance(pool, list) or not pool:
        raise ValueError("action.pool 要一份非空的部位列表：%s" % where)
    clean_pool: List[str] = []
    for item in pool:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("action.pool 里有一项不是部位名：%r（%s）" % (item, where))
        clean_pool.append(item.strip())
    rule = spec.get("rule")
    if not isinstance(rule, str) or not rule.strip():
        raise ValueError("action.rule 要一句话（写法规矩，人能改）：%s" % where)
    return {"pool": clean_pool, "rule": rule.strip(),
            "quota": _check_quota(spec.get("quota"), where)}


def _check_quota(spec: Any, where: str) -> Dict[str, Any]:
    """动作配额的四个键：window / max 两个数，blocked / sparing 两句话。

    max 不大于 window 是硬规矩：窗口里一共只有 window 轮，配额比它还大就等于永远不禁，
    那四个键也就是摆设了。blocked / sparing 不许空——空话进提示词等于这一轮没给规矩。
    """
    if not isinstance(spec, dict):
        raise ValueError("action.quota 要给 {window, max, blocked, sparing}（动作配额的四个键）：%s"
                         % where)
    clean: Dict[str, Any] = {}
    for name in ("window", "max"):
        value = spec.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("action.quota.%s 要正整数：%s" % (name, where))
        clean[name] = value
    if clean["max"] > clean["window"]:
        raise ValueError("action.quota.max 不能大于 window（%d > %d）：%s"
                         % (clean["max"], clean["window"], where))
    for name in ("blocked", "sparing"):
        value = spec.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("action.quota.%s 要一句非空的话：%s" % (name, where))
        clean[name] = value.strip()
    return clean


def _check_anchors(spec: Any, where: str) -> Optional[Dict[str, Any]]:
    """锚句那一格：**可以没有**（没有就是「未配锚句」），写了就要写全。

    来源固定是家·仓库那五张台词卡（organs.home.anchors 的公开读口）：这一格不再认
    room / name —— 认了就成第二处「台词从哪来」的真源。
    """
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise ValueError("anchors 要一份对象（label / cap / inject / instruction）：%s" % where)
    clean: Dict[str, Any] = {}
    label = spec.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("anchors.label 要一段非空的字：%s" % where)
    clean["label"] = label.strip()
    cap = spec.get("cap")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise ValueError("anchors.cap 要正整数：%s" % where)
    clean["cap"] = cap
    inject = spec.get("inject")
    if not isinstance(inject, dict):
        raise ValueError("anchors.inject 要给 {fraction, suffix}（「只给半句」的规矩）：%s" % where)
    fraction = inject.get("fraction")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float))             or not 0 < float(fraction) <= 1:
        raise ValueError("anchors.inject.fraction 要是 (0, 1] 之间的数：%s" % where)
    suffix = inject.get("suffix")
    if not isinstance(suffix, str) or not suffix.strip():
        raise ValueError("anchors.inject.suffix 要一段非空的字（半句后面接什么）：%s" % where)
    clean["inject"] = {"fraction": float(fraction), "suffix": suffix}
    instruction = spec.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("anchors.instruction 要一句话（注入给她的行为规矩，人能改）：%s" % where)
    clean["instruction"] = instruction.strip()
    clean["guard"] = _check_guard(spec.get("guard"), where)
    return clean


def _check_guard(spec: Any, where: str) -> Dict[str, Any]:
    """口癖护栏的六个门槛：缺一个、类型不对一律当场报——代码里不许有这些数的默认值。

    min_rounds 允许 0（等于不设小样本门槛），其余五个都必须是正整数；(0, 1] 只给阈值。
    """
    if not isinstance(spec, dict):
        raise ValueError("anchors.guard 要给 {window, threshold, opening_chars, "
                         "prefix_chars, reduced_count, min_rounds}（口癖护栏的门槛）：%s" % where)
    clean: Dict[str, Any] = {}
    for name in ("window", "opening_chars", "prefix_chars", "reduced_count"):
        value = spec.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("anchors.guard.%s 要正整数：%s" % (name, where))
        clean[name] = value
    min_rounds = spec.get("min_rounds")
    if isinstance(min_rounds, bool) or not isinstance(min_rounds, int) or min_rounds < 0:
        raise ValueError("anchors.guard.min_rounds 要非负整数（防小样本误触，0 等于不设门槛）：%s"
                         % where)
    clean["min_rounds"] = min_rounds
    threshold = spec.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
            or not 0 < float(threshold) <= 1:
        raise ValueError("anchors.guard.threshold 要是 (0, 1] 之间的数：%s" % where)
    clean["threshold"] = float(threshold)
    return clean


# —— 唯一入口 ————————————————————————————————————


def answer(text: str, who: str = "", *, store: Any = None, brain: Any = None,
           heart_root: Any = None, recent_root: Any = None, usage_root: Any = None,
           now: Optional[str] = None,
           transport: Optional[Any] = None, config: Optional[Mapping[str, Any]] = None,
           env: Optional[Mapping[str, str]] = None,
           assembly: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """装配一轮并说出口：返回页面要的那份成功体；模型这边的失败原样上抛（由 api 翻译）。

    store / brain 是底线：没有魂就没有底色，没有记忆就不开口——这里当场报，不假装说了一轮。
    now 给了就按它算（测试把时间往前推靠它），没给就用器官自己的钟（brain.model.now_iso）。
    transport / config / env 三个注入点原样转给 llm.complete：测试一律注入假 transport，
    绝不发真网络请求；本文件不写死任何模型名、地址与 key。
    recent_root：最近几轮那份账的落点（None → <器官目录>/mind/recent_turns.jsonl）。
    usage_root：口癖账的落点（None → <器官目录>/mind/anchor_usage.jsonl）。
    assembly：给一份内存里的装配说明就用它（不读盘）——评测脚本靠它关掉某几段做空基线。

    返回（键名对外冻结，pages/chat.html 照它渲染）：
      ok / reply / chunks[{text,gap_before,send_at}] / recalled[{id,text,source,
      source_type,evidence_key,score,who,note_display}] / heart{mood{tier,direction},
      voice{tone,direction,bans}, axes{轴: 档位词}, engage{state,reason}}
      / gate{decision, hit{clause_id,reason}|null, drops{today}} /
      assembly{order, sections[{key,chars,cap}], reply_budget{key,tier,chars}} /
      continuity{turns_loaded, omitted, notice} /
      banned{rounds, action_lines[{text,count}], openings[开场前 N 字]} /
      action{pool, rule, quota{window,max,used,blocked}} /
      llm{model, temperature, temperature_tier} /
      anchors{configured, count, available, injected, instruction, items[实际注入的],
      skipped[没注入的 scene]|note} /
      guard{window, threshold, min_rounds, rounds, hits, rate, over, injected, available} /
      memory{wrote[{id,who}]}

    guard 的读数一律是**这一轮之前**的滚动窗口（口癖账还没记这一轮），八个键对外冻结。
    落账放在最后一步：只有过了闸、reply 非空的那一轮才有「开头」可算——
    被闸弃掉的轮（reply 空）**不记口癖账**，她没说出口，就没有开头。
    """
    if store is None:
        raise ValueError("魂未载入（store=None）：没有魂就没有装配的底色")
    if brain is None:
        raise ValueError("脑未载入（brain=None）：没有记忆就不开口")
    said = str(text or "").strip()
    if not said:
        raise ValueError("text 不能为空：没有一句话就没有这一轮")
    conf = _check_assembly(assembly, "传入的 assembly") if assembly is not None else load_assembly()
    at = now if isinstance(now, str) and now else brain_model.now_iso()

    # 1) 魂与脑：先回想（这一轮该想起什么，要在写提示词之前定下来）
    soul_text = store.render()
    recalled, memory_text = _recall(brain, said, who)

    # 1.5) 最近几轮与锚句：连续性两个读数都在写提示词之前定下来
    loaded = recent.load(root=recent_root, turns=conf["recent"]["turns"])
    omitted = int(loaded.get("omitted") or 0)
    note = recent.notice(omitted, conf["recent"]["notice"])
    recent_text = _recent_text(loaded.get("turns") or [], note, conf)
    # 近轮禁用：原文段一个字都不删，禁令另起一节，只抽她那一侧（store 里她的自称就是她）
    her_name = store.snapshot().identity.display
    history = loaded.get("turns") or []
    banned_text, banned_read = _banned_text(history, conf["banned"], her_name)
    # 动作配额：数最近 quota.window 轮里有几轮带动作（轮数不是行数，口径与禁令同一份）
    action_text, action_read = _action_text(
        conf, repeat.count_with_actions(history, conf["action"]["quota"]["window"], her_name))
    # 口癖护栏：先读**这一轮之前**的滚动窗口，再定这一轮注入几条（先记后算会自己踩自己）。
    # 家现行台词读一次，注入按 guard 裁，口癖统计仍看全量——两件事各用各的那一份。
    anchors_rows = _anchor_rows()
    guard_conf = _guard(conf)
    guard_read = (usage.report(guard_conf, injected_available=len(anchors_rows),
                               root=usage_root)
                  if guard_conf is not None else _guard_missing())
    anchor_text, anchor_read = _anchor(conf, anchors_rows, guard_read["injected"])

    # 2) 心：唯一合成点 + 一次判决；坏卡降级成只要 mood 那一块
    mood_read = mood_module.read(heart_root, at)
    offsets = offset_module.overview(root=heart_root, now=at)
    eff = _effective(heart_root, at, mood_read, offsets)
    energy_read = energy_module.read(heart_root, at, **_sleep_terms(heart_root, at))
    judgement = _engage(heart_root, at, eff, energy_read, brain, who)
    heart_words = _heart_words(eff, judgement)
    heart_text = _heart_text(eff, heart_words)

    # 3) 嘴：称呼 → 语气档（口径行进提示词）
    voice = _voice(store, brain, who, mood_read, energy_read)
    mouth_text = _mouth_text(voice)
    person_text = _person_text(brain, who)

    # 4) 提示词与回复预算
    tier = heart_words["mood"]["tier"]
    # 配额满了就换另一份说话规矩：动作那条从「怎么写」改成「不许写」——格式要求本身也在
    # 催她每轮写一行动作，只留一句禁令压不住它。
    rules = (conf["reply"]["format_blocked"] if action_read["quota"]["blocked"]
             else conf["reply"]["format"])
    reply_rule = "\n".join([conf["reply"]["instruction"]] + list(rules))
    budget = conf["reply"]["budget_by_tier"].get(tier, conf["reply"]["default"])
    contents = {"soul": soul_text, "anchors": anchor_text, "recent": recent_text,
                "banned": banned_text, "action": action_text,
                "memory": memory_text, "heart": heart_text, "mouth": mouth_text,
                "person": person_text}
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
        "reply_instruction": reply_rule,
    })
    user = _fill(conf["prompt"]["user"], {"text": said})
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    # 4.5) 温度分档：两个温度与触发条件都从 data/llm.json 读，代码里没有字面量
    call_conf = llm.resolve_config(config)
    policy = llm.temperature_policy(config)
    temperature_tier, temperature = llm.choose_temperature(
        policy, recalled_nonempty=bool(recalled), reply_budget=budget)

    # 5) 发模型（LLMError 原样上抛）
    said_back = llm.complete(messages, config=call_conf, env=env, transport=transport,
                             temperature=temperature)

    # 5.5) 硬兜底：配额满了就替她把动作行删掉（不靠模型自觉）——删后的文本往下走全程
    said_back = _strip_blocked_actions(said_back, action_read)

    # 6) 过嘴：切分 + 节奏 + 闸（重写器 None：本轮命中即弃）
    batch, verdict, ledger = _pass_mouth(said_back, who, at)
    dropped = verdict.decision == "dropped"
    reply = "" if dropped else verdict.text
    chunks = [] if dropped else [{"text": part.text, "gap_before": part.gap_before,
                                  "send_at": part.send_at} for part in batch.parts]
    hit = verdict.hits[0] if verdict.hits else None

    # 7) 落脑：人的原话先记，她真说出口的那句再记（被弃的那句没说出口，不进脑）
    wrote = _remember(brain, store, who, said, reply, dropped, at)
    # 7.5) 落账：这一轮的原文进最近几轮（reply 不管有没有说出口，人那句一定留着）
    recent.append({"ts": at, "who": who or HUMAN_WHO, "said": said, "reply": reply},
                  root=recent_root, cap=conf["recent"]["cap"])
    # 8) 落口癖账：放在最后一步。被闸弃掉的轮（reply 空）不记——她没说出口就没有开头；
    #    命中一律对**家现行全部台词**，不是只注入的那几条（口癖统计看的是全部台词）。
    if not dropped and reply and isinstance(guard_conf, Mapping):
        # 名字不与上面的闸命中（hit）撞车：两件事互不相干，共用一个变量就会互相踩。
        spoken, scene = usage.match(reply, anchors_rows, int(guard_conf["prefix_chars"]))
        usage.append(spoken, usage.opening(reply, int(guard_conf["opening_chars"])), scene,
                     root=usage_root)

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
        "continuity": {"turns_loaded": len(loaded.get("turns") or []),
                       "omitted": omitted, "notice": note},
        "banned": banned_read,
        "action": action_read,
        "llm": {"model": call_conf["model"], "temperature": temperature,
                "temperature_tier": temperature_tier},
        "anchors": anchor_read,
        "guard": guard_read,
        "memory": {"wrote": wrote},
    }


# —— 各段 ————————————————————————————————————


def _recent_text(turns: List[Mapping[str, Any]], note: str,
                 conf: Mapping[str, Any]) -> str:
    """最近几轮这段进提示词的样子：一条一轮的原文（谁 + 原话），段尾接「更早还有 N 轮」。

    notice 先占位再截正文：段尾那句是这一轮最要紧的一句，被 cap 锯掉就等于没装进去。
    """
    body = recent.render(turns) or NO_TURNS
    spec = conf["sections"].get("recent")
    if not note:
        return body
    if not isinstance(spec, Mapping):
        return body + "\n" + note
    cap = spec["cap"]
    room = cap - len(note) - 1
    if room <= 0:
        return note[:cap]
    head = body if len(body) <= room else body[:room]
    return head + "\n" + note


def _banned_text(turns: List[Mapping[str, Any]], conf: Mapping[str, Any],
                 her_name: str) -> Tuple[str, Dict[str, Any]]:
    """近轮禁用这一段进提示词的样子：那句禁令 + 逐条列出她刚用过的动作行与开场。

    只抽她那一侧（repeat.her_texts），去重计次，重复几遍照实标出来——「用了一次」与
    「用了三次」是她这一轮该躲的两件事。开场是 usage.opening 的口径（去标点后前 N 字），
    本层不另写一套去标点。

    读出来的那一份原样交回调用方进返回体（页面与验收要看它到底禁了什么）；
    一条都抽不出来时写一句实话，不留白——空白她会读成「随便用」。
    """
    got = repeat.banned(turns, conf, her_name)
    lines = [str(conf["note"])]
    for row in got["action_lines"]:
        mark = repeat.REPEAT % row["count"] if row["count"] > 1 else ""
        lines.append("- %s%s%s" % (repeat.ACTION_PREFIX, row["text"], mark))
    for head in got["openings"]:
        lines.append("- %s%s" % (repeat.OPENING_PREFIX, head))
    if len(lines) == 1:
        lines.append(BANNED_EMPTY)
    return "\n".join(lines), got


def _action_text(conf: Mapping[str, Any], used: int) -> Tuple[str, Dict[str, Any]]:
    """动作池这一段进提示词的样子（两态）与返回体那一份 action。

    未达 quota.max：规矩 + quota.sparing + 部位池，三样都照抄 assembly.json——只说「写她自己的
    身体」，她就一直用尾巴；给了池子她才得挑。（旧口径「这一轮换一个部位」被她读成「每轮都要
    写一个」，六轮六次带动作，所以现在的规矩是「偶尔才写」，配额这一层替她数。）
    已达 quota.max：**整段只写 quota.blocked 那一句**，池子与规矩一个字都不给——给了她就会挑
    一个写，而这一轮该写的正是零个。

    返回体那一份照样三键齐全（pool / rule / quota）：blocked 时 pool 给空列表而不是 None，
    页面照同一套键渲染，不必为这一态另写一支。
    """
    spec = conf["action"]
    quota = spec["quota"]
    blocked = used >= quota["max"]
    read = {"pool": [] if blocked else list(spec["pool"]), "rule": spec["rule"],
            "quota": {"window": quota["window"], "max": quota["max"],
                      "used": used, "blocked": blocked,
                      "stripped": 0, "kept_because_empty": False}}
    if blocked:
        return quota["blocked"], read
    return ("%s\n%s\n可用部位：%s"
            % (spec["rule"], quota["sparing"], POOL_SEP.join(spec["pool"]))), read


def _strip_blocked_actions(reply: str, action: Dict[str, Any]) -> str:
    """blocked 时把成品里的整行动作行删掉，并把删了几行记进 action.quota.stripped。

    这里是「不许靠模型自觉」的落点：提示词那句禁令压不住另外几条同向指令（格式要求还在教她
    怎么写动作行、锚句的规矩预设了有动作、最近几轮她自己的动作行一直在示范），所以这一轮
    真写出来的动作行在这里删掉。删空就留原文（kept_because_empty）——不把一轮回复删没。
    未 blocked 时一个字都不动，stripped 恒 0（这一态她本来就该偶尔写一个）。
    """
    quota = action["quota"]
    if not quota["blocked"]:
        return reply
    clean, count, kept = repeat.strip_action_lines(reply)
    quota["stripped"] = count
    quota["kept_because_empty"] = kept
    return clean


def _anchor_rows() -> List[Mapping[str, Any]]:
    """家·仓库的现行台词（每 scene 一条，按现行顺序）：这一轮唯一的「她现在说什么」真源。

    没配 / 卡不在 / 读不出来一律回空表（那一格写「未配锚句」）——不在这里造第二份，
    也不替她补一句。
    """
    try:
        got = home_anchors.read_all()
    except (home_anchors.AnchorError, home_access.AccessDenied,
            home_store.StoreError, OSError):
        return []
    if not got.get("file_exists"):
        return []
    return list(got.get("anchors") or [])


def _guard(conf: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """口癖护栏的门槛：没配锚句就没有护栏（没有台词，也就无所谓「老用哪一句」）。"""
    spec = conf.get("anchors")
    if not isinstance(spec, Mapping):
        return None
    guard = spec.get("guard")
    return guard if isinstance(guard, Mapping) else None


def _guard_missing() -> Dict[str, Any]:
    """没配护栏那一态：八个键照样交出去，值一律 null，over 是 False（不降档不记账）。

    键名冻结的另一半：页面照同一套键渲染，缺字段就写「没给」，不留白也不报错。
    """
    return {"window": None, "threshold": None, "min_rounds": None, "rounds": None,
            "hits": None, "rate": None, "over": False, "injected": None,
            "available": None}


def _anchor(conf: Mapping[str, Any], rows: List[Mapping[str, Any]],
            limit: Optional[Any] = None) -> Tuple[str, Dict[str, Any]]:
    """锚句这一段：只走家的公开读口，只给半句，按护栏的读数决定注入几条。

    没配 / 卡不在 / 读不出来三种情况都写「未配锚句」——这一格不许空着，也不许悄悄跳过：
    空着她就以为自己没有过一句锚。

    **只给半句**（这一条的理由来自人）：整句给她，她就照着念；给半句，她才得自己补完。
    进提示词的是「正文前 fraction 段 + suffix」（两个数都在 assembly.json 的
    anchors.inject 里）；返回体另带 body 全文——半句是给她看的，全文是给人看的。

    limit 是这一轮**该注入几条**（来自 usage.report 的 injected）：降档时只取现行顺序的
    前 limit 条，其余不进提示词但在返回体的 skipped 里逐个点名——别静默丢。
    """
    spec = conf.get("anchors")
    if not isinstance(spec, Mapping):
        return _anchors_missing()
    fraction = float(spec["inject"]["fraction"])
    suffix = str(spec["inject"]["suffix"])
    items: List[Dict[str, str]] = []
    for row in rows:
        say = str(row.get("say") or "").strip()
        if not say:
            continue
        cut = min(max(int(math.ceil(len(say) * fraction)), 1), len(say))
        items.append({
            "say": say,
            "injected": say if cut >= len(say) else say[:cut] + suffix,
            "body": str(row.get("body") or ""),
            "scene": str(row.get("scene") or ""),
        })
    if not items:
        return _anchors_missing()
    keep = len(items) if limit is None else max(int(limit), 0)
    shown = items[:keep]
    if not shown:
        return _anchors_missing()
    instruction = str(spec["instruction"])
    text = '\n'.join([instruction] + ["- " + item["injected"] for item in shown])
    return text, {"configured": True, "count": len(items), "available": len(items),
                  "injected": len(shown), "instruction": instruction, "items": shown,
                  "skipped": [item["scene"] for item in items[keep:]]}


def _anchors_missing() -> Tuple[str, Dict[str, Any]]:
    """未配锚句那一态：提示词里写明那句话，返回体给机器一个 false（不是空串）。"""
    return ("%s。%s" % (ANCHORS_MISSING, ANCHORS_HINT),
            {"configured": False, "note": ANCHORS_MISSING})


def _recall(brain: Any, said: str, who: str) -> Tuple[List[Dict[str, Any]], str]:
    """脑装包：既进提示词，也原样进返回体（页面要显示「想起了什么 + 证据指针」）。

    记忆行在脑里是两栏：quote（对方原话，逐字）与 note（她的理解，可为空）。note_display
    由脑的 recall 装配好（「她理解：…」/「（无理解）」/「（unclear，未理解）」），本层只搬运、
    一个前缀都不自己加——「召回时标明这是她的理解」这条判据的真源在脑，不在这里。
    """
    result = brain.recall(query=said, who=who or None)
    items = result.get("items") or []
    recalled = [{"id": item.get("id", ""),
                 "text": item.get("text", ""),
                 "display_text": item.get("display_text", item.get("text", "")),
                 "source": item.get("source", ""),
                 "source_type": item.get("source_type", ""),
                 "evidence_key": item.get("evidence_key", ""),
                 "score": item.get("score", 0.0),
                 "who": item.get("who", ""),
                 "note_display": item.get("note_display", "")} for item in items]
    if not recalled:
        return recalled, NO_MEMORY
    lines: List[str] = []
    for row in recalled:
        # 第一行：原话 + 来源括号。display_text 由脑的 recall 造好——
        # 迁移来的「（无原话，来自旧家记录）」也在这里，装配侧不自造第二份。
        lines.append("- %s（来源：%s｜来源类型：%s｜证据：%s）"
                     % (row["display_text"] or row["text"] or NO_QUOTE,
                        row["source"], row["source_type"],
                        row["evidence_key"] or "无"))
        # 第二行：她的理解另起一行、缩进两格。没有理解就不写这一行（空串不进提示词，
        # 多出来的空行会把一条记忆劈成两条）。note_display 自带前缀，这里不再加一次。
        if row["note_display"]:
            lines.append(NOTE_INDENT + row["note_display"])
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


def _sleep_terms(heart_root: Any, at: str) -> Dict[str, Any]:
    """睡眠账给 energy 的两个派生量：生效窗口与睡眠回血封顶。

    照 api/heart.py 的 _sleep_terms：两个数都由 sleep 读时现算，本层只搬运、不重算。
    少传这两个，面板的精力读数与说话这边的读数在睡眠账非默认时就会分叉。
    """
    return {"sleep_window": sleep_module.effective_window(heart_root, at),
            "wake_cap": sleep_module.wake_cap(heart_root, at)}


def _intent_context(readout: Any, name: str, at: str,
                    energy_read: Mapping[str, Any]) -> Dict[str, Any]:
    """意图排序的上下文：今日条数、熟稔、精力那两口数（照 api/heart.py 的 _intent_context）。

    精力也注入：这一层已经按睡眠账的窗口结算过一次，排序层拿同一个数算清醒度，
    否则它会自己再读一次 energy——两次结算一条先后，档位与系数就可能与面板分叉。
    """
    context: Dict[str, Any] = {
        "who": name,
        "today_count": readout.today_count(name, at),
        "familiarity": readout.familiarity(name, at),
    }
    if isinstance(energy_read, Mapping):
        context["energy_value"] = energy_read.get("value")
        context["energy_tier"] = energy_read.get("tier")
    return context


def _engage(heart_root: Any, at: str, eff: Mapping[str, Any],
            energy_read: Mapping[str, Any], brain: Any, who: str) -> Dict[str, Any]:
    """一次判决：只 decide，不 decide_and_log（核不写门的日志，那是心的账）。

    入参与 api/heart.py 的 _engage 一毫不差：窗口、意图排序（第 5 道软门要它）、
    按睡眠账结算过的精力、熟稔与今日条数。少喂一项，门判出的理由就跟面板不一样——
    同一个人同一刻，面板说「不是时候」，她这边却说「夜里」。
    """
    readout = ports_module.from_brain(brain)
    name = who or DEFAULT_WHO
    intents = intent_module.rank(heart_root, at,
                                 context=_intent_context(readout, name, at, energy_read))
    judgement = engage_module.EngageInput.from_effective(
        dict(eff),
        window=engage_module.window_signal(heart_root, at),
        intents=intents,
        energy=energy_read["value"],
        familiarity=readout.familiarity(name, at),
        today_count=readout.today_count(name, at),
        intent=heart_model.INTENT_PASSIVE,
        who=name,
        now=at,
    )
    return engage_module.decide(judgement)


def _person_text(brain: Any, who: str) -> str:
    """关于他这一段：从名片取，每轮都进提示词，不占记忆名额、不被遗忘曲线淡掉。"""
    if brain is None or not who:
        return ""
    try:
        person = brain.lookup(who)
    except Exception:
        return ""
    if not person:
        return ""
    try:
        got = brain.card_view(person.get("id") or who) or {}
        card = got if isinstance(got, dict) else {}
    except Exception:
        card = {}

    def join(value: Any) -> str:
        if isinstance(value, (list, tuple)):
            return "、".join(str(x) for x in value if str(x).strip())
        return str(value or "").strip()

    bits = []
    for label, key in (("称呼", "display"), ("生日", "birthday"),
                       ("备注", "notes"), ("雷点", "prefs")):
        value = join(card.get(key))
        if value:
            bits.append("%s：%s" % (label, value))
    return "｜".join(bits)

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
