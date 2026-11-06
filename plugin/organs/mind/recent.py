"""最近几轮原文：一轮一问一答，一行一条，只追加、不重写全文件。

这一层存在的意义是**连续性**：她得记得刚才那几句话长什么样。所以存的是**原文**，不是摘要——
摘要由谁再写一遍，就多出第二处「她说过什么」的真源。

落点：paths.organ_dir("mind")/recent_turns.jsonl（root 可由参数覆盖，供测试）。
一条记录：{"ts", "who", "said", "reply", "truncated": {"said": bool, "reply": bool},
"fidelity": "live"|"reconciled", "msg_id": 与脑里那条对齐的 id}。
两侧各按 cap（默认来自 assembly.json 的 recent.cap）截断；**截断留痕**：截过的那一侧在
truncated 里记 true，渲染时带一句「（截断）」——绝不静默锯掉一半还当成原话。

**补记必须留痕**：进程重启、写入失败时，某一轮可能没落进本文件而脑里已经记了；
conversation.answer 装配前对账，把这类轮从脑回填进来，fidelity="reconciled"（正常落盘
的是 "live"）。渲染时每一行前面带「（补记）」，且**只**带在补的那几轮上——
补数据不补声明，等于让她把补的当原话。回填的那一轮 reply 记空串、渲染成
「（没有记到她那句）」：没有记到就说没有记到，绝不替她编一句她说的话。
旧行没有这两栏：读的时候一律按 live / 空串，不把旧数据标成补的。

「更早还有 N 轮没有加载」：盘上保留全部行（append-only），读时只取最近 turns 条，
omitted = 总行数 − 读到的条数。那句话的文案在 assembly.json 的 recent.notice 里，
本文件只把 {omitted} 换掉；omitted 为 0 时空串——不要编一句「还有 0 轮」。

坏行（不是 JSON、不是对象）跳过不炸：这一份是记忆的底稿，读不下去的一行不该让她闭口。
"""
from __future__ import annotations

