"""NapCat → 官方 NoneBot OneBot v11 → 既有通道 → 器官核。"""
import os
import json
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter, MessageEvent


class ForwardingAdapter(Adapter):
    @classmethod
    def json_to_event(cls, json_data):
        event = super().json_to_event(json_data)
        if isinstance(event, MessageEvent):
            # 业务通道负责有序查询引用。仅移除匹配器副本中的 reply，避免这里先异步查询而倒序。
            # 官方解析器已深拷贝 original_message；bridge 仍转发完整原文与引用段。
            event.message = event.message.exclude("reply")
        return event

token = os.environ.get("QQAGENT_ONEBOT_TOKEN")
if not token:
    settings = Path(__file__).resolve().parents[1] / "onebot/.state/nonebot.json"
    token = json.loads(settings.read_text())["token"]

nonebot.init(
    driver="~fastapi",
    host="127.0.0.1",
    port=int(os.environ.get("QQAGENT_NONEBOT_PORT", "6290")),
    api_timeout=float(os.environ.get("QQAGENT_ONEBOT_TIMEOUT", "8")),
    onebot_access_token=token,
    log_level="INFO",
)
nonebot.get_driver().register_adapter(ForwardingAdapter)
nonebot.load_plugin("bridge")

if __name__ == "__main__":
    nonebot.run()
