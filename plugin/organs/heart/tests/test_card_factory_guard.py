"""出厂卡守卫：实盘是真源、出厂是 seed；**结构丢了就是被削**，数值不同是正常。

六轴完整（2026-11-06 人定稿）之后的新状态：`_handoff.pending` 是空数组、六轴都带数。

只比结构，不比数：
- `_handoff` **逐字深比较相等**（两张卡记的是同一件事，一句话写差就是描述与实现不一致）；
  另外把 `_handoff.pending` 单独拎出来钉一次：**必须是空数组**——这是「无 pending 轴」的
  机器证据，不是「没有这一栏」也不是还留着一两个轴名；
- `status` 相等、`scale` 相等；
- `axes` 的 id 顺序相同；六轴都含 label/inner/shown，且**都不许带 pending**；
- inner/shown 的**数值允许不同**：实盘是人调过的，与 seed 不一样是常态。

出厂卡只读：所有「把它改坏」的用例都改临时拷贝（`card.SEED_CARD_PATH` 被 patch 指过去），
用例收尾按 sha 复核仓里那份没被动过。
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from organs.heart import card

#: 仓里那张出厂卡的**真实**位置：写死成模块常量，任何 patch 都改不到它，
#: 只用来拷副本、以及在用例收尾时复核「它没被写过」。
FACTORY_CARD_PATH = Path(__file__).resolve().parents[1] / "data" / card.CARD_FILE

#: 每根轴该有的键（只查存在性，不查值）——六轴一视同仁，不再分「成对 / pending」两类。
REQUIRED_KEYS = ("label", "inner", "shown")



def sha256_of(path: Any) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_json(path: Any) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AssertionError("卡顶层不是 dict：%s" % path)
    return data


def structure_problems(seed: Dict[str, Any], live: Dict[str, Any]) -> List[str]:
    """列出厂卡相对实盘的**结构**差异；空列表 = 结构一致（数值不同不算问题）。"""
    problems: List[str] = []
    problems.extend(_handoff_problems(seed.get("_handoff"), live.get("_handoff")))
    if seed.get("status") != live.get("status"):
        problems.append("status 不一致：出厂是 %r，实盘是 %r"
                        % (seed.get("status"), live.get("status")))
    if seed.get("scale") != live.get("scale"):
        problems.append("scale 不一致：出厂是 %r，实盘是 %r"
                        % (seed.get("scale"), live.get("scale")))

    seed_axes = seed.get("axes") or []
    live_axes = live.get("axes") or []
    seed_ids = [entry.get("id") for entry in seed_axes if isinstance(entry, dict)]
    live_ids = [entry.get("id") for entry in live_axes if isinstance(entry, dict)]
    if seed_ids != live_ids:
        problems.append("axes 的 id 顺序不一致：出厂 %r / 实盘 %r" % (seed_ids, live_ids))

    for entry, live_entry in zip(seed_axes, live_axes):
        if not isinstance(entry, dict) or not isinstance(live_entry, dict):
            problems.append("axes 里有一项不是 dict")
            continue
        axis_id = entry.get("id")
        seed_keys, live_keys = set(entry), set(live_entry)
        missing, extra = sorted(live_keys - seed_keys), sorted(seed_keys - live_keys)
        if missing or extra:
            problems.append("轴 %r 的键不一致：出厂少 %r、多 %r（实盘键 %r）"
                            % (axis_id, missing, extra, sorted(live_keys)))
        for key in REQUIRED_KEYS:
            if key not in entry:
                problems.append("轴 %r 少了 %r —— 结构被削" % (axis_id, key))
        if "pending" in entry or "pending" in live_entry:
            problems.append("轴 %r 带了 pending：六轴完整之后不许再有 pending 轴" % axis_id)
    return problems


def _handoff_problems(seed: Any, live: Any) -> List[str]:
    """`_handoff`：逐字深比较相等；`pending` 另钉一次空数组。

    不给「人写注记」开口子——两张卡记的是同一件事，verdict 写差一个字就该红。
    pending 单拎出来是因为它是**这次定稿的关键状态**：得是空数组，
    不是「没有这一栏」也不是还留着一两个轴名。
    """
    problems: List[str] = []
    if seed != live:
        problems.append("_handoff 不一致：出厂是 %r，实盘是 %r" % (seed, live))
    for side, data in (("出厂", seed), ("实盘", live)):
        pending = data.get("pending") if isinstance(data, dict) else None
        if not isinstance(pending, list) or pending:
            problems.append("%s的 _handoff.pending 不是空数组：%r" % (side, pending))
    return problems


class FactoryCardGuardTest(unittest.TestCase):
    """出厂 seed 与实盘卡结构一致；数值可不同（实盘是人调过的）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self._factory_sha = sha256_of(FACTORY_CARD_PATH)
        self.addCleanup(self._assert_factory_card_untouched)

    def _assert_factory_card_untouched(self) -> None:
        """收尾复核：测试结束出厂卡 sha 不变。"""
        self.assertEqual(self._factory_sha, sha256_of(FACTORY_CARD_PATH),
                         "测试结束出厂卡 sha 必须不变：它是 seed 料、有版本，测试不许写它")

    def _sandbox_seed(self) -> Path:
        """把仓里的出厂卡拷一份到临时目录，返回拷贝路径（要改就改这份）。"""
        copy = self.base / "seed" / card.CARD_FILE
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(FACTORY_CARD_PATH.read_bytes())
        return copy

    def _point_seed_at(self, path: Path) -> None:
        """把 card.SEED_CARD_PATH 指到拷贝上：守卫读 seed 只走这个常量。"""
        patcher = mock.patch.object(card, "SEED_CARD_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _live_data(self) -> Dict[str, Any]:
        live = card.live_card_path()
        if not live.exists():
            self.skipTest("这台机器上没有实盘卡：%s" % live)
        return _read_json(live)

    def _assert_factory_matches_live(self) -> None:
        """守卫本体：出厂卡（card.SEED_CARD_PATH）与实盘卡结构必须一致。"""
        seed = _read_json(card.SEED_CARD_PATH)
        live = self._live_data()
        self.assertEqual([], structure_problems(seed, live),
                         "出厂卡与实盘卡结构必须一致（结构丢了就是被削）：%s"
                         % structure_problems(seed, live))

    def _rewrite(self, path: Path, mutate: Any) -> None:
        data = _read_json(path)
        mutate(data)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _axis(self, data: Dict[str, Any], axis_id: str) -> Dict[str, Any]:
        for entry in data["axes"]:
            if entry.get("id") == axis_id:
                return entry
        raise AssertionError("卡里没有这根轴：%s" % axis_id)

    # —— 一致 / 只管结构 ————————————————————————————————

    def test_factory_and_live_agree_on_structure(self) -> None:
        """出厂与实盘：_handoff / status / scale 相等，轴序与每轴该有的键一致。"""
        self._assert_factory_matches_live()

    def test_guard_allows_different_numbers(self) -> None:
        """数值不同不算问题：实盘是人调过的，守卫只管结构。"""
        copy = self._sandbox_seed()
        self._point_seed_at(copy)

        def mutate(data: Dict[str, Any]) -> None:
            self._axis(data, "服气")["inner"], self._axis(data, "服气")["shown"] = 3, 5
            self._axis(data, "心疼")["inner"], self._axis(data, "心疼")["shown"] = 61, 62

        self._rewrite(copy, mutate)
        seed = _read_json(card.SEED_CARD_PATH)
        self.assertEqual([], structure_problems(seed, self._live_data()),
                         "只改数不该被判成结构问题")
        self._assert_factory_matches_live()

    # —— 该红的必须红 ————————————————————————————————

    def test_guard_catches_missing_handoff(self) -> None:
        """删掉 _handoff → 守卫必须红。"""
        copy = self._sandbox_seed()
        self._point_seed_at(copy)
        self._rewrite(copy, lambda data: data.pop("_handoff"))
        with self.assertRaises(AssertionError) as caught:
            self._assert_factory_matches_live()
        message = str(caught.exception)
        print("\n守卫红的那次（缺 _handoff）：%s" % message.splitlines()[0])
        self.assertIn("_handoff", message)

    def test_guard_catches_missing_label(self) -> None:
        """删 label → 守卫必须红（六轴各有 label/inner/shown，少一个就是被削）。"""
        copy = self._sandbox_seed()
        self._point_seed_at(copy)
        self._rewrite(copy, lambda data: self._axis(data, "服气").pop("label"))
        with self.assertRaises(AssertionError) as caught:
            self._assert_factory_matches_live()
        message = str(caught.exception)
        print("\n守卫红的那次（缺 label）：%s" % message.splitlines()[0])
        self.assertIn("label", message)

    def test_guard_catches_pending_list_not_empty(self) -> None:
        """`_handoff.pending` 从空数组改回 ["耐心"] → 守卫必须红。

        这条证明守卫**真的在认「零 pending」这个状态**，不是只要有这一栏就放行。
        """
        copy = self._sandbox_seed()
        self._point_seed_at(copy)

        def put_back_pending(data: Dict[str, Any]) -> None:
            data["_handoff"]["pending"] = ["耐心"]

        self._rewrite(copy, put_back_pending)
        with self.assertRaises(AssertionError) as caught:
            self._assert_factory_matches_live()
        message = str(caught.exception)
        print("\n守卫红的那次（pending 非空）：%s" % message.splitlines()[0])
        self.assertIn("pending", message)

    def test_guard_catches_verdict_typo(self) -> None:
        """`_handoff.verdict` 改一个字 → 守卫必须红（深比较真的在比字）。"""
        copy = self._sandbox_seed()
        self._point_seed_at(copy)

        def typo(data: Dict[str, Any]) -> None:
            data["_handoff"]["verdict"] = data["_handoff"]["verdict"].replace("完整", "完备")

        self._rewrite(copy, typo)
        with self.assertRaises(AssertionError) as caught:
            self._assert_factory_matches_live()
        message = str(caught.exception)
        print("\n守卫红的那次（verdict 改一个字）：%s" % message.splitlines()[0])
        self.assertIn("_handoff", message)

    def test_guard_catches_axis_pending_flag(self) -> None:
        """把补齐的耐心标回 pending: true → 守卫必须红（六轴不许有 pending 轴）。"""
        copy = self._sandbox_seed()
        self._point_seed_at(copy)

        def mark_pending(data: Dict[str, Any]) -> None:
            self._axis(data, "耐心")["pending"] = True

        self._rewrite(copy, mark_pending)
        with self.assertRaises(AssertionError) as caught:
            self._assert_factory_matches_live()
        message = str(caught.exception)
        print("\n守卫红的那次（轴标 pending）：%s" % message.splitlines()[0])
        self.assertIn("pending", message)


if __name__ == "__main__":
    unittest.main()
