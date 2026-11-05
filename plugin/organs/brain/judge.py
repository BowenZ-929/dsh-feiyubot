"""brain-judge：真正的判断。三件事各出一个结论，不产句子。

输入锁死五样读数：心的 mood/energy（由调用方从 effective_state/energy 取好传进来，
本模块不自己合成）、熟稔、材料量（装包结果）、当日额度；另有消息事实（红线/忽略/点名/直接提问）。
次序固定：红线 → 忽略规则 → 硬门（被点名/被直接提问）→ 软门（心的 engage 三态 + 本模块判断）。
每次结论带命中码：软门部分与 heart-engage 共用同一套理由码枚举（organs.heart.model.REASONS），
脑独有的门用 JUDGE_REASONS。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from organs.heart import engage as heart_engage
from organs.heart import model as heart_model

JUDGE_REASONS = ("redline", "ignore-rule", "named", "direct-question",
                 "no-material", "act-requested", "speak-default")

REPLY_YES = "回"
REPLY_NO = "不回"
DIRECTIONS = ("顺着说", "只答不问", "安全方向", "沉默")
ACT_YES = "动手"
ACT_NO = "不动手"


@dataclass
class JudgeInput:
    """一次判断的全部输入。读数五样 + 消息事实；本模块不读盘、不写盘。"""

    text: str
    heart_state: Dict[str, Any]      # mood.now 与 energy 从这取（effective_state/energy 的输出）
    familiarity: float
    material: Dict[str, Any]         # 装包结果（has_material / slots_used）
    daily_quota: Dict[str, int]      # {"used": int, "limit": int}
    named: bool = False              # 被点名
    direct_question: bool = False    # 被直接提问
    redline_hits: List[str] = field(default_factory=list)
    ignore_hits: List[str] = field(default_factory=list)
    who: str = "主人"
    intent: str = heart_model.INTENT_PASSIVE
    now: Optional[str] = None


def _codes_or_default(codes: List[str], default: str) -> List[str]:
    return list(codes) if codes else [default]


def decide(inp: JudgeInput) -> Dict[str, Any]:
    """三件事各出一个结论，每次结论带命中码。"""
    reply_codes: List[str] = []
    direction_codes: List[str] = []
    act_codes: List[str] = []

    reply = REPLY_YES
    direction = "顺着说"
    act = ACT_NO

    if inp.redline_hits:
        reply, direction = REPLY_NO, "安全方向"
        reply_codes.append("redline")
        direction_codes.append("redline")
    elif inp.ignore_hits:
        reply, direction = REPLY_NO, "沉默"
        reply_codes.append("ignore-rule")
        direction_codes.append("ignore-rule")
    elif inp.named:
        reply, direction = REPLY_YES, "顺着说"
        reply_codes.append("named")
        direction_codes.append("named")
    elif inp.direct_question:
        reply, direction = REPLY_YES, "只答不问"
        reply_codes.append("direct-question")
        direction_codes.append("direct-question")
    else:
        mood_now = float((inp.heart_state or {}).get("mood", {}).get("now", 0.0))
        energy = float((inp.heart_state or {}).get("energy", 0.0))
        engage_inp = heart_engage.EngageInput(
            mood_now=mood_now, energy=energy,
            familiarity=inp.familiarity,
            today_count=inp.daily_quota.get("used", 0),
            intent=inp.intent, who=inp.who, now=inp.now)
        engaged = heart_engage.decide(engage_inp)
        if engaged["state"] == heart_model.STATE_SILENT:
            reply, direction = REPLY_NO, "沉默"
            reply_codes.extend(engaged["hits"])
            direction_codes.extend(engaged["hits"])
        elif engaged["state"] == heart_model.STATE_LESS:
            reply, direction = REPLY_YES, "顺着说"
            reply_codes.extend(engaged["hits"])
            direction_codes.append("少说")
        else:
            material = inp.material or {}
            if not material.get("has_material"):
                reply, direction = REPLY_NO, "沉默"
                reply_codes.append("no-material")
                direction_codes.append("no-material")
            elif inp.daily_quota.get("used", 0) >= inp.daily_quota.get("limit", 0):
                reply, direction = REPLY_NO, "沉默"
                reply_codes.append(heart_model.REASON_TODAY_VOLUME)
                direction_codes.append(heart_model.REASON_TODAY_VOLUME)
            else:
                reply, direction = REPLY_YES, "顺着说"
                reply_codes.append("speak-default")
                direction_codes.append("speak-default")

    if "帮我" in inp.text:
        act = ACT_YES
        act_codes.append("act-requested")
    else:
        act_codes.append("no-act-request")

    # 命中为空却判不说 = 失败；回但无码也补默认码，保证每个结论可归因。
    if reply == REPLY_NO and not reply_codes:
        reply_codes.append(heart_model.REASON_UNATTRIBUTED)
        direction_codes.append(heart_model.REASON_UNATTRIBUTED)
    return {
        "reply": reply,
        "direction": direction,
        "act": act,
        "codes": {
            "reply": reply_codes,
            "direction": direction_codes,
            "act": act_codes,
        },
    }
