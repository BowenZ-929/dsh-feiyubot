"""醒时自检：三块在不在、账对不对得上、提案有没有绕过、有没有改了没记账。

只读：check 绝不写盘，也绝不给坏卡补默认值。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence

from . import model, redline, store


def _history_issues(root: Path, issues: List[model.Issue]) -> List[Dict[str, Any]]:
    try:
        rows = store.read_history(root / store.HISTORY_FILE)
    except model.SoulLoadError as exc:
        issues.extend(exc.issues)
        return []
    if not rows:
        return rows   # 空账：新装时没有账可比，不算改了没记账
    last = rows[-1].get("ts")
    try:
        last_epoch = store.iso_to_epoch(last)
    except (TypeError, ValueError):
        issues.append(model.Issue("HISTORY_UNREADABLE", model.ERROR,
                                  "history 末行 ts 不是 ISO8601：%r" % (last,)))
        return rows
    for block in model.BLOCK_NAMES:
        name = store.BLOCK_FILES[block]
        try:
            mtime = (root / name).stat().st_mtime
        except OSError:
            continue
        if mtime > last_epoch:
            issues.append(model.Issue("HISTORY_UNLOGGED_CHANGE", model.ERROR,
                                      "%s 的 mtime %s 晚于账末 %s：改了没记账"
                                      % (name, store.iso_from_epoch(mtime), last)))
    return rows


def _kind_issues(rows: Sequence[Dict[str, Any]], issues: List[model.Issue]) -> None:
    """账行的 kind 只承认这几种；未知字符串一律 error。

    manual 是"手工改盘不在事务内"的可见标记，只许事故补账用，报 warn 待人复核；
    migrate 是形状变化的唯一合法来路（store.migrate 写），probe 是为验证链路而改的标记。
    """
    for number, row in enumerate(rows, start=1):
        if "kind" not in row:
            continue      # kind 是后加的字段：旧账只增不改，缺字段不算"未知字符串"
        kind = row.get("kind")
        if kind not in store.KNOWN_KINDS:
            issues.append(model.Issue("HISTORY_KIND_UNKNOWN", model.ERROR,
                                      "history 第 %d 行的 kind %r 不认识：只许 %s"
                                      % (number, kind, "/".join(store.KNOWN_KINDS))))
            continue
        if kind == store.KIND_MANUAL:
            issues.append(model.Issue("MANUAL_EDIT_IN_LEDGER", model.WARN,
                                      "手工改盘不在事务内：%s.%s @%s，需人工复核"
                                      % (row.get("block") or "?", row.get("path") or "（整块）",
                                         row.get("ts") or "?")))


def _proposal_issues(rows: Sequence[Dict[str, Any]], issues: List[model.Issue]) -> None:
    """提案是改基线的唯一路径：账行必须能对上三条例据，且每月不得超过上限。"""
    per_month: Dict[str, int] = {}
    for number, row in enumerate(rows, start=1):
        if row.get("kind") != store.KIND_PROPOSAL:
            continue
        where = "history 第 %d 行" % number
        if not isinstance(row.get("proposal_id"), str) or not row.get("proposal_id", "").strip():
            issues.append(model.Issue("PROPOSAL_EVIDENCE_MISSING", model.ERROR,
                                      "%s 是提案账行，却没有 proposal_id" % where))
        evidence = row.get("evidence")
        if not (isinstance(evidence, list) and len(evidence) == model.limits.proposal_evidence
                and all(isinstance(item, str) and item.strip() for item in evidence)):
            issues.append(model.Issue("PROPOSAL_EVIDENCE_MISSING", model.ERROR,
                                      "%s 是提案账行，依据记忆要恰好 %d 条非空 id，实际 %r"
                                      % (where, model.limits.proposal_evidence, evidence)))
        target = row.get("target") or row.get("path") or ""
        if isinstance(target, str) and "bounds" in target:
            issues.append(model.Issue("PROPOSAL_FROZEN_TARGET", model.ERROR,
                                      "%s 提案碰了红线（%s）：红线永久冻结" % (where, target)))
        month = store.month_of(row.get("ts"))
        if month is None:
            issues.append(model.Issue("HISTORY_UNREADABLE", model.ERROR,
                                      "%s 的 ts 给不出月份，无法核对提案限额" % where))
            continue
        per_month[month] = per_month.get(month, 0) + 1
    for month, count in sorted(per_month.items()):
        if count > model.limits.proposal_monthly_limit:
            issues.append(model.Issue("PROPOSAL_MONTHLY_LIMIT_EXCEEDED", model.ERROR,
                                      "%s 有 %d 份提案账行，超过每月上限 %d 份：说明绕过了限额"
                                      % (month, count, model.limits.proposal_monthly_limit)))


def _match_issues(snapshot: store.Snapshot, issues: List[model.Issue]) -> None:
    """external 依赖外挂词表、review 依赖模型复核：魂内拦不住，报 warn 让人知道没接线。"""
    for item in snapshot.bounds.items:
        if item.match_type == "external":
            issues.append(model.Issue("MATCH_UNENFORCED", model.WARN,
                                      "%s 依赖外挂词表「%s」，词表归家·仓库、当前未接线；"
                                      "执行方 %s，等它接线"
                                      % (item.id, item.source, item.enforced_by or "（未登记）")))
        elif item.match_type == "review":
            issues.append(model.Issue("MATCH_UNENFORCED", model.WARN,
                                      "%s 需要模型复核语义边界，当前未接线；执行方 %s，等它接线"
                                      % (item.id, item.enforced_by or "（未登记）")))


_PLACEHOLDER_TOKENS = ("{{", "{%")


def _placeholder_issues(snapshot: store.Snapshot, issues: List[model.Issue]) -> None:
    """魂是纯文本注入、不做占位符替换：出现 {{ / {% 就提前示警，别等请求发不出去。"""
    fields = [
        ("identity.self_names.display", snapshot.identity.display),
        ("identity.self_names.synonyms", "\n".join(snapshot.identity.synonyms)),
        ("identity.appearance", snapshot.identity.appearance),
        ("identity.self_statements", "\n".join(snapshot.identity.self_statements)),
        ("identity.fears", snapshot.identity.fears),
        ("identity.likes", snapshot.identity.likes),
        ("manner.stance", snapshot.manner.stance),
    ]
    for item in snapshot.bounds.items:
        fields.append(("bounds.%s.rule" % item.id, item.rule))
        fields.append(("bounds.%s.match.pattern" % item.id, item.pattern))
    for where, text in fields:
        for token in _PLACEHOLDER_TOKENS:
            index = text.find(token)
            if index >= 0:
                issues.append(model.Issue("TEXT_HAS_PLACEHOLDER", model.WARN,
                                          "%s 含占位符「%s」：魂是纯文本注入，不做占位符替换"
                                          % (where, text[index:index + 16])))
                break


# 七件里哪几件会「缺了这一格」→ 摘要那一行要把 Issue 一并带上（字数照报：占位的字也算字）。
_DEGRADED_PARTS = {
    "名字与自称": ("display", "synonyms"),
    "形象": ("appearance",),
    "怕什么": ("fears",),
    "喜欢什么": ("likes",),
}


def collect(root: Path) -> List[model.Issue]:
    root = Path(root)
    raw, issues = store.read_blocks(root)
    rows = _history_issues(root, issues)
    _kind_issues(rows, issues)
    _proposal_issues(rows, issues)
    if store.blocking_errors(issues):
        return issues
    snapshot = store.build_snapshot(root, raw, ())
    _placeholder_issues(snapshot, issues)
    _match_issues(snapshot, issues)
    # M1：红线段断言在自检里也跑一次——面板看得到「这条禁令为什么不合格」，
    # 而不是等人去写盘时才第一次听说（写盘前那一次在 store._commit 里）。
    issues.extend(redline.check_bounds(snapshot.bounds))
    return issues


def summary(root: Path) -> List[str]:
    """人读摘要：七件各多少字、合计多少字、账多长、本月提案用了多少。"""
    root = Path(root)
    raw, issues = store.read_blocks(root)
    if store.blocking_errors(issues):
        return ["有 error 级问题，摘要不可用，见下面的 Issue 列表"]
    snapshot = store.build_snapshot(root, raw, issues)
    identity = snapshot.identity
    manner = snapshot.manner
    bounds = snapshot.bounds
    # 降级载入的那几格：字数照实际算（占位就是现在的值），Issue 码挂在同一行上，
    # 人一眼看得出「这一件是占位，不是 0 字，也不是没问题」。
    degraded = set(issue.code for issue in issues if not issue.blocking)
    lines = []
    for name, chars, limit, hard in model.body_parts(identity, manner, bounds):
        note = "硬线" if hard else "建议值"
        if name == "喜欢什么":
            # 这一件两条线都报：80 字是硬线（超了拒载），40 字只是建议（超了只 warn）
            note = "硬线；建议 %d" % model.limits.likes_suggested
        line = "七件·%s %d/%d 字（%s）" % (name, chars, limit, note)
        missing = [model.MISSING_CODES[key] for key in _DEGRADED_PARTS.get(name, ())
                   if model.MISSING_CODES[key] in degraded]
        if missing:
            line += "；%s 按占位降级载入（补齐后恢复硬门禁）" % "/".join(missing)
        lines.append(line)
    lines.append("七件合计 %d/%d 字（硬线）"
                 % (model.body_chars(identity, manner, bounds), model.limits.body_total))
    lines.append("manner 姿态 1 句；bounds %d 条（生效 %d 条）"
                 % (len(bounds.items), len([item for item in bounds.items if item.active])))
    per_enforcer: Dict[str, int] = {}
    for item in bounds.items:
        key = item.enforced_by or "（未登记）"
        per_enforcer[key] = per_enforcer.get(key, 0) + 1
    lines.append("红线执行方 %s"
                 % "，".join("%s %d 条" % (key, count)
                            for key, count in sorted(per_enforcer.items())))
    try:
        rows = store.read_history(root / store.HISTORY_FILE)
    except model.SoulLoadError as exc:
        lines.append("history 读不出来：%s" % exc)
        return lines
    last = rows[-1].get("ts") if rows else "（空账）"
    lines.append("history %d 行，末行 %s" % (len(rows), last))
    used = len(store.proposals_in_month(rows, store.now_iso()))
    lines.append("本月已接受提案 %d/%d 份"
                 % (used, model.limits.proposal_monthly_limit))
    # M5：整档归档的现状。history 解释「谁为什么改」，归档负责「回到哪一版」，两行并排给人看
    lines.append("整档归档 revision %d；环形 %d/%d 版；history 解释谁为什么改，归档负责回到哪一版"
                 % (store.current_revision(root), len(store.list_revisions(root)),
                    store.RING_SLOTS))
    return lines

