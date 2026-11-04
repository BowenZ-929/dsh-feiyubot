"""脑的六个只读端点：原样搬自 api/api.py，逻辑一个字没改。

脑的写入口是程序内的 ingest()，面板没有、也不许有写脑的路由（architecture §9.2）。
brain=None（脑没载入）时 /brain 保持"未接入"，其余如实报未载入。
"""
from __future__ import annotations

from typing import Any, Mapping

from organs.brain.model import PENDING

from .core import Result

BRAIN_ROUTES = {
    "/brain": "GET",
    "/brain/overview": "GET",
    "/brain/today": "GET",
    "/brain/people": "GET",
    "/brain/recall": "GET",
    "/brain/selfnarration": "GET",
    "/brain/proposals": "GET",
}


def _overview(brain: Any) -> Result:
    """脑的只读总览；/brain/overview 供面板页拉取（devserver 的 GET /brain 是静态页）。"""
    today = brain.today_view()
    return 200, {
        "status": "已接入",
        "readonly": True,
        "agent": "brain",
        "today_entries": len(today["entries"]),
        "people": len(brain.people_list()),
        "longterm_entries": len(brain.longterm_entries()),
        "pending_proposals": len(brain.proposals().get("proposals", [])),
        "selfnarration_version": (brain.selfnarration_latest() or {}).get("version", 0),
        "pending": ["热记忆的形状", "遗忘的力度", "记忆滤镜"],
    }


def dispatch(route: str, params: Mapping[str, str], brain: Any) -> Result:
    """脑的六个只读端点。brain=None 时 /brain 保持「未接入」，其余如实报未载入。"""
    if brain is None:
        if route == "/brain":
            return 200, {"status": "未接入", "readonly": True, "agent": "brain"}
        return 503, {"ok": False, "message": "脑未载入：先起脑的数据目录（devserver --brain-root）"}
    if route in ("/brain", "/brain/overview"):
        return _overview(brain)
    if route == "/brain/today":
        return 200, brain.today_view()
    if route == "/brain/people":
        return 200, {"people": brain.people_list()}
    if route == "/brain/recall":
        q = params.get("q", "").strip()
        if not q:
            return 400, {"ok": False, "message": "回想必须带 q（查询串）"}
        return 200, brain.recall(q, who=params.get("who") or None)
    if route == "/brain/selfnarration":
        return 200, {"latest": brain.selfnarration_latest(),
                     "versions": brain.selfnarration_versions()}
    return 200, brain.proposals()