import json
import os
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
FIDELITY = "fidelity"                 # 这一轮是亲历落的，还是从脑回填的
MSG_ID = "msg_id"                     # 与脑里那条对齐的 id（空串＝没对上，不能当对账凭据）
LIVE = "live"                         # 装配当场落盘：亲历
RECONCILED = "reconciled"             # 事后从脑回填：补记
FIDELITIES = (LIVE, RECONCILED)
BACKFILL_MARK = "（补记）"             # 提示词里补的那几轮必须带这四个字
MISSING_REPLY = "（没有记到她那句）"    # 补记的轮没有她那句：不许替她编
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

    fidelity / msg_id 由调用方给：没给 fidelity 就当 live（当场落盘），msg_id 允许空串
    （对不上脑里那条的轮没有凭据，对账时它帮不上忙，但也不假装对上了）。
    返回**真正落下去的那一条**（截断之后的原文 + truncated 标记），调用方不必自己再截一遍。
    """
    if cap is None:
        cap = policy()["cap"]
    row = _made(record, cap, _fidelity(record.get(FIDELITY)))
    path = path_of(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_line(row))
    return row


def insert_reconciled(records: List[Mapping[str, Any]], root: Optional[Any] = None,
                      cap: Optional[int] = None) -> List[Dict[str, Any]]:
    """把补记的几轮**按 ts 并进**整份账（稳定排序：已有行的相对顺序一个都不动）。

    补的是哪一轮，就补在哪一轮该在的位置：缺最新的一轮补在末尾、缺中间的补在中间。
    一律塞到最前面会把「刚才」写成「最早」——连续性正好被这一下打坏：她以为那是
    很久以前的事，其实上一句刚说完。顺序错了，补数据就变成了造时间线。

    ts 缺失的旧行与坏行（读不出 ts）：跟在上一条有 ts 的行后面（整份账都没 ts 才排最前），
    这条规则钉在 test_recent 里——没有测试钉住的「差不多」就是下一次的坏行。

    只在这一处重写全文件（补记本来就不是追加的时序）。插完一次，msg_id 就在盘上了，
    下一次对账按 msg_id 认得出来，不会再写第二遍（调用方负责先比对）。
    """
    if cap is None:
        cap = policy()["cap"]
    filled = sorted((_made(record, cap, RECONCILED) for record in records),
                    key=lambda row: row[TS])
    if not filled:
        return []
    path = path_of(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept: List[str] = []
    if path.is_file():
        try:
            kept = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            # 读不动就这一轮不补：宁可少补一次（下次还有机会），也不能把已有的行写没了
            return []
    placed: List[Tuple[str, int, str]] = []
    carried = ""            # 上一条的 ts：缺 ts 的那几行跟着它走，位置就不会乱飞
    for index, line in enumerate(kept):
        if not line.strip():
            continue
        stamp = _stamp(line) or carried
        placed.append((stamp, index, line))
        carried = stamp
    base = len(kept)        # 同 ts 时已有行在前、补的行在后（相邻，顺序稳定）
    for offset, row in enumerate(filled):
        placed.append((row[TS], base + offset, _line(row).rstrip("\n")))
    placed.sort(key=lambda item: (item[0], item[1]))
    temp = path.with_name(path.name + ".tmp")
    temp.write_text("".join(text + "\n" for _, _, text in placed), encoding="utf-8")
    os.replace(temp, path)
    return filled


def claim(entries: List[Mapping[str, Any]], root: Optional[Any] = None,
          cap: Optional[int] = None) -> int:
    """旧行认领：msg_id 为空的行，与脑里那条 (ts, said) 完全一致就把 id 写回去。

    升级前写下的行没有 msg_id——它们不是丢了的轮，只是没有凭据。不认领，对账就会把它们
    当成缺的、全量标成「（补记）」：误标与不标一样坏，而且更糟——她与人都以为那些是补的，
    真正补的那几条于是也不可信了。认领**只补凭据**：fidelity 一个字都不动（旧行是亲历）。

    比对用 (ts, 按 cap 截断后的 said)：落盘时两侧就截过，脑里存的是全文，直接比原文
    会把「长到被截断」的旧行永远认不出来（又一次误标）。ts 或 said 缺一行就不认。

    幂等：一条都认不出来就一个字节都不重写（返回 0）；认出来才原子写一次。
    """
    if cap is None:
        cap = policy()["cap"]
    path = path_of(root)
    if not path.is_file():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return 0
    pool: List[List[Any]] = []       # [ts, 截断后的 said, msg_id, 是否已被认领]
    for entry in entries:
        stamp = str(entry.get(TS) or "")
        # 脑里那一条的凭据叫 id（账上这一栏叫 msg_id）；只认 MSG_ID 会让池子恒空，
        # 于是「一条都认不回来」——误标就是这么进来的。
        key = str(entry.get(MSG_ID) or entry.get("id") or "")
        # 脑里的行把人那句记在 text（quote）上，账上这一栏叫 said——两头叫法不同，
        # 只认 SAID 会让旧行永远认不回来（那正是「误标」的入口）。
        said = clip(entry.get(SAID) or entry.get("text") or entry.get("quote"), cap)[0]
        if stamp and key:
            pool.append([stamp, said, key, False])
    if not pool:
        return 0
    claimed = 0
    for index, line in enumerate(lines):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict) or str(item.get(MSG_ID) or ""):
            continue
        stamp = str(item.get(TS) or "")
        said = str(item.get(SAID) or "")
        for slot in pool:
            if slot[3] or slot[0] != stamp or slot[1] != said:
                continue
            slot[3] = True
            item[MSG_ID] = slot[2]
            lines[index] = _line(item).rstrip("\n")
            claimed += 1
            break
    if not claimed:
        return 0
    temp = path.with_name(path.name + ".tmp")
    temp.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    os.replace(temp, path)
    return claimed


def _stamp(line: str) -> str:
    """一行里的 ts；读不出来（坏行 / 没有 ts）就是空串——排序时按「跟上一条」处理。"""
    try:
        item = json.loads(line)
    except ValueError:
        return ""
    if not isinstance(item, dict):
        return ""
    return str(item.get(TS) or "")


def _line(row: Mapping[str, Any]) -> str:
    """盘上一行的样子：一行一条，中文不转义（人得能直接读那份账）。"""
    return json.dumps(row, ensure_ascii=False) + "\n"


def _fidelity(value: Any) -> str:
    """fidelity 只认两值；别的一律按 live（亲历），不把没标过的当补记。"""
    text = str(value or "")
    return text if text in FIDELITIES else LIVE


def _made(record: Mapping[str, Any], cap: int, fidelity: str) -> Dict[str, Any]:
    """按 cap 截断两侧，拼出一条要落盘的记录（截断留痕与 fidelity / msg_id 一起给）。"""
    said, said_cut = clip(record.get(SAID), cap)
    reply, reply_cut = clip(record.get(REPLY), cap)
    return {
        TS: str(record.get(TS) or ""),
        WHO: str(record.get(WHO) or ""),
        SAID: said,
        REPLY: reply,
        TRUNCATED: {SAID: said_cut, REPLY: reply_cut},
        FIDELITY: fidelity,
        MSG_ID: str(record.get(MSG_ID) or ""),
    }


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


def stamps(root: Optional[Any] = None) -> List[str]:
    """整份账上出现过的时刻（含没读进提示词的那几轮）。

    回填时「同 ts 即同一轮」靠它：早些年她的回话被错标成 side=他说，那几条的 ts 与账上
    人那一轮**完全相同**——它们本来就是同一轮，再补一条就是把一轮写成两轮。
    """
    return [str(row.get(TS) or "") for row in _rows(path_of(root)) if row.get(TS)]


def notice(omitted: Any, template: Any) -> str:
    """「更早还有 N 轮没有加载」：纯函数，模板来自数据；N <= 0 时是空串。"""
    count = int(omitted or 0)
    text = "" if template is None else str(template)
    if count <= 0 or not text:
        return ""
    return text.replace(NOTE_SLOT, str(count))


def render(turns: List[Mapping[str, Any]]) -> str:
    """进提示词的样子：一轮两行，谁说的 + 原文；截断过、没说出口的都留一句痕迹。

    补记的那一轮两行前面都带「（补记）」，她那句写成「（没有记到她那句）」：
    这是补来的，不是她说过的——声明就在模型眼前，别指望它自己猜。
    """
    lines: List[str] = []
    for row in turns:
        cut = row.get(TRUNCATED) or {}
        mark = BACKFILL_MARK if _fidelity(row.get(FIDELITY)) == RECONCILED else ""
        who = str(row.get(WHO) or "") or "人"
        said = str(row.get(SAID) or "")
        reply = str(row.get(REPLY) or "")
        lines.append("- %s%s：%s%s" % (mark, who, said, CUT_MARK if cut.get(SAID) else ""))
        lines.append("- %s%s：%s%s" % (mark, HER,
                                       reply or (MISSING_REPLY if mark else SILENT),
                                       CUT_MARK if cut.get(REPLY) else ""))
    return "\n".join(lines)


def reconciled(turns: List[Mapping[str, Any]]) -> int:
    """这几轮里有几条是补记的（提示词里带「（补记）」的那几条，页面与返回体照它说）。"""
    return sum(1 for row in turns if _fidelity(row.get(FIDELITY)) == RECONCILED)


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
    """一条记录补齐六个键（旧文件缺哪一栏就给空），truncated 缺了按「没截过」算。

    旧行没有 fidelity / msg_id：一律按 live / 空串读——**不许**把旧数据标成补的
    （那等于凭空给她加一句「这是补的」，比缺字段更坏）。
    """
    cut = item.get(TRUNCATED)
    marks = cut if isinstance(cut, dict) else {}
    return {
        TS: str(item.get(TS) or ""),
        WHO: str(item.get(WHO) or ""),
        SAID: str(item.get(SAID) or ""),
        REPLY: str(item.get(REPLY) or ""),
        TRUNCATED: {SAID: bool(marks.get(SAID)), REPLY: bool(marks.get(REPLY))},
        FIDELITY: _fidelity(item.get(FIDELITY)),
        MSG_ID: str(item.get(MSG_ID) or ""),
    }
