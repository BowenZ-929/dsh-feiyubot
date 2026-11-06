"""偏好卡盖在政策上的那一下：两个旋钮怎么落到 split 上，**判据只长在这一处**。

「这个人希望她怎么说话」有两个旋钮（organs/home 的偏好卡，一人一张、后写的算）：
- max_chars = N        这一泡最多几个字；
- chunks_per_reply = M 一条回话最多几个块。

两个旋钮都不是 split 自己的键，得**盖掉** organs/mouth/data/policy.json 的 split 节才能生效。
这一口气有三条路要走（普通一轮在 organs/mind/conversation.py 的 _bubbles，边界话与冷却轮在
api/chat.py），三条路若各抄一份覆盖规则，早晚有一处忘了改——所以规则收拢到这里：**纯函数，
入参一份 policy、一张读数，出参新 policy 与一句留痕，别的什么都不干。**

三条规矩（冻结契约）：
- 有 max_chars=N：改 split.max_chars，并把 sentences_per_part 压成 1——「两句一泡」会突破字限，
  字限要真落地，条数那一条得让位；
- **只有** chunks_per_reply=M：改 split.max_parts（split._throttle 把泡合并到不超过 M 泡）；
- 两个都有：**字限优先**，chunks_per_reply 这次不生效，但留一句痕（applied / ignored）如实交代，
  不静默丢——一个人设了 M=1、N=20 却拿到 3 泡，他有权知道是哪一条赢的。

读数一律按**没设**处理的情形（0、负数、bool、非整数）：0 泡 0 字不是「更短的偏好」，是坏读数；
bool 是 int 的子类，True 落盘就成了 1。**坏读数不盖默认值，也不留痕**——口径与 api/chat.py 原来
那份 _knob_of 一字不差，改这一处不是改口径。

没有卡（pref 是空表）：返回的 policy 与入参**逐字相等**，留痕 None，一个键都不动。
改的永远是**深拷出来的那一份**：入参 policy 是 warehouse.load 的读数，谁都不许在原地改它。

纯函数：不读盘、不碰时钟、不写日志、不 import 家/手/api（本文件只 import 标准库的 copy 与 typing）。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, Mapping, Optional, Tuple

#: 覆盖规则真正会动的两个旋钮名（卡上那两个键）。
CHAR_KNOB = "max_chars"
PART_KNOB = "chunks_per_reply"


def _knob(pref: Mapping[str, Any], key: str) -> Optional[int]:
    """卡上那一个旋钮的读数：只认正整数，其余（0、负数、bool、非整数、没给）一律算没设。"""
    value = pref.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def overlay(policy: Mapping[str, Any],
            pref: Mapping[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """按这张卡盖掉 policy 的 split 旋钮，返回 (新 policy, 冲突留痕或 None)。

    第一个返回**深拷出来的新表**：入参 policy 一个字都不动，所以同一份读数可以被几处各盖一次。
    第二个只在「两个旋钮都给了」时非 None：字限赢，chunks_per_reply 这一次不生效。

    split 不在 / 不是键值表（坏件）时不动旋钮，但**留痕照样给**：卡上那两个数确实撞上了，
    这件事跟她能不能改 split 无关。
    """
    out: Dict[str, Any] = copy.deepcopy(dict(policy))
    pref = pref or {}
    cap = _knob(pref, CHAR_KNOB)
    parts = _knob(pref, PART_KNOB)
    rules = out.get("split")
    if isinstance(rules, dict) and (cap is not None or parts is not None):
        rules = dict(rules)
        out["split"] = rules
        if cap is not None:
            rules[CHAR_KNOB] = cap
            rules["sentences_per_part"] = 1
        elif parts is not None:
            rules["max_parts"] = parts
    if cap is None or parts is None:
        return out, None
    return out, {"chunks_per_reply": parts, "max_chars": cap, "applied": [CHAR_KNOB],
                 "ignored": [{"key": PART_KNOB, "reason": "char_limit_first"}]}
