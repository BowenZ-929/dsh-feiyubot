"""一致性评测：三种跑法 × 四格配置、两条轨道、三态判定。**这是评测脚本，不是规格。**

口径来自 docs/plan.md §3.5 与 docs/reference/aliveness.md §4：**别再用「同一句问 10 次看一不一样」**。
同序问 10 次、10 次逐字相同，在参考实现里判的是**该轮无效（疑似复制）**，不是稳定；洗牌后逐题
相同率 100% → 57%，说明**顺序本身就是连续性的来源**。所以这里：

- 跑法：same（同序 10 次）／shuffled（每轮打乱题序 10 次）／empty（空基线——就是 empty 配置那一行，
  单独一行报，见 render_summary 的空基线那一条）。
- 配置（一格格子）：empty（装配里不带魂、不带记忆）／soul／memory／both。
  **心与嘴四格都在**：那是她此刻的状态与说话的规矩，不是人设与记忆。
- 每一轮（run）：全新临时脑 + 全新 recent_root + 全新心根（出厂卡）；**同一轮内共享累积的最近 N 轮
  与脑记忆**。顺序本身是连续性的来源，别把每题跑成一次独立会话。
- 两条轨道（提取全是确定性匹配与计数，零 LLM）：
  A 不该变：称呼口径／自称／是否自称 AI／句长量级（按数据档界落桶）／口癖（命中与否）
  **A 轨按闭集判漂移，不按「命中词拼接」**：自称的闭集是魂 identity.self_names（display +
  synonyms，经 organs.soul 的公开读口现读；实盘卡优先、出厂卡兜底），称呼的闭集是称呼表
  address_table 里的称呼词（四档 + 回退 + 彩蛋，经 organs.mouth.warehouse.load 现读）加上数据里
  那个中性第二人称。集合内换词（魂名单里「我」↔「本鱼」）不算漂移；出现集合外的自称/称呼才算，
  只报第一个越界词。完全没有自称/称呼记「集内」——不是越界，也不算漂移。两处闭集都读不到时退回
  data/questions.json 里的 self／address fallback，并在报告头写明退回了（默认值不是配置）。
  句长量级只在**跨档**时算漂移，不逐字比长度。
  B 该变：措辞（原文哈希）／回应长度（字数）／情绪档位（heart.mood.tier）
- C 轨（读数，**不进判定**）：每句回答用 organs.mind.usage.match 判一次「是不是拿台词库的
  锚句开的场」，一格报**口癖命中率** = 该格所有回答里命中的比例。锚句整轮开始时读一次
  （organs.home.anchors.read_all()），前缀字数现读 assembly.json 的 anchors.guard.prefix_chars。
  C 只是趋势读数：判定仍是下面那四档，C 一列不参与。
- 判定（顺序写死，每格写清触发了哪条）：
  1) 逐题相同率 ≥ 0.90 → **该轮判无效（疑似复制）**；
  2) 否则若 A 漂移率 > 0 → **换人**；
  3) 否则若 B 全同率 == 1.00 → **死板**；
  4) 否则 → **通过**。

1) 与 3) 用两个**不同粒度**的量，这是刻意的：若两者都取「措辞逐字相同的题占比」，3) 永远被 1) 先吃掉
（1.00 ≥ 0.90），「死板」这一档就成了死代码。所以：
  逐题相同率 = **B 三特征（措辞 + 长度 + 情绪档位）10 次全同**的题占比——整轮连状态带措辞一起复制；
  B 全同率   = **措辞逐字相同**的题占比——措辞一字不改、只有心境在动，那是死板。
判法只有这四条，阈值只有两个常量（COPY_INVALID / DEAD_SAME），改口径改这里，别在报告里另立一套。

假模型（--dry / --template / --vary）全走 transport 注入，一个字节都不发真网络、不需要 key；
不带这三个开关就按真模型跑，缺 key 时把 LLMError 的人话原样打出来、退出码非 0——不装成跑通了。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import inspect
import json
import os
import random
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from organs import paths
from organs.brain import BrainStore
from organs.home import anchors as home_anchors
from organs.mind import conversation
from organs.soul import load as load_soul

HERE = Path(__file__).resolve().parent
DATA_FILE = HERE / "data" / "questions.json"
SOUL_FIXTURE = HERE.parents[1] / "soul" / "fixtures" / "valid"
HEART_FACTORY = HERE.parents[1] / "heart" / "data"
#: 出厂心的两件（卡 + 意图权重表）：每轮的心状态从零开始，卡不是——全新心根照抄这两件。
HEART_FILES = ("baseline-card.json", "engage_weights.json")

#: 逐题相同率（B 三特征全同）≥ 这个数就判「该轮无效（疑似复制）」
COPY_INVALID = 0.90
#: B 全同率（措辞逐字相同）正好到这个数就判「死板」
DEAD_SAME = 1.00

#: 两种跑法各自的种子，分开写进报告：同序不重排（种子只用来记账），洗牌每轮按它重排。
SEED_SAME = 20261105
SEED_SHUFFLED = 20261106
#: 固定时刻：心读数是「此刻」的，钟一动同一格就不可比，所以整轮评测用同一个钟。
NOW = "2026-11-05T20:00:00+00:00"
WHO = "主人"

CONFIGS = ("empty", "soul", "memory", "both")
CONFIG_LABELS = {
    "empty": "empty（空基线：不带魂、不带记忆）",
    "soul": "soul（只带魂）",
    "memory": "memory（只带记忆）",
    "both": "both（魂 + 记忆）",
}
MODES = ("same", "shuffled")
MODE_SHORT = {"same": "同序", "shuffled": "洗牌"}

#: assembly.json 里 C 轨认的两个字数（anchors.guard）。**现读**，读不到才退回文档默认值，
#: 退了会在报告头如实写出来——默认值不是配置。
GUARD_PATH = ("anchors", "guard")
GUARD_DEFAULTS = {"prefix_chars": 3, "opening_chars": 6}

#: 假模型的配置与 key：transport 注入了也照样要过 llm.complete 的 key 与配置校验
#: （真 network 一次都不发，见 llm.complete 的 transport 参数）。
FAKE_CONFIG = {"provider": "bench", "model": "bench-fake",
               "base_url": "http://example.invalid/v1", "key_env": "QQAGENT_LLM_KEY",
               "timeout_ms": 1000, "retries": 0, "temperature": 0.7, "max_tokens": 400}
FAKE_ENV = {"QQAGENT_LLM_KEY": "bench-fake-key"}

ENV_HOME = "QQAGENT_HOME"          # 装配说明书实盘根（也管 llm.json）：评测期间切到临时目录
ENV_TRACE = "MOUTH_TRACE_DIR"      # 嘴的弃言账本：评测期间切到临时目录，仓库一个字节都不写

#: 假模型的填充句：只换这一句，称呼/自称/口癖/句长档一个都不动——A 轨稳定、B 轨随提示词变。
FILLERS = ("这事儿我记着", "这个我得想想", "你说的我在听", "我先捋一捋", "嗯嗯是这么回事")
TEMPLATE_REPLY = "我在的，你说吧，我听着。"


# —— 数据 ————————————————————————————————


def load_data(path: Optional[Any] = None) -> Dict[str, Any]:
    """读题目与提取规则（缺件直接报，不补默认值——补了就是第二处真源）。"""
    target = Path(path) if path is not None else DATA_FILE
    data = json.loads(target.read_text(encoding="utf-8"))
    questions = [str(item["text"]) for item in data["questions"]]
    if not questions:
        raise ValueError("题目为空：%s 里一条题都没有" % target)
    return {"questions": questions, "rules": data["rules"], "path": str(target)}


# —— A 轨（不该变）：全确定性正则/计数 ————————————————


def _squash(text: Any) -> str:
    """去掉所有空白：句长与字数只数字符，不数排版。"""
    return "".join(str(text or "").split())


def _words(rule: Mapping[str, Any], key: str) -> List[str]:
    """规则里的一组词：词表只许在数据里，代码里不许有一份。"""
    raw = rule.get(key)
    if not isinstance(raw, list) or not raw:
        raise ValueError("规则缺 %s（名单）——这是数据，别在代码里补" % key)
    return [str(item) for item in raw]


def _hits(body: str, words: Sequence[str]) -> str:
    """命中了哪几个词：按数据里的先后拼；一个没命中记「无」。"""
    hit = [word for word in words if word and word in body]
    return "、".join(hit) if hit else "无"


def _bucket(size: int, rule: Mapping[str, Any]) -> str:
    """句长量级：落到数据给的哪一档（≤ 档界即落这一档，最后一个标签兜底）。"""
    edges = [int(edge) for edge in rule.get("edges") or []]
    labels = [str(label) for label in rule.get("labels") or []]
    if len(labels) != len(edges) + 1:
        raise ValueError("档界与档名对不上：edges=%r labels=%r" % (edges, labels))
    for edge, label in zip(edges, labels):
        if size <= edge:
            return label
    return labels[-1]


# —— A 轨的闭集（「不该变」的那几个值由几张卡定死，代码里一个词都不写） ————————


#: 魂的器官名：实盘卡（QQAGENT_HOME/organs/soul）优先，读不到才看出厂卡（organs/soul/data）。
SOUL_ORGAN = "soul"
#: 称呼表在嘴的仓库里的名字（经 organs.mouth.warehouse.load 读）。
ADDRESS_TABLE = "address_table"


def _dedup(items: Sequence[Any]) -> List[str]:
    """按先后去重：扫描顺序决定「第一个越界词」报哪个，所以顺序是给定的，不是集合的。"""
    out: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def read_self_names(root: Optional[Any] = None) -> Dict[str, Any]:
    """自称闭集：魂 identity.self_names 的 display + synonyms，从 organs.soul 的公开读口现读。

    实盘卡优先、出厂卡兜底；两处都读不到就返回空词表与一句原因，由 closed_set 退回数据里的
    fallback——不在这里补一个词，补了就是第二处真源。
    """
    roots = [Path(root)] if root is not None else [paths.organ_dir(SOUL_ORGAN),
                                                   paths.factory_dir(SOUL_ORGAN)]
    reason = ""
    for candidate in roots:
        try:
            identity = load_soul(candidate).snapshot().identity
            names = _dedup([identity.display] + list(identity.synonyms))
        except Exception as exc:           # 缺卡／坏卡／红线不过：都只是「这一处读不到」
            reason = "%s（%s）" % (candidate, type(exc).__name__)
            continue
        if names:
            return {"words": names, "source": "魂 identity.self_names（%s）" % candidate,
                    "reason": "", "fallback": False}
        reason = "%s（identity.self_names 是空的）" % candidate
    return {"words": [], "source": "", "reason": reason, "fallback": True}


def _address_words(table: Mapping[str, Any]) -> List[str]:
    """称呼表里的称呼词：四档 addresses + fallback.address + easter_egg.address，表里没有的不补。"""
    words: List[str] = []
    for tier in table.get("tiers") or []:
        words.extend(list((tier or {}).get("addresses") or []))
    for key in ("fallback", "easter_egg"):
        block = table.get(key) or {}
        words.append(block.get("address") if isinstance(block, Mapping) else "")
    return words


def read_address_names(rules: Mapping[str, Any], root: Optional[Any] = None) -> Dict[str, Any]:
    """称呼闭集：称呼表里的称呼词 + 数据里那个中性第二人称（rules.address.closed_extra）。

    表缺件／坏件就返回空词表与一句原因，由 closed_set 退回数据里的 fallback。
    """
    extra = _words(rules["address"], "closed_extra")
    try:
        from organs.mouth.warehouse import load as mouth_load, path_of as mouth_path
        table = mouth_load(ADDRESS_TABLE, root)
        path = mouth_path(ADDRESS_TABLE, root)
        names = _dedup(_address_words(table) + list(extra))
        if names:
            return {"words": names,
                    "source": "称呼表 %s（四档称呼词 + 回退 + 彩蛋 + %s）"
                              % (path, "、".join(extra)),
                    "reason": "", "fallback": False}
        reason = "%s（称呼表里的称呼词是空的）" % path
    except Exception as exc:               # 缺件／坏件：读不到就是读不到
        reason = "%s（%s）" % (ADDRESS_TABLE, type(exc).__name__)
    return {"words": [], "source": "", "reason": reason, "fallback": True}


def closed_set(raw: Mapping[str, Any], fallback: Sequence[str], label: str) -> Dict[str, Any]:
    """一组闭集：卡里读得到就用卡里的名单，读不到就退回数据里那份，并把「退回了」写进来源。

    scan（扫词表）= 数据里那份词的先后 + 闭集里多出来的词。两份都不可少：数据决定「扫得见哪些
    词」，闭集决定「哪些算合规」——少了前者就看不见集合外的话，少了后者就判不出越界。
    """
    words = _dedup(raw.get("words") or [])
    if words:
        return {"words": words, "scan": _dedup(list(fallback) + words),
                "source": str(raw.get("source") or label), "fallback": False}
    return {"words": _dedup(fallback), "scan": _dedup(fallback),
            "source": "%s 读不到（%s），退回数据里的 fallback" % (label, raw.get("reason") or "—"),
            "fallback": True}


def read_closed_sets(rules: Mapping[str, Any], *, soul_root: Optional[Any] = None,
                     mouth_root: Optional[Any] = None) -> Dict[str, Any]:
    """A 轨的两套闭集：自称认魂的自称名单，称呼认称呼表 + 数据里那个中性第二人称。

    整轮读一次（轮内不重读）：评测期间实盘根被切到临时目录，轮内读会读错地方。
    """
    return {"self": closed_set(read_self_names(soul_root),
                              _words(rules["self"], "fallback"),
                              "魂 identity.self_names"),
            "address": closed_set(read_address_names(rules, mouth_root),
                                 _words(rules["address"], "fallback"),
                                 "称呼表 address_table")}


def _classify(body: str, scan: Sequence[str], closed: Sequence[str]) -> str:
    """闭集判读：扫到的词全在闭集里就是「集内」，否则报第一个越界词；一个没扫到也记「集内」。"""
    allowed = {word for word in closed if word}
    for word in scan:
        if word and word in body and word not in allowed:
            return "越界:%s" % word
    return "集内"


def a_features(text: Any, rules: Mapping[str, Any],
               closed: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """A 轨一句读数：称呼口径／自称（按闭集判：集内／越界:<词>）／是否自称 AI／句长量级／口癖。

    全是确定性匹配与计数，零 LLM。前两个特征的取值域只有「集内」与「越界:<词>」两种形状：
    集合内换词不改取值（不漂），跳出集合才改；后三个照旧（命中／档界／命中）。
    closed 不给就现读一次闭集——整轮评测应当在外面读好再传进来（轮内环境是临时根）。
    """
    body = _squash(text)
    sets = dict(closed) if closed is not None else read_closed_sets(rules)
    return {
        "称呼口径": _classify(body, sets["address"]["scan"], sets["address"]["words"]),
        "自称": _classify(body, sets["self"]["scan"], sets["self"]["words"]),
        "自称AI": "是" if _hits(body, _words(rules["ai_marker"], "words")) != "无" else "否",
        "句长量级": _bucket(len(body), rules["length"]),
        "口癖": "是" if _hits(body, _words(rules["tic"], "words")) != "无" else "否",
    }


# —— B 轨（该变） ——————————————————————————


def _digest(text: str) -> str:
    """措辞的原文指纹：逐字相同 = 指纹相同。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def b_features(body: Mapping[str, Any]) -> Dict[str, Any]:
    """B 轨一句读数：措辞（原文哈希）／回应长度（字数）／情绪档位（heart.mood.tier）。"""
    reply = str(body.get("reply") or "")
    mood = (body.get("heart") or {}).get("mood") or {}
    return {"措辞": _digest(reply), "长度": len(_squash(reply)),
            "情绪档位": str(mood.get("tier") or "无")}


