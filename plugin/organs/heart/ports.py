"""心的只读端口：心只许从脑读两个数——某人的熟稔、今天与某人说了几条。

边界（硬规则，写在这里就是给自己看的）：
- 本模块**不 import 脑的任何模块**（organs.brain 从不进本模块的 import 表），
  不碰脑的存储路径、文件名、内部 dict。唯一允许的两条通道是脑的公开读数：
  people_list(now) 与 today_view(now)；鸭子类型适配，脑换实现、换目录都不影响这边。
- 熟稔 21 天那把尺**在脑，心不持有、不重算、不抄常数**。这里只搬运脑算好的
  familiarity 字段，以及今日条目里属于某人的条数——一个系数都不乘。
- 本模块自己也不持有任何衰减半衰：读数的时间效应全由脑在读的那一刻现算，
  本模块只把 now 透传下去（None → model.now_iso()），不挂定时器、不缓存。
- 读数缺失不许把心炸掉：brain=None 返回 0.0 / 0 的实现并 WARN 一行，
  脑某次读数抛异常也一样降级成 0——判 engage 时少两个数，不该让整个人停下。
- 共用词表是唯一的例外，钉在明处：拒绝记录（refuse.py）要用话题名，话题名取自
  <实盘>/organs/brain/topics.json 的顶层键——单独一个受限加载器只读这一个文件、
  只取顶层键，见 refuse.vocabulary。**这是共用词表，不是脑的记忆；除此之外心仍
  不碰脑存储**：不给它开第二条通路，也不许拿它当脑的读数用。
"""
from __future__ import annotations

import warnings
from typing import Any, Dict, List, Optional, Set

from organs.heart import model


def _now(now: Optional[str]) -> str:
    """now 给了就用，没给就取此刻（与 model.now_iso 同一个口径）。"""
    return now or model.now_iso()


class BrainReadout(object):
    """心对脑的全部视野：只有两个读法，多一个都不许加。

    familiarity(who, now) -> float   某人的熟稔（脑算好的，21 天那把尺在脑）
    today_count(who, now) -> int     今天与某人说了几条（脑按条目数的）
    """

    def familiarity(self, who: str, now: Optional[str] = None) -> float:
        """某人的熟稔读数；具体实现给。"""
        raise NotImplementedError("BrainReadout 是端口：读法由实现类给")

    def today_count(self, who: str, now: Optional[str] = None) -> int:
        """今天与某人说了几条；具体实现给。"""
        raise NotImplementedError("BrainReadout 是端口：读法由实现类给")


class StaticReadout(BrainReadout):
    """写死的读数：测试与干跑用，不连脑、不读盘。"""

    def __init__(self, familiarity: float = 0.0, today_count: int = 0) -> None:
        self._familiarity = float(familiarity)
        self._today_count = int(today_count)

    def familiarity(self, who: str, now: Optional[str] = None) -> float:
        """不管是谁、不管几点，都给构造时那个数。"""
        return self._familiarity

    def today_count(self, who: str, now: Optional[str] = None) -> int:
        """不管是谁、不管几点，都给构造时那个数。"""
        return self._today_count

    def __repr__(self) -> str:
        return "StaticReadout(familiarity=%r, today_count=%r)" % (
            self._familiarity, self._today_count)


class _MissingReadout(BrainReadout):
    """读数缺失时的实现：两个数都是 0，问几次答几次 0，不抛。"""

    def __init__(self, note: str = "") -> None:
        self._note = note or "读数缺失"

    def familiarity(self, who: str, now: Optional[str] = None) -> float:
        return 0.0

    def today_count(self, who: str, now: Optional[str] = None) -> int:
        return 0

    def __repr__(self) -> str:
        return "MissingReadout(%s)" % self._note


class _DuckReadout(BrainReadout):
    """鸭子类型适配：只调 people_list / today_view 两个公开读数，别的一概不碰。"""

    def __init__(self, brain: Any) -> None:
        self._brain = brain
        self._warned = False

    # —— 只此两条通道 ——

    def _people(self, now: Optional[str]) -> List[Dict[str, Any]]:
        try:
            people = self._brain.people_list(_now(now))
        except Exception as exc:  # 脑坏了也不许把心带走
            self._warn("people_list", exc)
            return []
        return [p for p in (people or []) if isinstance(p, dict)]

    def _entries(self, now: Optional[str]) -> List[Dict[str, Any]]:
        try:
            view = self._brain.today_view(_now(now))
        except Exception as exc:
            self._warn("today_view", exc)
            return []
        if not isinstance(view, dict):
            return []
        return [e for e in (view.get("entries") or []) if isinstance(e, dict)]

    def _warn(self, what: str, exc: Exception) -> None:
        """同一次失败只 WARN 一行，免得每个判决刷一遍日志。"""
        if self._warned:
            return
        self._warned = True
        warnings.warn(
            "心·ports：读脑 %s 失败（%s：%s），该读数按 0 处理"
            % (what, type(exc).__name__, exc),
            RuntimeWarning, stacklevel=3)

    def _matches(self, person: Dict[str, Any], who: str) -> bool:
        """这条人名录是不是 who：按 id / display / 别名认人。"""
        if not who:
            return False
        if person.get("id") == who or person.get("display") == who:
            return True
        aliases = person.get("aliases") or []
        return who in aliases

    def _names_for(self, who: str, now: Optional[str]) -> Set[str]:
        """who 可能在人名录里的所有写法；条目里存的是 id，认人得宽松点。"""
        names: Set[str] = set()
        if who:
            names.add(who)
        for person in self._people(now):
            if not self._matches(person, who):
                continue
            for key in ("id", "display"):
                value = person.get(key)
                if isinstance(value, str) and value:
                    names.add(value)
            for alias in person.get("aliases") or []:
                if isinstance(alias, str) and alias:
                    names.add(alias)
        return names

    # —— 两个读数 ——

    def familiarity(self, who: str, now: Optional[str] = None) -> float:
        """在人名录里找这个人的 familiarity 字段；找不到就 0.0。"""
        for person in self._people(now):
            if not self._matches(person, who):
                continue
            try:
                return float(person.get("familiarity") or 0.0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def today_count(self, who: str, now: Optional[str] = None) -> int:
        """数今日条目里属于这个人的条数；不读任何文件、不自己分组。"""
        names = self._names_for(who, now)
        entries = self._entries(now)
        count = 0
        for entry in entries:
            if entry.get("who") in names:
                count += 1
        return count

    def __repr__(self) -> str:
        return "DuckReadout(%r)" % (type(self._brain).__name__,)


def from_brain(brain: Any) -> BrainReadout:
    """任何形状的脑 → BrainReadout 端口；None 给缺失读数，绝不抛。

    传进来的对象只需有 people_list(now) 与 today_view(now) 两个方法；
    没有 / 抛异常都不在这里预检，等真读的那一刻降级成 0 并 WARN。
    """
    if brain is None:
        warnings.warn(
            "心·ports：没有给脑（brain=None），熟稔与今日条数按 0 处理——"
            "心照常判，只是少了两个数",
            RuntimeWarning, stacklevel=2)
        return _MissingReadout("brain=None")
    return _DuckReadout(brain)
