"""心的落盘工具：只管读写，不懂任何器官语义。

所有写入口都走"同目录临时文件 + os.replace"或"追加一行 + fsync"，
半截文件不留在正式路径上；所有读入口坏数据一律回默认值，不许把
Caller 炸掉——心跳线程读坏一个文件不该让整个人停下。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path(__file__).resolve().parent / "data"


def ensure_dir(path: Any) -> Path:
    """确保目录存在（含各级父目录），返回 Path。"""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def read_json(path: Any, default: Dict[str, Any]) -> Dict[str, Any]:
    """读 JSON 对象；读不到 / 坏 JSON / 不是 dict 一律返回 default。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, dict) else default


def write_json(path: Any, data: Dict[str, Any]) -> None:
    """整文件覆盖写：同目录 .tmp → fsync → os.replace。"""
    target = Path(path)
    ensure_dir(target.parent)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)


def append_jsonl(path: Any, row: Dict[str, Any]) -> None:
    """追加一行并落盘；父目录不存在就建。"""
    target = Path(path)
    ensure_dir(target.parent)
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def read_jsonl(path: Any, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """读 JSONL；坏行跳过，limit 给了就只取最后 limit 条。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    if limit is not None and limit >= 0:
        return rows[-limit:] if limit else []
    return rows
