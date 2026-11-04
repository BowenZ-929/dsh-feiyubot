"""夜编排：固定四步，顺序不改，一步失败记一行继续下一步。

四步（顺序固定，理由写在每一步头上）：
1. brain_compact：真调脑的公开动作 BrainStore.run_night(now_ts)（脑自己幂等）。
2. brain_decay：**空实现**——脑没有公开的衰减结算动作（_settle_retention 是私有的，
   结算发生在 recall 读的时候）。所以只报 stubbed + 理由，什么也不写。
3. home_diary：**空实现**——卧室结构性只读（home/rooms.py:34 + data/access.json
   白名单未开口子），这一步没有可写的路。只报 stubbed + 理由。
4. heart_settle：真调心的公开只读动作 energy.read（读时结算并写回，精力是时间的函数），
   回报 value/tier。本模块自己不写心的任何文件。

每步单独 try/except：失败记 status=failed + 一行错误，**继续下一步**——一件坏事
不许让整夜停摆。跑完往 data/night_log.jsonl 追加**一行** JSON：
    {"ts": ..., "day": ..., "written": 1, "steps": {四步各自 {status, detail}}, "failed": [...]}
written 是本次落盘的日志行数（恒为 1；其余各步的落盘由各器官自己负责）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from organs.heart import energy

from . import model

STEPS = ("brain_compact", "brain_decay", "home_diary", "heart_settle")
LOG_FILE = "night_log.jsonl"
STATUS_OK = "ok"
STATUS_STUBBED = "stubbed"
STATUS_FAILED = "failed"

STUB_BRAIN_DECAY = "脑没有公开的衰减结算动作（_settle_retention 是私有，读时结算在 recall 里）"
STUB_HOME_DIARY = "卧室结构性只读，白名单未开口子（home/rooms.py:34 + data/access.json）"


def log_path(root: Any = None) -> Path:
    return model.day_root(root) / LOG_FILE


# ---- 四步 -----------------------------------------------------------------


def _brain_compact(at: str, brain: Any, heart_root: Any) -> Dict[str, Any]:
    """真调脑的公开动作：换日降级 + 从刚结束的一天里提炼提案（脑自己幂等）。"""
    if brain is None:
        return {"status": STATUS_FAILED, "detail": "脑未载入（brain=None），夜结算没得调"}
    out = brain.run_night(at)
    proposals = out.get("proposals") or []
    detail = "night_of=%s 换日降级 %s 条，提案 %d 条" % (
        out.get("night_of", ""), out.get("rolled_over", 0), len(proposals))
    if out.get("rerun"):
        detail += "（重跑：同一天已经结过，幂等返回）"
    if out.get("note"):
        detail += "；%s" % out["note"]
    return {"status": STATUS_OK, "detail": detail, "night_of": out.get("night_of"),
            "proposals": len(proposals), "rerun": bool(out.get("rerun"))}


def _brain_decay(at: str, brain: Any, heart_root: Any) -> Dict[str, Any]:
    """空实现：没有公开的衰减结算动作，就只报 stubbed，不假装结过。"""
    return {"status": STATUS_STUBBED, "detail": STUB_BRAIN_DECAY}


def _home_diary(at: str, brain: Any, heart_root: Any) -> Dict[str, Any]:
    """空实现：卧室结构性只读，本步没有可写的路，只报 stubbed。"""
    return {"status": STATUS_STUBBED, "detail": STUB_HOME_DIARY}


def _heart_settle(at: str, brain: Any, heart_root: Any) -> Dict[str, Any]:
    """真调心的公开只读动作：read 读时结算并写回；本模块不碰心的任何文件。"""
    read = energy.read(root=heart_root, now=at)
    detail = "value=%s tier=%s（结算 %s → %s，%s 小时%s）" % (
        read.get("value"), read.get("tier"), read.get("settled_from"), read.get("settled_to"),
        read.get("hours"), "，触到上限" if read.get("capped") else "")
    return {"status": STATUS_OK, "detail": detail,
            "value": read.get("value"), "tier": read.get("tier")}


_STEP_FUNCS = {
    "brain_compact": _brain_compact,
    "brain_decay": _brain_decay,
    "home_diary": _home_diary,
    "heart_settle": _heart_settle,
}


# ---- 跑一次夜 --------------------------------------------------------------


def _append_log(line: Dict[str, Any], root: Any = None) -> Path:
    """追加一行 JSON（一行一次，不重写旧行；夜日志是流水，不是账）。"""
    target = log_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 不排序键：四步的顺序是契约，落进日志也得看得出来（排一下就成了字典序）
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    os.chmod(str(target), 0o600)
    return target


def run(now_ts: Any = None, *, root: Any = None, brain: Any = None, heart_root: Any = None,
        break_step: Optional[int] = None) -> Dict[str, Any]:
    """跑一次夜：四步固定顺序，每步单独兜错，末尾落一行日志。

    break_step=N 只给验收用：让第 N 步（1 起数）当场抛错，看后面几步还跑不跑。
    """
    at = model.iso(now_ts) if now_ts is not None else model.now_iso()
    day = model.day_key(at)
    steps: Dict[str, Dict[str, Any]] = {}
    for number, name in enumerate(STEPS, 1):
        try:
            if break_step is not None and int(break_step) == number:
                raise RuntimeError("验收注入：故意让第 %d 步（%s）抛错" % (number, name))
            steps[name] = _STEP_FUNCS[name](at, brain, heart_root)
        except Exception as exc:  # noqa: BLE001 - 一步失败只记一行，继续下一步
            steps[name] = {"status": STATUS_FAILED,
                          "detail": "%s: %s" % (type(exc).__name__, exc)}
    failed: List[str] = [name for name in STEPS if steps[name]["status"] == STATUS_FAILED]
    line = {"ts": at, "day": day, "written": 1, "steps": steps, "failed": failed}
    target = _append_log(line, root)
    return {"ok": not failed, "now": at, "day": day, "steps": steps, "failed": failed,
            "written": 1, "log_path": str(target), "log_line": line}


def log_tail(limit: int = 20, *, root: Any = None) -> Dict[str, Any]:
    """夜日志尾部：lines 是解出来的对象，raw 是原样的那几行（贴回执用）。"""
    target = log_path(root)
    if not target.exists():
        return {"ok": True, "path": str(target), "count": 0, "bad": 0, "lines": [], "raw": []}
    rows = target.read_text(encoding="utf-8").splitlines()
    tail = rows[-int(limit):] if limit and int(limit) > 0 else rows
    lines: List[Any] = []
    bad = 0
    for text in tail:
        if not text.strip():
            continue
        try:
            lines.append(json.loads(text))
        except ValueError:
            bad += 1
    return {"ok": True, "path": str(target), "count": len(lines), "bad": bad,
            "lines": lines, "raw": tail}
