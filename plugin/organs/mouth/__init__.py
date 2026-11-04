"""嘴：怎么说。切分（切在哪）+ 节奏（隔多久），阈值全在 data/policy.json。

两件事分在两个互不 import 的文件里：split.py 只管切在哪，pace.py 只管多久一条；
政策数据走 warehouse.py 从「家·仓库」借阅（现在是包内 data/，家做出来只换路径不换格式）。
"""
from __future__ import annotations

from . import address
from . import gate
from . import tone
from .model import Batch, Policy, PolicyMiss, Seg
from .pace import fill, gaps, visible_chars
from .split import split
from .warehouse import WarehouseBroken, WarehouseMiss, load

__all__ = ["Batch", "Seg", "Policy", "PolicyMiss", "split", "fill", "gaps",
           "visible_chars", "load", "WarehouseMiss", "WarehouseBroken",
           "address", "gate", "tone"]
