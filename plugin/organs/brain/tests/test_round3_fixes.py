"""第三轮收尾修复：无到期时读路径零写盘；话题候选池进出。"""
from __future__ import annotations

import os
import unittest

from organs.brain.tests.helpers import fresh_store, ingest


def _mtimes(store) -> dict:
    return {name: os.stat(store.root / name).st_mtime_ns
            for name in os.listdir(store.root) if (store.root / name).is_file()}


class ZeroWriteTest(unittest.TestCase):
    def test_ten_recalls_with_no_expiry_write_nothing(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        # 今日条目，词表能盖住（医院），不会进候选池；未换日、未超 60、无到期
        ingest(store, "3425635473", "我妈住院了", "2026-11-03T10:00:00+00:00")
        before = _mtimes(store)
        for _ in range(10):
            store.recall("我妈", now_ts="2026-11-03T10:05:00+00:00")
        after = _mtimes(store)
        changed = [name for name in before if before[name] != after.get(name)]
        self.assertEqual([], changed, "10 次 recall 且无到期：一次盘都不写，实际动了 %s" % changed)


class TopicCandidateTest(unittest.TestCase):
    def test_recalled_unknown_text_enters_pool_and_promotes_into_table(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        store.import_rows([{"id": "1000101", "who": "3425635473",
                            "ts": "2026-11-02T10:00:00+00:00",
                            "text": "我今天去了一趟新开的书店", "source": "today"}])
        store.recall("书店", now_ts="2026-11-03T12:00:00+00:00")
        candidates = store.topic_candidates()
        self.assertEqual(1, len(candidates))
        self.assertIn("书店", candidates[0]["text"])
        # 人把候选收进词表
        promoted = store.promote_topic_candidate("书店", candidates[0]["text"])
        self.assertTrue(promoted["ok"])
        self.assertEqual([], store.topic_candidates())
        # 收进词表后，新消息能提取出这个话题
        result = ingest(store, "3425635473", "我今天去了一趟新开的书店",
                        "2026-11-03T12:05:00+00:00")
        topics = store._topics()
        self.assertIn("书店", topics)
        self.assertIn("我今天去了一趟新开的书店", topics["书店"])


if __name__ == "__main__":
    unittest.main()
