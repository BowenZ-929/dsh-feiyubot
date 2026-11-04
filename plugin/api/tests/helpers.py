"""临时数据目录与读写小工具。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "organs" / "soul" / "fixtures"


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
