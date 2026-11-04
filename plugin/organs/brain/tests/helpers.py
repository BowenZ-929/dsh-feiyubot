"""脑测试的小工具：每个用例一个空目录里的新脑，不碰仓库里的真数据。"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from organs.brain import BrainStore


def fresh_store() -> Any:
    """返回 (BrainStore, tmpdir)：临时空目录，store 自己建默认文件。"""
    tmp = tempfile.TemporaryDirectory()
    store = BrainStore(Path(tmp.name))
    return store, tmp


def ingest(store: BrainStore, who: str, text: str, ts: str, **extra: Any) -> dict:
    return store.ingest(who, text, ts=ts, **extra)
