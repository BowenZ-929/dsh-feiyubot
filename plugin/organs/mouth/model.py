"""嘴的数据形状：一泡、一批、一份政策。

形状是接口（第二批通道、第三批表达、第四批面板照这些签名接），改形状等于改接口。
Policy 是一份只读的 dict，内容来自 organs/mouth/data/policy.json（经 warehouse.load 读入）。
本文件只放取值器：缺节缺键就抛 PolicyMiss，不给默认值——数据里没有的数值不许在代码里补，
否则「阈值在数据里」这句话就成了两处真源。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping

Policy = Dict[str, Any]           # 一份政策文档：从 data/policy.json 读出来的整份键值表，代码只读不写


class PolicyMiss(KeyError):
    """政策缺节或缺键：代码要用的数值必须写在数据里，不许在代码里补默认值。"""

    def __init__(self, what: str) -> None:
        self.what = what
        super().__init__("policy 缺 %s（政策数据在 organs/mouth/data/policy.json）" % what)


@dataclass(frozen=True)
class Seg:
    """一泡：要说的一段字，加上它自己的发送节奏。"""

    text: str
    gap_before: float = 0.0     # 距上一泡多少秒；split 只填 0.0，pace 才填
    send_at: float = 0.0        # 从这批开始算起第几秒发；split 只填 0.0，pace 才填
    degraded: bool = False      # 这一泡是硬切出来的（原段没有停顿符号，只好按字数切）
    reason: str = ""            # 硬切的人话理由；没硬切就是空串


@dataclass(frozen=True)
class Batch:
    """一批：这一轮要发的几泡，按发送顺序排。"""

    parts: List[Seg] = field(default_factory=list)
    summary: str = ""           # 人话摘要，例如「1/3 立即 · 2/3 +1.4s · 3/3 +1.1s」；pace 才填


def section(policy: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """取政策里的一节；缺节或那一节不是键值表就抛 PolicyMiss。"""
    try:
        part = policy[name]
    except (KeyError, TypeError):
        raise PolicyMiss("节 %r" % name) from None
    if not isinstance(part, Mapping):
        raise PolicyMiss("节 %r（读到的不是一份键值表）" % name)
    return part


def need(data: Mapping[str, Any], key: str) -> Any:
    """取政策里的一个键；缺键就抛 PolicyMiss。"""
    try:
        return data[key]
    except (KeyError, TypeError):
        raise PolicyMiss("键 %r" % key) from None
