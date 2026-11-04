"""M6：演化五闸——接在 accept_proposal 前面，是「人接受提案」之前的前置机器校验。

§5 第 13 条（只有人接受才落魂）一字不改：五闸不改它，只在它前面多一道机器门。任一不过即拒，
拒因进面板，盘上一个字节都不写。

① 依据 ≥3 条同源记忆——**实现只做到「不重复 + ≥3 条」**：现在没有记忆库，evidence 只是字符串
   id，查得出重复、查不出同源。同源校验等脑接上记忆 id 再补（见 evidence_issues 的注释与拒因文案，
   不许在这里假装校验过同源）。
② 单次改动 ≤ 该字段可编辑范围的 1/10，最小 3 字：stance 100→10、display 40→4、
   appearance/fears 60→6、红线条目 250→25；差值取 |新-旧| 字数。
③ 同一字段冷却 168 小时：按 history 里该 path 的最后一次改动算（migrate/edit 与 proposal 都算，
   probe 不算——probe 是「为验证链路而改」的显式声明，不是人改魂，不该白锁 168 小时）。
④ 护栏：不得移动红线、不得删条目（条目 id 与顺序逐条对齐）。
⑤ 落盘前先存整档快照（M5）：取不到快照就整次拒。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import model

EVIDENCE = "GATE_EVIDENCE"
STEP = "GATE_STEP"
COOLDOWN = "GATE_COOLDOWN"
GUARD = "GATE_GUARD"
SNAPSHOT = "GATE_SNAPSHOT"

MIN_STEP = 3            # 步长下限：1/10 算出来比 3 字还小就取 3 字
COOLDOWN_HOURS = 168.0
# ② 的可编辑范围（建议/硬上限）：步长 = max(3, 上限 // 10)
FIELD_SPAN = {
    ("identity", "self_names.display"): 40,
    ("identity", "appearance"): 60,
    ("identity", "fears"): 60,
    ("manner", "stance"): 100,
}
BOUNDS_RULE_SPAN = 250
# ③ 算冷却的账行：probe 只为验证链路，不算「改过这个字段」
COOLDOWN_KINDS = ("edit", "migrate", "proposal", "manual")


def span_for(block: str, path: str) -> Optional[int]:
    """该字段的可编辑范围；红线条目按 rule 算，其余按块内 path 对表。"""
    if block == "bounds":
        return BOUNDS_RULE_SPAN if path.endswith("rule") else None
    return FIELD_SPAN.get((block, path))


def step_for(block: str, path: str) -> Optional[int]:
    """②的步长：可编辑范围的 1/10，最小 3 字。没有范围就返回 None（五闸只能拒）。"""
    span = span_for(block, path)
    if span is None:
        return None
    return max(MIN_STEP, span // 10)


def step_table() -> Dict[str, int]:
    """六件各自的实际步长：回报与面板都读这一份，别在两处各算一次。"""
    table = {}
    for (block, path), _span in FIELD_SPAN.items():
        table["%s.%s" % (block, path)] = step_for(block, path) or MIN_STEP
    table["bounds.items[].rule"] = step_for("bounds", "items[].rule") or MIN_STEP
    return table


def evidence_issues(evidence: Any, minimum: int = 3) -> List[model.Issue]:
    """① 依据 ≥3 条，且不得重复。

    同源校验等脑接上再补：evidence 现在只是字符串 id，没有记忆库就没有「源」可比。
    这条注释不是免责声明——拒因文案里也如实这么写，不许让人以为同源已经查过。
    """
    issues: List[model.Issue] = []
    if not (isinstance(evidence, list) and all(isinstance(item, str) for item in evidence)):
        return [model.Issue(EVIDENCE, model.ERROR,
                            "依据必须是字符串 id 数组（每条一次唤起，不许重复）")]
    ids = [item.strip() for item in evidence if item.strip()]
    if len(ids) < minimum:
        issues.append(model.Issue(
            EVIDENCE, model.ERROR,
            "依据只有 %d 条 < %d 条：少于三条不许动她（同源校验等脑接上记忆 id 再补）"
            % (len(ids), minimum)))
    duplicated = sorted(set(item for item in ids if ids.count(item) > 1))
    if duplicated:
        issues.append(model.Issue(
            EVIDENCE, model.ERROR,
            "依据重复：%s（同一个 id 只算一次；同源校验等脑接上记忆 id 再补）"
            % "/".join(duplicated)))
    return issues


def step_issues(block: str, path: str, old_value: Any, new_value: Any) -> List[model.Issue]:
    """② 单次改动 ≤ 步长，差值取 |新-旧| 字数。"""
    step = step_for(block, path)
    if step is None:
        return [model.Issue(STEP, model.ERROR,
                            "%s.%s 没有可编辑范围，算不出步长：先给它定上限再来改"
                            % (block, path))]
    if not (isinstance(old_value, str) and isinstance(new_value, str)):
        return [model.Issue(STEP, model.ERROR,
                            "%s.%s 的旧值/新值不是文本，算不出步长：魂里这几件都是句子"
                            % (block, path))]
    delta = abs(model.char_count(new_value) - model.char_count(old_value))
    if delta > step:
        return [model.Issue(STEP, model.ERROR,
                            "%s.%s 一次改了 %d 字 > 步长 %d 字（可编辑范围 %d 字的 1/10）："
                            "一次只能挪一小步" % (block, path, delta, step,
                                                 span_for(block, path) or 0))]
    return []


def _row_epoch(row: Dict[str, Any], iso_to_epoch) -> Optional[float]:
    stamp = row.get("ts")
    try:
        return iso_to_epoch(stamp)
    except (TypeError, ValueError):
        return None


def cooldown_issues(rows: Sequence[Dict[str, Any]], block: str, path: str, now_iso: str,
                    iso_to_epoch, hours: float = COOLDOWN_HOURS) -> List[model.Issue]:
    """③ 同一字段冷却 168 小时：最后一条「改过这个字段」的账行离现在还不到 7 天就拒。"""
    try:
        now_epoch = iso_to_epoch(now_iso)
    except (TypeError, ValueError):
        return [model.Issue(COOLDOWN, model.ERROR,
                            "算冷却要一个 ISO 时间，拿到的是 %r" % (now_iso,))]
    last_epoch = None
    for row in rows:
        if row.get("kind") not in COOLDOWN_KINDS:
            continue
        if row.get("block") != block:
            continue
        row_path = row.get("path") or ""
        # 整块写入（migrate、整块替换）把这一格也一起改了，照样算改过这个字段
        if row_path not in (path, ""):
            continue
        epoch = _row_epoch(row, iso_to_epoch)
        if epoch is None:
            continue
        if last_epoch is None or epoch > last_epoch:
            last_epoch = epoch
    if last_epoch is None:
        return []
    waited = now_epoch - last_epoch
    if waited >= hours * 3600:
        return []
    return [model.Issue(COOLDOWN, model.ERROR,
                        "%s.%s 上次改动在 %.1f 小时前，冷却 %.0f 小时未满："
                        "同一格一周只许动一次" % (block, path, waited / 3600.0, hours))]


def guard_issues(before: model.Bounds, after: model.Bounds) -> List[model.Issue]:
    """④ 护栏：不得移动红线、不得删条目——id 顺序与每条的禁令逐条对齐。"""
    old_ids = [item.id for item in before.items]
    new_ids = [item.id for item in after.items]
    if old_ids != new_ids:
        return [model.Issue(GUARD, model.ERROR,
                            "红线被动过：条目 id 从 %s 变成 %s（不得删条目、不得移动顺序）"
                            % ("/".join(old_ids), "/".join(new_ids)))]
    old_rules = dict((item.id, item.rule) for item in before.items)
    for item in after.items:
        if old_rules.get(item.id) != item.rule:
            return [model.Issue(GUARD, model.ERROR,
                                "红线 %s 的禁令被改写：护栏不许动红线，红线只许人逐条加/撤"
                                % item.id)]
    return []
