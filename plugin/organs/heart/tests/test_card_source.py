"""六轴卡真源只有一个——实盘（store.default_root()/baseline-card.json）。

出厂那份 `card.SEED_CARD_PATH` 只是 seed 料：只给新机器播种与测试用，改它不影响任何一次
真实读取。三条断言照家的 `organs/home/tests/test_anchors.py` 那套（`read_seed()` vs
`read_all()` + 「真源只有一个：实盘」）：① 缺省读取经实盘根；② 改实盘读跟着变；
③ 改出厂那份读不变。另加一条边界：实盘没有卡时回退 seed 并 WARN 一行。

改出厂那份/实盘那张都必须**字节级还原**——seed 就在代码树里，脏了会连累所有人。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import warnings
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from organs.heart import card, model, store
from organs.heart.tests import helpers

#: 实盘那张的「可区分值」：出厂 seed 里没有这组数，读到哪组数就是读了哪份的证据。
LIVE_PAIRS: Dict[str, Tuple[int, int]] = {
    "服气": (11, 13),
    "心疼": (22, 24),
    "想黏人": (33, 35),
    "怕失去": (44, 46),
}


def card_data(overrides: Optional[Dict[str, Tuple[int, int]]] = None) -> Dict[str, Any]:
    """一张合规的六轴卡，成对轴的值可逐个覆盖。"""
    pairs = dict(LIVE_PAIRS)
    pairs.update(overrides or {})
    axes = []
    for axis_id in model.AXES:
        if axis_id in model.PENDING_AXES:
            axes.append({"id": axis_id, "pending": True})
        else:
            inner, shown = pairs[axis_id]
            axes.append({"id": axis_id, "inner": inner, "shown": shown})
    return {"scale": {"min": 0, "max": 100}, "axes": axes}


def sha256_of(path: Any) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CardSourceTest(unittest.TestCase):
    """每条用例自带实盘根（HEART_ROOT 指到临时目录），不依赖本机 home/organs/heart。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = helpers.make_root(self.base)
        self.live = self.root / card.CARD_FILE
        self.live.write_text(json.dumps(card_data(), ensure_ascii=False, indent=2),
                             encoding="utf-8")
        self._old_env: Optional[str] = os.environ.get(store.ENV_ROOT)
        os.environ[store.ENV_ROOT] = str(self.root)
        self.addCleanup(self.restore_env)

    def restore_env(self) -> None:
        """复原 HEART_ROOT：用例怎么改的，就怎么还回去。"""
        if self._old_env is None:
            os.environ.pop(store.ENV_ROOT, None)
        else:
            os.environ[store.ENV_ROOT] = self._old_env

    # —— 真源只有一个：实盘 ——————————————————————————————

    def test_default_read_goes_through_the_live_root(self) -> None:
        """① 缺省读取经实盘根：读到的就是临时根里那张，不是出厂 seed。"""
        self.assertEqual(self.root / card.CARD_FILE, card.live_card_path(),
                         "live_card_path() 必须由 store.default_root() 解析出来")
        loaded = card.load_card()
        self.assertEqual(LIVE_PAIRS["服气"], (loaded.axes["服气"].inner, loaded.axes["服气"].shown),
                         "读到的必须是实盘那张的可区分值")
        seed = card.load_card(card.SEED_CARD_PATH)
        self.assertNotEqual((seed.axes["服气"].inner, seed.axes["服气"].shown),
                            LIVE_PAIRS["服气"], "出厂那份没有这组数——两份确实不是同一张")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            card.load_card()          # 实盘在就不许 WARN：回退只是边界，不是常态

    def test_live_copy_is_the_source_of_truth(self) -> None:
        """② 改实盘 → 读跟着变（档位也变）；出厂那份一个字节都不许被碰。"""
        original = self.live.read_bytes()
        seed_before = card.SEED_CARD_PATH.read_bytes()
        try:
            self.assertEqual(1, card.tier_of(card.load_card().axes["服气"].inner, 0, 100))
            self.live.write_text(json.dumps(card_data({"服气": (77, 79)}), ensure_ascii=False,
                                            indent=2), encoding="utf-8")
            changed = card.load_card()
            self.assertEqual((77, 79), (changed.axes["服气"].inner, changed.axes["服气"].shown),
                             "改了实盘，读出来的必须跟着变")
            self.assertEqual(4, card.tier_of(changed.axes["服气"].inner, 0, 100),
                             "换的值得换成另一个档位，肉眼能看出来")
        finally:
            self.live.write_bytes(original)
            self.restore_env()
        self.assertEqual(original, self.live.read_bytes(), "验完必须把实盘那张还原成原样")
        self.assertEqual(seed_before, card.SEED_CARD_PATH.read_bytes(), "改实盘不许改到出厂那份")

    def test_seed_corruption_does_not_affect_reads(self) -> None:
        """③ 改出厂那份 → 读实盘完全不受影响；这是「真源只有一个」的机器证明。"""
        seed = card.SEED_CARD_PATH
        if not seed.exists():
            self.skipTest("这台机器上没有出厂 seed 那份")
        before_sha = sha256_of(seed)
        original = seed.read_bytes()
        try:
            self.assertEqual(LIVE_PAIRS["服气"], (card.load_card().axes["服气"].inner,
                                                  card.load_card().axes["服气"].shown))
            seed.write_text(json.dumps(card_data({"服气": (99, 99)}), ensure_ascii=False,
                                      indent=2), encoding="utf-8")
            after = card.load_card()
            self.assertEqual(LIVE_PAIRS["服气"], (after.axes["服气"].inner, after.axes["服气"].shown),
                             "改出厂那份，读实盘一点不受影响")
            seed.write_text('{"scale": ', encoding="utf-8")
            still = card.load_card()          # seed 坏到读不了也不许影响实盘
            self.assertEqual(LIVE_PAIRS["服气"], (still.axes["服气"].inner, still.axes["服气"].shown))
        finally:
            seed.write_bytes(original)
            self.restore_env()
        self.assertEqual(before_sha, sha256_of(seed), "验完必须把出厂那份字节级还原")

    def test_missing_live_card_falls_back_to_seed_with_one_warning(self) -> None:
        """边界：实盘没有卡 → 回退 seed，WARN 一行，且绝不替她播种。"""
        empty = helpers.make_root(self.base / "empty")
        os.environ[store.ENV_ROOT] = str(empty)
        try:
            self.assertFalse(card.live_card_path().exists())
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                loaded = card.load_card()
            self.assertEqual(1, len(caught),
                             "回退只许 WARN 一行：%r" % [str(w.message) for w in caught])
            message = str(caught[0].message)
            self.assertIn("实盘没有卡", message)
            self.assertIn("seed", message)
            self.assertIn("播种", message)
            seed = card.load_card(card.SEED_CARD_PATH)
            self.assertEqual((seed.axes["服气"].inner, seed.axes["服气"].shown),
                             (loaded.axes["服气"].inner, loaded.axes["服气"].shown),
                             "回退读到的就是出厂 seed 那份")
            self.assertFalse(card.live_card_path().exists(), "回退是只读的，不许替她播种")
        finally:
            self.restore_env()

    def test_default_card_path_alias_is_gone(self) -> None:
        """旧名不许留成别名：两个名字一个东西，正是这个仓要杀的。"""
        # 旧名是拼出来再查的：这条断言自己也不许被「旧名 grep」命中。
        self.assertFalse(hasattr(card, "DEFAULT_" + "CARD_PATH"))
        self.assertTrue(hasattr(card, "SEED_CARD_PATH") and hasattr(card, "live_card_path"))


if __name__ == "__main__":
    unittest.main()
