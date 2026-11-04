"""三臂对照：冷启动 / 朴素字面 / 我们的方案。

判定函数唯一：answer 子串是否出现在该臂"被选中的条目"的完整正文里。
- 冷启动：没有任何记忆，被选中的条目 = 空。
- 朴素字面：同一套字面检索（instr + 字二元组），不做 status 过滤、不做配额、
  不看 α/β 与出处，取最相关一条作为被选中的条目（完整正文）。
- 我们的方案：BrainStore.recall（status 过滤 + 装包 + 可信度 + 出处），
  被选中的条目 = 装包里 testimony=true 的条目（虚构只作谈资、不作证词，不参与判答）。

题分两类：answer（答案必须在被选中条目里）与 refuse（答案不得在被选中条目里——
正确拒答得分，只算命中等于奖励乱答）。判定用条目完整正文；cap 只截注入文本。
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from organs.brain import BrainStore
from organs.brain.recall import literal_score
from organs.brain.tests.helpers import fresh_store

from .fixture import build_store, load_fixture

NOW = "2026-11-03T12:00:00+00:00"


def judge(question: Dict[str, Any], selected_full_texts: Sequence[str]) -> bool:
    """唯一判定函数：answer 子串是否出现在被选中的条目（完整正文）里。"""
    answer = question.get("answer", "")
    present = any(answer in text for text in selected_full_texts)
    if question.get("kind") == "refuse":
        return not present      # 该拒答：没把答案递出去才算对
    return present


def arm_cold(questions: List[Dict[str, Any]]) -> int:
    """冷启动：没有任何记忆，被选中的条目 = 空。"""
    return sum(1 for q in questions if judge(q, []))


def _naive_top1(fixture: Dict[str, Any], question: Dict[str, Any]) -> str:
    """朴素字面基线：同一套字面打分，取最相关一条的完整正文。"""
    best_score = -1.0
    best_ts = ""
    best_text = ""
    for message in fixture["messages"]:
        score = literal_score(question.get("q", ""), message.get("text", ""))
        ts = message.get("ts", "")
        if score > best_score or (score == best_score and ts > best_ts):
            best_score, best_ts, best_text = score, ts, message.get("text", "")
    return best_text


def arm_naive(fixture: Dict[str, Any]) -> int:
    hits = 0
    for q in fixture["questions"]:
        hits += 1 if judge(q, [_naive_top1(fixture, q)]) else 0
    return hits


def arm_ours(store: BrainStore, questions: List[Dict[str, Any]], now_ts: str) -> int:
    hits = 0
    for q in questions:
        result = store.recall(q.get("q", ""), now_ts=now_ts)
        selected = [item.get("full_text", item.get("text", ""))
                    for item in result.get("items", [])
                    if item.get("testimony") is True]
        hits += 1 if judge(q, selected) else 0
    return hits


def _probe_scores(fixture: Dict[str, Any], store: BrainStore, now_ts: str) -> Dict[str, Any]:
    """三类探针 + 下坡基线：只记录分数并标注，不计入验收线。"""
    probes = fixture.get("probe_questions", [])
    by_category: Dict[str, Dict[str, int]] = {}
    for probe in probes:
        category = probe.get("category", "未分类")
        bucket = by_category.setdefault(category, {"total": 0, "cold": 0, "naive": 0, "ours": 0})
        bucket["total"] += 1
        bucket["cold"] += 1 if judge(probe, []) else 0
        bucket["naive"] += 1 if judge(probe, [_naive_top1(fixture, probe)]) else 0
        result = store.recall(probe.get("q", ""), now_ts=now_ts)
        selected = [item.get("full_text", item.get("text", ""))
                    for item in result.get("items", [])
                    if item.get("testimony") is True]
        bucket["ours"] += 1 if judge(probe, selected) else 0
    return by_category


def run(now_ts: str = NOW) -> Dict[str, Any]:
    fixture = load_fixture()
    store, tmp = build_store(fixture)
    try:
        questions = fixture["questions"]
        cold = arm_cold(questions)
        naive = arm_naive(fixture)
        ours = arm_ours(store, questions, now_ts)
        total = len(questions)
        return {"total": total, "cold": cold, "naive": naive, "ours": ours,
                "net_vs_cold": ours - cold, "net_vs_naive": ours - naive,
                "probe_scores": _probe_scores(fixture, store, now_ts)}
    finally:
        tmp.cleanup()


def render(report: Dict[str, Any]) -> str:
    lines = [
        "冷启动    %d/%d" % (report["cold"], report["total"]),
        "朴素字面  %d/%d" % (report["naive"], report["total"]),
        "我们的方案 %d/%d" % (report["ours"], report["total"]),
        "净增益(vs 冷启动) %+d  净增益(vs 朴素) %+d"
        % (report["net_vs_cold"], report["net_vs_naive"]),
        "",
        "探针（不计入验收线，等稳定了再纳入）",
    ]
    for category in ("换说法", "指代", "时间", "下坡基线"):
        bucket = report["probe_scores"].get(category)
        if bucket:
            lines.append("%s  ours %d/%d | naive %d/%d | cold %d/%d"
                         % (category, bucket["ours"], bucket["total"],
                            bucket["naive"], bucket["total"],
                            bucket["cold"], bucket["total"]))
    return "\n".join(lines)


if __name__ == "__main__":
    print(render(run()))
