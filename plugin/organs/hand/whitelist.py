"""白名单：手能做的那几件事，一个名字一件事。

只有五个具名动作。**没有通用入口**——不写文件、不读任意路径、不执行命令，也不留
「动态注册动作」的口子（列表是模块级常量，运行期只读）。这条由 tests/test_source_rules.py
直接读字面量看住，不靠评审时眼睛看。

第五轮（手线第二批）在第四轮的两个动作后面接了 `send_image` 与 `draw`：一个是把图发出去、
一个是在本地画一张。两个都不是通用入口——参数里没有任意路径可写、没有命令可执行。

第三批（手线）接着加了 `set_style`：她只能改「怎么说话」，不能改「她是谁」。可写的旋钮
**恰两个**，取值范围只写在下面的 STYLE_KEYS 里——那一份是唯一真源：动作层按它判、面板按它
显示（api/hand.py 的 style_keys 从这里派生，不另抄一份）。「她是谁」归魂，手这边连读都不读
它：这里没有路径、没有打开文件、也没有「通用配置写」的口子（由 test_source_rules.py 看住）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

ACTIONS: Tuple[str, ...] = ("add_task", "complete_task", "send_image", "draw", "set_style")

#: 「怎么说话」的两个旋钮与它们的**闭区间**：chunks_per_reply＝一泡几句，max_chars＝一次几个字。
STYLE_KEYS: Dict[str, Tuple[int, int]] = {"chunks_per_reply": (1, 3), "max_chars": (10, 80)}


def allowed(name: Any) -> bool:
    """这个动作名在名单里吗。不认「最接近的那一个」，也不做名字归一化。"""
    return name in ACTIONS


def style_range(name: Any) -> Optional[Tuple[int, int]]:
    """这个旋钮可写的闭区间；不在 STYLE_KEYS 里就 None（同样不认「最接近的那一个」）。"""
    return STYLE_KEYS.get(name) if isinstance(name, str) else None
