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


#: 根不对就抛这个。**它只是 paths.PathError 的别名字**——门禁与错误码都长在 organs/paths.py，
#: 这里不许再写判断（同一个错误出现过三次，每次都是两份标准分叉）。
RootsError = paths.PathError
HOME_ROOT_EMPTY = paths.HOME_ROOT_EMPTY
HOME_ROOT_IN_REPO = paths.HOME_ROOT_IN_REPO
HOME_ROOT_MISSING = paths.HOME_ROOT_MISSING
#: 魂没搬进来：这不是「根不对」，是「根对了但里面没有她」——错误码分开，别混成一个
SOUL_MISSING = "SOUL_MISSING"


def home_root() -> Path:
    """实盘器官目录（各器官目录的父）：QQAGENT_HOME > <仓库>/home/organs，且必须过门禁。

    空串不当「用默认」：那是有人设了变量又没给值，放过去就是第二条标准。
    """
    return paths.guard_root_env(
        os.environ.get(paths.ENV_HOME), paths.ENV_HOME, paths.ORGANS_HOME, must_exist=False)


def organ_root(organ: str, explicit: Any = None) -> Path:
    """某器官的实盘目录。explicit 是兼容用的显式覆盖（devserver --root 那种），同样过门禁。"""
    if organ in FACTORY_ONLY:
        return factory_root(organ)
    if explicit is not None:
        return paths.guard_root(explicit, "显式覆盖（--root）", must_exist=True)
    return home_root() / str(organ)


def factory_root(organ: str) -> Path:
    """出厂默认目录：plugin/organs/<器官>/data。"""
    return paths.factory_dir(organ)


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
            SOUL_MISSING,
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
