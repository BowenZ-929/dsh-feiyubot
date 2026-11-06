"""语气账与「不许截半句」这两条边的验收（装配那一层的用例在 test_conversation）。

判据：
1. **账只追加**：两轮两行，第一轮那行一个字节都不动（回头改账，人就没法事后核对）。
2. **坏行跳过**：不是 JSON / 不是对象的那一行跳过不炸，其余照读——记账的一份底稿读不下去，
   不该让谁闭口。
3. **一句都放不下就留整句**：回话只有一句、又比上限长，原样留着并记 truncated=false
   （截了才算截，没截不许在账上写「截了」）；上限那一栏照样收紧，人事后看得见这是哪一条。

全部注入假 transport：一个字节都不发真网络。心 / 嘴 / 装配的临时根都经环境变量切走，
仓库里的真数据一个字节不写。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from organs.brain import BrainStore
from organs.heart import energy
from organs.heart import model as heart_model
from organs.heart import mood
from organs.mind import conversation
from organs.soul import load as load_soul

PLUGIN = Path(__file__).resolve().parents[3]
SOUL_FIXTURES = PLUGIN / "organs" / "soul" / "fixtures"
HEART_DATA = PLUGIN / "organs" / "heart" / "data"
HEART_FACTORY = ("baseline-card.json", "engage_weights.json")

CONFIG = {"provider": "fake", "model": "fake-model", "base_url": "http://example.invalid/v1",
          "key_env": "QQAGENT_LLM_KEY", "timeout_ms": 1000, "retries": 0,
          "temperature": 0.0, "max_tokens": 200}
ENV = {"QQAGENT_LLM_KEY": "test-key"}

SENTENCE = "今天挺好的。你问医院那回事，我记得的，是陪你妈去复查对吧。"
BASE = datetime(2025, 1, 15, 14, 0, 0, tzinfo=datetime.now().astimezone().tzinfo)


def _at(minutes: float = 0.0) -> str:
    return heart_model.iso(BASE + timedelta(minutes=minutes))


def _transport(reply: str):
    def send(url, headers, payload, timeout):
        body = {"choices": [{"message": {"content": reply}}]}
        return 200, json.dumps(body, ensure_ascii=False).encode("utf-8")
    return send


class ToneLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        patches = mock.patch.dict(os.environ, {
            "QQAGENT_HOME": str(self.tmp / "home"),
            "HEART_ROOT": str(self.tmp / "heart"),
            "MOUTH_TRACE_DIR": str(self.tmp / "trace"),
        })
        patches.start()
        self.addCleanup(patches.stop)
        target = self.tmp / "soul"
        shutil.copytree(str(SOUL_FIXTURES / "valid"), str(target))
        self.store = load_soul(target)
        self.brain = BrainStore(self.tmp / "brain")

    def _answer(self, reply: str = SENTENCE, *, now: str = _at(5), heart_root=None):
        return conversation.answer("你还记得医院那回事吗？", "主人", store=self.store,
                                   brain=self.brain, now=now,
                                   transport=_transport(reply), config=CONFIG, env=ENV,
                                   heart_root=heart_root)

    def _heart_state(self, name: str, *, mood_now: float, energy_value: float):
        """一份心状态：两个状态文件写在这一刻（与 api 侧 _double_low 同一种写法）。"""
        root = self.tmp / ("heart_" + name)
        root.mkdir()
        for name_of_file in HEART_FACTORY:
            shutil.copy(str(HEART_DATA / name_of_file), str(root / name_of_file))
        when = _at(5)
        (root / mood.MOOD_FILE).write_text(
            json.dumps({"now": mood_now, "baseline": 60.0, "updated_at": when}),
            encoding="utf-8")
        (root / energy.ENERGY_FILE).write_text(
            json.dumps({"value": energy_value, "updated_at": when, "tier": "一般"}),
            encoding="utf-8")
        return root

    def test_the_ledger_is_append_only(self):
        """两轮两行，第一轮那行一个字节都不动。"""
        self._answer(now=_at(5))
        first = conversation.tone_log_path().read_text(encoding="utf-8")
        self._answer(now=_at(6))
        second = conversation.tone_log_path().read_text(encoding="utf-8")
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith(first), "第二行把第一行改了：不是追加")
        self.assertEqual(2, len([line for line in second.splitlines() if line.strip()]))

    def test_bad_lines_are_skipped_while_reading(self):
        """坏行（不是 JSON / 不是对象 / 空行）跳过不炸，好行照读。"""
        self._answer()
        path = conversation.tone_log_path()
        good = path.read_text(encoding="utf-8")
        path.write_text("这不是 JSON\n" + good + "[1, 2, 3]\n\n",
                        encoding="utf-8")
        rows = conversation.read_tone_log()
        self.assertEqual(1, len(rows), "坏行没跳过：%r" % (rows,))
        self.assertEqual(set(conversation.TONE_FIELDS), set(rows[0]))

    def test_one_over_long_sentence_is_kept_whole_not_half_cut(self):
        """整段就是一句、又比上限长：留整句，账上记 truncated=false（没截就不许说截了）。"""
        rules = conversation.load_assembly()["tone_rules"]
        root = self._heart_state("cold", mood_now=20.0, energy_value=80.0)
        one = ("你问医院那回事，我记得的，是陪你妈去复查对吧，那天还下了很大的雨，"
               "我把伞落在车上了，回去的时候衣服全湿了，你就笑我记性不好")
        self.assertGreater(len(one), rules["max_chars_low"], "这句本来就不长，验不出这一条")
        got = self._answer(reply=one, heart_root=root)
        self.assertLess(got["tone"]["max_chars"], len(one), "上限没收紧，验不出这一条")
        self.assertEqual(one, got["reply"], "一句都放不下时不许锯半句")
        self.assertFalse(got["tone"]["truncated"], "没截却写了截了")
        row = conversation.read_tone_log()[-1]
        self.assertFalse(row["truncated"])
        self.assertEqual(got["tone"]["max_chars"], row["max_chars"])


if __name__ == "__main__":
    unittest.main()
