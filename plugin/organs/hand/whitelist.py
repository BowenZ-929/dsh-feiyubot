"""白名单：手能做的那几件事，一个名字一件事。

只有四个具名动作。**没有通用入口**——不写文件、不读任意路径、不执行命令，也不留
「动态注册动作」的口子（列表是模块级常量，运行期只读）。这条由 tests/test_source_rules.py
直接读字面量看住，不靠评审时眼睛看。

第五轮（手线第二批）在第四轮的两个动作后面接了 `send_image` 与 `draw`：一个是把图发出去、
一个是在本地画一张。两个都不是通用入口——参数里没有任意路径可写、没有命令可执行。
"""
from __future__ import annotations

from typing import Any, Tuple

ACTIONS: Tuple[str, ...] = ("add_task", "complete_task", "send_image", "draw")


def allowed(name: Any) -> bool:
    """这个动作名在名单里吗。不认「最接近的那一个」，也不做名字归一化。"""
    return name in ACTIONS
