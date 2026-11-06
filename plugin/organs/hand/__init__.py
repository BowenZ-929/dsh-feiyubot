"""手：四个具名动作 + 三层许可 + 一本只增的手账。

第四轮做「记下一件事」（`add_task` / `complete_task`）；第五轮（手线第二批）接上两个
产物型动作：`send_image`（把一张图交给通道发出去）与 `draw`（画一张、落自己的盘、登记进仓库、再发出去）。
四个动作共用同一套：白名单 → 单次授权 → 每人每天额度，先落 reserved 再动手，结果行写不进就改判 failed。

模块各管一段，职责不重叠：whitelist 定名单，permit 判三层，quota 算额度，timelang 认最小时间词表，
imgphrase 认「想看哪张图」那句短语，ledger 记手账，store 管路径与字节（图也落在这里），
assets 按家的房表与格子表取图的指针（仓库看卡，抽屉看格子），
channel 与 draw 是**仅有的两个能上网的模块**，actions 做动作并给统一回执，cli 是人可读的自检。
**写盘只经 store 与 ledger**——别的模块一律调它们，不许自己 open 一个文件。
`draw` 这个名字在包级指的是**模块**（网络那一层）；动作在 `actions.draw` 上，不在这里导出。
"""
from __future__ import annotations

from . import actions, assets, channel, imgphrase, ledger, permit, quota, store, timelang, whitelist
from . import draw as draw_client
from .actions import add_task, complete_task, due, send_image
from .permit import ACTION_RISK, RISK_LEVELS, check, needs_grant
from .quota import remaining
from .timelang import parse as parse_time

__all__ = [
    "actions", "assets", "channel", "draw_client", "imgphrase", "ledger", "permit", "quota",
    "store", "timelang", "whitelist",
    "add_task", "complete_task", "due", "send_image",
    "ACTION_RISK", "RISK_LEVELS", "check", "needs_grant", "remaining", "parse_time",
]
