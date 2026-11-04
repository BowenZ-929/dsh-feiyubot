"""round2 回归基准：加载手写 fixture（20–30 条中文短消息、3–5 人、1000xxxx 占位 id）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Tuple

from organs.brain import BrainStore
from organs.brain.tests.helpers import fresh_store

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "round2_baseline.json"


def load_fixture(path: Path = FIXTURE) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_store(fixture: Dict[str, Any]) -> Tuple[BrainStore, Any]:
    """把 fixture 灌进一个空脑：消息进长期档（1000xxxx 占位 id 原样保留），
    再对 retired_ids 逐条 retire（打 stale，不物理删）。返回 (store, tmpdir)。"""
    store, tmp = fresh_store()
    store.import_rows(fixture["messages"])
    for entry_id in fixture.get("retired_ids", []):
        store.retire(entry_id)
    return store, tmp
