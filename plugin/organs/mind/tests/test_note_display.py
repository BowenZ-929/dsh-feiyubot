"""召回时标明这是她的理解：note_display 必须落到提示词里（原文一行、理解一行）。

判据（每条对应交付里的一句话）：
1. **带 note 的记忆**：问一句能召回它的话 → 提示词里原文行与「她理解：…」行都在，
   recalled[0]["note_display"] 有值（返回体七键照旧，只多这一栏）。
2. **原话空、只有她的理解**：第一行写「（无原话，只有她的理解）」再接来源括号，
   第二行照旧 note_display（别把这条记忆整条吞掉，它也是想起过的一件事）。
3. **没有理解**：脑给的「（无理解）」也照写（空 note 是正常状态，得说出来是「没理解」）；
   而 note_display 是空串时（脑没给这一栏）不许在提示词里多出空行。

记忆全走脑自己的写入路径，不手搓盘上的 JSON：
  · 带 note 的那条用 brain.ingest(note=...)（store.py 的两栏写入口）；
  · 原话空、note 有值那一态脑的公开写入口不收（ingest / import_rows 都要求 text 非空），
    故走脑里更底层的 store._append_longterm，行格式照 store._normalize_row 的两栏口径
    （text 与 quote 同为空，note 有值）。

全部注入假 transport：一个字节都不发真网络。脑 / 心 / 嘴 / 装配的根都在临时目录里，
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
from organs.brain.model import now_iso as brain_now_iso
from organs.heart import model as heart_model
from organs.mind import conversation
from organs.soul import load as load_soul

WHO = "3425635473"   # 脑里的 who 现在是数字号（WHO_RE），昵称只作 display
PLUGIN = Path(__file__).resolve().parents[3]
SOUL_FIXTURES = PLUGIN / "organs" / "soul" / "fixtures"

CONFIG = {"provider": "fake", "model": "fake-model", "base_url": "http://example.invalid/v1",
          "key_env": "QQAGENT_LLM_KEY", "timeout_ms": 1000, "retries": 0,
          "temperature": 0.0, "max_tokens": 200}
ENV = {"QQAGENT_LLM_KEY": "test-key"}

QUESTION = "你还记得医院那回事吗？"
MEMORY_TEXT = "我上次去医院是陪我妈复查"
NOTE = "他以为是去体检"
SENTENCE = "记得，陪你妈去复查对吧。"
NO_QUOTE = "（无原话，只有她的理解）"
UNDERSTOOD = "（无理解）"
SEVEN_KEYS = ("id", "text", "source", "source_type", "evidence_key", "score", "who")

BASE = datetime(2025, 1, 15, 14, 0, 0, tzinfo=datetime.now().astimezone().tzinfo)


def _at(minutes: float = 0.0, base: datetime = BASE) -> str:
    return heart_model.iso(base + timedelta(minutes=minutes))


def _transport(reply: str, captured: list = None):
    """假 transport：拿一句话当模型应答，顺手把请求体记下来（读提示词用）。"""
    def send(url, headers, payload, timeout):
        if captured is not None:
            captured.append(payload)
        body = {"choices": [{"message": {"content": reply}}]}
        return 200, json.dumps(body, ensure_ascii=False).encode("utf-8")
    return send


def _section(text: str, label: str) -> str:
    """提示词里某一节的正文：从「[标题]」到下一个空行分节为止（段与段之间是空行）。"""
    head = text.index("[%s]" % label) + len(label) + 2
    tail = text.find("\n\n[", head)
    body = text[head:tail] if tail >= 0 else text[head:]
    return body[1:] if body.startswith("\n") else body   # 标题后面那个换行不算正文


class _BlankNoteDisplayBrain:
    """照真脑跑，只把 recall 结果里的 note_display 抹成空串（脑没给这一栏那一态）。"""

    def __init__(self, real: BrainStore) -> None:
        self._real = real

    def recall(self, query: str, who: str = None):
        result = self._real.recall(query=query, who=who)
        for item in result.get("items") or []:
            item["note_display"] = ""
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


class NoteDisplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        patches = mock.patch.dict(os.environ, {
            "QQAGENT_HOME": str(self.tmp / "home"),          # 装配说明书的实盘根：空着，走出厂
            "HEART_ROOT": str(self.tmp / "heart"),           # 心的状态文件：临时根
            "MOUTH_TRACE_DIR": str(self.tmp / "trace"),      # 弃言账本：临时根
        })
        patches.start()
        self.addCleanup(patches.stop)
        target = self.tmp / "soul"
        shutil.copytree(str(SOUL_FIXTURES / "valid"), str(target))
        self.store = load_soul(target)
        self.brain = BrainStore(self.tmp / "brain")

    def _answer(self, *, brain: object = None, captured: list = None, **extra) -> dict:
        capture = captured if captured is not None else []
        return conversation.answer(QUESTION, WHO, store=self.store,
                                   brain=self.brain if brain is None else brain,
                                   now=_at(5), transport=_transport(SENTENCE, capture),
                                   config=CONFIG, env=ENV, **extra)

    def _memory_section(self, captured: list) -> str:
        system = captured[0]["messages"][0]["content"]
        return _section(system, conversation.load_assembly()["sections"]["memory"]["label"])

    def _append_quote_less_row(self, note: str, ts: str) -> str:
        """原话空、note 有值的一行：走脑里更底层的长期档写入（ingest 不收空 text）。

        ts 用脑自己的钟：核调脑的 recall 时不传 now_ts（脑按此刻结算遗忘曲线），
        写一条去年的行会被判成「太久没用」当场归档，这一轮就召回不到了。
        """
        entry_id = "t-quoteless-1"
        self.brain._append_longterm([{
            "id": entry_id, "day": ts[:10], "ts": ts, "who": WHO,
            "text": "", "quote": "", "count": 1, "status": "active",
            "source_type": "亲历", "alpha": 2, "beta": 3,
            "note": note, "unclear": False, "beta_note": 2,
            "evidence_key": entry_id,
        }])
        return entry_id

    # —— 1) 带 note：原文行与「她理解：…」行都在 ————————————————

    def test_memory_with_note_shows_both_lines_and_the_field(self):
        self.brain.ingest(who=WHO, text=MEMORY_TEXT, note=NOTE, ts=_at(0))
        capture: list = []
        got = self._answer(captured=capture)

        self.assertTrue(got["recalled"], "这一轮该想起医院那回事")
        self.assertEqual("她理解：" + NOTE, got["recalled"][0]["note_display"])
        for key in SEVEN_KEYS:
            self.assertIn(key, got["recalled"][0], "原有那一键被挤掉了：%s" % key)

        section = self._memory_section(capture)
        self.assertIn(MEMORY_TEXT, section, "原文那行没进提示词")
        self.assertIn("\n  " + "她理解：" + NOTE, section,
                      "她的理解没另起一行（缩进两格）地写进提示词")
        self.assertIn("（来源：", section, "来源括号不见了")
        self.assertLess(section.index(MEMORY_TEXT), section.index("她理解：" + NOTE),
                        "理解那行跑到了原文前面")

    # —— 2) 原话空、只有她的理解 ————————————————————————————

    def test_quote_less_memory_says_so_on_the_first_line(self):
        entry_id = self._append_quote_less_row("他说九点做手术，她以为是明天上午", brain_now_iso())
        capture: list = []
        got = self._answer(captured=capture)

        self.assertEqual([entry_id], [row["id"] for row in got["recalled"]],
                         "原话空的那一行没被召回：%r" % (got["recalled"],))
        row = got["recalled"][0]
        self.assertEqual("", row["text"])
        self.assertEqual("她理解：他说九点做手术，她以为是明天上午", row["note_display"])

        section = self._memory_section(capture)
        self.assertIn("- %s（来源：" % NO_QUOTE, section,
                      "原话空时第一行没写明「只有她的理解」")
        self.assertIn("\n  她理解：他说九点做手术，她以为是明天上午", section,
                      "原话空时理解那行没写出来")

    # —— 3) 空 note 与「脑没给这一栏」两态 ——————————————————————

    def test_empty_note_says_no_understanding_and_never_leaves_a_blank_line(self):
        self.brain.ingest(who=WHO, text=MEMORY_TEXT, ts=_at(0))
        capture: list = []
        got = self._answer(captured=capture)
        self.assertEqual(UNDERSTOOD, got["recalled"][0]["note_display"])
        self.assertIn("\n  " + UNDERSTOOD, self._memory_section(capture),
                      "空 note 也得说出「没理解」，不许吞掉")

        blank: list = []
        self._answer(brain=_BlankNoteDisplayBrain(self.brain), captured=blank)
        section = self._memory_section(blank)
        self.assertNotIn("\n  ", section, "note_display 是空串时多出了一行空行")
        self.assertTrue(all(line.startswith("- ") for line in section.splitlines()),
                        "note_display 空串时提示词的记忆段每行都该是原文行：%r" % (section,))


if __name__ == "__main__":
    unittest.main()
