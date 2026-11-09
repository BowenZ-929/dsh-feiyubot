"""三层闸：**白名单 → 单次授权 → 额度**，顺序不许换。

先认动作名（这一步不做任何 IO），再问要不要授权，最后才数额度——反过来的话，
一个名单外的动作会先被数一遍额度，等于允许从门外探测「今天还有几次」。

每一层都对着一个拒绝码：`ACTION_NOT_ALLOWED` / `GRANT_REQUIRED` / `QUOTA_EXCEEDED`。
回执分两层：`say` 说人话（含**缺哪一条**），`code` 给面板与状态机断言。

风险档只有三值：无害 / 可撤销 / 不可逆。本轮五个动作都是「可撤销」（家的流水只增，
完成一笔也能看出来是第几次；图发出去可撤回；画只落本地的盘；改口吻只改一张偏好卡，
随时能再改回来），所以都免授权；但闸得在——第一个不可逆动作来时不用现加。表里没有的动作
按**最坏档**算（不可逆），fail closed：漏一条就等于没有。

第 1 条白名单只看名字。六个动作为什么都是「可撤销」（判据，不是措辞）：`send_image` 的消息可撤回、且通道那边
有自己的门与配额，本器官不为它再立一道一次性授权；`draw` 只在本地生成一张图、落点在自己的
盘上，删掉就没了；`set_style` 写的是家·名片盒里的偏好卡（只增不改），改回去就是再追加一行。
「可撤销」不等于「随便做」：额度那一层照旧，账照旧只增。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from . import quota, whitelist

#: 风险档：无害 / 可撤销 / 不可逆。三值，不另立第四档。
RISK_LEVELS = ("无害", "可撤销", "不可逆")
HARMLESS = "无害"
REVERSIBLE = "可撤销"
IRREVERSIBLE = "不可逆"
#: 表里没有的动作按最坏档算。
DEFAULT_RISK = IRREVERSIBLE

#: 本轮六个动作，且都可撤销（新增的三个不另立一次性授权：本轮没有 grant 存储）。
#: lookup 为什么也标「可撤销」而不是「无害」：它读写不了本地任何东西，但它一次外发加一笔费用
#: 买不回来——三档里没有「只读但有代价」这一档，而标成不可逆就等于永远要一份不存在的 grant
#: （即永远拒）。代价由额度与只增的手账看住，所以归这一档，边界写在这里，不留给人猜。
ACTION_RISK: Dict[str, str] = {"add_task": REVERSIBLE, "complete_task": REVERSIBLE,
                               "send_image": REVERSIBLE, "draw": REVERSIBLE,
                               "set_style": REVERSIBLE, "lookup": REVERSIBLE}

#: 三层，顺序就是判定顺序。
GATES = ("whitelist", "grant", "quota")


def risk_of(action: Any) -> str:
    return ACTION_RISK.get(action, DEFAULT_RISK)


def needs_grant(action: Any) -> bool:
    """风险档是「不可逆」就要一次性授权。本轮五个动作都免授权。"""
    return risk_of(action) == IRREVERSIBLE


def _deny(code: str, say: str, detail: Dict[str, Any]) -> Dict[str, Any]:
    return {"allowed": False, "code": code, "say": say, "detail": detail}


def check(action: Any, *, who: str, now: Any = None, root=None) -> Dict[str, Any]:
    """三层逐条走到黑：任一层不过就拒，并在 `say` 里说清缺的是第几条。

    返回值固定四栏：`allowed` / `code`（放行时是 None）/ `say` / `detail`。
    """
    detail: Dict[str, Any] = {"action": action, "who": who, "risk": risk_of(action),
                              "passed": []}

    # 第 1 条：白名单。只有五个具名动作，不认「最接近的那一个」。
    if not whitelist.allowed(action):
        return _deny("ACTION_NOT_ALLOWED",
                     "「%s」不在手能做的事里（只有 %s）——缺第 1 条：白名单"
                     % (action, "、".join(whitelist.ACTIONS)),
                     dict(detail, gate="whitelist", allowed=sorted(whitelist.ACTIONS)))
    detail["passed"] = ["whitelist"]

    # 第 2 条：单次授权。不可逆的动作必须先拿到一次性授权，没有就拒。
    if needs_grant(action):
        return _deny("GRANT_REQUIRED",
                     "「%s」的风险档是「%s」，做下去收不回来——缺第 2 条：单次授权"
                     "（这一次没有 grant）" % (action, risk_of(action)),
                     dict(detail, gate="grant", need="grant_id", risk=risk_of(action)))
    detail["passed"] = ["whitelist", "grant"]

    # 搜索只有每轮上限，不占用写任务、发图等动作的日额度。
    if action == "lookup":
        return {"allowed": True, "code": None, "say": "可以查询公开资料", "detail": detail}

    # 第 3 条：每人每天额度。数值来自数据文件，用量来自手账。
    left = quota.remaining(who, now=now, root=root)
    if left["remaining"] <= 0:
        return _deny("QUOTA_EXCEEDED",
                     "%s 今天还能 %d 次（上限 %d 次，已用 %d 次，额度来自 %s）——缺第 3 条：额度"
                     % (who, left["remaining"], left["limit"], left["used"], left["source"]),
                     dict(detail, gate="quota", **left))
    detail["passed"] = ["whitelist", "grant", "quota"]
    detail["quota"] = left
    return {"allowed": True, "code": None,
            "say": "可以：%s（%s，%s 今天还能 %d 次）"
                   % (action, risk_of(action), who, left["remaining"]),
            "detail": detail}
