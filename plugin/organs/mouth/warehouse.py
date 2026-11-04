"""家·仓库的只读借阅层：按名字读一份 JSON，读不到就响。

**真源是「家·仓库」。** 这里只是嘴伸过去借阅的那只手：数据现在暂放在 organs/mouth/data/，
家做出来以后**只换路径、不换格式**——名字、`.json` 后缀、顶层是一份键值表，这三件事不变，
所以那时改的是根目录（构造参数或环境变量），不是读法。

根目录三级（前一级没给才看下一级）：构造参数 root > 环境变量 MOUTH_DATA_DIR > 包内 data/。

缺件必须抛 WarehouseMiss，不许静默返回空表：读不到不等于「0 错误」，库不存在／库空／零命中
是三件事（CONTRIBUTING.md §1.1 第三条）。也**不提供** load_or_none——一条会返回 None 的入口
早晚被人当成「没有就是空」用，默认值就是这么长出来的。

只读：本文件没有任何写盘路径。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

DATA_DIR = Path(__file__).resolve().parent / "data"   # 包内数据目录（家里的仓库做出来前的临时落脚点）
ENV_VAR = "MOUTH_DATA_DIR"                            # 环境变量：换一个数据根目录，不动代码
SUFFIX = ".json"                                      # 名字不带后缀时自动补


class WarehouseMiss(FileNotFoundError):
    """仓库里没有这件：名字与找过的路径都带着，好让人一眼看出是路径错还是名字错。"""

    def __init__(self, name: str, path: str) -> None:
        self.name = name
        self.path = path
        super().__init__("家·仓库缺件：找不到 %r（找过 %s）" % (name, path))


class WarehouseBroken(ValueError):
    """仓库里有这件，但内容读不出来（不是合法 JSON，或顶层不是键值表）。

    与缺件分开报：文件在但坏了，和文件不在，是两种要修的地方。
    """

    def __init__(self, name: str, path: str, detail: str) -> None:
        self.name = name
        self.path = path
        self.detail = detail
        super().__init__("家·仓库读不动 %r（%s）：%s" % (name, path, detail))


def root_dir(root: Optional[Any] = None) -> Path:
    """定一下这次去哪儿找：构造参数 > 环境变量 MOUTH_DATA_DIR > 包内 data/。"""
    if root is not None:
        return Path(root)
    from_env = os.environ.get(ENV_VAR, "").strip()
    if from_env:
        return Path(from_env)
    return DATA_DIR


def path_of(name: str, root: Optional[Any] = None) -> Path:
    """名字（形如 "policy"、"clauses"）落成路径；带不带 .json 都认。"""
    if not name or not str(name).strip():
        raise WarehouseMiss(name, str(root_dir(root)))
    leaf = str(name).strip()
    if not leaf.endswith(SUFFIX):
        leaf += SUFFIX
    return root_dir(root) / leaf


def load(name: str, root: Optional[Any] = None) -> Dict[str, Any]:
    """按名字读一份 JSON，返回顶层键值表。

    缺件抛 WarehouseMiss，坏件抛 WarehouseBroken；两条路都不返回空表、也不造默认值。
    """
    path = path_of(name, root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise WarehouseMiss(name, str(path)) from None
    except OSError as exc:
        raise WarehouseBroken(name, str(path), "读不动：%s" % exc) from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WarehouseBroken(name, str(path), "不是合法 JSON：%s" % exc) from None
    if not isinstance(data, dict):
        raise WarehouseBroken(name, str(path), "顶层不是键值表，是 %s" % type(data).__name__)
    return data
