"""edit 是人的手：只许改六件里的字段，同事务追一行账，删一律不许。"""
from __future__ import annotations

import json
import tempfile
import unittest

from organs.soul.store import load
from organs.soul.tests.helpers import (BLOCK_FILES, copy_fixture, read_bytes, read_json,
                                       write_json)


class EditTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)

    def _edit(self, block: str, path: str, value, **kwargs):
        params = {"actor": "bowen", "reason": "面板上改一句"}
        params.update(kwargs)
        return self.store.edit(block=block, path=path, value=value, **params)

    def test_manner_stance_edit_touches_only_manner_and_logs_one_line(self):
        before = dict((name, read_bytes(self.root / name))
                      for name in BLOCK_FILES.values() if name != "manner.json")
        result = self._edit("manner", "stance", "改完的姿态")
        self.assertTrue(result["ok"], result)
        self.assertEqual("改完的姿态", read_json(self.root / "manner.json")["stance"])
        for name, data in before.items():
            self.assertEqual(data, read_bytes(self.root / name), name)
        rows = self.store.history()
        self.assertEqual(1, len(rows))
        self.assertEqual("edit", rows[0]["kind"])
        self.assertEqual("bowen", rows[0]["actor"])
        self.assertEqual("面板上改一句", rows[0]["reason"])

    def test_red_line_text_can_be_edited_by_hand(self):
        result = self._edit("bounds", "items[0].rule", "不自称 AI，也不解释设定")
        self.assertTrue(result["ok"], result)
        self.assertEqual("不自称 AI，也不解释设定",
                         read_json(self.root / "bounds.json")["items"][0]["rule"])

    def test_red_line_can_only_be_switched_off_not_deleted(self):
        result = self._edit("bounds", "items[1].active", False)
        self.assertTrue(result["ok"], result)
        self.assertFalse(read_json(self.root / "bounds.json")["items"][1]["active"])
        self.assertEqual([], self.store.match_bounds("【待定稿·二】"))

    def test_deleting_a_red_line_is_rejected_and_nothing_written(self):
        before = read_bytes(self.root / "bounds.json")
        for path, value in (("items[1]", None), ("items", [])):
            result = self._edit("bounds", path, value)
            self.assertFalse(result["ok"], path)
            self.assertEqual("BOUNDS_DELETE_ATTEMPT", result["issues"][0]["code"])
        self.assertEqual(before, read_bytes(self.root / "bounds.json"))
        self.assertEqual([], self.store.history())

    def test_renaming_a_red_line_is_rejected(self):
        result = self._edit("bounds", "items[0].id", "b99")
        self.assertFalse(result["ok"])
        self.assertIn("不能删除也不能改 id", result["message"])

    def test_explicit_minimal_personality_and_statement_count_limits(self):
        result = self._edit('identity', 'self_statements', ['可爱'], structural=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.store.snapshot().identity.self_statements, ('可爱',))
        self.assertIn('【性格底色】可爱。', self.store.render())
        for value in ([], ['一', '二', '三', '四']):
            result = self._edit('identity', 'self_statements', value, structural=True)
            self.assertFalse(result['ok'])
            self.assertEqual(self.store.snapshot().identity.self_statements, ('可爱',))

    def test_dropping_a_self_statement_is_rejected(self):
        result = self._edit("identity", "self_statements", ["只留一句"])
        self.assertFalse(result["ok"])
        self.assertEqual("SOUL_DELETE_ATTEMPT", result["issues"][0]["code"])

    def test_dropping_a_synonym_is_rejected(self):
        added = self._edit("identity", "self_names.synonyms", ["鱼"])
        self.assertTrue(added["ok"], added)
        dropped = self._edit("identity", "self_names.synonyms", [])
        self.assertFalse(dropped["ok"])
        self.assertEqual("SOUL_DELETE_ATTEMPT", dropped["issues"][0]["code"])

    def test_whole_block_replace_needs_an_explicit_structural_ok(self):
        """整块替换会连带删字段：不声明 structural 就一律拒，声明了才许动结构。"""
        result = self._edit("manner", "", {"stance": "整块换掉"})
        self.assertFalse(result["ok"])
        self.assertEqual("SOUL_DELETE_ATTEMPT", result["issues"][0]["code"])
        allowed = self._edit("manner", "", {"stance": "整块换掉"}, structural=True)
        self.assertTrue(allowed["ok"], allowed)
        self.assertEqual({"stance": "整块换掉"}, read_json(self.root / "manner.json"))
        line = self.store.history()[-1]
        self.assertTrue(line["structural"])        # 账上写明这次动了结构
        self.assertNotIn("removed", line)          # 这一换没丢东西，账上不记删除

    def test_structural_edit_may_shrink_a_list_and_add_a_field(self):
        """结构变更才许删条目/新建字段：换名字与登记执行方都走这条路。"""
        added = self._edit("identity", "self_names.synonyms", ["鱼", "本鱼"])
        self.assertTrue(added["ok"], added)
        shrunk = self._edit("identity", "self_names.synonyms", ["鱼"], structural=True)
        self.assertTrue(shrunk["ok"], shrunk)
        self.assertEqual(["鱼"], read_json(self.root / "identity.json")["self_names"]["synonyms"])
        self.assertIn("少了 1 条", self.store.history()[-1]["removed"])   # 删了什么，账上要指名
        field = self._edit("bounds", "items[0].enforced_by", "soul·字面", structural=True)
        self.assertTrue(field["ok"], field)
        self.assertEqual("soul·字面", read_json(self.root / "bounds.json")["items"][0]["enforced_by"])

    def test_unknown_path_is_rejected_without_writing(self):
        before = read_bytes(self.root / "manner.json")
        result = self._edit("manner", "stance.mood", "不该有这个字段")
        self.assertFalse(result["ok"])
        self.assertIn("path 无法定位", result["message"])
        self.assertEqual(before, read_bytes(self.root / "manner.json"))

    def test_baseline_is_not_a_soul_block(self):
        """六轴卡归心：block=baseline 按未知 block 拒，那条「不能直改基线」的码随之删掉。"""
        result = self._edit("baseline", "axes[0].inner", 50)
        self.assertFalse(result["ok"])
        self.assertIn("block 只能是 identity/manner/bounds", result["message"])
        self.assertEqual([], result["issues"])
        self.assertEqual([], self.store.history())

    def test_unknown_block_is_rejected(self):
        self.assertFalse(self._edit("history", "anything", 1)["ok"])

    def test_blank_actor_or_reason_is_rejected(self):
        self.assertFalse(self._edit("manner", "stance", "x", actor="  ")["ok"])
        self.assertFalse(self._edit("manner", "stance", "x", reason="")["ok"])
        self.assertEqual([], self.store.history())

    def test_actor_and_reason_are_stripped(self):
        result = self._edit("manner", "stance", "x", actor=" bowen ", reason=" 收尾 ")
        self.assertTrue(result["ok"], result)
        self.assertEqual("bowen", self.store.history()[0]["actor"])
        self.assertEqual("收尾", self.store.history()[0]["reason"])

    def test_edit_rejected_when_the_card_changed_after_load(self):
        data = read_json(self.root / "manner.json")
        data["stance"] = "别人手改过"
        write_json(self.root / "manner.json", data)
        result = self._edit("manner", "stance", "我的改法")
        self.assertFalse(result["ok"])
        self.assertIn("被改过", result["message"])

    def test_results_are_json_safe(self):
        json.dumps(self._edit("manner", "stance", "x"), ensure_ascii=False)
        json.dumps(self._edit("baseline", "axes[0].inner", 1), ensure_ascii=False)

    def test_raw_is_a_copy_in_disk_shape(self):
        raw = self.store.raw("bounds")
        self.assertIn("match", raw["items"][0])
        raw["items"][0]["rule"] = "改这份拷贝不该影响 store"
        self.assertNotEqual("改这份拷贝不该影响 store",
                            self.store.raw("bounds")["items"][0]["rule"])

    def test_edit_path_follows_raw_shape(self):
        result = self._edit("bounds", "items[0].match.type", "regex")
        self.assertTrue(result["ok"], result)
        self.assertEqual("regex", self.store.raw("bounds")["items"][0]["match"]["type"])
        normalized = self._edit("bounds", "items[0].match_type", "literal")
        self.assertFalse(normalized["ok"])
        self.assertIn("path 无法定位", normalized["message"])


if __name__ == "__main__":
    unittest.main()

