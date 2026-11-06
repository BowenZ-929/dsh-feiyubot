"""「给我看你的自画像」这一类话：装配前认一次，认出来就交给手把家里的图发出去。

她回一句「给你看」本身是空的——真正的动作是**手把家里那张图交给通道**（organs/hand/actions.py
的 send_image）。这个模块只做那一段：**认一句话 → 抽一个名字 → 交出手去发**。它不是通用工具
框架，只认「看图」这一件事，形状照 remind.py：数据驱动、认不出回 None、绝不猜。

三段，次序钉死：

1. **意图**（intent.words）：句子里出现意图词表任一词，这一轮就算「要看图」。有没有指名另说
   ——**没指名就反问**（「给我看」），手一次都不调，也就一分额度都不烧。
2. **抽名字**（frame）：把意图词与请求词从这句话里削掉，再剥一次指示词（「你的」「那张」…），
   剩下就是「要看的那张」叫什么。**只剥一次**，削光了就是没指名。这里只判「有没有名字」，
   **不拼路径**：图在哪、叫什么，全由手从她原话里认（整句当 utterance 原样交出去）。
3. **交出手**（handle）：send_image(who=这一轮的号, scene=场合, target=同一个号, utterance=整句)。
   按手的回答分四支：
   · 认不出她说的是哪张（IMAGE_NOT_UNDERSTOOD）→ 说 say.unknown（「不认识」）；
   · 认得出、家里没有（IMAGE_NOT_FOUND）→ 说 say.missing（「没找着」），**不假装**；
   · 成（state=done）→ 把手的产物（detail.path → file://）落进回包那一格，由装配把图挂到
     最后一泡上（chunk.image）；她被允许补一句跟图有关的话（见 say）；
   · 其余失败（额度、通道没连上、场景／号不合规…）→ 说 say.fail，why **原样带手那边的码**。

**红线**：成功那一轮她的措辞里**不许出现「我画了／我拍的／我照的」这一类话**——图是家里的，
不是她现做的。禁止清单在 data/look.json 的 say.forbid：模型那句命中清单、或者干脆空着，就换成
数据里 say.ok 那一句口径。改数据即改判据（测试对着回包 reply 的文本 grep）。

**词表、指示词、五句人话、禁止清单全在 data/look.json**：代码里一个中文词都没有。读法同
assembly.json／remind.json：实盘 <仓库>/home/organs/mind/look.json 优先（存在才读），否则读出厂
organs/mind/data/look.json；数据缺键当场 ValueError 并点名缺的是哪一格（不在代码里补默认值）。

回包那一格（packet，六键冻结）：{intent, target, ok, why, name, image}——认出的是哪个意图词、
发给谁、成没成、为什么没成、要看的那张叫什么、图在哪儿（file://）。name 是**核削出来的那个名字**
（给人核对用），不保证与手从整句里认出的名字逐字相同：两边各读各的表，谁都不替谁改口。
**普通闲聊这一格根本不出现**（handle 回 None，调用方一个字都不改）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .. import paths
from ..hand import actions as hand_actions
from ..hand import channel as hand_channel

DATA_NAME = "look.json"

#: 回包那一格的六个键（冻结）：哪个意图词 / 发给谁 / 成没成 / 为什么没成 / 叫什么 / 图在哪。
PACKET_KEYS = ("intent", "target", "ok", "why", "name", "image")
#: why 的三档（这三档是本模块自己的口径，其余原样带手的码）。
WHY_NO_NAME = "no_name"                # 有意图、没指名——反问，手一次都不调
WHY_NOT_UNDERSTOOD = "not_understood"  # 手认不出她说的是哪张
WHY_NOT_FOUND = "not_found"            # 手认得出、家里没有这一张
#: 手那边的两个码（只在这里对一次，别处一律不认字符串）。
HAND_NOT_UNDERSTOOD = "IMAGE_NOT_UNDERSTOOD"
HAND_NOT_FOUND = "IMAGE_NOT_FOUND"
#: 通道对 image.summary 的上限（与 channels/onebot/outbox.ts 的口径一致）：名字再长也只送这么多。
SUMMARY_MAX = 80


# —— 数据 ——————————————————————————————————————


def load_config(root: Optional[Any] = None) -> Dict[str, Any]:
    """读词表与文案：显式 root 只读那一份；否则实盘覆盖（存在才读）→ 出厂数据。

    两份都没有是错，不是「用一套默认词表悄悄跑起来」——词表就是判据，没有默认值。
    """
    if root is not None:
        return _read(Path(root) / DATA_NAME)
    live = paths.organ_dir("mind") / DATA_NAME
    if live.is_file():
        return _read(live)
    factory = paths.factory_dir("mind") / DATA_NAME
    if factory.is_file():
        return _read(factory)
    raise ValueError("找不到看图的词表：%s 与 %s 都不存在" % (live, factory))


def _read(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("读不了看图的词表 %s：%s" % (path, exc)) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError("看图的词表不是 JSON：%s：%s" % (path, exc)) from exc
    return _check(data, str(path))


def _get(spec: Any, dotted: str, where: str) -> Any:
    """按点号取一格；缺了当场点名。数据里没有的规矩不许在代码里补。"""
    node = spec
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ValueError("look.json 缺 %s：%s" % (dotted, where))
        node = node[part]
    return node


def _text(spec: Any, dotted: str, where: str) -> str:
    """一格非空的话（文案与格式串）。空串等于没写，照样当场报。"""
    value = _get(spec, dotted, where)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("look.json 的 %s 要一句非空的话：%s" % (dotted, where))
    return value.strip()


def _words(spec: Any, dotted: str, where: str) -> List[str]:
    """一格词表：列表，每一项都得是非空词；空表等于没配。"""
    value = _get(spec, dotted, where)
    if not isinstance(value, list) or not value:
        raise ValueError("look.json 的 %s 要一份非空的词表：%s" % (dotted, where))
    words: List[str] = []
    for word in value:
        if not isinstance(word, str) or not word.strip():
            raise ValueError("look.json 的 %s 里有空词：%s" % (dotted, where))
        words.append(word.strip())
    return words


def _check(data: Any, where: str) -> Dict[str, Any]:
    """校验并归一化：缺节、缺键、类型不对一律当场报（名字写全，人才知道改哪一格）。"""
    if not isinstance(data, dict):
        raise ValueError("看图的词表不是一份对象：%s" % where)
    version = _get(data, "version", where)
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ValueError("look.json 的 version 要正整数：%s" % where)
    return {
        "version": version,
        "intent": {"words": _words(data, "intent.words", where)},
        "frame": {
            "strip": _words(data, "frame.strip", where),
            "owner": _words(data, "frame.owner", where),
            "trim": _text(data, "frame.trim", where),
        },
        "say": {"ask": _text(data, "say.ask", where),
                "unknown": _text(data, "say.unknown", where),
                "missing": _text(data, "say.missing", where),
                "fail": _text(data, "say.fail", where),
                "ok": _text(data, "say.ok", where),
                "forbid": _words(data, "say.forbid", where)},
    }


# —— 认一句话（纯函数：只判，不碰手）——————————————————


def plan(text: Any, *, config: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """一句话 → 这一轮要不要看图。**None 就是普通闲聊**：一个字都不动，手一次都不调。

    回来的是 {word, name}：word 是命中的那个意图词（回包那一格拿它当证据），name 是
    「要看的那张」叫什么——**空串就是没指名**（这一轮反问，不调手）。
    """
    cfg = config or load_config()
    said = str(text if text is not None else "").strip()
    if not said:
        return None
    word = _intent(said, cfg)
    if word is None:
        return None
    return {"word": word, "name": _name(said, cfg)}


def _intent(text: str, cfg: Mapping[str, Any]) -> Optional[str]:
    """命中哪个意图词（按长度从长到短试：长的框先试，短的才不会先抢走）。"""
    for word in sorted(cfg["intent"]["words"], key=len, reverse=True):
        if word in text:
            return word
    return None


def _name(text: str, cfg: Mapping[str, Any]) -> str:
    """把这句话削到只剩「要看的那张叫什么」；削光了就是没指名（**不返回替代名字**）。

    削两遍：先削意图词与请求词（「给我看」「把」「帮我」…），再剥**一次**指示词
    （「你的」「那张」…）。被削出来的空档补成空格再收一遍空白，中文挨着也不怕。
    """
    piece = text
    # 意图词也算要削的字：换了词表（「让我瞧一眼」）就自动跟着削，不必在两格里抄一遍。
    # **顺序要稳**（先数据里的次序，再按长度从长到短）：等长的框谁先谁后决定削出什么名字，
    # 而 set 的次序每跑一次都可能不同——那会让同一个句子两次抽到两个名字。
    cut = list(dict.fromkeys(list(cfg["frame"]["strip"]) + list(cfg["intent"]["words"])))
    for word in sorted(cut, key=len, reverse=True):
        piece = piece.replace(word, " ")
    piece = " ".join(piece.split())
    if not piece:
        return ""
    for owner in sorted(cfg["frame"]["owner"], key=len, reverse=True):
        if piece.startswith(owner):
            piece = piece[len(owner):]
            break
    return piece.strip(cfg["frame"]["trim"])


# —— 看图（会动手：手的 send_image）————————————————————


def handle(text: Any, *, who: str = "", scene: Any = None, now: Any = None,
           root: Optional[Any] = None,
           config: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """认出来就交给手发图。**普通闲聊回 None**（调用方据此一个字节都不动）。

    root 是**手自己的实盘根**（家按兄弟目录换算，与手那边同一条口径）。who 是这一轮的号：
    图发给**他**（target=who），不发给别人——场合另算，由 scene 带下去。

    没指名的那一轮**一次都不调手**：反问的成本是一句话，调手的成本是一份额度。
    """
    cfg = config or load_config()
    found = plan(text, config=cfg)
    if found is None:
        return None
    name = found["name"]
    read: Dict[str, Any] = {"intent": found["word"], "target": str(who), "ok": False,
                            "why": "", "name": name, "image": None, "say": "", "fixed": True}
    if not name:
        # 有意图没指名：反问，**手一次都不调**（不烧额度、也不落账）
        read["why"] = WHY_NO_NAME
        read["say"] = cfg["say"]["ask"]
        return read

    outcome = hand_actions.send_image(who=who, scene=scene, target=who,
                                      utterance=str(text if text is not None else "").strip(),
                                      now=now, root=root)
    state = str(outcome.get("state") or "")
    code = str(outcome.get("code") or "")
    detail = outcome.get("detail") or {}
    if state == "done":
        image = _image_uri(detail.get("path"))
        if image:
            # 只有手给了路径才算成：图没落到回包里，「发出去了」就是一句空话
            read.update({"ok": True, "why": "", "image": image, "say": ""})
            return read
        read["why"] = "IMAGE_PATH_MISSING"
        read["say"] = cfg["say"]["fail"]
        return read
    if code == HAND_NOT_UNDERSTOOD:
        read["why"] = WHY_NOT_UNDERSTOOD
        read["say"] = cfg["say"]["unknown"]
        return read
    if code == HAND_NOT_FOUND:
        read["why"] = WHY_NOT_FOUND
        read["say"] = cfg["say"]["missing"]
        return read
    # 其余失败（额度 / 通道 / 场景与号不合规…）：why 原样带手那边的码，不替它编
    read["why"] = code or "HAND_FAILED"
    read["say"] = cfg["say"]["fail"]
    return read


def _image_uri(path: Any) -> str:
    """手的产物（绝对路径）→ 通道认的 file:// 串；没给就是空串（宁可报失败，也不编一个）。"""
    text = "" if path is None else str(path).strip()
    return hand_channel.file_uri(text) if text else ""


