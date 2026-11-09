"""只接线；人格、合批、发言判断、记忆均留在原位置。

使用 NoneBot 官方连接钩子、事件预处理器、Bot.call_api，不复制协议适配器。
"""
import asyncio
import json
import os
from contextlib import suppress

from nonebot import get_app, get_driver, logger
from nonebot.adapters.onebot.v11 import Bot, Event, MessageEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.compat import model_dump
from nonebot.message import event_preprocessor
from nonebot.utils import DataclassEncoder
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

CHANNEL_URL = os.environ.get("QQAGENT_BUSINESS_WS", "ws://127.0.0.1:6200/ws")
links = {}
driver = get_driver()


class Link:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.socket = None
        self.ready = asyncio.Event()
        self.task = asyncio.create_task(self.run())

    async def run(self):
        # websockets 自带连接退避；只重连，不重发任何 action。
        async for socket in connect(CHANNEL_URL, open_timeout=3):
            self.socket = socket
            self.ready.set()
            logger.info("业务通道已连接，QQ={}", self.bot.self_id)
            try:
                async for raw in socket:
                    frame = json.loads(raw)
                    try:
                        data = await self.bot.call_api(frame["action"], **frame.get("params", {}))
                        ack = {"status": "ok", "retcode": 0, "data": data}
                    except ActionFailed as error:
                        ack = error.info.copy()
                    except NetworkError:
                        if frame["action"] in ("get_msg", "get_record"):
                            # 只读取件超时不代表发消息不确定，保留连接让本轮降级继续。
                            ack = {"status": "failed", "retcode": 1200,
                                   "data": None, "wording": "只读接口超时，未自动重试"}
                            ack["echo"] = frame.get("echo")
                            await socket.send(json.dumps(ack, ensure_ascii=False))
                            continue
                        # QQ 可能已收到；关闭内部连接，使既有通道记为 uncertain。
                        # 不能假称发送失败，也不能重试造成重复消息。
                        logger.warning("QQ 接口回执不确定，关闭本轮连接；不重发")
                        await socket.close()
                        break
                    ack["echo"] = frame.get("echo")
                    await socket.send(json.dumps(ack, ensure_ascii=False))
            except ConnectionClosed:
                logger.warning("业务通道断开，等待重连")
            finally:
                self.ready.clear()
                self.socket = None
                await socket.close()


@driver.on_bot_connect
async def connected(bot: Bot):
    links[bot.self_id] = Link(bot)


@driver.on_bot_disconnect
async def disconnected(bot: Bot):
    link = links.pop(bot.self_id, None)
    if link:
        link.task.cancel()
        with suppress(asyncio.CancelledError):
            await link.task


@driver.on_shutdown
async def shutdown():
    for link in list(links.values()):
        await disconnected(link.bot)


@event_preprocessor
async def forward(bot: Bot, event: Event):
    link = links.get(bot.self_id)
    if link is None:
        logger.error("事件未转发：业务连接尚未创建")
        return
    frame = model_dump(event)
    if isinstance(event, MessageEvent):
        # NoneBot 会移除 @、reply 等段；原始副本保留完整上下文给既有业务判断。
        frame["message"] = event.original_message
        for key in ("original_message", "reply", "to_me"):
            frame.pop(key, None)
    try:
        await asyncio.wait_for(link.ready.wait(), timeout=3)
        await link.socket.send(json.dumps(frame, cls=DataclassEncoder, ensure_ascii=False, separators=(",", ":")))
    except (TimeoutError, ConnectionClosed):
        logger.error("事件未转发：业务通道离线，未重放到记忆")


@get_app().get("/healthz")
async def health():
    return {"channel": "nonebot", "bots": [
        {"self_id": key, "business_connected": link.ready.is_set(), "running": not link.task.done()}
        for key, link in links.items()
    ]}
