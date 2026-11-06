"""第十轮：scene/audience 只用于出口判断，不参与召回。"""
from __future__ import annotations

import unittest

from organs.brain.tests.helpers import fresh_store, ingest


class SceneAudienceTest(unittest.TestCase):
    def test_group_memory_answers_private_question(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "123456789", "那家店明天打八折", "2026-11-03T10:00:00+00:00",
               scene="group")
        result = store.recall("那家店", now_ts="2026-11-03T10:05:00+00:00",
                              scene="private")
        self.assertTrue(result["has_material"], "共享记忆是判据：群里说的私聊必须答得上")

    def test_private_audience_not_retold_in_group_but_still_recalled(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "123456789", "我妈住院了，别到处说", "2026-11-03T10:00:00+00:00",
               scene="private", audience="私密")
        result = store.recall("我妈", now_ts="2026-11-03T10:05:00+00:00",
                              scene="group")
        self.assertTrue(result["has_material"], "召回不过滤 scene：条目还在")
        item = result["items"][0]
        self.assertFalse(item["retellable"], "群里不当众复述私密")
        self.assertIn("不当众复述", item["retell_note"])

    def test_same_who_across_scenes_is_one_card(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        ingest(store, "123456789", "群里说一句", "2026-11-03T10:00:00+00:00", scene="group")
        ingest(store, "123456789", "私聊说一句", "2026-11-03T10:02:00+00:00", scene="private")
        people = store.people_list("2026-11-03T10:05:00+00:00")
        self.assertEqual(1, len(people), "跨群/私聊是同一个 who，不是两张卡")

    def test_defaults_are_unknown_and_parties(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "123456789", "没说在哪说的", "2026-11-03T10:00:00+00:00")
        self.assertEqual("unknown", result["entry"]["scene"])
        self.assertEqual("当事人", result["entry"]["audience"])


if __name__ == "__main__":
    unittest.main()