# —— C 轨（读数，不进判定）：口癖命中 ————————————————


def usage_module() -> Any:
    """organs.mind.usage 按需取：没落地（或落了半个）就返回 None，C 轨报「未落地」，不补不猜。"""
    try:
        from organs.mind import usage
    except ImportError:
        return None
    for name in ("match", "opening"):
        if not callable(getattr(usage, name, None)):
            return None
    return usage


def read_anchors(root: Optional[Any] = None) -> Dict[str, Any]:
    """台词库的现行几条：整轮评测开始时读一次（她此刻说什么），轮内不重读。"""
    data = home_anchors.read_all(root)
    return {"anchors": list(data.get("anchors") or []),
            "file": str(data.get("file") or ""),
            "file_exists": bool(data.get("file_exists")),
            "total": int(data.get("total") or 0)}


def guard_settings(assembly: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """从这一份装配说明里现读 anchors.guard 的两个字数；缺哪项就退回默认并记下是哪项。"""
    raw: Any = dict(assembly or {})
    for key in GUARD_PATH:
        raw = (raw or {}).get(key) or {}
    got: Dict[str, Any] = {}
    missing: List[str] = []
    for name, default in GUARD_DEFAULTS.items():
        value = raw.get(name)
        if isinstance(value, int) and value > 0:
            got[name] = value
        else:
            got[name] = default
            missing.append(name)
    got["from_assembly"] = not missing
    got["missing"] = missing
    return got


def c_features(text: Any, anchors: Sequence[Mapping[str, Any]], *, prefix_chars: int,
               opening_chars: int, module: Any = None) -> Dict[str, Any]:
    """C 轨一句读数：这一句是不是拿台词库里的锚句开的场（口癖命中）。

    匹配规则**一条都不写在这里**：全在 organs.mind.usage.match（前缀几个字也由它说了算）。
    它没落地时 hit 记 None ——「未落地」不等于「没命中」，不拿 0 冒充数据。
    """
    body = str(text or "")
    usage = module if module is not None else usage_module()
    if usage is None:
        return {"hit": None, "scene": "usage 未落地", "opening": ""}
    hit, scene = usage.match(body, list(anchors), int(prefix_chars))
    return {"hit": bool(hit), "scene": str(scene or ""),
            "opening": str(usage.opening(body, int(opening_chars)) or "")}


# —— 判定 ————————————————————————————————


def question_stats(samples: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """一题在一格里的 10 次读数 → 漂移没漂移、全同没全同。"""
    if not samples:
        raise ValueError("一题一次都没跑：拿不到读数就判不了")
    names = sorted(samples[0]["a"])
    a_values = {name: [str(sample["a"][name]) for sample in samples] for name in names}
    drift_features = [name for name in names if len(set(a_values[name])) > 1]
    b_values = {key: [sample["b"][key] for sample in samples]
                for key in sorted(samples[0]["b"])}
    wording = {str(value) for value in b_values["措辞"]}
    joints = {tuple((key, str(sample["b"][key])) for key in sorted(sample["b"]))
              for sample in samples}
    known = [sample["c"] for sample in samples
             if isinstance(sample.get("c"), dict) and sample["c"].get("hit") is not None]
    return {"drift": bool(drift_features), "drift_features": drift_features,
            "same_text": len(wording) == 1, "same_all": len(joints) == 1,
            "a_values": a_values, "b_values": b_values,
            "c_hits": sum(1 for item in known if item["hit"]),
            "c_known": len(known), "c_answers": len(samples)}


def judge(stats: Mapping[str, Any]) -> Dict[str, Any]:
    """三态判定，四条顺序写死；返回判词、第几条、以及「为什么是这一格」的一句话。"""
    copy_rate = float(stats["copy_rate"])
    drift_rate = float(stats["a_drift_rate"])
    same_rate = float(stats["b_same_rate"])
    features = "、".join(stats.get("drift_features") or []) or "—"
    if copy_rate >= COPY_INVALID:
        return {"verdict": "无效", "rule": 1,
                "why": "逐题相同率 %.2f ≥ %.2f（B 三特征十次全同）→ 疑似复制，这一轮数据不能用"
                       % (copy_rate, COPY_INVALID)}
    if drift_rate > 0:
        return {"verdict": "换人", "rule": 2,
                "why": "A 轨漂移率 %.2f > 0（漂移特征：%s）→ 不该变的变了"
                       % (drift_rate, features)}
    if same_rate >= DEAD_SAME:
        return {"verdict": "死板", "rule": 3,
                "why": "B 全同率 %.2f == %.2f（措辞十次逐字相同）→ 模板化，没有活人感"
                       % (same_rate, DEAD_SAME)}
    return {"verdict": "通过", "rule": 4,
            "why": "逐题相同率 %.2f < %.2f、A 漂移率 0.00、B 全同率 %.2f < %.2f → 该变的变了、不该变的没变"
                   % (copy_rate, COPY_INVALID, same_rate, DEAD_SAME)}


def cell_stats(per_question: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """一格（配置 × 跑法）的汇总：两条轨道的两个率 + C 的口癖命中率，然后按写死的顺序判一次。

    C 只算不判：judge() 只认 A/B，C 不进那四档。
    """
    total = len(per_question)
    if not total:
        raise ValueError("这一格一道题都没有：判不了")
    rows = [per_question[question] for question in per_question]
    drift_features = sorted({name for row in rows for name in row["drift_features"]})
    c_answers = sum(int(row.get("c_answers") or 0) for row in rows)
    c_known = sum(int(row.get("c_known") or 0) for row in rows)
    c_hits = sum(int(row.get("c_hits") or 0) for row in rows)
    stats = {
        "a_drift_rate": sum(1 for row in rows if row["drift"]) / total,
        "b_same_rate": sum(1 for row in rows if row["same_text"]) / total,
        "copy_rate": sum(1 for row in rows if row["same_all"]) / total,
        "drift_features": drift_features,
        # C 命中率 = 该格**所有回答**里命中的比例；一次都判不了（usage 没落地）时记 None，不记 0。
        "c_rate": (c_hits / c_answers) if c_answers and c_known == c_answers else None,
        "c_hits": c_hits, "c_answers": c_answers,
    }
    stats.update(judge(stats))
    return stats


# —— 假模型（transport 注入，零网络） ————————————————


def _prompt_text(payload: Mapping[str, Any]) -> str:
    """把这次请求的提示词拼回一段文本：假模型的回答只认它。"""
    messages = payload.get("messages") or []
    return "\n".join(str((message or {}).get("content") or "") for message in messages)


#: 脑给证据指针的尾巴是随机的（t-8e4f95dad09f 这种）：它每轮都变，但**内容没变**。
#: 假模型要的是「同一段内容 ⇒ 同一句回答」，所以哈希之前把随机十六进制尾巴抹平。
RANDOM_TAIL = re.compile(r"[0-9a-f]{8,}")


def _prompt_key(payload: Mapping[str, Any]) -> str:
    """这一段提示词的稳定指纹：抹掉随机 id 之后再哈希，同内容就同指纹。"""
    return RANDOM_TAIL.sub("#", _prompt_text(payload))


def _say(text: str) -> Tuple[int, bytes]:
    """把一句话包成 OpenAI 兼容的 200 响应体。"""
    body = {"choices": [{"message": {"content": text}}]}
    return 200, json.dumps(body, ensure_ascii=False).encode("utf-8")


def hash_transport(fillers: Sequence[str] = FILLERS) -> Callable[..., Any]:
    """--dry：确定性假模型——回答由提示词文本的哈希决定，提示词一模一样就一模一样。

    同一段提示词永远同一句，提示词一变就变；同序多轮因此 100% 相同，会被判成「无效轮」——
    这是对的，别把它当 bug 掩盖（洗牌后提示词变了，回答才会变）。
    """
    def send(url: Any, headers: Any, payload: Mapping[str, Any], timeout: Any) -> Tuple[int, bytes]:
        digest = hashlib.sha256(_prompt_key(payload).encode("utf-8")).hexdigest()
        filler = fillers[int(digest[:8], 16) % len(fillers)]
        return _say("嗯，%s。我听明白了，你继续说。" % filler)
    return send


def template_transport(reply: str = TEMPLATE_REPLY) -> Callable[..., Any]:
    """--template：固定模板回答——每道题、每一轮都同一句，用来演示「完全相同 → 无效轮」。"""
    def send(url: Any, headers: Any, payload: Mapping[str, Any], timeout: Any) -> Tuple[int, bytes]:
        return _say(reply)
    return send


def vary_transport(fillers: Sequence[str] = FILLERS) -> Callable[..., Any]:
    """--vary：在确定性回答上掺一个「第几遍」的轮次计数，模拟采样抖动。

    措辞每轮都不一样（B 轨该变），称呼/自称/口癖/句长档一个都不动（A 轨不该变），
    用来展示「不无效、也不死板」的通过格。
    """
    seen: Dict[str, int] = {}

    def send(url: Any, headers: Any, payload: Mapping[str, Any], timeout: Any) -> Tuple[int, bytes]:
        digest = hashlib.sha256(_prompt_key(payload).encode("utf-8")).hexdigest()
        seen[digest] = seen.get(digest, 0) + 1
        filler = fillers[int(digest[:8], 16) % len(fillers)]
        return _say("嗯，%s。我听明白了，你继续说。（第%d遍）" % (filler, seen[digest]))
    return send


# —— 装配与应答 ————————————————————————————


def answer_params() -> Tuple[str, ...]:
    """装配入口这一刻认哪些参数：并行任务在加 assembly / recent_root，这里每次现读。"""
    return tuple(inspect.signature(conversation.answer).parameters)


def assembly_for(config: str, base: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """四格配置 = 装配顺序里留不留 soul / memory 两段；锚句与最近 N 轮四格都在。

    心与嘴四格都在，所以它们不在这里开合。
    """
    source = dict(base) if base is not None else conversation.load_assembly()
    keep = {"soul": config in ("soul", "both"), "memory": config in ("memory", "both")}
    order = [key for key in source["order"]
             if key not in keep or keep[key]]
    source["order"] = order
    return source


@contextlib.contextmanager
def isolated_environment():
    """评测期间把装配说明书的实盘根与嘴的弃言账本都切到临时目录：仓库一个字节都不写。"""
    previous = {name: os.environ.get(name) for name in (ENV_HOME, ENV_TRACE)}
    with tempfile.TemporaryDirectory(prefix="qqagent-bench-env-") as tmp:
        os.environ[ENV_HOME] = str(Path(tmp) / "home")
        os.environ[ENV_TRACE] = str(Path(tmp) / "trace")
        try:
            yield Path(tmp)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _assembled_kwargs(assembly: Mapping[str, Any]) -> Dict[str, Any]:
    """装配说明怎么递进去：认 assembly 就直接给内存里那一份；不认就写临时实盘（不读不改真数据）。"""
    if "assembly" in answer_params():
        return {"assembly": assembly}
    target = paths.organ_dir("mind") / "assembly.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(assembly, ensure_ascii=False), encoding="utf-8")
    return {}


def _recent_kwargs(recent_root: Path) -> Dict[str, Any]:
    """最近 N 轮的落点：认 recent_root 就每轮给一个独立临时目录。"""
    return {"recent_root": recent_root} if "recent_root" in answer_params() else {}


def ask(text: str, *, store: Any, brain: Any, heart_root: Any, recent_root: Path,
        now: str, who: str, assembly: Mapping[str, Any],
        transport: Optional[Callable[..., Any]],
        config: Optional[Mapping[str, Any]], env: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """问一句：装配入口原样调用，transport 注入了就不发真网络。"""
    kwargs: Dict[str, Any] = {"store": store, "brain": brain, "heart_root": heart_root,
                              "now": now, "transport": transport,
                              "config": config, "env": env}
    kwargs.update(_recent_kwargs(recent_root))
    kwargs.update(_assembled_kwargs(assembly))
    return conversation.answer(text, who, **kwargs)


def _order_for(mode: str, questions: Sequence[str], run: int, seed: int) -> List[str]:
    """这一轮的题序：同序照原样，洗牌按种子每轮重排（种子进报告，保证可复现）。"""
    order = list(questions)
    if mode == "shuffled":
        random.Random(seed + run).shuffle(order)
    return order


def _heart_root(path: Path) -> Path:
    """每一轮一个全新心根：只放出厂卡与意图权重表，状态文件从零开始长。"""
    path.mkdir(parents=True, exist_ok=True)
    for name in HEART_FILES:
        source = HEART_FACTORY / name
        if source.is_file():
            shutil.copyfile(str(source), str(path / name))
    return path


def _load_soul(target: Path) -> Any:
    """魂：出厂 fixtures 里那份完整的卡（四格都要 store 才能装配，魂那一段由装配顺序开关）。"""
    shutil.copytree(str(SOUL_FIXTURE), str(target))
    return load_soul(target)


def run_cell(mode: str, config: str, *, runs: int, questions: Sequence[str],
             rules: Mapping[str, Any], transport: Optional[Callable[..., Any]] = None,
             seed: int = SEED_SAME, now: str = NOW, who: str = WHO,
             config_data: Optional[Mapping[str, Any]] = None,
             env_data: Optional[Mapping[str, Any]] = None,
             anchors: Optional[Mapping[str, Any]] = None,
             closed: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """跑一格（配置 × 跑法）：每一轮全新脑 + 全新 recent_root + 全新心根，轮内共享累积。"""
    per_question: Dict[str, List[Dict[str, Any]]] = {question: [] for question in questions}
    dropped = 0
    # 锚句与两套闭集整轮读一次（在外面读：isolated_environment 会把家根切到临时目录，
    # 那里既没有台词库、也没有魂的实盘卡）；前缀字数从这一格的装配说明里现读。
    anchor_book = dict(anchors) if anchors is not None else read_anchors()
    closed_sets = dict(closed) if closed is not None else read_closed_sets(rules)
    cards = list(anchor_book.get("anchors") or [])
    usage = usage_module()
    guard = guard_settings(assembly_for(config))
    with isolated_environment(), tempfile.TemporaryDirectory(prefix="qqagent-bench-cell-") as tmp:
        root = Path(tmp)
        store = _load_soul(root / "soul")
        for run in range(int(runs)):
            round_root = root / ("run%02d" % run)
            brain = BrainStore(round_root / "brain")
            assembly = assembly_for(config)
            for question in _order_for(mode, questions, run, seed):
                got = ask(question, store=store, brain=brain,
                          heart_root=_heart_root(round_root / "heart"),
                          recent_root=round_root / "recent",
                          now=now, who=who, assembly=assembly,
                          transport=transport, config=config_data, env=env_data)
                reply = str(got.get("reply") or "")
                if (got.get("gate") or {}).get("decision") == "dropped":
                    dropped += 1
                per_question[question].append({
                    "run": run, "reply": reply,
                    "a": a_features(reply, rules, closed_sets),
                    "b": b_features(got),
                    "c": c_features(reply, cards, prefix_chars=guard["prefix_chars"],
                                    opening_chars=guard["opening_chars"], module=usage)})
    rows = {question: question_stats(samples) for question, samples in per_question.items()}
    return {"config": config, "mode": mode, "runs": int(runs),
            "questions": list(questions), "dropped": dropped,
            "anchors": {"file": anchor_book.get("file"), "total": len(cards),
                        "available": usage is not None},
            "guard": guard,
            "closed": closed_sets,
            "stats": cell_stats(rows),
            "per_question": {question: {"stats": rows[question],
                                        "samples": per_question[question]}
                             for question in questions}}


def run_bench(*, runs: int = 10, transport: Optional[Callable[..., Any]] = None,
              configs: Sequence[str] = CONFIGS, modes: Sequence[str] = MODES,
              questions: Optional[Sequence[str]] = None, data: Optional[Mapping[str, Any]] = None,
              seed: int = SEED_SAME, shuffle_seed: int = SEED_SHUFFLED,
              now: str = NOW, who: str = WHO,
              model_label: str = "假模型 dry（回答 = 提示词哈希）",
              config_data: Optional[Mapping[str, Any]] = None,
              env_data: Optional[Mapping[str, Any]] = None,
              anchors: Optional[Mapping[str, Any]] = None,
              closed: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """整张表跑一遍：配置 × 跑法，一格一格来（格子之间不共享脑、不共享心、不共享最近 N 轮）。

    锚句**整轮开始前读一次**（就在这儿，没进 isolated_environment：家根切走之后读不到台词库），
    然后一路传给每一格；轮内不再重读。
    """
    source = dict(data) if data is not None else load_data()
    picked = [str(item) for item in (questions or source["questions"])]
    rules = source["rules"]
    anchor_book = dict(anchors) if anchors is not None else read_anchors()
    # 闭集整轮读一次（在 isolated_environment 之外：家根一被切走就读不到魂的实盘卡）。
    closed_sets = dict(closed) if closed is not None else read_closed_sets(rules)
    cells: List[Dict[str, Any]] = []
    with isolated_environment():
        for mode in modes:
            for config in configs:
                cells.append(run_cell(mode, config, runs=runs, questions=picked, rules=rules,
                                      transport=transport,
                                      seed=shuffle_seed if mode == "shuffled" else seed,
                                      now=now, who=who,
                                      config_data=config_data, env_data=env_data,
                                      anchors=anchor_book, closed=closed_sets))
    params = answer_params()
    meta = {
        "model_label": model_label,
        "questions": picked,
        "question_count": len(picked),
        "runs": int(runs),
        "configs": list(configs),
        "modes": list(modes),
        "seed_same": int(seed),
        "seed_shuffled": int(shuffle_seed),
        "now": now,
        "who": who,
        "copy_invalid": COPY_INVALID,
        "dead_same": DEAD_SAME,
        "answer_params": list(params),
        "recent_note": ("每一轮独立的 recent_root（answer 认这个参数）"
                        if "recent_root" in params else
                        "⚠ answer() 还没有 recent_root 参数：最近 N 轮没法隔离到临时目录，"
                        "连续性只来自脑记忆（如实报，不改装配）"),
        "assembly_note": ("answer(assembly=…) 直接喂内存里那份装配说明，不读盘"
                          if "assembly" in params else
                          "⚠ answer() 还没有 assembly 参数：退回临时实盘 "
                          "$QQAGENT_HOME/mind/assembly.json（不读也不改真数据）"),
        "anchors": {"file": anchor_book.get("file"), "total": anchor_book.get("total"),
                    "file_exists": anchor_book.get("file_exists")},
        "usage_ready": usage_module() is not None,
        # A 轨的三件事：怎么判、闭集从哪读、有没有退回 fallback（原样进 --json）。
        "a_note": a_rule_note(rules),
        "closed_note": closed_source_note(closed_sets),
        "fallback_note": closed_fallback_note(closed_sets),
        "closed": closed_sets,
    }
    guard = dict(cells[0]["guard"]) if cells else dict(GUARD_DEFAULTS)
    meta["prefix_chars"] = guard.get("prefix_chars")
    meta["opening_chars"] = guard.get("opening_chars")
    meta["guard_note"] = (
        "前缀 %d 字／开场 %d 字：现读自 assembly.json 的 anchors.guard"
        % (guard.get("prefix_chars"), guard.get("opening_chars"))
        if guard.get("from_assembly") else
        "⚠ assembly.json 的 anchors.guard 还没写 %s：退回文档默认值（前缀 %d 字／开场 %d 字），"
        "如实报，别当配置"
        % ("、".join(guard.get("missing") or []), guard.get("prefix_chars"),
           guard.get("opening_chars")))
    meta["anchors_note"] = (
        "台词库现行 %d 条（整轮读一次：%s）；C 轨用 organs.mind.usage.match 判口癖命中"
        % (anchor_book.get("total") or 0, anchor_book.get("file") or "—")
        if meta["usage_ready"] else
        "⚠ organs.mind.usage 还没落地：C 轨报「未落地」（rate = None），不拿 0 冒充没命中")
    report = {"meta": meta, "cells": cells}
    report["summary"] = summary_stats(report)
    return report


# —— 汇总与报告 ————————————————————————————


def mode_label(mode: str, runs: Any) -> str:
    """跑法的写法：轮数写进标签，免得表头永远写着 10 次而 --runs 明明是 3。"""
    return "%s（%s %s 次）" % (mode, MODE_SHORT.get(mode, mode), runs)


def _cell_index(report: Mapping[str, Any]) -> Dict[Tuple[str, str], Mapping[str, Any]]:
    return {(cell["config"], cell["mode"]): cell for cell in report["cells"]}


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summary_stats(report: Mapping[str, Any]) -> Dict[str, Any]:
    """表下面那三行汇总用的数：两跑法对比、空基线、净增益。"""
    index = _cell_index(report)
    modes = list(report["meta"]["modes"])
    configs = list(report["meta"]["configs"])
    by_mode = {}
    for mode in modes:
        rows = [index[(config, mode)] for config in configs if (config, mode) in index]
        by_mode[mode] = {"a_drift_mean": _mean([row["stats"]["a_drift_rate"] for row in rows]),
                         "b_same_mean": _mean([row["stats"]["b_same_rate"] for row in rows]),
                         "invalid_cells": sum(1 for row in rows if row["stats"]["verdict"] == "无效")}
    baseline = {mode: index.get(("empty", mode)) for mode in modes}
    gain = []
    for mode in modes:
        base = baseline.get(mode)
        if base is None:
            continue
        for config in configs:
            cell = index.get((config, mode))
            if cell is None or config == "empty":
                continue
            gain.append({
                "config": config, "mode": mode,
                "a_drift_delta": cell["stats"]["a_drift_rate"] - base["stats"]["a_drift_rate"],
                "b_same": cell["stats"]["b_same_rate"],
                "b_same_maxed": cell["stats"]["b_same_rate"] >= DEAD_SAME,
            })
    c_rates = {mode: {config: (index[(config, mode)]["stats"].get("c_rate")
                               if (config, mode) in index else None)
                      for config in configs} for mode in modes}
    return {"by_mode": by_mode, "baseline": {mode: (None if cell is None else cell["stats"])
                                            for mode, cell in baseline.items()},
            "gain": gain, "c_rates": c_rates}


def render_table(report: Mapping[str, Any]) -> str:
    """一张 Markdown 表：行 = 配置，列 = same / shuffled，格子里是 A/B 两个率 + C 命中 + 判定。"""
    index = _cell_index(report)
    modes = list(report["meta"]["modes"])
    runs = report["meta"]["runs"]
    lines = ["| 配置 | " + " | ".join(mode_label(mode, runs) for mode in modes) + " |",
             "| --- | " + " | ".join("---" for _ in modes) + " |"]
    for config in report["meta"]["configs"]:
        cells = []
        for mode in modes:
            cell = index.get((config, mode))
            if cell is None:
                cells.append("—")
                continue
            stats = cell["stats"]
            c_rate = stats.get("c_rate")
            cells.append("A 漂移 %.2f · B 全同 %.2f · C 命中 %s · **%s**（第 %d 条）"
                         % (stats["a_drift_rate"], stats["b_same_rate"],
                            "—" if c_rate is None else "%.2f" % c_rate,
                            stats["verdict"], stats["rule"]))
        lines.append("| %s | %s |" % (CONFIG_LABELS.get(config, config), " | ".join(cells)))
    return "\n".join(lines)


def render_reasons(report: Mapping[str, Any]) -> str:
    """每格一行「为什么是这一格」：判词 + 第几条 + 那一句话。"""
    lines = ["判定理由（每格一行，顺序写死：1→2→3→4）："]
    for cell in report["cells"]:
        stats = cell["stats"]
        lines.append("- %s × %s：第 %d 条 —— %s"
                     % (CONFIG_LABELS.get(cell["config"], cell["config"]),
                        mode_label(cell["mode"], cell["runs"]),
                        stats["rule"], stats["why"]))
        if cell["dropped"]:
            lines.append("  - 另有 %d 句被八股闸判 dropped（reply 为空，照实计入这一格）"
                         % cell["dropped"])
    return "\n".join(lines)


def a_rule_note(rules: Mapping[str, Any]) -> str:
    """报告头第一件：A 轨现在的判定口径（集内／越界／跨档）。"""
    edges = "、".join(str(edge) for edge in rules["length"].get("edges") or [])
    labels = "／".join(str(label) for label in rules["length"].get("labels") or [])
    return ("**按闭集判漂移**：称呼 → 集内／越界:<词>，自称 → 集内／越界:<词>；"
            "**集合内换词不算漂移**（魂名单里「我」↔「本鱼」这种），**跳出集合才算**"
            "（出现集合外的自称/称呼，只报第一个越界词）；完全没有自称/称呼记「集内」——"
            "不是越界，也不算漂移。句长量级按档界 ≤%s（%s）落桶，**只在跨档时**算漂移"
            "（不逐字比长度）；自称 AI 命中即「是」；口癖只判命不命中。"
            % (edges, labels))


def closed_source_note(closed: Mapping[str, Any]) -> str:
    """报告头第二件：两套闭集分别从哪儿读的（连词数一起写，好核）。"""
    self_set, address_set = closed["self"], closed["address"]
    return ("自称 ← %s（%d 个词：%s）；称呼 ← %s（%d 个词：%s）"
            % (self_set["source"], len(self_set["words"]), "、".join(self_set["words"]),
               address_set["source"], len(address_set["words"]), "、".join(address_set["words"])))


def closed_fallback_note(closed: Mapping[str, Any]) -> str:
    """报告头第三件：有没有退回 fallback（退回时判不出越界，得如实说）。"""
    fell = ["自称" if name == "self" else "称呼"
            for name in ("self", "address") if closed[name].get("fallback")]
    if not fell:
        return ("没有退回 fallback：两套闭集都是现读的，改魂的自称名单或称呼表，下一次评测就换口径。"
                "data/questions.json 里的 self／address 两份词表这时候只是扫词表——它决定"
                "「扫得见哪些词」，不决定「哪些算合规」。")
    return ("⚠ 退回了 fallback：%s 读不到闭集，用 data/questions.json 里的 fallback 顶替；"
            "退回时「集合外」判不出来（扫到的词必然都在这份表里），那一路只算连通性冒烟，不是数字。"
            % "、".join(fell))


def render_meta(report: Mapping[str, Any]) -> str:
    """报告头：读数口径、种子、判定顺序、以及装配注入的现状（改不动的地方如实写）。"""
    meta = report["meta"]
    caveats = []
    if meta["runs"] < 2:
        caveats.append("- ⚠ 轮数 < 2：每格只采了一次，B 全同必然 1.00 → 一律判「无效」，"
                       "这张表只算连通性冒烟，不是数字。")
    return "\n".join(caveats + [
        "# 一致性评测 · %s" % meta["model_label"],
        "- 题数 %d · 每格 %d 轮 · 同序种子 %d（题序不重排，只用来记账）· 洗牌种子 %d · 固定时刻 %s"
        % (meta["question_count"], meta["runs"], meta["seed_same"], meta["seed_shuffled"],
           meta["now"]),
        "- 心与嘴四格都在：那是她此刻的状态与说话的规矩，不是人设与记忆（所以四格都带心与嘴）。",
        "- A 轨怎么判（不该变）：%s" % meta.get("a_note", "口径未报"),
        "- A 轨的闭集从哪读：%s" % meta.get("closed_note", "未报"),
        "- A 轨有没有退回 fallback：%s" % meta.get("fallback_note", "未报"),
        "- B 轨（该变）：措辞（原文哈希）／回应长度（字数）／情绪档位（heart.mood.tier）。"
        "提取全是确定性匹配与计数，零 LLM。",
        "- C 轨（**不进判定**）：每句用 organs.mind.usage.match 判一次口癖命中，C 命中率 = "
        "该格所有回答里命中的比例；%s" % meta.get("anchors_note", "未报"),
        "- 锚句前缀口径：%s" % meta.get("guard_note", "未报"),
        "- 判定顺序写死：1) 逐题相同率（B 三特征十次全同的题占比）≥ %.2f → 无效（疑似复制）；"
        "2) 否则 A 漂移率 > 0 → 换人；3) 否则 B 全同率（措辞逐字相同的题占比）== %.2f → 死板；"
        "4) 否则 通过。" % (meta["copy_invalid"], meta["dead_same"]),
        "- 装配：empty/soul/memory/both 只切装配说明书里的 soul 与 memory 两段（锚句与最近 N 轮"
        "四格都在）；心根用出厂卡、每轮全新。",
        "- 最近 N 轮：%s" % meta["recent_note"],
        "- 装配注入：%s" % meta["assembly_note"],
    ])


def render_summary(report: Mapping[str, Any]) -> str:
    """表下三行汇总：两跑法对比（只报同序会高估）、空基线、净增益。"""
    meta = report["meta"]
    stats = report["summary"]
    by_mode = stats["by_mode"]
    modes = list(meta["modes"])
    lines = []

    same = by_mode.get("same")
    shuffled = by_mode.get("shuffled")
    if same and shuffled:
        gap = same["b_same_mean"] - shuffled["b_same_mean"]
        if gap > 1e-9:
            tail = "洗牌把 B 全同率压低 %.2f —— **只报同序会高估**（顺序本身就是一致性的来源）" % gap
        elif meta["runs"] < 2:
            tail = ("轮数 < 2（每格只采了一次），两跑法不可比；**只报同序会高估**是真模型上的"
                    "已知结论，先按 --runs 10 再谈数字")
        else:
            tail = ("本次两跑法数字相同（假模型不随上下文改写），但**只报同序会高估**是真模型上的"
                    "已知结论：顺序本身就是一致性的来源，别拿这一格当反证")
        lines.append("- 同序 vs 洗牌（四格均值）：同序 A 漂移 %.2f／B 全同 %.2f；洗牌 A 漂移 %.2f／"
                     "B 全同 %.2f。%s" % (same["a_drift_mean"], same["b_same_mean"],
                                          shuffled["a_drift_mean"], shuffled["b_same_mean"], tail))

    baseline = stats["baseline"]
    parts = []
    for mode in modes:
        row = baseline.get(mode)
        if row is None:
            continue
        parts.append("%s：A 漂移 %.2f／B 全同 %.2f → %s（第 %d 条）"
                     % (mode_label(mode, meta["runs"]), row["a_drift_rate"],
                        row["b_same_rate"], row["verdict"], row["rule"]))
    lines.append("- 空基线（empty 行，不带魂也不带记忆）：" + ("；".join(parts) if parts else "未跑"))

    for mode in modes:
        rows = [row for row in stats["gain"] if row["mode"] == mode]
        if not rows:
            continue
        chunks = []
        for row in rows:
            maxed = "B 全同被拉满 → 判死板" if row["b_same_maxed"] else "B 全同未拉满"
            chunks.append("%s：ΔA 漂移 %+.2f／B 全同 %.2f（%s）"
                          % (row["config"], row["a_drift_delta"], row["b_same"], maxed))
        lines.append("- 净增益（相对 empty 同跑法，%s）：%s"
                     % (mode_label(mode, meta["runs"]), "；".join(chunks)))

    c_rates = stats.get("c_rates") or {}
    parts = []
    for mode in modes:
        rates = c_rates.get(mode) or {}
        parts.append("%s：%s" % (mode_label(mode, meta["runs"]),
                                 "／".join("%s %s" % (config, "—" if rate is None else "%.2f" % rate)
                                           for config, rate in rates.items())))
    lines.append("- C 口癖命中率（四格两跑法）：%s。这一列**不进判定**：判定仍是 1→2→3→4 那四档，"
                 "C 只是趋势读数（假模型不会用台词，C 应当是 %.2f）。"
                 % ("；".join(parts) if parts else "未跑", 0.0))
    return "\n".join(lines)


def render_report(report: Mapping[str, Any]) -> str:
    """整份报告：头 + 表 + 判定理由 + 汇总（含只读数不判定的 C 列）。"""
    return "\n\n".join([render_meta(report), render_table(report),
                        render_reasons(report), render_summary(report)])


# —— 命令行 ————————————————————————————————


def preflight(*, transport: Optional[Callable[..., Any]] = None,
              config_data: Optional[Mapping[str, Any]] = None,
              env_data: Optional[Mapping[str, Any]] = None,
              question: Optional[str] = None) -> None:
    """先探一句：真模型缺 key 就当场把 LLMError 的人话抛出来，别跑完整张表再装死。"""
    data = load_data()
    with isolated_environment():
        with tempfile.TemporaryDirectory(prefix="qqagent-bench-preflight-") as tmp:
            root = Path(tmp)
            ask(question or data["questions"][0], store=_load_soul(root / "soul"),
                brain=BrainStore(root / "brain"),
                heart_root=_heart_root(root / "heart"),
                recent_root=root / "recent", now=NOW, who=WHO,
                assembly=assembly_for("empty"), transport=transport,
                config=config_data, env=env_data)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m organs.mind.bench.consistency",
        description="一致性评测：三种跑法 × 四格配置、A/B 两条轨道、三态判定。")
    parser.add_argument("--runs", type=int, default=10, help="每格跑几轮（默认 10）")
    parser.add_argument("--questions", default=None, help="题目与规则的数据文件（默认 data/questions.json）")
    parser.add_argument("--limit", type=int, default=0, help="只用前 N 道题（冒烟用，0 = 全部）")
    parser.add_argument("--seed", type=int, default=SEED_SAME, help="同序种子（记账用，默认 %d）" % SEED_SAME)
    parser.add_argument("--shuffle-seed", type=int, default=SEED_SHUFFLED,
                        help="洗牌种子（默认 %d）" % SEED_SHUFFLED)
    parser.add_argument("--now", default=NOW, help="固定时刻（默认 %s）" % NOW)
    parser.add_argument("--json", nargs="?", const="-", default=None,
                        metavar="路径", help="把原始统计打成 JSON：不给值 = 打到 stdout，给路径 = 写文件")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry", action="store_true",
                       help="确定性假模型：回答由提示词哈希决定（同序多轮会判出无效轮，这是对的）")
    group.add_argument("--template", action="store_true",
                       help="固定模板假模型：演示「完全相同 → 无效轮」")
    group.add_argument("--vary", action="store_true",
                       help="确定性回答 + 轮次计数：演示「不无效、也不死板」的通过格")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.runs < 1:
        print("--runs 至少 1（2 以上才谈得上「漂移」）", file=sys.stderr)
        return 2
    data = load_data(args.questions)
    questions = data["questions"][:args.limit] if args.limit else data["questions"]

    transport: Optional[Callable[..., Any]] = None
    config_data: Optional[Mapping[str, Any]] = None
    env_data: Optional[Mapping[str, Any]] = None
    if args.dry:
        label, transport = "假模型 dry（回答 = 提示词哈希，提示词变了才变）", hash_transport()
        config_data, env_data = FAKE_CONFIG, FAKE_ENV
    elif args.template:
        label, transport = "假模型 template（固定模板回答）", template_transport()
        config_data, env_data = FAKE_CONFIG, FAKE_ENV
    elif args.vary:
        label, transport = "假模型 vary（确定性回答 + 轮次计数）", vary_transport()
        config_data, env_data = FAKE_CONFIG, FAKE_ENV

    if transport is None:
        from organs.mind import llm
        try:
            preflight(transport=None, config_data=config_data, env_data=env_data)
            source = llm.resolve_config(config_data)
        except llm.LLMError as exc:                   # 缺 key / 配置错：人话原样打，退出码非 0
            print(str(exc), file=sys.stderr)
            return 2
        label = "真模型 %s（key 来自 .env；真模型数字等 key 到位再跑）" % source.get("model", "?")
    else:
        preflight(transport=transport, config_data=config_data, env_data=env_data,
                  question=questions[0])

    report = run_bench(runs=args.runs, transport=transport, questions=questions, data=data,
                       seed=args.seed, shuffle_seed=args.shuffle_seed, now=args.now,
                       model_label=label, config_data=config_data, env_data=env_data)
    print(render_report(report))

    if args.json is not None:
        blob = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        if args.json == "-":
            print("")
            print(blob)
        else:
            Path(args.json).write_text(blob + "\n", encoding="utf-8")
            print("\n原始统计已写入 %s" % args.json)
    return 0


if __name__ == "__main__":                            # pragma: no cover
    raise SystemExit(main())