"""边：人—事—话题三类边；邻居查询；退役/落冷仓后边不悬空；结构边守卫。"""
from __future__ import annotations

import unittest

from organs.brain import edges as edges_mod
from organs.brain.tests.helpers import fresh_store, ingest


class EdgesTest(unittest.TestCase):
    def test_neighbors_of_event_node_include_person_and_topic(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "主人", "我妈住院了", "2026-11-03T10:00:00+00:00")
        entry_id = result["entry"]["id"]
        neighbors = store.neighbors(entry_id)["neighbors"]
        labels = [n["label"] for n in neighbors]
        self.assertIn("主人", labels)
        self.assertIn("医院", labels)
        for n in neighbors:
            self.assertTrue(n["ts"])
            self.assertIn(n["edge_type"], ("person-event", "event-topic"))

    def test_edges_do_not_dangle_after_forget(self):
        store, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        result = ingest(store, "主人", "我妈住院了", "2026-11-03T10:00:00+00:00")
        entry_id = result["entry"]["id"]
        store.forget(entry_id, ts="2026-11-03T10:05:00+00:00")
        neighbors = store.neighbors(entry_id)["neighbors"]
        self.assertEqual([], neighbors, "退役后边不许悬空（标失效、不再返回）")

    def test_structural_edge_guard_fails_when_event_topic_edges_missing(self):
        graph = {"nodes": {}, "edges": [
            {"id": "e1", "type": "person-event", "from": "person:主人",
             "to": "event:m1", "ts": "2026-11-03T10:00:00+00:00",
             "weight": 1.0, "status": "active"},
        ]}
        health = edges_mod.edge_health(graph)
        self.assertFalse(health["ok"])
        self.assertEqual(0.0, health["structural_ratio"])


if __name__ == "__main__":
    unittest.main()
