"""提案被接受/拒绝之后，状态要真的落回脑里——这是那个「点了没反应」的 bug 的判据。

真 bug 的链路：魂接受了（revision +1、history 多一行），但 proposals.json 里没有 status，
面板刷新照旧把这一条摆在「等她点头」；再点一次被演化五闸的 GATE_COOLDOWN 拦下，
人看到的就是「点了没反应」。所以这里的每一条都盯着「脑里的终态」而不是只盯 200。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from api import api
from api.tests.helpers import copy_fixture
from organs.brain import BrainStore
from organs.soul import load

# value 要过得了演化五闸的步长（manner.stance 一次最多挪 10 字）：句子短一点，
# 好让这些用例盯的是「状态有没有落」，不是闸门本身。
PENDING_ONE = {"id": "bp-2026-11-04-1", "target": "manner.stance",
               "value": "先接话再问一句",
               "evidence": ["m-1", "m-2", "m-3"],
               "reason": "最近几条都指向「不熟/不理」的方向"}
PENDING_TWO = {"id": "bp-2026-11-04-2", "target": "manner.stance",
               "value": "说话前先软一句",
               "evidence": ["m-4", "m-5", "m-6"],
               "reason": "最近几条都指向「说话太直」的方向"}


class SoulProposalsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        from unittest.mock import patch
        disabled = patch("organs.day.growth.enabled", return_value=False)
        disabled.start()
        self.addCleanup(disabled.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = copy_fixture("valid", self._tmp.name)
        self.store = load(self.root)
        self.brain_root = Path(self._tmp.name) / "brain"
        self.brain = BrainStore(self.brain_root)
        self.batch = {"night_of": "2026-11-04",
                      "proposals": [dict(PENDING_ONE), dict(PENDING_TWO)]}
        self._write_batch(self.batch)

    def _write_batch(self, batch: Dict[str, Any]) -> None:
        (self.brain_root / "proposals.json").write_text(
            json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
              brain: Any = "default") -> Tuple[int, Dict[str, Any]]:
        if brain == "default":
            brain = self.brain
        status, payload = api.handle(method, path, body, self.store, brain=brain)
        json.dumps(payload, ensure_ascii=False)
        return status, payload

    def _rows(self) -> Dict[str, Dict[str, Any]]:
        return {row["id"]: row for row in self.brain.proposals()["proposals"]}

    def _ids(self, params: str) -> list:
        status, payload = self._call("GET", "/soul/proposals" + params)
        self.assertEqual(200, status, payload)
        return [row["id"] for row in payload["proposals"]]

    # —— 1/2：接受之后那条从 pending 消失，all 里带 actor 与 ts，重启还在 ——
    def test_accept_with_growth_updates_effective_profile_without_touching_baseline(self):
        from unittest.mock import patch
        from organs.mind import persona
        revision = self.store.revision()
        with patch('organs.day.growth.enabled', return_value=True):
            status, result = self._call('POST','/soul/proposal',{'actor':'panel','proposal':dict(PENDING_ONE)})
            self.assertEqual(status,200)
            self.assertEqual(result['applied_to'],'profile')
            self.assertIn(PENDING_ONE['value'],persona.render(self.store,self.brain))
            self.assertEqual(self.store.revision(),revision)
            again, _ = self._call('POST','/soul/proposal',{'actor':'panel','proposal':dict(PENDING_ONE)})
            self.assertEqual(again,400)
        self.assertEqual(self._rows()[PENDING_ONE['id']]['profile_version'],1)

    def test_accept_leaves_pending_and_lands_in_all(self):
        self.assertEqual(["bp-2026-11-04-1", "bp-2026-11-04-2"], self._ids(""))
        status, payload = self._call("POST", "/soul/proposal",
                                     {"actor": "panel", "proposal": dict(PENDING_ONE)})
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["mark"]["ok"], payload["mark"])
        self.assertEqual(["bp-2026-11-04-2"], self._ids("?status=pending"))
        status, payload = self._call("GET", "/soul/proposals?status=all")
        self.assertEqual(200, status)
        self.assertEqual(2, len(payload["proposals"]))
        accepted = self._rows()["bp-2026-11-04-1"]
        self.assertEqual("accepted", accepted["status"])
        self.assertEqual("panel", accepted["actor"])
        self.assertNotIn("decision_reason", accepted, "接受不带拒绝理由")
        self.assertEqual(PENDING_ONE["reason"], accepted["reason"], "她提的理由原样留着")
        # ts 就是魂那行账的 ts：两边对得上，人才知道哪一次点头算的
        line = self.store.history()[-1]
        self.assertEqual("proposal", line["kind"])
        self.assertEqual(line["ts"], accepted["ts"])
        self.assertEqual("bp-2026-11-04-1", line["proposal_id"])
        # 没接受的那条一个字都不许动
        self.assertEqual(PENDING_TWO, self._rows()["bp-2026-11-04-2"])

    def test_accept_status_survives_a_restart(self):
        self._call("POST", "/soul/proposal",
                   {"actor": "panel", "proposal": dict(PENDING_ONE)})
        reopened = BrainStore(self.brain_root)
        rows = {row["id"]: row for row in reopened.proposals()["proposals"]}
        self.assertEqual("accepted", rows["bp-2026-11-04-1"]["status"])
        self.assertEqual("panel", rows["bp-2026-11-04-1"]["actor"])

    # —— 3：拒绝落 rejected + decision_reason（她的 reason 不动），不写魂、不产生 revision ——
    def test_reject_marks_status_without_touching_the_soul(self):
        revision = self.store.revision()
        history = len(self.store.history())
        status, payload = self._call("POST", "/soul/proposal/reject",
                                     {"actor": "panel", "proposal_id": "bp-2026-11-04-2",
                                      "reason": "这一条会让她更别扭"})
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(revision, self.store.revision(), "拒绝不产生新版本")
        self.assertEqual(history, len(self.store.history()), "拒绝不写魂的账")
        rejected = self._rows()["bp-2026-11-04-2"]
        self.assertEqual("rejected", rejected["status"])
        # 人的理由落 decision_reason；她「为什么提」那句 reason 逐字保留
        self.assertEqual("这一条会让她更别扭", rejected["decision_reason"])
        self.assertEqual(PENDING_TWO["reason"], rejected["reason"])
        self.assertEqual("panel", rejected["actor"])
        self.assertTrue(rejected["ts"])
        self.assertEqual(["bp-2026-11-04-1"], self._ids("?status=pending"))

    def test_status_all_carries_both_reasons_for_both_rows(self):
        """?status=all 里两条都要读得到：接受那条没有 decision_reason，拒绝那条两个理由都在。"""
        self._call("POST", "/soul/proposal",
                   {"actor": "panel", "proposal": dict(PENDING_ONE)})
        self._call("POST", "/soul/proposal/reject",
                   {"actor": "panel", "proposal_id": "bp-2026-11-04-2",
                    "reason": "这一条会让她更别扭"})
        status, payload = self._call("GET", "/soul/proposals?status=all")
        self.assertEqual(200, status)
        rows = {row["id"]: row for row in payload["proposals"]}
        self.assertEqual({"bp-2026-11-04-1", "bp-2026-11-04-2"}, set(rows))
        self.assertEqual("accepted", rows["bp-2026-11-04-1"]["status"])
        self.assertEqual(PENDING_ONE["reason"], rows["bp-2026-11-04-1"]["reason"])
        self.assertNotIn("decision_reason", rows["bp-2026-11-04-1"])
        self.assertEqual("rejected", rows["bp-2026-11-04-2"]["status"])
        self.assertEqual(PENDING_TWO["reason"], rows["bp-2026-11-04-2"]["reason"])
        self.assertEqual("这一条会让她更别扭", rows["bp-2026-11-04-2"]["decision_reason"])
        for row in rows.values():
            for key in ("actor", "ts"):
                self.assertTrue(row.get(key), "%s 缺 %s" % (row["id"], key))

    def test_reject_needs_reason_actor_and_id(self):
        self.assertEqual(400, self._call("POST", "/soul/proposal/reject",
                                         {"actor": "panel",
                                          "proposal_id": "bp-2026-11-04-2",
                                          "reason": "  "})[0])
        self.assertEqual(400, self._call("POST", "/soul/proposal/reject",
                                         {"actor": "panel", "proposal_id": "  ",
                                          "reason": "没写清"})[0])
        self.assertEqual(400, self._call("POST", "/soul/proposal/reject",
                                         {"actor": " ", "proposal_id": "bp-2026-11-04-2",
                                          "reason": "没写清"})[0])
        self.assertEqual(400, self._call("POST", "/soul/proposal/reject", {})[0])
        status, payload = self._call("POST", "/soul/proposal/reject",
                                     {"actor": "panel", "proposal_id": "没有这条",
                                      "reason": "没写清"})
        self.assertEqual(400, status)
        self.assertIn("没有这条提案", payload["message"])
        self.assertEqual(["bp-2026-11-04-1", "bp-2026-11-04-2"], self._ids(""))

    def test_reject_without_brain_is_honest(self):
        status, payload = self._call("POST", "/soul/proposal/reject",
                                     {"actor": "panel", "proposal_id": "bp-2026-11-04-2",
                                      "reason": "没写清"}, brain=None)
        self.assertEqual(503, status)
        self.assertIn("脑未载入", payload["message"])

    # —— 4：同一条再接受一次，拒因照旧，但要看得见「已经处理过」 ——
    def test_second_accept_says_it_is_already_handled(self):
        self.assertEqual(200, self._call("POST", "/soul/proposal",
                                         {"actor": "panel",
                                          "proposal": dict(PENDING_ONE)})[0])
        status, payload = self._call("POST", "/soul/proposal",
                                     {"actor": "panel", "proposal": dict(PENDING_ONE)})
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertIn("已经处理过", payload["message"])
        self.assertEqual("accepted", payload["handled"]["status"])
        self.assertEqual("panel", payload["handled"]["actor"])
        codes = {issue["code"] for issue in payload["issues"]}
        self.assertIn("GATE_COOLDOWN", codes, "第二次点头该被演化闸拦住")

    # —— 5：两边 id 对不上，接受仍生效，但 mark 失败要如实说 ——
    def test_unknown_proposal_id_still_accepts_but_reports_mark_failure(self):
        revision = self.store.revision()
        stranger = dict(PENDING_ONE, id="bp-魂里没有这条")
        status, payload = self._call("POST", "/soul/proposal",
                                     {"actor": "panel", "proposal": stranger})
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"], "魂的写入是既成事实：接受照样算数")
        self.assertEqual(revision + 1, self.store.revision())
        self.assertFalse(payload["mark"]["ok"])
        self.assertIn("没有这条提案", payload["mark"]["message"])
        self.assertIn("bp-魂里没有这条", payload["mark"]["message"])
        # 脑里那两条一个都没被动过
        self.assertEqual(["bp-2026-11-04-1", "bp-2026-11-04-2"], self._ids(""))

    def test_accept_without_brain_reports_mark_failure(self):
        status, payload = self._call("POST", "/soul/proposal",
                                     {"actor": "panel", "proposal": dict(PENDING_ONE)},
                                     brain=None)
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["mark"]["ok"])
        self.assertIn("脑未载入", payload["mark"]["message"])

    # —— 清单：默认只给 pending，缺 status 也算 pending ——
    def test_list_defaults_to_pending_and_counts_missing_status(self):
        batch = {"night_of": "2026-11-04",
                 "proposals": [dict(PENDING_ONE),
                               dict(PENDING_TWO, status="pending"),
                               dict(PENDING_ONE, id="bp-old", status="accepted",
                                    actor="panel", ts="2026-11-01T09:00:00+00:00")]}
        self._write_batch(batch)
        self.assertEqual(["bp-2026-11-04-1", "bp-2026-11-04-2"], self._ids(""))
        self.assertEqual(["bp-2026-11-04-1", "bp-2026-11-04-2", "bp-old"],
                         self._ids("?status=all"))
        self.assertEqual(400, self._call("GET", "/soul/proposals?status=done")[0])
        status, payload = self._call("GET", "/soul/proposals", brain=None)
        self.assertEqual(503, status)
        self.assertIn("脑未载入", payload["message"])


if __name__ == "__main__":
    unittest.main()
