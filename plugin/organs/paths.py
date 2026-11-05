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
# ── 门禁：只此一份 ──────────────────────────────────────────────────────────
# 同一个错误在这个仓库里出现过三次（六轴卡、anchors、access/rooms），每次都是「两份标准各活
# 一阵，然后分叉」。所以「实盘根能不能用」这条判断只在这里写一次：器官层（organs/home/store.py）
# 与启动层（api/roots.py）都调它，谁也不许再写一遍自己的判断。

#: 设了覆盖变量却是空串——空值不会被当成「用默认」悄悄放过去
HOME_ROOT_EMPTY = "HOME_ROOT_EMPTY"
#: 落在仓库里却不在 <仓库>/home 下：那是出厂默认参数目录（只读），不是她的实盘
HOME_ROOT_IN_REPO = "HOME_ROOT_IN_REPO"
#: 目录不存在：不替她造一个根，猜出来的根迟早和真源分叉
HOME_ROOT_MISSING = "HOME_ROOT_MISSING"


class PathError(Exception):
    """根不对一律带码抛：调用方要分得清「空值」「指着出厂目录」「根本没有这个目录」。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


def resolve(path: Any) -> Path:
    """尽量取真实路径；路径太怪/权限不够就按字面比，让门禁自己说话。"""
    try:
        return Path(str(path)).expanduser().resolve()
    except OSError:
        return Path(str(path)).expanduser()


def guard_root(path: Any, origin: str, *, must_exist: bool = False) -> Path:
    """实盘根唯一门禁。三条规矩，一条都不许在别处重写：

    1. 仓库内的路径只认 `<仓库>/home` 底下——`plugin/`、`docs/` 是出厂默认与文档，不是实盘；
    2. 仓库**外**的已存在目录放行（部署与家的测试都这么放实盘，别收紧掉）；
    3. `must_exist` 时目录必须已经存在——不替她建根，也不回退出厂默认。
    """
    raw = Path(str(path)).expanduser()
    resolved = resolve(raw)
    hub = resolve(HOME)
    repo = resolve(REPO_ROOT)
    in_hub = resolved == hub or hub in resolved.parents
    in_repo = resolved == repo or repo in resolved.parents
    if in_repo and not in_hub:
        raise PathError(
            HOME_ROOT_IN_REPO,
            "%s 解析到 %s：这是仓库内、但不在 %s 下的目录（plugin/ 是出厂默认参数、只读；"
            "docs/ 是文档）。她的实盘要么放 %s 下，要么用 %s 指到仓库外一个已存在的目录。"
            % (origin, resolved, HOME, HOME, ENV_HOME),
        )
    if must_exist and not raw.is_dir():
        raise PathError(
            HOME_ROOT_MISSING,
            "%s 解析到 %s，可那个目录不存在：要么把它建好，要么把 %s 指到已存在的实盘。"
            "程序不替她造一个根——猜出来的根迟早和真源分叉。" % (origin, raw, ENV_HOME),
        )
    return raw

def guard_root_env(raw: Any, origin: str, default: Any, *, must_exist: bool = False) -> Path:
    """带环境变量覆盖的门禁：没设 → 用默认根；设了但是空串 → HOME_ROOT_EMPTY；
    设了有值 → 走 guard_root。**「空值不等于没设」这条规矩也只写在这里。**"""
    if raw is None:
        return guard_root(default, "默认实盘根", must_exist=must_exist)
    if not str(raw).strip():
        raise PathError(
            HOME_ROOT_EMPTY,
            "设了 %s 却是空的：要么给它一个已存在的实盘目录，要么干脆别设，用默认的 %s"
            "——空值不会被当成「用默认」悄悄放过去" % (origin, default),
        )
    return guard_root(raw, origin, must_exist=must_exist)
