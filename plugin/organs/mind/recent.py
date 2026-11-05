"""最近几轮原文：一轮一问一答，一行一条，只追加、不重写全文件。

这一层存在的意义是**连续性**：她得记得刚才那几句话长什么样。所以存的是**原文**，不是摘要——
摘要由谁再写一遍，就多出第二处「她说过什么」的真源。

落点：paths.organ_dir("mind")/recent_turns.jsonl（root 可由参数覆盖，供测试）。
一条记录：{"ts", "who", "said", "reply", "truncated": {"said": bool, "reply": bool}}。
两侧各按 cap（默认来自 assembly.json 的 recent.cap）截断；**截断留痕**：截过的那一侧在
truncated 里记 true，渲染时带一句「（截断）」——绝不静默锯掉一半还当成原话。

「更早还有 N 轮没有加载」：盘上保留全部行（append-only），读时只取最近 turns 条，
omitted = 总行数 − 读到的条数。那句话的文案在 assembly.json 的 recent.notice 里，
本文件只把 {omitted} 换掉；omitted 为 0 时空串——不要编一句「还有 0 轮」。

坏行（不是 JSON、不是对象）跳过不炸：这一份是记忆的底稿，读不下去的一行不该让她闭口。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .. import paths

FILE_NAME = "recent_turns.jsonl"     # 落脚的文件名
NOTE_SLOT = "{omitted}"              # notice 模板里那个占位符
HER = "她"                            # 她自己那句话记在谁名下（人那句按记录里的 who）

SAID = "said"
REPLY = "reply"
TS = "ts"
WHO = "who"
TRUNCATED = "truncated"
CUT_MARK = "（截断）"
SILENT = "（没说出口）"


def path_of(root: Optional[Any] = None) -> Path:
    """这一份账的落点：root 给定时按它（测试），否则 <器官目录>/recent_turns.jsonl。"""
    base = Path(root) if root is not None else paths.organ_dir("mind")
    return base / FILE_NAME


def policy() -> Dict[str, Any]:
    """装配说明里的 recent 段：turns / cap / notice 三个数只在数据里，代码里不留默认值。

    延迟导入 conversation：装配入口要读本模块，反过来只在没给参数时才问它一句。
    """
    from .conversation import load_assembly

    return load_assembly()["recent"]


def clip(text: str, cap: int) -> Tuple[str, bool]:
    """按字数截断，返回 (截断后的正文, 是否截过)——截没截过要一起交出去。"""
    body = "" if text is None else str(text)
    size = int(cap)
    if size <= 0 or len(body) <= size:
        return body, False
    return body[:size], True


def append(record: Mapping[str, Any], root: Optional[Any] = None,
           cap: Optional[int] = None) -> Dict[str, Any]:
    """追一轮（一行），不重写全文件；两侧按 cap 截断并把留痕写进记录。

    返回**真正落下去的那一条**（截断之后的原文 + truncated 标记），调用方不必自己再截一遍。
    """
    if cap is None:
        cap = policy()["cap"]
    said, said_cut = clip(record.get(SAID), cap)
    reply, reply_cut = clip(record.get(REPLY), cap)
    row: Dict[str, Any] = {
        TS: str(record.get(TS) or ""),
        WHO: str(record.get(WHO) or ""),
        SAID: said,
        REPLY: reply,
        TRUNCATED: {SAID: said_cut, REPLY: reply_cut},
    }
    path = path_of(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def load(root: Optional[Any] = None, turns: Optional[int] = None) -> Dict[str, Any]:
    """读最近几轮：{"turns": [...], "omitted": 更早还有几轮, "total": 盘上一共几条}。

    盘上留着全部行，这里只切最后 turns 条；坏行跳过，不计进 total（它本来就不算一条对话）。
    """
    if turns is None:
        turns = policy()["turns"]
    rows = _rows(path_of(root))
    total = len(rows)
    keep = int(turns)
    shown = rows[-keep:] if keep > 0 else []
    return {"turns": shown, "omitted": max(total - len(shown), 0), "total": total}


def notice(omitted: Any, template: Any) -> str:
    """「更早还有 N 轮没有加载」：纯函数，模板来自数据；N <= 0 时是空串。"""
    count = int(omitted or 0)
    text = "" if template is None else str(template)
    if count <= 0 or not text:
        return ""
    return text.replace(NOTE_SLOT, str(count))


def render(turns: List[Mapping[str, Any]]) -> str:
    """进提示词的样子：一轮两行，谁说的 + 原文；截断过、没说出口的都留一句痕迹。"""
    lines: List[str] = []
    for row in turns:
        cut = row.get(TRUNCATED) or {}
        who = str(row.get(WHO) or "") or "人"
        said = str(row.get(SAID) or "")
        reply = str(row.get(REPLY) or "")
        lines.append("- %s：%s%s" % (who, said, CUT_MARK if cut.get(SAID) else ""))
        lines.append("- %s：%s%s" % (HER, reply or SILENT,
                                     CUT_MARK if cut.get(REPLY) else ""))
    return "\n".join(lines)


def _rows(path: Path) -> List[Dict[str, Any]]:
    """整份读回来，坏行跳过：不是 JSON、不是对象的那一行就当没有过。"""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        rows.append(_row(item))
    return rows


def _row(item: Mapping[str, Any]) -> Dict[str, Any]:
    """一条记录补齐四个键（旧文件缺哪一栏就给空），truncated 缺了按「没截过」算。"""
    cut = item.get(TRUNCATED)
    marks = cut if isinstance(cut, dict) else {}
    return {
        TS: str(item.get(TS) or ""),
        WHO: str(item.get(WHO) or ""),
        SAID: str(item.get(SAID) or ""),
        REPLY: str(item.get(REPLY) or ""),
        TRUNCATED: {SAID: bool(marks.get(SAID)), REPLY: bool(marks.get(REPLY))},
    }
