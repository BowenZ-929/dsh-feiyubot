"""日子的测试小工具：临时数据根、可注入的时刻、候选计划。

两条规矩：测试一律用临时目录（绝不写 organs/day/data/ 的真实文件），
时刻一律用注入的 now_ts（绝不依赖真实时钟）。
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from organs.brain import BrainStore
from organs.day import model


def temp_root(case: Any) -> Path:
    """一次测试一个临时根，用完删掉。"""
    holder = tempfile.TemporaryDirectory()
    case.addCleanup(holder.cleanup)
    return Path(holder.name)


def at(text: str) -> str:
    """把"墙上时间"钉到本机时区，再归一成本地绝对时刻串。

    这样断言与机器时区无关：换台机器跑，结论还是同一个（换日只看本地钟点）。
    """
    return model.iso(datetime.fromisoformat(text).replace(tzinfo=model.local_tz()))


def candidates(*pairs: Tuple[str, str]) -> List[Dict[str, str]]:
    """(kind, 墙上时间) → 候选计划列表。"""
    return [{"kind": kind, "at": at(stamp)} for kind, stamp in pairs]


def brain(root: Path) -> BrainStore:
    """临时脑：夜的第 1 步要一个真 BrainStore，落在临时根下。"""
    return BrainStore(Path(root) / "brain")


def log_lines(root: Path) -> List[Dict[str, Any]]:
    """夜日志每一行解出来的对象（一行一次）。"""
    target = Path(root) / "night_log.jsonl"
    if not target.exists():
        return []
    return [json.loads(text) for text in target.read_text(encoding="utf-8").splitlines() if text.strip()]
