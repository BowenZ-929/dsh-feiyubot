"""临时数据目录与读写小工具。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BLOCK_FILES = {
    "identity": "identity.json",
    "manner": "manner.json",
    "bounds": "bounds.json",
}
HISTORY = "history.jsonl"


def copy_fixture(name: str, temp_dir: str) -> Path:
    """把样例卡拷进临时目录，返回魂数据目录本身。"""
    target = Path(temp_dir) / "soul"
    shutil.copytree(str(FIXTURES / name), str(target))
    return target


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_bytes(path: Path) -> bytes:
    return Path(path).read_bytes()


def edit_card(root: Path, block: str, mutate) -> None:
    """按 fixture 改一块卡（真数据不动）：mutate 收到 dict，就地改。"""
    path = Path(root) / BLOCK_FILES[block]
    data = read_json(path)
    mutate(data)
    write_json(path, data)


def append_row(root: Path, row: Dict[str, Any]) -> None:
    """直接往账里塞一行：构造「绕过限额」「没写依据」这类场景，也用来注入月份。"""
    with open(str(Path(root) / HISTORY), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def proposal(**overrides) -> Dict[str, Any]:
    """一份能过的提案；测试只改自己要考的那一项。"""
    data = {
        "id": "p-0001",
        "target": "manner.stance",
        "value": "改完的基调",
        "evidence": ["mem-1", "mem-2", "mem-3"],
        "reason": "三条记忆都指向同一处：她最近对人更松了",
    }
    data.update(overrides)
    return data

