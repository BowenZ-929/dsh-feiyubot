"""白名单：手能做的那几件事，一个名字一件事。

只有两个具名动作。**没有通用入口**——不写文件、不读任意路径、不执行命令，也不留
「动态注册动作」的口子（列表是模块级常量，运行期只读）。这条由 tests/test_source_rules.py
直接读字面量看住，不靠评审时眼睛看。
"""
from __future__ import annotations

from typing import Any, Tuple

ACTIONS: Tuple[str, ...] = ("add_task", "complete_task")


def allowed(name: Any) -> bool:
    """这个动作名在名单里吗。不认「最接近的那一个」，也不做名字归一化。"""
    return name in ACTIONS
