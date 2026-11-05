"""近轮禁用：把她自己最近几轮用过的动作行与开场抽出来，去重计次。

三件事都是纯函数——「一整行只包着一对圆括号才算动作行」「重复几遍怎么数」「开场取几个字」——
所以单开一个模块：装配那层只该拿结果拼一句话，正则与去重不该堆进 conversation.py。

**开场的口径借 usage.py**（normalize 去标点 → 取前 N 字），不在这里另写一份去标点：
两处各写一套，同一句在「禁用」与「口癖护栏」上就会算出两个开头。开场算的是**去掉动作行
之后**的正文：动作行另有禁令，不去掉它，「开场」就永远是那一行（她一回话就先写动作）。

**只抽她那一侧**：recent 一行里 reply 是她的，said 是人的——人的话一个字都不许进禁令，
进了她就会以为人说过的话也不许重复。将来某行若把 who 写成她自己的自称，那一行的 said
才算她的；who 不是她就只认 reply。

**动作行原文照搬**（连圆括号一起），去重之后只多标一句「重复 N 次」：她下一轮要躲的是
这一整行的形状，改写半句都躲不掉。

**数配额的也是这里**（count_with_actions）：动作改配额制之后，这一轮该不该写动作要数
「最近几轮里有几轮带动作」——一轮里写两行仍是一轮，数成两轮就会提前封顶。

**删动作行的也是这里**（strip_action_lines）：配额满了就该一个动作都不写，可模型不听提示词，
所以成品里那几行要在这里删掉——认行的口径与禁令、配额同一份（action_lines），三处不许分叉。
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Tuple

from . import recent
from . import usage

PAIRS = {"（": "）", "(": ")"}      # 动作行只认整行圆括号：全角与半角都算
REPEAT = "（重复 %d 次）"             # 重复几遍要留痕；只出现一次的不标
ACTION_PREFIX = "动作行："            # 禁令里逐条的分类前缀
OPENING_PREFIX = "开场："             # 同上：开场那半句


def action_lines(text: Any) -> List[str]:
    """这条回复里的动作行原文：一整行只包着一对圆括号才算，混在句子里的括号不算。"""
    out: List[str] = []
    for raw in str(text if text is not None else "").splitlines():
        line = raw.strip()
        if len(line) < 2:
            continue
        close = PAIRS.get(line[0])
        if close is not None and line[-1] == close:
            out.append(line)
    return out


def her_texts(turns: Any, her_name: Any = "") -> List[str]:
    """最近几轮里**她那一侧**的原文：每行的 reply，加上 who 写成她自称的那些 said。"""
    name = str(her_name if her_name is not None else "").strip()
    out: List[str] = []
    for row in turns or []:
        if not isinstance(row, Mapping):
            continue
        out.append(str(row.get(recent.REPLY) or ""))
        if name and str(row.get(recent.WHO) or "").strip() == name:
            out.append(str(row.get(recent.SAID) or ""))
    return [text for text in out if text.strip()]


def without_actions(text: Any) -> str:
    """去掉动作行之后的正文：开场要从这一份算。

    动作行已经单列进禁令了，不去掉它，「开场」就永远是那一行——同一句话在禁令里出现
    两遍，她还会以为要躲的是两个东西。去标点与截字仍然走 usage（本模块不另写一套）。
    """
    body = str(text if text is not None else "")
    drops = set(action_lines(body))
    return "\n".join(line for line in body.splitlines() if line.strip() not in drops)


def tally(values: Any) -> List[Tuple[str, int]]:
    """去重并计次：第一次出现的顺序就是输出的顺序，重复几遍记在第二个分量里。"""
    counts: Dict[str, int] = {}
    for value in values or []:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return list(counts.items())


def banned(turns: Any, conf: Mapping[str, Any], her_name: Any = "") -> Dict[str, Any]:
    """这一轮该禁用什么：只取她那一侧最近 rounds 轮的动作行与开场（去重计次）。

    返回 {"rounds", "action_lines": [{"text", "count"}], "openings": [...]}：
    rounds 是**读了几轮**（配置里的 N），不是盘上有几轮——窗口外的更早轮不算禁令。
    开场从去掉动作行的正文算（without_actions），两个列表各自去重，互不重复。
    """
    rounds = int(conf["rounds"])
    window = list(turns or [])[-rounds:] if rounds > 0 else []
    texts = her_texts(window, her_name)
    actions = tally([line for text in texts for line in action_lines(text)])
    chars = int(conf["opening_chars"])
    openings: List[str] = []
    for head, _count in tally(usage.opening(without_actions(text), chars) for text in texts):
        if head:
            openings.append(head)
    return {"rounds": rounds,
            "action_lines": [{"text": text, "count": count} for text, count in actions],
            "openings": openings}


def count_with_actions(turns: Any, window: int, her_name: Any = None) -> int:
    """最近 window 轮里**她带动作行的轮数**：一轮里写两行也只算一轮。

    窗口与「她那一侧」的口径与 banned 段同一份（her_texts + action_lines），本函数不另写
    正则、不另定谁算她：两处各认一套，同一段历史在禁令与配额上就会数出两个数。
    窗口是**轮数**不是行数：只取最近 window 轮，更早的轮一律不进；window <= 0 等于没有历史。
    调用方给进来的就是历史（不含本轮），所以这里不减去任何东西。
    """
    rounds = int(window)
    if rounds <= 0:
        return 0
    used = 0
    for row in list(turns or [])[-rounds:]:
        if any(action_lines(text) for text in her_texts([row], her_name)):
            used += 1
    return used


def strip_action_lines(text: Any) -> Tuple[str, int, bool]:
    """删掉整行圆括号动作行（连着它后面那几行多余的空行）：(删后的原文, 删了几行, 是否留了原文)。

    口径仍走 action_lines：动作行只认整行圆括号，混在句子里的括号一个字节都不动。
    删完正文一个字都不剩时**不删**（第三项为 True，并把原文交回，删了几行报 0）——一轮回复被
    剥成空比带着一行动作更糟：话被删没了，人那侧就只收到一个空回音。
    第三项交给调用方去报 kept_because_empty，本函数不替它决定返回体长什么样。
    """
    body = str(text if text is not None else "")
    lines = action_lines(body)
    if not lines:
        return body, 0, False
    drops = set(lines)
    kept: List[str] = []
    skipping = False
    for line in body.splitlines():
        if line.strip() in drops:
            skipping = True          # 动作行后面那几行空行是它留下的空档，一起收走
            continue
        if skipping and not line.strip():
            continue
        skipping = False
        kept.append(line)
    clean = "\n".join(kept).strip()
    if not clean:
        return body, 0, True
    return clean, len(lines), False
