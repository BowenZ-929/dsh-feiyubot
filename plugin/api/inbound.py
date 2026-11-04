"""核的入站端点（M2）：通道送来一条消息，这里回一段写死的字符串。

只做搬运的落点：不读魂、不读判据、不看消息内容、不落盘、无分支。
M2 只证明「通道 → 核 → 通道」这条路通；怎么回话是后面里程碑的事。

契约：POST /inbound，body 任意 JSON（通道当前送 who/where/text/hasImage/messageId），
响应 200 {"ok": true, "text": "<写死的字符串>"}。无鉴权，只许绑 127.0.0.1。
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

INBOUND_PATH = "/inbound"
INBOUND_METHOD = "POST"
INBOUND_TEXT = "回声：收到"

Result = Tuple[int, Dict[str, Any]]


def owns(method: str, path: str) -> bool:
    """这个请求归不归本端点：devserver 据此在转给 api.handle 之前分流。"""
    return method.upper() == INBOUND_METHOD and path.partition("?")[0] == INBOUND_PATH


def handle(body: Optional[Mapping[str, Any]] = None) -> Result:
    """不管送来什么，都回同一句话。"""
    return 200, {"ok": True, "text": INBOUND_TEXT}