def say(read: Dict[str, Any], spoken: Any = "",
        config: Optional[Mapping[str, Any]] = None) -> str:
    """这一轮她嘴里那句：数据里的固定话，或者她那句（但要过红线）。

    · 没成（没指名 / 不认识 / 没找着 / 没发出去）→ 说数据里那一句，模型那句不用：
      这一轮要说的话在动手那一刻就已经定了，再让她自由发挥就有了两种说法。
    · 成 → 模型那句照用（**允许她补一句跟图有关的话**），但它命中 say.forbid、或者干脆空着，
      就换成数据里 say.ok 那一句口径——图是家里的，不是她现做的。

    顺带把「这一轮嘴里那句是不是数据里的整句」记回 read["fixed"]：装配据此不锯固定话
    （「少说」那两把旋钮不许把一句回执锯成半句）。
    """
    cfg = config or load_config()
    if not read.get("ok"):
        read["fixed"] = True                     # 固定话：装配不许锯
        return str(read.get("say") or "")
    text = str(spoken if spoken is not None else "").strip()
    if not text or _banned(text, cfg):
        read["fixed"] = True
        return _render(cfg["say"]["ok"], {"name": read.get("name") or ""})
    read["fixed"] = False
    return text


def _banned(text: str, cfg: Mapping[str, Any]) -> bool:
    """措辞红线：命中 say.forbid 任一个词就不许说（清单在数据里，改数据即改判据）。"""
    return any(word in text for word in cfg["say"]["forbid"])


def with_image(chunks: List[Dict[str, Any]], read: Mapping[str, Any]
               ) -> List[Dict[str, Any]]:
    """图挂到**最后一泡**上（image.file 是手给的 file://，summary 是那张的名字）。

    没有图、或者这一轮一个字都没出口（chunks 空）就原样交回去：被闸弃掉的那一轮不补发图片，
    「她说了什么」与「图上没上」不许是两份不同的账。
    """
    image = str(read.get("image") or "")
    if not image or not chunks:
        return chunks
    out = [dict(one) for one in chunks]
    summary = str(read.get("name") or "")[:SUMMARY_MAX]
    out[-1]["image"] = {"file": image, "summary": summary}
    return out


def packet(read: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """回包那一格：只留冻结的六个键（say 是嘴里那句，已经进了 reply，不在这儿重复）。"""
    if not read:
        return {}
    return {key: read.get(key) for key in PACKET_KEYS}


def _render(template: str, fields: Mapping[str, Any]) -> str:
    """占位符逐个替换（不认 str.format：文案里本来就带着别的花括号）。"""
    text = str(template)
    for key, value in fields.items():
        text = text.replace("{%s}" % key, str(value))
    return text
