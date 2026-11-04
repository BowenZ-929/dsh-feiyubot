"""心的测试小工具：每个用例一个临时根目录，不碰仓库里的真数据。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from organs.heart import store


def make_root(temp_dir: str) -> Path:
    """造 Path(temp_dir)/"heart" 并建好，返回这个根目录。"""
    root = Path(temp_dir) / "heart"
    root.mkdir(parents=True, exist_ok=True)
    return root


def read_json_file(path: Any) -> Dict[str, Any]:
    """读回一个 JSON 文件（测试里用，坏了就让它炸，好定位）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_raw(path: Any, text: str) -> None:
    """原样写一段文本，用来伪造坏 JSON / 半截行。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def heart_data() -> Path:
    """仓库里真的 data 目录——只读引用，测试别往里写。"""
    return store.DATA_DIR
