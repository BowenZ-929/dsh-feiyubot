#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""说话像不像人的回归量尺：20 条固定探针 → 真链路回答 → 三个指标 + 一行账。

跑一章前先读 docs/organs/mind/style-baseline.md（口径、怎么读、探针冻结规则）。

怎么跑：
    python3 bench/style_probe.py --check                      # 只查探针集（20 条、七类、群聊 2 条）
    python3 bench/style_probe.py --runs 1 --dry-run           # 不接模型：固定假回复跑通量尺
    python3 bench/style_probe.py                              # 真链路（真 transport），runs 取口径里的默认值
    python3 bench/style_probe.py --too-short                  # 额外两行：同心的对照行 + max_chars_low=8 的实验行

本文件只做四件事：量（句长／句末标点／八股命中）、比（对语料）、判（区间在数据里）、记（表格一行）。
所有阈值、窗口、3-gram 长度、探针版本、时刻、称呼都来自 bench/data/style_baseline.json——
改口径＝改数据，不改这份代码。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import shutil
import statistics
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench"
PLUGIN = ROOT / "plugin"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

NA = "N/A"
BASELINE_FILE = BENCH / "data" / "style_baseline.json"


# —— 读料 ——————————————————————————————————————


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_baseline(path: Optional[Path] = None) -> Dict[str, Any]:
    return _read_json(Path(path) if path is not None else BASELINE_FILE)


def _path_in_repo(rel: str) -> Path:
    return ROOT / rel


