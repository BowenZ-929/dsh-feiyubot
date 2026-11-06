"""主题词表：**只扩查询，不动记忆**。

字面召回（organs/brain/recall.py）只认同一套写法：对方这次换了一种说法，记忆里存的是另一
种说法，两条话一个字都不重合，字面引擎就召不回（「换了说法就想不起来」正是它治的那个病）。
这一层不改记忆、也不许改记忆（记忆是她的证词，为了召回去改措辞等于替她改口），只在**这一句
查询**上补词：认出这句话落在哪个主题里，就把同一主题下的其它说法一并拼进查询串，交给脑的
recall 用。

三条硬界线：

1. **只加同一主题的词。** 不加「以前」「记得」这类泛词——泛词会把整本记忆都捞回来，
   那不是想起，是倒库。
2. **一句话一个主题都没命中，就原样查。** 不改一个字、不多一个词（不猜）。
3. **一条记忆都不写。** 这里只读词表（load_config 只读文件），不碰脑的任何一寸盘。

词表全在 data/themes.json：代码里一个中文词都没有，加词减词就改行为。读法同 remind.json：
实盘 <仓库>/home/organs/mind/themes.json 优先（存在才读），否则读出厂
organs/mind/data/themes.json。数据坏了当场 ValueError 并点名缺的是哪一格——不在代码里
补默认词表，补了人还以为改的是数据。

查询串的形状：`原句 + 同主题的其它词`，空格分开、去重、保序，总条数不超过数据里的
max_terms（原句占一条）。原句里已经有的词不再补一遍——补了只是把查询串拉长，字面引擎按
二元组重合率打分，拉长这一串等于把每一分都摊薄。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import paths

DATA_NAME = "themes.json"


# —— 数据 ——————————————————————————————————————


def load_config(root: Optional[Any] = None) -> Dict[str, Any]:
    """读主题词表：显式 root 只读那一份；否则实盘覆盖（存在才读）→ 出厂数据。

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
    raise ValueError("找不到主题词表：%s 与 %s 都不存在" % (live, factory))


def _read(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("读不了主题词表 %s：%s" % (path, exc)) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError("主题词表不是 JSON：%s：%s" % (path, exc)) from exc
    return _check(data, str(path))


def _get(spec: Any, dotted: str, where: str) -> Any:
    """按点号取一格；缺了当场点名。数据里没有的规矩不许在代码里补。"""
    node = spec
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ValueError("themes.json 缺 %s：%s" % (dotted, where))
        node = node[part]
    return node


def _words(value: Any, where: str) -> List[str]:
    """一份词表：非空列表，每一项都得是非空词；空表等于没配，照样报。"""
    if not isinstance(value, list) or not value:
        raise ValueError("themes.json 的 %s 要一份非空的词表：%s" % (where, where))
    words: List[str] = []
    for word in value:
        if not isinstance(word, str) or not word.strip():
            raise ValueError("themes.json 的 %s 里有空词：%s" % (where, where))
        words.append(word.strip())
    return words


def _check(data: Any, where: str) -> Dict[str, Any]:
    """校验并归一化：缺节、缺键、类型不对、主题重名一律当场报（名字写全，人才知道改哪一格）。"""
    if not isinstance(data, dict):
        raise ValueError("主题词表不是一份对象：%s" % where)
    version = _get(data, "version", where)
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ValueError("themes.json 的 version 要正整数：%s" % where)
    max_terms = _get(data, "max_terms", where)
    if isinstance(max_terms, bool) or not isinstance(max_terms, int) or max_terms <= 0:
        raise ValueError("themes.json 的 max_terms 要正整数：%s" % where)
    raw = _get(data, "themes", where)
    if not isinstance(raw, list) or not raw:
        raise ValueError("themes.json 的 themes 要一份非空的主题表：%s" % where)
    themes: List[Dict[str, Any]] = []
    taken = set()
    for index, theme in enumerate(raw):
        at = "themes[%d]" % index
        if not isinstance(theme, dict):
            raise ValueError("themes.json 的 %s 要一份 {name, words}：%s" % (at, where))
        if "name" not in theme:
            raise ValueError("themes.json 缺 %s.name：%s" % (at, where))
        name = theme["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("themes.json 的 %s.name 要一个非空的主题名：%s" % (at, where))
        name = name.strip()
        if name in taken:
            raise ValueError("themes.json 的 themes 里主题名重复：%s（%s）" % (name, where))
        taken.add(name)
        if "words" not in theme:
            raise ValueError("themes.json 缺 %s.words：%s" % (at, where))
        themes.append({"name": name, "words": _words(theme["words"], at + ".words")})
    return {"version": version, "max_terms": max_terms, "themes": themes}


# —— 查询扩展 ————————————————————————————————————


def _squash(text: str) -> str:
    """压掉所有空白：脑的字面引擎也先压空白再比，这一层照同一副尺子量「这个词在不在句里」。"""
    return "".join((text or "").split())


def hits(text: str, conf: Optional[Dict[str, Any]] = None,
         root: Optional[Any] = None) -> List[Dict[str, Any]]:
    """这句话落在哪几个主题里（按数据里的顺序，命中一个词就算命中这一条主题）。"""
    conf = conf or load_config(root)
    squashed = _squash(text)
    if not squashed:
        return []
    return [theme for theme in conf["themes"]
            if any(_squash(word) in squashed for word in theme["words"])]


def expand(text: str, conf: Optional[Dict[str, Any]] = None,
           root: Optional[Any] = None) -> str:
    """原句 → 查询串：原句 + 命中主题里的其它词（去重、保序、不超过 max_terms 条）。

    一个主题都没命中（空句、全是标点、闲话）就原样返回**入参那一条**：不改写、不裁剪、
    不加词——「原样查」是这一层的兜底，不是近似原样。
    """
    said = (text or "")
    conf = conf or load_config(root)
    matched = hits(said, conf)
    if not matched:
        return text
    squashed = _squash(said)
    terms: List[str] = [said.strip()]
    seen = set()
    for theme in matched:
        for word in theme["words"]:
            key = _squash(word)
            if not key or key in seen:
                continue
            seen.add(key)
            if key in squashed:      # 原句里已经有的词不再补一遍（补了只是把查询串拉长）
                continue
            if len(terms) >= conf["max_terms"]:
                break
            terms.append(word)
        if len(terms) >= conf["max_terms"]:
            break
    if len(terms) == 1:              # 命中了主题、却没有一个「其它词」可补：还是原样
        return text
    return " ".join(terms)
