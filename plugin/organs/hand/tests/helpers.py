"""手的测试小工具：临时实盘根、直接读账、伪造文件与额度。

读账一律**直接读盘**（不走被测代码），这样「盘上到底几行、最后一行是什么」才是独立证据。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from organs.home import cards as home_cards
from organs.hand import store

#: 固定的「现在」：用例不看本机时钟，也就不会半夜跑挂。
NOW = "2026-11-06T09:00:00+09:00"
#: 明天中午（相对 NOW）。
TOMORROW_NOON = "2026-11-07T12:00:00+09:00"


def make_root(temp_dir: str) -> Path:
    """造 Path(temp_dir)/"hand" 并建好，返回**手**的实盘根；家的根是它的兄弟目录。"""
    root = Path(temp_dir) / "hand"
    root.mkdir(parents=True, exist_ok=True)
    return root


def home_root(root: Any) -> Path:
    return store.home_root(root)


def ledger_path(root: Any) -> Path:
    return Path(root) / "ledger.jsonl"


def ledger_lines(root: Any) -> List[Dict[str, Any]]:
    """盘上每一行（含 reserved），坏行就让它炸——测试里要的是准数。"""
    path = ledger_path(root)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def tasks(root: Any) -> list:
    """家里工作台上的卡（折叠后）。"""
    return home_cards.list_tasks(root=home_root(root))


def write_limits(root: Any, limit: int, version: int = 1) -> Path:
    """往临时实盘塞一份 limits.json。"""
    path = Path(root) / "limits.json"
    path.write_text(json.dumps({"version": version, "per_person_per_day": limit,
                                "note": "测试用"}, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def write_raw(path: Any, text: str) -> Path:
    """原样写一段文本：造坏 JSON、半截行、假账用。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
