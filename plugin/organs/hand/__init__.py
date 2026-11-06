"""手：把「记下一件事」做成两个具名动作，外面套三层许可，旁边一本只增的手账。

第四轮只做一件能验证的事——她真能记下一件事：`add_task` 与 `complete_task` 两个动作
（白名单只有这两个），三层闸（白名单 → 单次授权 → 每人每天额度），一本只增的
`ledger.jsonl`（先落 reserved 再动手，结果另起一行）。

六个文件各管一段，职责不重叠：whitelist 定名单，permit 判三层，quota 算额度，
timelang 认最小时间词表，ledger 记手账，store 管路径与字节，actions 做动作并给统一回执。
**写盘只经 store 与 ledger**——别的模块一律调它们，不许自己 open 一个文件。
不画图、不刷 B 站、不发消息、不做提醒派发：那些不在本轮，也不在这里埋钩子。
"""
from __future__ import annotations

from . import actions, ledger, permit, quota, store, timelang, whitelist
from .actions import add_task, complete_task, due
from .permit import ACTION_RISK, RISK_LEVELS, check, needs_grant
from .quota import remaining
from .timelang import parse as parse_time

__all__ = [
    "actions", "ledger", "permit", "quota", "store", "timelang", "whitelist",
    "add_task", "complete_task", "due",
    "ACTION_RISK", "RISK_LEVELS", "check", "needs_grant", "remaining", "parse_time",
]
