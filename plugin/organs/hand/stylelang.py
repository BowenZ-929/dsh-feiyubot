"""最小「怎么说话」词表：把人话翻成两个旋钮之一（chunks_per_reply／max_chars）。

手法照抄 timelang.py：**只认一张表、认不出返回 None、绝不猜、绝不补默认值**。表外的说法
（「温柔一点」「像我妈那样」「别那么凶」）一律 None，上层就反问她一句——猜错一个旋钮，
比反问一句贵得多。

表（去掉空格与句末标点之后**整句**对得上才算）：
  chunks_per_reply=1：一句话说完 / 别分开发 / 别拆开发 / 一条说完 / 一次说完
  chunks_per_reply=N：最多 N 条 / 最多 N 泡 / 分 N 条 / 分 N 泡
  max_chars=N：一次别超 N 字 / 别超 N 个字 / 一次 N 字以内 / 每条不超 N 字
  max_chars=20（短话那条固定档）：说短点 / 短一点 / 说简短点
  身份类：身份 / 魂 / 人设 / 名字 / 改成别的人 / 你叫（**出现即整句按身份类算**）

三点写下来的判断：

1. **身份类不返回 None**。同一句话里只要带上面那几个词，就返回 IDENTITY_KEY 这个明确的信号，
   上层据此回 KEY_NOT_ALLOWED（「改她是谁」不在这里）。返回 None 会被当成「没听懂」反问回去，
   等于放它从旁边的门溜过去——「你把自己改成别的人」听起来像在改口吻，其实是在改身份。
2. **一段话里说了两件不同的事就返回 None**，不挑一件办。她要是同时说「说短点，一句话说完」，
   猜哪个都不是我该做的；反过来，两段说的是同一件事（「一句话说完，别分开发」）照常认。
3. **范围不在这里判**。这里只负责「认得出是哪两个旋钮、说的是几」，能不能写、写多少合不合规，
   归 whitelist.STYLE_KEYS 与 actions.set_style——判据只有一处，不在这里抄第二遍。
"""
from __future__ import annotations

import re
from typing import Any, Optional, Tuple

#: 两个旋钮的名字。范围在 whitelist.STYLE_KEYS（本文件只管认字，不管范围）。
CHUNKS_KEY = "chunks_per_reply"
CHARS_KEY = "max_chars"

#: 身份类的信号：**不是**一个可写的 key，而是一个「别往这边走」的标记，由上层翻成 KEY_NOT_ALLOWED。
IDENTITY_KEY = "identity"

#: 「说短点」这类话翻成的长度档（嘴的短话档 20 字）。它必须落在 STYLE_KEYS["max_chars"] 里，
#: 这一条由 tests/test_set_style.py 钉住。
SHORT_MAX_CHARS = 20

#: chunks_per_reply=1 的固定说法。
CHUNKS_ONE: Tuple[str, ...] = ("一句话说完", "别分开发", "别拆开发", "一条说完", "一次说完")
#: max_chars=SHORT_MAX_CHARS 的固定说法。
SHORT_SAY: Tuple[str, ...] = ("说短点", "短一点", "说简短点")
#: 身份类：**出现即整句按身份类算**（子串命中，不做正则、不做词序猜测）。
IDENTITY_WORDS: Tuple[str, ...] = ("身份", "魂", "人设", "名字", "改成别的人", "你叫")

_CHUNKS_COUNT = re.compile(r"^(?:最多|至多)(\d{1,2})(?:条|泡)$")
_CHUNKS_SPLIT = re.compile(r"^分(\d{1,2})(?:条|泡)$")
_CHARS_PATTERNS = (re.compile(r"^一次别超(\d{1,3})字$"),
                   re.compile(r"^别超(\d{1,3})个字$"),
                   re.compile(r"^一次(\d{1,3})字以内$"),
                   re.compile(r"^每条不超(\d{1,3})字$"))

#: 一句话可以分成几段来说（逗号／句号／顿号／分号算断开）。**空格不算断开**：
#: 「一次别超 20 字」里的空格是排版，不是分句——分段只看标点，空格在段内一律抹平。
_SPLIT = re.compile(r"[，,。.、；;：:！!？?]+")
#: 段首段尾可以剥掉的标点与语气尾巴：只多认一个「吧／呀／啊／呢」，不多认一个字。
_TRAILING_PUNCT = "，,。.、；;：:！!？?～~…"
_PARTICLES = ("吧", "呀", "啊", "呢")


def parse(text: Any) -> Optional[Tuple[str, Any]]:
    """→ (key, value)；身份类 → (IDENTITY_KEY, None)；认不出 → None。"""
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None
    if _is_identity(_normalise(raw)):
        return IDENTITY_KEY, None

    found = []
    for piece in _SPLIT.split(raw):
        fragment = _normalise(piece)
        if not fragment:
            continue
        if _is_identity(fragment):
            return IDENTITY_KEY, None
        got = _parse_fragment(fragment)
        if got is not None and got not in found:
            found.append(got)
    if not found:
        return None
    if len(found) > 1:
        return None                 # 一段话里两件不同的事：不挑一件办，交给上层反问
    return found[0]


def is_identity(key: Any) -> bool:
    """这个词是身份类吗：哨兵本身，或有人直接把「改魂／改你的名字」写进了 key。

    上层据此把人话说到「改她是谁不在这里」上——它比「key 不在名单里」更具体，码是同一个。
    """
    if not isinstance(key, str):
        return False
    return key == IDENTITY_KEY or _is_identity(_normalise(key))


def _is_identity(text: str) -> bool:
    return any(word in text for word in IDENTITY_WORDS)


def _normalise(text: str) -> str:
    """去掉空格、句末标点与一个语气尾巴（只削这些，不动字）。"""
    piece = str(text).replace(" ", "").strip()
    while piece and (piece[-1] in _TRAILING_PUNCT or piece[-1] in _PARTICLES):
        if piece[-1] in _PARTICLES and len(piece) <= 1:
            break
        piece = piece[:-1]
    return piece


def _parse_fragment(fragment: str) -> Optional[Tuple[str, Any]]:
    """一整段对表；对不上就 None。**不做子串匹配**：多一个字就不认。"""
    if fragment in CHUNKS_ONE:
        return CHUNKS_KEY, 1
    if fragment in SHORT_SAY:
        return CHARS_KEY, SHORT_MAX_CHARS
    for pattern in (_CHUNKS_COUNT, _CHUNKS_SPLIT):
        found = pattern.match(fragment)
        if found:
            return CHUNKS_KEY, int(found.group(1))
    for pattern in _CHARS_PATTERNS:
        found = pattern.match(fragment)
        if found:
            return CHARS_KEY, int(found.group(1))
    return None
