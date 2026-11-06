"""心：她此刻如何。

不判断对错，只记状态——五件：

1. mood：此刻心情 + 会往回走的基线。
2. energy：精力，醒着掉、睡着回。
3. offset：性格偏移，心只偏移魂，不改写魂。
4. engage：说不说、说多少（说 / 少说 / 不说）。
5. 落盘：上述状态各按自己的节奏写文件，坏数据不炸调用方。

两个投递口（供核与通道调，别的地方不许碰心情数值）：
- report_event(kind, who, …)：五类情绪事件的投递**建议**，心按冷却与权重自己决定推多少；
- report_reaction(kind, who, …)：她开口之后有没有人理；真进了静音，再补推一条 cold。

本包只由器官内部使用，不 import 宿主。
"""
from __future__ import annotations

from .report import report_event, report_reaction

__all__ = ("report_event", "report_reaction")
