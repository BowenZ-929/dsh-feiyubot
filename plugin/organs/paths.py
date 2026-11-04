"""实盘落点：她的实盘在 <仓库>/home/organs/<id>，出厂默认在 organs/<id>/data。

器官层只从这里换算目录——"包内 data/" 不再是实盘默认，而是出厂默认参数（只读）。
能不能用、要不要从出厂默认补件、魂在不在，由 api/roots.py 在启动时判一次。
"""
from __future__ import annotations

import os
from pathlib import Path

# organs/paths.py -> organs -> plugin -> <仓库>
REPO_ROOT = Path(__file__).resolve().parents[2]
HOME = REPO_ROOT / "home"
ORGANS_HOME = HOME / "organs"
FACTORY = Path(__file__).resolve().parent
ENV_HOME = "QQAGENT_HOME"


def home_root() -> Path:
    """实盘器官目录（各器官目录的父）：QQAGENT_HOME > <仓库>/home/organs。"""
    raw = os.environ.get(ENV_HOME, "").strip()
    return Path(raw).expanduser() if raw else ORGANS_HOME


def organ_dir(organ: str) -> Path:
    """某个器官的实盘目录（环境变量覆盖只管父目录，不逐个器官开口子）。"""
    return home_root() / str(organ)


def factory_dir(organ: str) -> Path:
    """出厂默认：plugin/organs/<器官>/data（只读参数，不写）。"""
    return FACTORY / str(organ) / "data"