def load_probes(path: Optional[Path] = None, baseline: Optional[Mapping[str, Any]] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """探针集：第一行是冻结说明（没有 id），其余每行一条探针。坏行当场抛，不悄悄跳过。"""
    target = Path(path) if path is not None else _path_in_repo(str((baseline or load_baseline())["probes"]))
    meta: Dict[str, Any] = {}
    rows: List[Dict[str, Any]] = []
    for number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError("探针第 %d 行不是 JSON：%s" % (number, exc))
        if not isinstance(row, dict):
            raise ValueError("探针第 %d 行不是对象" % number)
        if "id" not in row:
            meta.update(row)          # 冻结说明那一行：只认键值，不参与跑
            continue
        rows.append(row)
    return meta, rows


def check_probes(probes: Sequence[Mapping[str, Any]], baseline: Mapping[str, Any]) -> List[str]:
    """探针集的自检：条数、七类覆盖、场景计数、id 唯一、必填栏、文字不像模板。"""
    problems: List[str] = []
    categories = baseline["categories"]
    min_per = int(baseline["min_per_category"])
    scenes = baseline["scenes"]
    wanted = int(baseline["probe_count"])
    if len(probes) != wanted:
        problems.append("条数是 %d，口径冻结的是 %d 条" % (len(probes), wanted))
    seen: set = set()
    counts: Counter = Counter()
    for probe in probes:
        pid = str(probe.get("id") or "")
        if not pid:
            problems.append("有一条探针没有 id")
            continue
        if pid in seen:
            problems.append("id 重复：%s" % pid)
        seen.add(pid)
        category = str(probe.get("category") or "")
        if category not in categories:
            problems.append("%s 的 category 不认识：%r" % (pid, category))
        counts[category] += 1
        scene = str(probe.get("scene") or "")
        if scene not in scenes["allowed"]:
            problems.append("%s 的 scene 不认识：%r" % (pid, scene))
        text = probe.get("text")
        if not isinstance(text, str) or not text.strip():
            problems.append("%s 没有 text" % pid)
    for category in categories:
        if counts[category] < min_per:
            problems.append("类别 %s 只有 %d 条（至少要 %d 条）" % (category, counts[category], min_per))
    groups = sum(1 for probe in probes if probe.get("scene") == "group")
    if not (int(scenes["group_min"]) <= groups <= int(scenes["group_max"])):
        problems.append("群聊探针 %d 条，口径要 %s–%s 条" % (groups, scenes["group_min"], scenes["group_max"]))
    return problems


def probe_version(probes_file: Path, baseline: Mapping[str, Any]) -> str:
    """版本 = 口径里的版号 + 探针文件内容的短哈希：探针被改过，版本一定跟着变。"""
    digest = hashlib.sha256(Path(probes_file).read_bytes()).hexdigest()[:8]
    return "%s/%s" % (baseline["probe_version"], digest)


# —— 字面量（口径都在数据里，代码只做算术）————————————————


def sentences(text: str, ends: str) -> List[str]:
    """按句末标点切句；换行也是分隔（不把换行算进任何一句）。空段一律不返回。"""
    rows: List[str] = []
    buf: List[str] = []
    for char in text or "":
        if char in ends:
            if char != "\n":
                buf.append(char)
            if buf:
                rows.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    if buf:
        rows.append("".join(buf))
    return [row for row in rows if row.strip()]


def _body_chars(sentence: str, ends: str) -> int:
    """一句的字数：去掉首尾空白与句末标点之后的字数（标点不算字）。"""
    body = sentence.strip()
    while body and (body[-1] in ends or body[-1].isspace()):
        body = body[:-1]
    return len(body.strip())


def sentence_lengths(text: str, ends: str) -> List[int]:
    return [size for size in (_body_chars(row, ends) for row in sentences(text, ends)) if size > 0]


def reply_length(text: str) -> int:
    """一句回话的字数：原文去掉所有空白后的字数（含标点，篇幅就是这么量的）。"""
    return len("".join((text or "").split()))


def endings(text: str, ends: str) -> Counter:
    """句末标点分布：每句的最后一个字落在句末表里的那个标点；没有就是「（无）」。"""
    counter: Counter = Counter()
    for row in sentences(text, ends):
        last = row.rstrip()[-1:] or ""
        counter[last if last in ends else "（无）"] += 1
    return counter


def opening(text: str, strip_chars: str, window: int) -> str:
    """句式归一：去掉标点／空白后取前 window 个字——「同一句式」就是这串字相同。"""
    body = "".join(char for char in (text or "") if char not in strip_chars)
    return body[: int(window)]


def ngram_vector(text: str, size: int) -> Counter:
    body = "".join((text or "").split())
    size = int(size)
    if size <= 0 or len(body) < size:
        return Counter()
    return Counter(body[index: index + size] for index in range(len(body) - size + 1))


def cosine(left: Mapping[str, int], right: Mapping[str, int]) -> Optional[float]:
    """字符 n-gram 余弦：两边都是计数向量，口径写进 docs。空向量不算 0，算「没得比」（None）。"""
    if not left or not right:
        return None
    dot = sum(count * right.get(gram, 0) for gram, count in left.items())
    left_norm = sum(count * count for count in left.values()) ** 0.5
    right_norm = sum(count * count for count in right.values()) ** 0.5
    if not left_norm or not right_norm:
        return None
    return dot / (left_norm * right_norm)


def median(values: Sequence[float]) -> Optional[float]:
    rows = [float(value) for value in values]
    return statistics.median(rows) if rows else None


def ratio(left: Optional[float], right: Optional[float]) -> Optional[float]:
    if left is None or right is None or not right:
        return None
    return left / right


def repeated_share(signatures: Sequence[str]) -> Optional[float]:
    """同一句式在整批里重复出现的比例：落在「出现 >=2 次」的句式里的那几条 / 全部。"""
    rows = [signature for signature in signatures if signature]
    if not rows:
        return None
    counts = Counter(rows)
    return sum(1 for signature in rows if counts[signature] >= 2) / float(len(rows))


def text_signatures(texts: Iterable[str], strip_chars: str, window: int) -> List[str]:
    return [opening(text, strip_chars, window) for text in texts]


# —— 语料（不在仓库里就如实说没有）————————————————————


def read_corpus(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """读 legacy.jsonl：每行 {"role","text"}。缺文件／条数不够都算「没有语料」，不拿别的料顶替。"""
    path = _path_in_repo(str(spec["path"]))
    out: Dict[str, Any] = {"available": False, "reason": "", "path": str(path),
                           "assistant": [], "user": [], "bad_lines": 0}
    if not path.is_file():
        out["reason"] = "语料不在（%s）" % path
        return out
    assistant_role = str(spec["assistant_role"])
    user_role = str(spec["user_role"])
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            out["bad_lines"] += 1
            continue
        if not isinstance(row, dict):
            out["bad_lines"] += 1
            continue
        role, text = str(row.get("role") or ""), row.get("text")
        if not isinstance(text, str) or not text.strip():
            out["bad_lines"] += 1
            continue
        if role == assistant_role:
            out["assistant"].append(text)
        elif role == user_role:
            out["user"].append(text)
    need = int(spec["min_texts_per_role"])
    if len(out["assistant"]) < need or len(out["user"]) < need:
        out["reason"] = "条数不够（assistant %d／user %d，各要至少 %d）" % (
            len(out["assistant"]), len(out["user"]), need)
        return out
    out["available"] = True
    return out


def human_side(corpus: Mapping[str, Any], text_spec: Mapping[str, Any]) -> Dict[str, Any]:
    """人类那一侧的三把尺：句长中位、回复长中位、模板复现率（只看 role=user）。"""
    ends = str(text_spec["_ends"])
    strip_chars = str(text_spec["strip_chars"])
    window = int(text_spec["template_window"])
    users = list(corpus["user"])
    lengths: List[int] = []
    for text in users:
        lengths.extend(sentence_lengths(text, ends))
    return {
        "sentence_median": median(lengths),
        "reply_median": median([reply_length(text) for text in users]),
        "template_rate": repeated_share(text_signatures(users, strip_chars, window)),
    }


# —— 跑真链路 ————————————————————————————————————


def _soul(store_dir: Path) -> Any:
    from organs.soul import load as load_soul
    shutil.copytree(str(PLUGIN / "organs" / "soul" / "fixtures" / "valid"), str(store_dir))
    return load_soul(store_dir)


def _heart_dir(target: Path, source: Path, profile: Optional[Mapping[str, Any]], now: str) -> Path:
    """每一轮一个全新心根：拷源心里的 JSON（日志不拷），再按档写 energy/mood。

    profile 为 None 就照源读数走（正常工作状态）；给了档就把它钉死——两行实验只差
    tone_rules.max_chars_low 一个字段，所以心的读数必须逐字相同。
    """
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted(Path(source).glob("*.json")):
        shutil.copyfile(str(item), str(target / item.name))
    if profile is None:
        return target
    day = now[:10]
    (target / "energy.json").write_text(json.dumps({
        "version": 1, "value": float(profile["energy"]), "updated_at": now, "day": day,
        "deducted": 0.0, "last_burst_at": None}, ensure_ascii=False), encoding="utf-8")
    (target / "mood.json").write_text(json.dumps({
        "version": 1, "now": float(profile["mood"]), "baseline": float(profile["mood"]),
        "updated_at": now, "day": day, "conflict_used": 0, "window": {}}, ensure_ascii=False),
        encoding="utf-8")
    return target


@contextlib.contextmanager
def sandbox(mind_source: Optional[Path]) -> Iterable[Dict[str, Any]]:
    """评测期把四个根都切到临时目录：仓库一个字节都不写（含 home/**、嘴的账本、语气账）。

    mind_source 给了就把那份实盘 mind 的 assembly.json／llm.json 拷进临时根（读数按实盘走，
    写落点仍是临时目录）；不给（--dry-run）就走纯出厂，好让干跑可复现。
    """
    tmp = Path(tempfile.mkdtemp(prefix="qqagent-style-"))
    home = tmp / "home"
    (home / "mind").mkdir(parents=True)
    copied: List[str] = []
    if mind_source is not None and Path(mind_source).is_dir():
        for name in ("assembly.json", "llm.json"):
            source = Path(mind_source) / name
            if source.is_file():
                shutil.copyfile(str(source), str(home / "mind" / name))
                copied.append(name)
    roots = {"QQAGENT_HOME": home, "HEART_ROOT": tmp / "heart",
             "MOUTH_TRACE_DIR": tmp / "trace", "HOME_ROOT": tmp / "home_root"}
    for path in roots.values():
        Path(path).mkdir(parents=True, exist_ok=True)
    previous = {name: os.environ.get(name) for name in roots}
    for name, path in roots.items():
        os.environ[name] = str(path)
    try:
        yield {"tmp": tmp, "home": home, "copied": copied}
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _dry_transport(reply: str) -> Callable[..., Tuple[int, bytes]]:
    """干跑的假模型：不管提示词是什么，都回同一句固定的话（--dry-run 专用）。"""
    def send(url: Any, headers: Any, payload: Mapping[str, Any], timeout: Any) -> Tuple[int, bytes]:
        body = {"choices": [{"message": {"content": reply}}]}
        return 200, json.dumps(body, ensure_ascii=False).encode("utf-8")
    return send


def run_arm(*, probes: Sequence[Mapping[str, Any]], runs: int, assembly: Mapping[str, Any],
            work: Path, mind_source: Path, profile: Optional[Mapping[str, Any]],
            baseline: Mapping[str, Any], dry: Optional[Mapping[str, str]],
            config: Optional[Mapping[str, Any]], env: Optional[Mapping[str, str]]) -> List[Dict[str, Any]]:
    """一条臂 = 全批探针各跑 runs 次；每次都是真链路 answer，只是模型那一步可换。"""
    from organs.brain import BrainStore
    from organs.mind import conversation

    now = str(baseline["now"])
    who = str(baseline["who"])
    scene_default = str(baseline["scenes"]["default"])
    rounds: List[Dict[str, Any]] = []
    for run_index in range(1, int(runs) + 1):
        for probe in probes:
            pid = str(probe["id"])
            cell = work / ("r%03d-%s" % (run_index, pid))
            cell.mkdir(parents=True, exist_ok=True)
            heart = _heart_dir(cell / "heart", mind_source, profile, now)
            transport = _dry_transport(dry[pid]) if dry is not None else None
            record: Dict[str, Any] = {"probe": pid, "category": str(probe["category"]),
                                      "scene": str(probe.get("scene") or scene_default),
                                      "run": run_index, "error": "", "reply": "",
                                      "dropped": False, "clause_hit": False, "mouth_hit": None,
                                      "tone": None}
            try:
                payload = conversation.answer(
                    str(probe["text"]), who, scene=record["scene"], store=_soul(cell / "soul"),
                    brain=BrainStore(cell / "brain"), heart_root=heart,
                    recent_root=cell / "recent", usage_root=cell / "usage", now=now,
                    transport=transport, config=config if transport is not None else None,
                    env=env if transport is not None else None, assembly=dict(assembly))
                gate = payload.get("gate") or {}
                record["reply"] = str(payload.get("reply") or "")
                record["dropped"] = str(gate.get("decision") or "") == "dropped"
                record["mouth_hit"] = ((gate.get("hit") or {}).get("clause_id")) if gate.get("hit") else None
                record["tone"] = {"max_chars": (payload.get("tone") or {}).get("max_chars"),
                                  "truncated": (payload.get("tone") or {}).get("truncated"),
                                  "tier": (payload.get("tone") or {}).get("tier")}
            except Exception as exc:                      # 真链路失败如实记，不拿假话填
                record["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            rounds.append(record)
    return rounds


# —— 量 ——————————————————————————————————————


def measure(rounds: Sequence[Mapping[str, Any]], text_spec: Mapping[str, Any]) -> Dict[str, Any]:
    """这一批的读数：句长中位、句末标点分布、八股命中率、模板复现率、回复长中位。"""
    ends = str(text_spec["_ends"])
    strip_chars = str(text_spec["strip_chars"])
    window = int(text_spec["template_window"])
    lived = [row for row in rounds if not row.get("error")]
    replies = [str(row.get("reply") or "") for row in lived if str(row.get("reply") or "").strip()]
    lengths: List[int] = []
    ending_counter: Counter = Counter()
    for reply in replies:
        lengths.extend(sentence_lengths(reply, ends))
        ending_counter.update(endings(reply, ends))
    total_sentences = sum(ending_counter.values())
    hits = sum(1 for row in lived
               if row.get("mouth_hit") or _clause_hit(str(row.get("reply") or ""), text_spec))
    per_run: Dict[int, List[str]] = {}
    for row in lived:
        per_run.setdefault(int(row.get("run") or 0), []).append(str(row.get("reply") or ""))
    rates = [repeated_share(text_signatures(rows, strip_chars, window)) for rows in per_run.values()]
    rates = [rate for rate in rates if rate is not None]
    period = ending_counter.get("。", 0)
    return {
        "rounds": len(rounds),
        "lived": len(lived),
        "errors": len(rounds) - len(lived),
        "dropped": sum(1 for row in lived if row.get("dropped")),
        "replies": len(replies),
        "sentence_median": median(lengths),
        "endings": dict(ending_counter),
        "period_rate": (period / float(total_sentences)) if total_sentences else None,
        "clause_rate": (hits / float(len(lived))) if lived else None,
        "clause_hits": hits,
        "template_rate": (sum(rates) / float(len(rates))) if rates else None,
        "reply_median": median([reply_length(reply) for reply in replies]),
        "text": "\n".join(replies),
    }


def _clause_hit(reply: str, text_spec: Mapping[str, Any]) -> bool:
    """拿嘴现行的条款表再判一次返回原文（只读调用）。

    注意：能返回来的句子本来就过了闸，所以这里多半读到 0——「这一轮模型有没有说八股」
    主要靠 answer 回包里的 gate.hit（见 measure 的 hits 那一处）。两处合起来才算判过。
    """
    from organs.mouth import gate as gate_module
    from organs.mouth import warehouse
    if not reply.strip():
        return False
    clauses = warehouse.load("clauses").get("clauses")
    return gate_module.check(reply, clauses) is not None


def verdict_of(metrics: Mapping[str, Any], reads: Mapping[str, Any], ranges: Mapping[str, Any]) -> List[str]:
    """判定：区间全在数据里。低于下限 too-short、高于上限 too-long、模板率超限 too-template。"""
    flags: List[str] = []
    if not reads.get("available"):
        flags.append("no-corpus")                     # 没语料就是没比过：不写 ok，也不拿她的回话顶替
    else:
        sentence_ratio = ratio(metrics.get("sentence_median"), reads.get("sentence_median"))
        reply_ratio = ratio(metrics.get("reply_median"), reads.get("reply_median"))
        low, high = ranges["sentence_ratio"]
        if sentence_ratio is not None:
            if sentence_ratio < float(low):
                flags.append("too-short")
            elif sentence_ratio > float(high):
                flags.append("too-long")
        low, high = ranges["reply_ratio"]
        if reply_ratio is not None:
            if reply_ratio < float(low):
                flags.append("too-short")
            elif reply_ratio > float(high):
                flags.append("too-long")
        template_rate = metrics.get("template_rate")
        if template_rate is not None and template_rate > float(ranges["template_rate_max"]):
            flags.append("too-template")
    if not metrics.get("replies"):
        flags.append("no-replies")            # 一句都没剩下：没数可判，不是 ok
    if metrics.get("errors"):
        flags.append("errors=%d" % int(metrics["errors"]))
    if not flags:
        flags.append("ok")
    return flags


# —— 记 ——————————————————————————————————————


TABLE_HEADER = (
    "| 时间 | 探针版本 | runs | 句长中位 | 句末句号率 | 八股命中率 | 指标1 相似度 "
    "| 指标2a 句长比 | 指标2b 模板复现率 | 指标2c 回复长比 | 判定 |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
)


def _cell(value: Optional[float], digits: int) -> str:
    return NA if value is None else ("%." + str(digits) + "f") % value


def table_line(stamp: str, version: str, runs: int, metrics: Mapping[str, Any],
               reads: Mapping[str, Any], similarity: Optional[float], flags: Sequence[str]) -> str:
    available = bool(reads.get("available"))
    sentence_ratio = ratio(metrics.get("sentence_median"), reads.get("sentence_median")) if available else None
    reply_ratio = ratio(metrics.get("reply_median"), reads.get("reply_median")) if available else None
    template_rate = metrics.get("template_rate") if available else None
    cells = [
        stamp, version, str(runs),
        _cell(metrics.get("sentence_median"), 1),
        _cell(metrics.get("period_rate"), 3),
        _cell(metrics.get("clause_rate"), 3),
        _cell(similarity if available else None, 3),
        _cell(sentence_ratio, 3),
        _cell(template_rate, 3),
        _cell(reply_ratio, 3),
        " ".join(flags),
    ]
    return "| " + " | ".join(cells) + " |\n"


def append_table(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or not path.read_text(encoding="utf-8").strip():
        path.write_text(TABLE_HEADER + line, encoding="utf-8")
        return
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + line, encoding="utf-8")


def write_detail(path: Path, header: Mapping[str, Any], rounds: Sequence[Mapping[str, Any]],
                 text_spec: Mapping[str, Any]) -> None:
    """逐条读数（覆盖写）：回复原文、句长、句末标点、八股命中都在这里，表格只放汇总。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ends = str(text_spec["_ends"])
    rows: List[str] = [json.dumps({"_run": dict(header)}, ensure_ascii=False)]
    for row in rounds:
        reply = str(row.get("reply") or "")
        lengths = sentence_lengths(reply, ends)
        rows.append(json.dumps({
            "arm": row.get("arm"), "probe": row.get("probe"), "category": row.get("category"),
            "scene": row.get("scene"), "run": row.get("run"),
            "reply": reply,
            "sentence_lengths": lengths,
            "sentence_median": median(lengths),
            "ending": dict(endings(reply, ends)),
            "dropped": bool(row.get("dropped")), "mouth_hit": row.get("mouth_hit"),
            "clause_hit": bool(row.get("clause_hit")), "tone": row.get("tone"),
            "error": str(row.get("error") or ""),
        }, ensure_ascii=False))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


# —— 一条臂从头到尾 ————————————————————————————————


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def _mind_source(dry: bool) -> Tuple[Optional[Path], str]:
    """实盘的 mind 目录从哪来：环境变量 QQAGENT_HOME > <仓库>/home/organs，再看 mind 子目录。

    --dry-run 一律用出厂那份（可复现），所以这里返回 None。
    """
    if dry:
        return None, "出厂（dry-run 不看实盘）"
    base = os.environ.get("QQAGENT_HOME") or str(ROOT / "home" / "organs")
    candidate = Path(base) / "mind"
    return candidate, str(candidate)


def _heart_source() -> Path:
    env = os.environ.get("HEART_ROOT")
    return Path(env) if env else PLUGIN / "organs" / "heart" / "data"


def _assembly_with(max_chars_low: Optional[float]) -> Dict[str, Any]:
    """装配说明：实盘那份（缺就出厂），再按需要只改内存里的 tone_rules.max_chars_low。"""
    from organs.mind import conversation
    assembly = copy.deepcopy(conversation.load_assembly())
    if max_chars_low is not None:
        assembly["tone_rules"]["max_chars_low"] = max_chars_low
    return assembly


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="八股／人味回归量尺（20 条固定探针走真链路）")
    parser.add_argument("--runs", type=int, default=None, help="每条探针跑几次（默认取口径里的 runs_default）")
    parser.add_argument("--dry-run", action="store_true", help="不接模型：用 bench/data/dry_replies.json 的固定假回复")
    parser.add_argument("--too-short", action="store_true", help="实验：同心的对照行 + max_chars_low 改成口径里那个数的一行")
    parser.add_argument("--check", action="store_true", help="只查探针集（20 条／七类／群聊 2 条）")
    parser.add_argument("--table", default=None, help="表格路径（默认取口径）")
    parser.add_argument("--detail", default=None, help="逐条读数路径（默认取口径）")
    parser.add_argument("--no-table", action="store_true", help="只打读数，不写表格")
    parser.add_argument("--baseline", default=None, help="口径文件（默认 bench/data/style_baseline.json）")
    args = parser.parse_args(argv)

    baseline = load_baseline(Path(args.baseline) if args.baseline else None)
    probes_file = _path_in_repo(str(baseline["probes"]))
    meta, probes = load_probes(probes_file, baseline)
    problems = check_probes(probes, baseline)
    if args.check:
        return _print_check(meta, probes, baseline, problems)
    if problems:
        for problem in problems:
            print("探针集不合格：%s" % problem, file=sys.stderr)
        return 1

    runs = int(args.runs if args.runs is not None else baseline["runs_default"])
    if runs < 1:
        print("--runs 至少 1", file=sys.stderr)
        return 1

    dry_replies: Optional[Dict[str, str]] = None
    if args.dry_run:
        source = _read_json(_path_in_repo(str(baseline["dry"]["replies"])))
        dry_replies = {str(key): str(value) for key, value in dict(source["replies"]).items()}
        missing = [str(probe["id"]) for probe in probes if str(probe["id"]) not in dry_replies]
        if missing:
            print("干跑缺固定假回复：%s" % "、".join(missing), file=sys.stderr)
            return 1

    mind_source, mind_label = _mind_source(bool(args.dry_run))
    heart_source = _heart_source()
    text_spec = dict(baseline["text"])
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    version = probe_version(probes_file, baseline)
    table_path = Path(args.table) if args.table else _path_in_repo(str(baseline["table"]))
    detail_path = Path(args.detail) if args.detail else _path_in_repo(str(baseline["detail"]))

    watched = [path for path in (Path(mind_source) / "assembly.json" if mind_source else None,
                                 PLUGIN / "organs" / "mind" / "data" / "assembly.json") if path]
    before = {str(path): _sha(path) for path in watched}

    corpus = read_corpus(baseline["corpus"])
    with sandbox(mind_source) as env_box:
        from organs.mind import conversation
        text_spec["_ends"] = str(conversation.load_assembly()["tone_rules"]["sentence_end_chars"])
        reads = human_side(corpus, text_spec) if corpus["available"] else {"available": False}
        reads["available"] = bool(corpus["available"])
        assembly = _assembly_with(None)

        profiles = baseline["heart"]["profiles"]
        too_short_low = baseline["experiment"]["too_short_max_chars_low"]
        arms: List[Dict[str, Any]] = [{"name": "normal", "profile": profiles.get("source"),
                                       "limit": None, "tag": ""}]
        if args.too_short:
            arms.append({"name": "too-short-control", "profile": profiles.get("low"),
                         "limit": None, "tag": "experiment=too-short-control"})
            arms.append({"name": "too-short", "profile": profiles.get("low"),
                         "limit": too_short_low, "tag": "experiment=too-short"})

        all_rounds: List[Dict[str, Any]] = []
        lines: List[str] = []
        summaries: List[Dict[str, Any]] = []
        failed = 0
        for arm in arms:
            work = Path(env_box["tmp"]) / ("arm-" + arm["name"])
            assembly_arm = _assembly_with(arm["limit"])
            rounds = run_arm(probes=probes, runs=runs, assembly=assembly_arm, work=work,
                             mind_source=heart_source,
                             profile=arm["profile"], baseline=baseline, dry=dry_replies,
                             config=(dict(baseline["dry"]["config"]) if dry_replies else None),
                             env=(dict(baseline["dry"]["env"]) if dry_replies else None))
            for row in rounds:
                row["arm"] = arm["name"]
            metrics = measure(rounds, text_spec)
            similarity = (cosine(ngram_vector(metrics["text"], text_spec["ngram"]),
                                 ngram_vector("\n".join(corpus["assistant"]), text_spec["ngram"]))
                          if corpus["available"] else None)
            flags = verdict_of(metrics, reads, baseline["ranges"])
            if arm["tag"]:
                flags.append(arm["tag"])
            line = table_line(stamp, version, runs, metrics, reads, similarity, flags)
            lines.append(line)
            failed += int(metrics["errors"])
            all_rounds.extend(rounds)
            summaries.append({"arm": arm["name"], "metrics": metrics, "flags": flags,
                              "similarity": similarity, "line": line, "reads": reads})

    after = {str(path): _sha(path) for path in watched}
    _print_summary(baseline, probes, corpus, reads, summaries, mind_label, text_spec,
                   assembly_disk_same=before == after, runs=runs, version=version)

    write_detail(detail_path, {"stamp": stamp, "version": version, "runs": runs,
                               "dry": bool(args.dry_run), "mind": mind_label,
                               "arms": [row["arm"] for row in summaries]}, all_rounds, text_spec)
    if not args.no_table:
        for line in lines:
            append_table(table_path, line)
        print("表格：%s（追加 %d 行）" % (table_path, len(lines)))
    print("逐条：%s" % detail_path)
    if failed and failed >= len(all_rounds):
        print("一条都没跑成：上面每一行的 error 就是原因（不写表、不假装跑过）", file=sys.stderr)
        return 2
    return 2 if failed else 0


def _print_check(meta: Mapping[str, Any], probes: Sequence[Mapping[str, Any]],
                 baseline: Mapping[str, Any], problems: Sequence[str]) -> int:
    counts: Counter = Counter(str(probe["category"]) for probe in probes)
    print("探针集自检")
    print("  冻结说明：%s" % (meta.get("_frozen") or "（缺！文件头要写一句）"))
    print("  条数：%d" % len(probes))
    print("  %-10s %-6s %s" % ("类别", "条数", "id"))
    for category, label in baseline["categories"].items():
        ids = [str(probe["id"]) for probe in probes if probe["category"] == category]
        print("  %-10s %-6d %s" % (label, counts[category], "、".join(ids)))
    scenes = Counter(str(probe.get("scene") or "") for probe in probes)
    print("  场景：%s" % "、".join("%s %d" % (name, count) for name, count in sorted(scenes.items())))
    for problem in problems:
        print("  不合格：%s" % problem)
    print("结论：%s" % ("通过" if not problems else "不合格 %d 处" % len(problems)))
    return 0 if not problems else 1


def _print_summary(baseline: Mapping[str, Any], probes: Sequence[Mapping[str, Any]],
                   corpus: Mapping[str, Any], reads: Mapping[str, Any],
                   summaries: Sequence[Mapping[str, Any]], mind_label: str,
                   text_spec: Mapping[str, Any], assembly_disk_same: bool, runs: int,
                   version: str) -> None:
    print("探针 %d 条 × runs=%d，装配来源：%s" % (len(probes), runs, mind_label))
    print("语料：%s" % ("可用（assistant %d 条／user %d 条）" % (len(corpus["assistant"]), len(corpus["user"]))
                        if corpus["available"] else NA + "（%s）" % corpus["reason"]))
    if reads.get("available"):
        print("人类那一侧：句长中位 %s／回复长中位 %s／模板复现率 %s" % (
            _cell(reads.get("sentence_median"), 1), _cell(reads.get("reply_median"), 1),
            _cell(reads.get("template_rate"), 3)))
    for summary in summaries:
        metrics = summary["metrics"]
        print("—— %s ——" % summary["arm"])
        print("  跑成 %d／%d（闸弃 %d，错 %d）；可用回复 %d 条" % (
            metrics["lived"], metrics["rounds"], metrics["dropped"], metrics["errors"], metrics["replies"]))
        print("  句长中位 %s｜句末句号率 %s｜八股命中率 %s（%d 条）" % (
            _cell(metrics.get("sentence_median"), 1), _cell(metrics.get("period_rate"), 3),
            _cell(metrics.get("clause_rate"), 3), metrics["clause_hits"]))
        print("  句末标点分布 %s" % json.dumps(metrics.get("endings") or {}, ensure_ascii=False))
        print("  指标1 相似度 %s｜指标2a %s｜指标2b %s｜指标2c %s" % (
            _cell(summary["similarity"] if reads.get("available") else None, 3),
            _cell(ratio(metrics.get("sentence_median"), reads.get("sentence_median"))
                  if reads.get("available") else None, 3),
            _cell(metrics.get("template_rate") if reads.get("available") else None, 3),
            _cell(ratio(metrics.get("reply_median"), reads.get("reply_median"))
                  if reads.get("available") else None, 3)))
        print("  判定 %s" % " ".join(summary["flags"]))
    print("assembly 落盘未改：%s" % ("是" if assembly_disk_same else "否（！）"))


if __name__ == "__main__":
    raise SystemExit(main())
