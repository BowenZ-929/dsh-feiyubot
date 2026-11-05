"""说话端点：POST /chat —— 核上唯一一条写，把一轮真对话从入口说出去。

**只做 HTTP 映射，不装第二遍。** 装配（魂 → 脑 → 心 → 嘴 → 模型 → 过嘴 → 落脑）全在
organs/mind/conversation.py 的 answer()：那一处是全仓唯一的装配点，QQ 通道将来也直接调它。
本文件只做三件 HTTP 的事：认 body、把模型失败翻成状态码、把结果原样交出去。

失败一律 **非 200 + ok:false**，body 固定三键 {ok:false, kind, message}：
kind 给机器分岔，message 是模型那边的人话原文（str(LLMError) == message），一个字节都不改。
模型回了空内容也不算成功——那是 empty，不是「她轻轻没说话」；把空串当 200 发出去，页面就会
画出一个「她说了一句空话」的假成功。

不加鉴权、不改绑地址：这一层的入口由 devserver / 宿主决定，端点只认 POST /chat。
每次失败写一行 WARNING 日志（logger 名 qqagent.chat，含 kind 与 message）——失败要留痕，
但日志不是给页面的东西，页面只读返回体。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from organs.mind import conversation
from organs.mind.llm import LLMError

from .core import Result

CHAT_ROUTES = {"/chat": "POST"}

LOGGER_NAME = "qqagent.chat"
MISSING_TEXT = "POST /chat 要给 text（一句话）"
STORE_MISSING = "魂未载入：说话前先起核"
BRAIN_MISSING = "脑未载入：没有记忆就不开口"

# 模型失败 → HTTP：拿不到 key 是配置问题（503，去补），鉴权/服务端是上游的脸，
# 超时与限流是「等会儿再来」，空内容是上游没给话（502）而不是这一轮的错。
STATUS_BY_KIND = {
    "no-key": 503,
    "auth": 502,
    "timeout": 504,
    "rate_limit": 429,
    "empty": 502,
    "http": 502,
    "config": 500,
}
DEFAULT_STATUS = 502

LOG = logging.getLogger(LOGGER_NAME)


def dispatch(route: str, body: Optional[Dict[str, Any]],
             store: Any, brain: Any) -> Result:
    """POST /chat：一句话进来，一整轮出去；模型失败翻成状态码与 kind。

    body 非 dict、text 缺失或全空白 → 400；store / brain 未载入 → 503（说不出话的两种原因
    分开报：没有魂是没底色，没有脑是没记忆）。LLMError 之后一律非 200 + ok:false。
    """
    text = _text(body)
    if text is None:
        return 400, {"ok": False, "message": MISSING_TEXT}
    if store is None:
        return 503, {"ok": False, "message": STORE_MISSING}
    if brain is None:
        return 503, {"ok": False, "message": BRAIN_MISSING}
    try:
        payload = conversation.answer(text, who=_who(body), store=store, brain=brain)
    except LLMError as exc:
        status = STATUS_BY_KIND.get(exc.kind, DEFAULT_STATUS)
        LOG.warning("POST /chat 失败：kind=%s message=%s", exc.kind, exc.message)
        return status, {"ok": False, "kind": exc.kind, "message": exc.message}
    return 200, payload


def _text(body: Any) -> Optional[str]:
    """body 里的那句话：不是对象、缺 text、全空白都算没给（返回 None，调用方回 400）。"""
    if not isinstance(body, dict):
        return None
    raw = body.get("text")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def _who(body: Any) -> str:
    """这一轮是谁在说话：可选，没给就是空串（装配那边按生人档算称呼）。"""
    if not isinstance(body, dict):
        return ""
    raw = body.get("who")
    return raw.strip() if isinstance(raw, str) else ""
