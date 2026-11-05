"""第八轮：迁移来源 + 她自己的话不进"关于他"的召回。"""
from __future__ import annotations

import unittest

from organs.brain.model import SIDE_SELF, SOURCE_MIGRATED
from organs.brain.store import BrainReadError
from organs.brain.tests.helpers import fresh_store, ingest


class MigrationSourceTest(unittest.TestCase):
    def test_migrated_quote_marks_old_home_and_is_not_testimony(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        store.import_rows([{"id": "1000201", "who": "主人",
                            "ts": "2026-11-02T10:00:00+00:00",
                            "text": "旧家摘要：他喜欢猫", "source_type": "迁移",
                            "source": "today"}])
        item = store.recall("喜欢猫", now_ts="2026-11-03T10:05:00+00:00")["items"][0]
        self.assertIn("（旧家记录）", item["display_text"])
        self.assertFalse(item["testimony"], "迁移来的旧家摘要不能当证词")

    def test_migrated_empty_quote_marks_no_original(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        store.import_rows([{"id": "1000202", "who": "主人",
                            "ts": "2026-11-02T10:00:00+00:00",
                            "quote": "", "source_type": "迁移", "source": "today"}])
        rows = store.longterm_active_entries()
        self.assertEqual("迁移", rows[0]["source_type"])
        self.assertEqual("", rows[0]["quote"])
        # 泛回忆问句走"最近在役"兜底，能把它带出来并标清无原话
        result = store.recall("你上次说过什么", now_ts="2026-11-03T10:05:00+00:00")
        migrated = [item for item in result["items"]
                    if item["source_type"] == "迁移"]
        self.assertTrue(migrated)
        self.assertIn("（无原话，来自旧家记录）", migrated[0]["display_text"])

    def test_unknown_source_type_errors_not_silently_lived(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(BrainReadError):
            store.import_rows([{"id": "1000203", "who": "主人",
                                "ts": "2026-11-02T10:00:00+00:00",
                                "text": "未知来源", "source_type": "外星",
                                "source": "today"}])


class SelfSideTest(unittest.TestCase):
    def test_her_replies_never_recalled_as_his_memory(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        # 第一轮：他问，她答
        ingest(store, "小明", "你昨天去哪了", "2026-11-03T10:00:00+00:00")
        ingest(store, "她说", "我昨天去了医院", "2026-11-03T10:01:00+00:00",
               side=SIDE_SELF)
        # 第二三轮：连问同一件事，recalled 里不许出现她的回话
        for i in (2, 3):
            result = store.recall("去哪了",
                                  now_ts="2026-11-03T10:%02d:00+00:00" % i)
            texts = [item["full_text"] for item in result["items"]]
            self.assertNotIn("我昨天去了医院", texts,
                             "第 %d 轮 recalled 里出现了她自己的回话：%r" % (i, texts))
            self.assertIn("你昨天去哪了", texts,
                          "第 %d 轮该召回的是他说的原话" % i)


if __name__ == "__main__":
    unittest.main()
