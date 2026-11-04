"""她的实盘根：解析、门禁、补件。实盘在 <仓库>/home，出厂默认在 plugin/organs/<id>/data。

四条规矩（测试见 api/tests/test_roots.py）：
1. 门禁：解析出来的根必须落在 <仓库>/home 下。指向 plugin/、docs/、仓库外一律拒绝启动——
   她的实盘是活人的东西，混进"出厂默认参数"里就再也分不清哪份是真的。
2. 补件：实盘里缺的文件，从 plugin/organs/<器官>/data/ 复制一份初始化；只补缺的，不覆盖已有的。
3. 魂例外：home/organs/soul 空着就报「魂还没搬进来」并拒绝启动。魂只从她自己的实盘/备份搬，
   程序绝不替她造一个。
4. 嘴不进 home：它的三份是语料与参数，不是实盘状态，留在 plugin 当只读参数。

环境变量 QQAGENT_HOME 可以把实盘父目录挪到别处（测试与验收用），但第 1 条对它同样生效。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List

from organs import paths

#: 进 home 的器官（实盘状态）
HOUSE_ORGANS = ("soul", "brain", "heart", "home", "day")
#: 不进 home 的器官（只读语料/参数）
FACTORY_ONLY = ("mouth",)
#: 补件时跳过的文件名：这份忽略规则在 home 里会二次生效，反而把 .gitkeep 一起吃掉了
SKIP_ON_SEED = (".gitignore",)


class RootsError(Exception):
    """根不对 / 魂没搬进来：这两种情况都必须拒绝启动，不能降级跑。"""


def home_root() -> Path:
    """实盘器官目录（各器官目录的父）：QQAGENT_HOME > <仓库>/home/organs，且必须过门禁。"""
    return _guard(paths.home_root())


def organ_root(organ: str, explicit: Any = None) -> Path:
    """某器官的实盘目录。explicit 是兼容用的显式覆盖（devserver --root 那种），同样过门禁。"""
    if organ in FACTORY_ONLY:
        return factory_root(organ)
    if explicit is not None:
        return _guard(explicit)
    return home_root() / str(organ)


def factory_root(organ: str) -> Path:
    """出厂默认目录：plugin/organs/<器官>/data。"""
    return paths.factory_dir(organ)


def _guard(path: Any) -> Path:
    """门禁：只认 <仓库>/home 底下的路径。"""
    raw = Path(str(path)).expanduser()
    resolved = _resolve(raw)
    hub = _resolve(paths.HOME)
    if resolved != hub and hub not in resolved.parents:
        raise RootsError(
            "她的实盘必须放在 %s 下，不能放在 plugin/、docs/ 或仓库外：现在解析到 %s。"
            "（plugin/organs/<器官>/data 是出厂默认参数，只读，不是她的实盘；"
            "把 QQAGENT_HOME 指回 %s，或者干脆别设它，用默认的 %s）"
            % (hub, resolved, hub, paths.ORGANS_HOME))
    return resolved


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # 路径太怪/权限不够：按字面比，让门禁自己说话
        return path


def seed(organ: str) -> List[Path]:
    """从出厂默认补件：只补实盘里缺的文件，已有的一个字节都不动。"""
    source = factory_root(organ)
    target = organ_root(organ)
    copied: List[Path] = []
    if not source.is_dir():
        return copied
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.name in SKIP_ON_SEED:
            continue
        out = target / path.relative_to(source)
        if out.exists():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(path), str(out))
        copied.append(out)
    return copied


def bootstrap() -> Dict[str, Any]:
    """启动前跑一次：门禁 → 魂在不在 → 其余器官补件 → 把实盘根写进环境变量。

    返回一份可以打进日志的小结；根不对或魂不在就抛 RootsError，调用方必须拒绝启动。
    """
    base = home_root()  # 门禁在这一步
    soul = base / "soul"
    if not _has_any_file(soul):
        raise RootsError(
            "魂还没搬进来：%s 里没有她的魂。魂只从她自己的实盘/备份搬，"
            "程序不会替她造一个——把魂放进 %s 再来。" % (soul, soul))
    seeded: Dict[str, int] = {}
    for organ in HOUSE_ORGANS:
        if organ == "soul":  # 魂不补
            continue
        seeded[organ] = len(seed(organ))
    env = export_env()
    return {"home": str(base), "seeded": seeded, "env": env}


def _has_any_file(path: Path) -> bool:
    return path.is_dir() and any(item.is_file() for item in path.iterdir())


def export_env() -> Dict[str, str]:
    """把实盘根写进各器官的环境变量（器官层的三级解析：构造参数 > 环境变量 > 出厂默认）。

    单一真相留在这里：器官自己不去猜仓库结构。
    """
    base = home_root()
    mapping = {
        "HOME_ROOT": base / "home",
        "DAY_ROOT": base / "day",
        "HEART_ROOT": base / "heart",
    }
    for name, value in mapping.items():
        os.environ[name] = str(value)
    return {name: str(value) for name, value in mapping.items()}
