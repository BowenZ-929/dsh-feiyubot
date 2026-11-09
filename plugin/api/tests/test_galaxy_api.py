"""星图用真脑假数据：读取不改变记忆，边/状态/来源保真，不虚构召回解释。"""
import json
import unittest
from urllib.parse import quote

from api import api
from api.pages import PAGE_FILES, read_page
from organs.brain.tests.helpers import fresh_store


class GalaxyApiTest(unittest.TestCase):
    def test_recorded_person_links_without_topic_do_not_invent_people_or_write_graph(self):
        from organs.brain.model import SIDE_SELF
        incoming = self.brain.ingest('3000000001', '随便聊两句', ts=self.at)['entry']
        own = self.brain.ingest('3000000001', '嗯，我在呢', ts=self.at, side=SIDE_SELF)['entry']
        unknown = self.brain.ingest('', '来源不明的文字', ts=self.at)['entry']
        # 旧关系图即使已有“人”占位节点，也不能把不同未知来源当作同一人。
        self.brain.ingest('', '医院里的另一段话', ts=self.at)
        self.brain.ingest('', '医院相关的自言自语', ts=self.at, side=SIDE_SELF)
        self.brain.ingest('', '没有收件人的自言自语', ts=self.at, side=SIDE_SELF)
        topic = self.brain.ingest('3000000001', '医院复查', ts=self.at)['entry']
        before = self.files()
        data = self.brain.graph_snapshot(self.at)
        links = [e for e in data['edges'] if e['type'] == 'record-person']
        self.assertEqual({e['to'] for e in links}, {'event:'+incoming['id'], 'event:'+own['id']})
        self.assertTrue(all(e['from'] == 'person:3000000001' and e['basis'] == 'memory.who' for e in links))
        self.assertFalse(any(e['to'] == 'event:'+unknown['id'] for e in data['edges']))
        self.assertEqual(len([e for e in data['edges'] if e['to'] == 'event:'+topic['id']]), 1)
        self.assertEqual(data['unresolved_edges'], 0)
        self.assertEqual(before, self.files())
        self.brain.retire(incoming['id'], ts=self.at)
        before = self.files()
        status, detail = self.call('/api/memory/event:'+incoming['id']+'/why')
        self.assertEqual(status, 200)
        self.assertEqual(detail['edges'][0]['status'], 'invalid')
        self.assertEqual(before, self.files())

    def setUp(self):
        self.brain, self.tmp = fresh_store()
        self.addCleanup(self.tmp.cleanup)
        self.at = "2026-11-07T12:00:00+09:00"

    def call(self, path, method="GET", brain=True):
        return api.handle(method, path, None, None, brain=self.brain if brain else None)

    def files(self):
        return {str(path.relative_to(self.brain._root)): path.read_bytes()
                for path in self.brain._root.rglob("*") if path.is_file()}

    def test_snapshot_and_detail_are_readonly_even_across_day_boundary(self):
        result = self.brain.ingest("3000000001", "妈妈在医院复查", ts=self.at)
        entry = result["entry"]
        before = self.files()
        data = self.brain.graph_snapshot("2080-01-01T12:00:00+09:00")
        node = next(row for row in data["nodes"] if row.get("memory_id") == entry["id"])
        self.assertEqual(("亲历", "妈妈在医院复查", entry["id"]),
                         (node["source"], node["quote"], node["evidence_key"]))
        self.assertEqual("today", node["tier"])
        self.assertEqual(3, len(data["edges"]))
        self.assertEqual(data["revision"], self.brain.graph_snapshot("2080-01-01T12:00:00+09:00")["revision"])
        status, detail = self.call("/api/memory/" + quote(node["id"], safe="") + "/why")
        self.assertEqual(200, status)
        self.assertEqual(2, len(detail["edges"]))
        self.assertIsNone(detail["why"]["rank"])
        self.assertEqual(0, detail["node"]["hit_count"])
        self.assertEqual(before, self.files(), "浏览不能换日、降级或增加命中")

    def test_retirement_changes_revision_and_keeps_invalid_relationships_explicit(self):
        entry = self.brain.ingest("3000000001", "医院复查", ts=self.at)["entry"]
        old = self.brain.graph_snapshot(self.at)
        self.brain.retire(entry["id"], ts=self.at)
        new = self.brain.graph_snapshot(self.at)
        self.assertNotEqual(old["revision"], new["revision"])
        memory = next(n for n in new["nodes"] if n.get("memory_id") == entry["id"])
        self.assertEqual("stale", memory["status"])
        attached = [e for e in new["edges"] if memory["id"] in (e["from"], e["to"])]
        self.assertTrue(attached)
        self.assertTrue(all(e["status"] == "invalid" for e in attached))

    def test_migrated_note_and_latest_version_are_not_invented_quotes(self):
        self.brain.import_rows([{"id": "legacy1", "ts": self.at, "who": "3000000001", "source_type": "迁移",
                                 "quote": "", "note": "旧记录的理解", "status": "active"}])
        self.brain.import_rows([{"id": "legacy1", "ts": self.at, "who": "3000000001", "source_type": "迁移",
                                 "quote": "", "note": "更正后的理解", "status": "stale"}])
        nodes = [n for n in self.brain.graph_snapshot(self.at)["nodes"] if n["kind"] == "event"]
        self.assertEqual(1, len(nodes))
        self.assertEqual(("迁移", "", "更正后的理解", ""),
                         (nodes[0]["source"], nodes[0]["quote"], nodes[0]["note"], nodes[0]["evidence_key"]))

    def test_retired_longterm_detail_keeps_original_provenance_without_recalling_it(self):
        entry = self.brain.ingest("3000000001", "周六去河岸图书馆", ts=self.at,
                                  note="先前的周六安排")["entry"]
        later = "2026-11-08T12:00:00+09:00"
        self.brain.today_entries(later)  # 真正换日进入长期档。
        self.brain.retire(entry["id"], ts=later)
        before = self.files()
        status, detail = self.call("/api/memory/event:" + entry["id"] + "/why")
        self.assertEqual(200, status)
        node = detail["node"]
        self.assertEqual("周六去河岸图书馆", node["quote"])
        self.assertEqual(("先前的周六安排", "3000000001", "亲历", entry["id"], self.at, "stale", later),
                         (node["note"], node["who"], node["source"], node["evidence_key"],
                          node["at"], node["status"], node["status_changed_at"]))
        self.assertEqual("周六去河岸图书馆", self.brain.cold_lookup(entry["id"])["quote"])
        self.assertEqual(before, self.files(), "查看退役记录不能物理回填或增加命中")
        self.assertFalse(self.brain.recall("河岸图书馆", now_ts=later)["has_material"])
        self.assertTrue(all(e["status"] == "invalid" for e in detail["edges"]))

    def test_legacy_status_records_keep_quote_when_note_is_edited_after_retirement(self):
        entry = self.brain.ingest("3000000001", "周六去河岸图书馆", ts=self.at)["entry"]
        later = "2026-11-08T12:00:00+09:00"
        self.brain.today_entries(later)
        # 按旧版实盘格式追加，确保以后即便改写者也继续兼容历史。
        with (self.brain.root / "longterm.jsonl").open("a") as handle:
            for at in (later, "2026-11-08T12:01:00+09:00"):
                handle.write(json.dumps({"id": entry["id"], "status": "stale", "ts": at,
                                        "who": "", "text": "", "count": 0,
                                        "source": "status", "day": ""}) + "\n")
        self.brain.backfill()  # 旧版补字段也不能让空状态行冒充原话。
        self.brain.edit_note(entry["id"], "测试者", "这段原计划已取消")
        got = self.brain.cold_lookup(entry["id"])
        self.assertEqual("周六去河岸图书馆", got.get("quote"))
        self.assertEqual(("stale", self.at, "这段原计划已取消"),
                         (got["status"], got["ts"], got["note"]))
        self.assertFalse(self.brain.recall("河岸图书馆", now_ts=later)["has_material"])

    def test_unresolved_edges_are_counted_not_connected_to_fabricated_memories(self):
        graph = {"nodes": {"event:missing": {"id": "event:missing", "kind": "event", "label": "missing"},
                            "topic:医院": {"id": "topic:医院", "kind": "topic", "label": "医院"}},
                 "edges": [{"from": "event:missing", "to": "topic:医院", "status": "active"}]}
        (self.brain._root / "edges.json").write_text(json.dumps(graph))
        data = self.brain.graph_snapshot(self.at)
        self.assertEqual(1, data["unresolved_edges"])
        self.assertEqual([], data["edges"])
        self.assertNotIn("event:missing", [n["id"] for n in data["nodes"]])

    def test_empty_missing_store_missing_node_and_invalid_data_are_distinct(self):
        status, data = self.call("/api/graph/snapshot")
        self.assertEqual((200, []), (status, data["nodes"]))
        self.assertEqual(503, self.call("/api/graph/snapshot", brain=False)[0])
        self.assertEqual(404, self.call("/api/memory/no-such-id/why")[0])
        for path in ("/api/graph/snapshot", "/api/memory/no-such-id/why"):
            self.assertEqual(405, self.call(path, method="POST")[0])
        (self.brain._root / "longterm.jsonl").write_text("broken json")
        self.assertEqual(503, self.call("/api/graph/snapshot")[0])

    def test_page_assets_are_registered(self):
        self.assertEqual("galaxy.html", PAGE_FILES["/galaxy"])
        self.assertEqual("galaxy.js", PAGE_FILES["/galaxy.js"])
        self.assertTrue(read_page("galaxy.html"))
        self.assertTrue(read_page("galaxy.js"))

    def test_turn_link_survives_archival_and_retirement_without_inventing_legacy_ids(self):
        turn = "651a77b183364c139e1278b34b65c0e0"
        entry = self.brain.ingest("3000000001", "这张画很可爱", ts=self.at, turn_id=turn)["entry"]
        old = self.brain.ingest("3000000001", "旧的记录没有回合编号", ts=self.at)["entry"]
        for retired in (False, True):
            if retired:
                self.brain.today_entries("2026-11-08T12:00:00+09:00")
                self.brain.retire(entry["id"], ts="2026-11-08T12:01:00+09:00")
            before = self.files()
            for memory_id, expected in ((entry["id"], turn), (old["id"], "")):
                status, data = self.call("/api/memory/event:" + memory_id + "/why")
                self.assertEqual(200, status)
                self.assertEqual(expected, data["node"]["turn_id"])
            self.assertEqual(before, self.files())


if __name__ == "__main__":
    unittest.main()
