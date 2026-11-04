"""delivery：发出≠送达、ack 的 retcode 为准、重启后三路对账、四条入口不崩。

规矩与别的日子测试一致：只用临时根（绝不写 organs/day/data/ 的真实文件），
时刻一律注入 now_ts（绝不依赖真实时钟）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from organs.day import delivery, model
from organs.day.tests.helpers import at, temp_root

NOW = "2026-11-05T08:00:00"
PLUGIN_DIR = Path(__file__).resolve().parents[3]
CFG = {"stuck_minutes": 5, "backoff_minutes": 2, "backoff_cap_minutes": 32}


def advance(minutes: int) -> str:
    """以 NOW 为起点的绝对时刻（分钟级），测试里不许问真实时钟。"""
    return model.iso(model.parse_ts(at(NOW)) + timedelta(minutes=minutes))


def raw_queue(root: Path):
    """盘上那一份原样读回来（不经过 delivery.load，验收要贴真实文件）。"""
    return json.loads((Path(root) / delivery.PENDING_FILE).read_text(encoding="utf-8"))


def row(root: Path, item_id: str):
    return [entry for entry in raw_queue(root)["items"] if entry["id"] == item_id][0]


class QueueOnDiskTest(unittest.TestCase):
    """C1：intake 先落盘 expect；换一个进程重新 load 仍读得到。"""

    def setUp(self) -> None:
        self.root = temp_root(self)

    def test_intake_lands_expect_before_sending(self):
        result = delivery.intake("今天想说的第一句", to="her", now_ts=at(NOW), root=self.root)
        self.assertTrue(result["ok"])
        self.assertEqual("expect", result["item"]["state"])
        self.assertEqual(0, result["item"]["attempts"])
        self.assertIsNone(result["item"]["ack"])
        self.assertEqual(result["item"]["id"], result["send_request"]["echo"])
        self.assertTrue(result["sent"])

        on_disk = raw_queue(self.root)
        self.assertEqual(1, len(on_disk["items"]))
        landed = on_disk["items"][0]
        for field in ("id", "text", "to", "created_at", "attempts", "state",
                      "next_try_at", "last_try_at", "ack", "echo", "reason"):
            self.assertIn(field, landed)
        self.assertEqual("expect", landed["state"])
        self.assertEqual(at(NOW), landed["created_at"])

    def test_a_fresh_process_reads_the_same_queue(self):
        result = delivery.intake("重启之后还要能对账", now_ts=at(NOW), root=self.root)
        code = (
            "import json, sys;"
            "sys.path.insert(0, %r);"
            "from organs.day import delivery;"
            "print(json.dumps(delivery.pending(root=%r), ensure_ascii=False))"
            % (str(PLUGIN_DIR), str(self.root))
        )
        env = {key: value for key, value in os.environ.items() if key != model.ENV_ROOT}
        done = subprocess.run([sys.executable, "-c", code], cwd=str(PLUGIN_DIR), env=env,
                              capture_output=True, text=True, check=True)
        payload = json.loads(done.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, payload["counts"]["expect"])
        self.assertEqual([result["item"]["id"]], [entry["id"] for entry in payload["items"]])

    def test_send_fn_is_injected_and_a_failing_one_is_not_delivery(self):
        seen = []
        ok = delivery.intake("递出去", now_ts=at(NOW), root=self.root,
                             send_fn=lambda request: seen.append(request))
        self.assertEqual([ok["item"]["id"]], [entry["id"] for entry in seen])

        def boom(request):
            raise RuntimeError("嘴没接上")

        bad = delivery.intake("递不出去", now_ts=at(NOW), root=self.root, send_fn=boom)
        self.assertTrue(bad["ok"])
        self.assertFalse(bad["sent"])
        self.assertEqual("expect", bad["item"]["state"])
        self.assertIn("send_failed", row(self.root, bad["item"]["id"])["reason"])


class AckIsTheOnlyProofTest(unittest.TestCase):
    """C2：ack retcode==0 才算送达；delivered 之后 reconcile 不再 requeue。"""

    def setUp(self) -> None:
        self.root = temp_root(self)

    def test_zero_retcode_becomes_delivered_and_never_requeues(self):
        created = delivery.intake("她真的开始了", now_ts=at(NOW), root=self.root)
        item_id = created["item"]["id"]
        acked = delivery.ack(item_id, 0, now_ts=advance(1), root=self.root)
        self.assertTrue(acked["delivered"])
        self.assertEqual("delivered", row(self.root, item_id)["state"])
        self.assertEqual({"retcode": 0, "at": advance(1)}, row(self.root, item_id)["ack"])

        late = delivery.reconcile(advance(600), root=self.root)
        self.assertEqual(1, late["delivered"])
        self.assertEqual(0, late["requeued"])
        self.assertEqual(["delivered"], [entry["action"] for entry in late["items"]])
        landed = row(self.root, item_id)
        self.assertEqual("delivered", landed["state"])
        self.assertEqual(0, landed["attempts"])

    def test_nonzero_retcode_is_recorded_but_not_delivery(self):
        created = delivery.intake("没送到", now_ts=at(NOW), root=self.root)
        item_id = created["item"]["id"]
        failed = delivery.ack(item_id, 100, now_ts=advance(1), root=self.root)
        self.assertFalse(failed["delivered"])
        landed = row(self.root, item_id)
        self.assertEqual("expect", landed["state"])
        self.assertEqual({"retcode": 100, "at": advance(1)}, landed["ack"])
        self.assertIn("ack_retcode_100", landed["reason"])
        watched = delivery.reconcile(advance(2), root=self.root)
        self.assertEqual(0, watched["delivered"])


class StuckTimeoutTest(unittest.TestCase):
    """C3：没 ack 且超过 stuck_minutes → requeue；两次退避一次比一次长。"""

    def setUp(self) -> None:
        self.root = temp_root(self)

    def test_stuck_item_requeues_with_growing_backoff(self):
        created = delivery.intake("没人确认", now_ts=at(NOW), root=self.root)
        item_id = created["item"]["id"]
        delivery.ack(item_id, 100, now_ts=at(NOW), root=self.root)
        self.assertEqual(0, row(self.root, item_id)["attempts"])

        first = delivery.reconcile(advance(6), root=self.root, config=CFG)
        self.assertEqual(1, first["requeued"])
        first_row = row(self.root, item_id)
        self.assertEqual(1, first_row["attempts"])
        self.assertIn("requeue_1", first_row["reason"])

        second = delivery.reconcile(advance(30), root=self.root, config=CFG)
        self.assertEqual(1, second["requeued"])
        second_row = row(self.root, item_id)
        self.assertEqual(2, second_row["attempts"])

        gap1 = model.parse_ts(first_row["next_try_at"]) - model.parse_ts(advance(6))
        gap2 = model.parse_ts(second_row["next_try_at"]) - model.parse_ts(advance(30))
        self.assertEqual(timedelta(minutes=2), gap1)
        self.assertEqual(timedelta(minutes=4), gap2)
        self.assertGreater(gap2, gap1)

    def test_backoff_is_capped(self):
        self.assertEqual(2, delivery.backoff_minutes(1, CFG))
        self.assertEqual(16, delivery.backoff_minutes(4, CFG))
        self.assertEqual(32, delivery.backoff_minutes(9, CFG))

    def test_reconcile_twice_in_the_backoff_window_does_not_inflate_attempts(self):
        created = delivery.intake("没人确认", now_ts=at(NOW), root=self.root)
        item_id = created["item"]["id"]
        delivery.reconcile(advance(6), root=self.root, config=CFG)
        self.assertEqual(1, row(self.root, item_id)["attempts"])
        again = delivery.reconcile(advance(6), root=self.root, config=CFG)
        self.assertEqual(0, again["requeued"])
        self.assertEqual(1, again["watching"])
        self.assertEqual(1, row(self.root, item_id)["attempts"])


class InFlightTest(unittest.TestCase):
    """C4：在途未超时 → 一个字节不动（发出≠送达的核心）。"""

    def setUp(self) -> None:
        self.root = temp_root(self)

    def test_just_sent_item_is_only_watched(self):
        created = delivery.intake("刚刚发出", now_ts=at(NOW), root=self.root)
        item_id = created["item"]["id"]
        before = row(self.root, item_id)

        report = delivery.reconcile(advance(1), root=self.root, config=CFG)
        self.assertEqual({"delivered": 0, "watching": 1, "requeued": 0},
                         {key: report[key] for key in ("delivered", "watching", "requeued")})
        self.assertEqual("watching", report["items"][0]["action"])
        after = row(self.root, item_id)
        self.assertEqual(before, after)
        self.assertEqual("expect", after["state"])
        self.assertEqual(0, after["attempts"])
        self.assertIsNone(after["ack"])
        self.assertEqual(at(NOW), after["next_try_at"])

    def test_exactly_at_the_stuck_edge_is_still_watching(self):
        created = delivery.intake("卡在边上", now_ts=at(NOW), root=self.root)
        item_id = created["item"]["id"]
        report = delivery.reconcile(advance(5), root=self.root, config=CFG)
        self.assertEqual(0, report["requeued"])
        self.assertEqual(0, row(self.root, item_id)["attempts"])


class RobustnessTest(unittest.TestCase):
    """C5：空 body / 缺 item_id / 缺 text / 坏 now → 明确失败，不崩，json 安全。"""

    def setUp(self) -> None:
        self.root = temp_root(self)

    def assertSafe(self, payload):
        json.dumps(payload, ensure_ascii=False)
        self.assertIn("ok", payload)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["message"])

    def test_missing_text_is_refused(self):
        for value in (None, "", "   "):
            payload = delivery.intake(value, now_ts=at(NOW), root=self.root)
            self.assertSafe(payload)
        self.assertFalse((Path(self.root) / delivery.PENDING_FILE).exists())

    def test_missing_item_id_or_unknown_item_is_refused(self):
        self.assertSafe(delivery.ack("", 0, now_ts=at(NOW), root=self.root))
        self.assertSafe(delivery.ack(None, 0, now_ts=at(NOW), root=self.root))
        self.assertSafe(delivery.ack("dl-nope", 0, now_ts=at(NOW), root=self.root))

    def test_bad_retcode_is_refused(self):
        created = delivery.intake("一句话", now_ts=at(NOW), root=self.root)
        self.assertSafe(delivery.ack(created["item"]["id"], "abc", root=self.root))
        self.assertSafe(delivery.ack(created["item"]["id"], None, root=self.root))

    def test_bad_now_is_refused_everywhere(self):
        self.assertSafe(delivery.intake("一句话", now_ts="昨天下午", root=self.root))
        self.assertSafe(delivery.ack("dl-x", 0, now_ts="昨天下午", root=self.root))
        self.assertSafe(delivery.pending(now_ts="昨天下午", root=self.root))
        self.assertSafe(delivery.reconcile("昨天下午", root=self.root))

    def test_empty_root_is_an_empty_queue_not_an_error(self):
        report = delivery.pending(now_ts=at(NOW), root=self.root)
        self.assertTrue(report["ok"])
        self.assertEqual({"expect": 0, "delivered": 0, "total": 0}, report["counts"])
        self.assertEqual([], report["items"])
        nothing = delivery.reconcile(now_ts=at(NOW), root=self.root)
        self.assertEqual((0, 0, 0), (nothing["delivered"], nothing["watching"], nothing["requeued"]))
        self.assertFalse((Path(self.root) / delivery.PENDING_FILE).exists())

    def test_corrupt_queue_is_reported_not_swallowed(self):
        target = Path(self.root) / delivery.PENDING_FILE
        target.write_text("{不是 json", encoding="utf-8")
        self.assertSafe(delivery.pending(now_ts=at(NOW), root=self.root))
        self.assertSafe(delivery.reconcile(now_ts=at(NOW), root=self.root))
        self.assertSafe(delivery.intake("一句话", now_ts=at(NOW), root=self.root))

    def test_http_layer_empty_bodies_do_not_crash(self):
        """契约测试会用空 query/空 body 打四条路由：不许穿成未捕获异常。"""
        from api import day as day_api

        with mock.patch.dict(os.environ, {model.ENV_ROOT: str(self.root)}):
            cases = [("GET", "/day/pending", None), ("POST", "/day/deliver", None),
                     ("POST", "/day/deliver", {}), ("POST", "/day/ack", {}),
                     ("POST", "/day/ack", {"item_id": "dl-x"}), ("POST", "/day/reconcile", {})]
            for method, route, body in cases:
                status, payload = day_api.dispatch(route, {}, body)
                json.dumps(payload, ensure_ascii=False)
                self.assertLess(status, 500, "%s %s 崩了：%r" % (method, route, payload))
            delivered = day_api.dispatch("/day/deliver", {}, {"text": "空 query 也能收"})
            self.assertEqual(200, delivered[0], delivered[1])
            self.assertEqual("expect", delivered[1]["state"])
            seen = day_api.dispatch("/day/pending", {}, None)
            self.assertEqual(200, seen[0])
            self.assertEqual(1, seen[1]["counts"]["expect"])
            acked = day_api.dispatch("/day/ack", {},
                                     {"item_id": delivered[1]["item"]["id"], "retcode": "0"})
            self.assertEqual(200, acked[0], acked[1])
            self.assertTrue(acked[1]["delivered"])


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root(self)

    def test_thresholds_come_from_the_data_file(self):
        cfg = delivery.load_config()
        for name in delivery.CONFIG_KEYS:
            self.assertIn(name, cfg)
            self.assertIsInstance(cfg[name], int)
        self.assertEqual(5, cfg["stuck_minutes"])

    def test_root_local_config_wins_and_bad_values_fall_back(self):
        (Path(self.root) / delivery.CONFIG_FILE).write_text(
            json.dumps({"stuck_minutes": "soon", "backoff_minutes": 1}), encoding="utf-8")
        cfg = delivery.load_config(self.root)
        self.assertEqual(5, cfg["stuck_minutes"])
        self.assertEqual(1, cfg["backoff_minutes"])
        self.assertEqual(32, cfg["backoff_cap_minutes"])


if __name__ == "__main__":
    unittest.main()
