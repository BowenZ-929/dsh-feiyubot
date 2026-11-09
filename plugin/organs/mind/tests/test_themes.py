"""主题词表扩查询：只改「这一句怎么问」，一个字节记忆都不动。

判据（每条对应交付里的一句话）：

1. **命中主题才补词**：原句落在某个主题里 → 查询串以原句开头、接上**同一主题**的其它说法；
   别的主题的词一个都不加。
2. **不命中就原样查**：闲话（一个主题的词都没有）→ 返回值与入参逐字相同，不加词、不改写。
3. **空句 / 全是标点**：原样返回，不抛。
4. **去重、保序、受 max_terms 限制**：原句里已经有的词不再补一遍；词表里重复的词只加一次；
   查询串总条数不超过数据里的 max_terms（原句占一条）。
5. **数据驱动**：同一句话，换一份词表 → 查询串跟着变（代码一个字没动）；出厂词表本身能过校验。
6. **坏数据当场点名**：缺 themes / words 为空 / 主题重名 / max_terms 不是正整数 → ValueError，
   报错里写着是哪一格。
7. **装配侧真的用了它**：同一句话走 `answer()` 时传给脑 recall 的查询串就是
   `themes.expand(原句)`（用假脑截获参数断言），且这一轮**不改脑里的记忆**——记忆文件
   （longterm.jsonl / today.json）前后 sha256 逐条相同；一个只截参数的假脑放在临时目录里，
   整个目录前后逐文件 sha256 也相同（这一层没有往脑里写过一个字节）。

接在 `answer()` 上的那两条走的是装配的真实路径（魂用 fixtures、心与嘴的根切到临时目录），
假 transport 兜住模型那一头；仓库里的真数据一个字节不写。
"""
from __future__ import annotations

import hashlib
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
from organs.mind import conversation, themes
from organs.soul import load as load_soul

WHO = "3425635473"          # 脑里的 who 是数字号（WHO_RE），昵称只作 display
PLUGIN = Path(__file__).resolve().parents[3]
SOUL_FIXTURES = PLUGIN / "organs" / "soul" / "fixtures"

CONFIG = {"provider": "fake", "model": "fake-model", "base_url": "http://example.invalid/v1",
          "key_env": "QQAGENT_LLM_KEY", "timeout_ms": 1000, "retries": 0,
          "temperature": 0.0, "max_tokens": 200}
ENV = {"QQAGENT_LLM_KEY": "test-key"}

BASE = datetime(2025, 1, 15, 14, 0, 0, tzinfo=datetime.now().astimezone().tzinfo)
SENTENCE = "记得，那回是我不对。"
SAID = "咱们以前闹过别扭吗"
ASIDE = "今天的菜市场怎么样"
QUOTED = "咋啦，又生气了。表情包不就是随便发发嘛"

#: 一份最小可用的词表：两个主题，词都是这一层要用的判据。
DATA = {
    "version": 1,
    "max_terms": 8,
    "themes": [
        {"name": "别扭", "words": ["别扭", "拌嘴", "吵嘴", "又生气了", "生气", "赌气"]},
        {"name": "想念", "words": ["想你", "惦记"]},
    ],
}
#: 记忆里长什么样、词表里就得能补上什么样：这条是「换说法」的验收样本（与词表无关）。
MEMORY_LINE = QUOTED


def _at(minutes: float = 0.0, base: datetime = BASE) -> str:
    return heart_model.iso(base + timedelta(minutes=minutes))


def _transport(reply: str):
    def send(url, headers, payload, timeout):
        body = {"choices": [{"message": {"content": reply}}]}
        return 200, json.dumps(body, ensure_ascii=False).encode("utf-8")
    return send


def _write_data(root: Path, data: object) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "themes.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return root


def _dir_sha(root: Path) -> list:
    """整个目录逐文件 sha256（相对路径 + 内容摘要），前后一比就知道写没写过盘。"""
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rows.append((str(path.relative_to(root)),
                         hashlib.sha256(path.read_bytes()).hexdigest()))
    return rows


class _CaptureBrain:
    """照真脑跑，只把 recall 收到的查询串记下来（装配侧断言「用的就是展开后的那串」）。"""

    def __init__(self, real: BrainStore) -> None:
        self._real = real
        self.queries = []

    def recall(self, query: str, who: str = None, scene: str = None):
        self.queries.append(query)
        try:
            return self._real.recall(query=query, who=who, scene=scene)
        except TypeError:        # 老脑 / 两参脑：真脑支持 scene，这里只为守住同一条兜底
            return self._real.recall(query=query, who=who)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _RecordOnlyBrain:
    """只截参数、一个字节都不落盘的假脑：用来证明「召回这一步不写脑」。"""

    def __init__(self) -> None:
        self.queries = []

    def recall(self, query: str, who: str = None, scene: str = None):
        self.queries.append(query)
        return {"items": []}


class ExpandTest(unittest.TestCase):
    """themes.expand 的手艺：补哪些词、补几条、什么时候一个词都不补。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _expand(self, said: str, data: object = None) -> str:
        root = _write_data(Path(tempfile.mkdtemp(dir=str(self.tmp))), data or DATA)
        return themes.expand(said, root=root)

    # —— 1) 命中主题：补同一主题的其它词 ————————————————————————

    def test_hit_appends_the_other_words_of_the_same_theme(self):
        got = self._expand(SAID)
        terms = got.split(" ")
        self.assertEqual(SAID, terms[0], "查询串必须以原句开头")
        for word in ("拌嘴", "吵嘴", "又生气了", "生气", "赌气"):
            self.assertIn(word, terms, "同一主题的说法没补进来：%s → %r" % (word, got))
        for word in ("想你", "惦记"):
            self.assertNotIn(word, terms, "别的主题的词混进来了：%s → %r" % (word, got))

    def test_every_theme_hit_contributes_in_data_order(self):
        data = {"version": 1, "max_terms": 8,
                "themes": [{"name": "别扭", "words": ["别扭", "拌嘴"]},
                           {"name": "想念", "words": ["想你", "惦记"]}]}
        got = self._expand("闹别扭之后又想你了", data)
        self.assertEqual(["闹别扭之后又想你了", "拌嘴", "惦记"], got.split(" "),
                         "两个主题都命中时按数据顺序接、各自去掉原句里已有的词：%r" % (got,))

    # —— 2) 不命中：原样查 ————————————————————————————————

    def test_a_sentence_that_hits_no_theme_is_queries_as_given(self):
        self.assertEqual(ASIDE, self._expand(ASIDE), "一个主题都没命中却动了查询串")

    # —— 3) 空句 / 全是标点 ———————————————————————————————

    def test_blank_and_punctuation_only_are_returned_as_given(self):
        for said in ("", "   ", "？！……", "。，、"):
            with self.subTest(said=said):
                self.assertEqual(said, self._expand(said), "空句/标点句该原样返回")

    # —— 4) 去重、保序、条数上限 ———————————————————————————————

    def test_words_already_in_the_sentence_are_not_appended_twice(self):
        got = self._expand("他又生气了，我不理他")
        terms = got.split(" ")
        self.assertEqual("他又生气了，我不理他", terms[0])
        self.assertNotIn("又生气了", terms[1:], "原句里已经有的说法又补了一遍：%r" % (got,))
        self.assertEqual(1, got.count("生气"), "同一个说法在查询串里出现了两次：%r" % (got,))

    def test_duplicate_words_collapse(self):
        data = {"version": 1, "max_terms": 8,
                "themes": [{"name": "别扭", "words": ["别扭", "拌嘴", "拌嘴", "吵嘴"]}]}
        got = self._expand("闹别扭了", data)
        self.assertEqual(["闹别扭了", "拌嘴", "吵嘴"], got.split(" "),
                         "重复的词只该出现一次：%r" % (got,))

    def test_max_terms_caps_the_query(self):
        data = {"version": 1, "max_terms": 3,
                "themes": [{"name": "别扭", "words": ["别扭", "拌嘴", "吵嘴", "生气", "赌气"]}]}
        got = self._expand("闹别扭了", data)
        self.assertEqual(3, len(got.split(" ")), "查询串超了 max_terms：%r" % (got,))
        self.assertEqual(["闹别扭了", "拌嘴", "吵嘴"], got.split(" "),
                         "上限先按数据顺序截断，不是随机挑：%r" % (got,))

    # —— 5) 数据驱动 ——————————————————————————————————

    def test_changing_the_word_list_changes_the_query(self):
        narrow = {"version": 1, "max_terms": 8,
                  "themes": [{"name": "别扭", "words": ["别扭", "拌嘴"]}]}
        wide = {"version": 1, "max_terms": 8,
                "themes": [{"name": "别扭", "words": ["别扭", "拌嘴", "赌气", "翻脸"]}]}
        first = self._expand("闹别扭了", narrow)
        second = self._expand("闹别扭了", wide)
        self.assertNotEqual(first, second, "改了词表查询串却没变：%r / %r" % (first, second))
        self.assertEqual(["闹别扭了", "拌嘴", "赌气", "翻脸"], second.split(" "))

    def test_the_factory_word_list_is_valid_and_has_the_awkward_theme(self):
        conf = themes.load_config(root=PLUGIN / "organs" / "mind" / "data")
        names = [theme["name"] for theme in conf["themes"]]
        self.assertIn("别扭", names, "出厂词表里没有「别扭」这个主题：%r" % (names,))
        self.assertGreaterEqual(len(names), 4, "主题太少（口子是给日常说法用的）：%r" % (names,))
        self.assertTrue(conf["max_terms"] >= 2, "max_terms 太小，扩了等于没扩")

    def test_the_shipped_data_expands_the_awkward_phrase(self):
        got = themes.expand(SAID, root=PLUGIN / "organs" / "mind" / "data")
        self.assertEqual(SAID, got.split(" ")[0])
        self.assertGreater(len(got.split(" ")), 1, "出厂词表没扩出一句「闹别扭」：%r" % (got,))

    # —— 6) 坏数据当场点名 ————————————————————————————————

    def test_bad_data_raises_value_error_and_names_the_cell(self):
        cases = {
            "缺 themes": ({"version": 1, "max_terms": 8}, "themes"),
            "themes 为空": ({"version": 1, "max_terms": 8, "themes": []}, "themes"),
            "缺 version": ({"max_terms": 8, "themes": [{"name": "别扭", "words": ["别扭"]}]},
                           "version"),
            "max_terms 是 0": ({"version": 1, "max_terms": 0,
                                "themes": [{"name": "别扭", "words": ["别扭"]}]},
                               "max_terms"),
            "words 为空": ({"version": 1, "max_terms": 8,
                            "themes": [{"name": "别扭", "words": []}]}, "words"),
            "words 里有空词": ({"version": 1, "max_terms": 8,
                                "themes": [{"name": "别扭", "words": ["别扭", "  "]}]},
                               "空词"),
            "缺 name": ({"version": 1, "max_terms": 8, "themes": [{"words": ["别扭"]}]}, "name"),
            "主题重名": ({"version": 1, "max_terms": 8,
                          "themes": [{"name": "别扭", "words": ["别扭"]},
                                     {"name": "别扭", "words": ["拌嘴"]}]}, "重复"),
        }
        for label, (data, needle) in cases.items():
            with self.subTest(label=label):
                root = _write_data(Path(tempfile.mkdtemp(dir=str(self.tmp))), data)
                with self.assertRaises(ValueError) as caught:
                    themes.expand(SAID, root=root)
                self.assertIn(needle, str(caught.exception),
                              "%s：报错没点名该改哪一格：%s" % (label, caught.exception))

    def test_missing_data_file_raises_value_error(self):
        with self.assertRaises(ValueError) as caught:
            themes.expand(SAID, root=Path(tempfile.mkdtemp(dir=str(self.tmp))))
        self.assertIn("themes.json", str(caught.exception))

    def test_recall_hands_the_brain_the_expanded_query(self):
        """装配那一层的接缝：`_recall` 交给脑的就是展开后的那串，且只问一次。"""
        brain = _RecordOnlyBrain()
        recalled, text, _audit = conversation._recall(brain, SAID, WHO)
        self.assertEqual([], recalled)
        self.assertEqual(conversation.NO_MEMORY, text)
        self.assertEqual([themes.expand(SAID)], brain.queries, "传给脑的查询串不是扩展后的那串")
        self.assertNotEqual(SAID, brain.queries[0], "查询串没被扩开，等于这一层没接上")


class AnswerTest(unittest.TestCase):
    """装配侧：走 `answer()` 的同一句话，交给脑的就是展开后的查询串；记忆一个字节不动。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        patches = mock.patch.dict(os.environ, {
            "QQAGENT_HOME": str(self.tmp / "home"),          # 装配说明书/词表的实盘根：空着，走出厂
            "HEART_ROOT": str(self.tmp / "heart"),           # 心的状态文件：临时根
            "MOUTH_TRACE_DIR": str(self.tmp / "trace"),      # 弃言账本：临时根
        })
        patches.start()
        self.addCleanup(patches.stop)
        target = self.tmp / "soul"
        shutil.copytree(str(SOUL_FIXTURES / "valid"), str(target))
        self.store = load_soul(target)
        self.brain = BrainStore(self.tmp / "brain")

    def _append_memory(self) -> str:
        """一条长期档的记忆：ts 用脑自己的钟（写一条去年的会被遗忘曲线当场归档）。"""
        ts = brain_now_iso()
        entry_id = "t-themes-1"
        self.brain._append_longterm([{
            "id": entry_id, "day": ts[:10], "ts": ts, "who": WHO,
            "text": QUOTED, "quote": QUOTED, "count": 1, "status": "active",
            "source_type": "亲历", "alpha": 2, "beta": 3,
            "note": "", "unclear": False, "beta_note": None,
            "evidence_key": entry_id,
        }])
        return entry_id

    def _answer(self, brain: object = None) -> dict:
        return conversation.answer(SAID, WHO, store=self.store,
                                   brain=self.brain if brain is None else brain,
                                   now=_at(0), transport=_transport(SENTENCE),
                                   config=CONFIG, env=ENV)

    def test_answer_sends_the_expanded_query_to_recall(self):
        self._append_memory()
        capture = _CaptureBrain(self.brain)
        got = self._answer(brain=capture)

        self.assertEqual(1, len(capture.queries), "这一轮该只问一次脑")
        self.assertEqual(themes.expand(SAID), capture.queries[0],
                         "传给脑的查询串不是 themes.expand(原句)：%r" % (capture.queries[0],))
        self.assertNotEqual(SAID, capture.queries[0], "查询串没被扩开，等于这一层没接上")
        self.assertEqual([MEMORY_LINE],
                         [row["text"] for row in got["recalled"]],
                         "换说法没想起那句原话：%r" % (got["recalled"],))

    def test_the_recall_step_never_writes_the_memory_files(self):
        """只读回忆：长期记忆那一份前后逐字节相同（写的是脑自己的命中账 usage.jsonl，不是记忆）。"""
        self._append_memory()
        before = hashlib.sha256((self.tmp / "brain" / "longterm.jsonl").read_bytes()).hexdigest()
        recalled, _, _audit = conversation._recall(self.brain, SAID, WHO)
        after = hashlib.sha256((self.tmp / "brain" / "longterm.jsonl").read_bytes()).hexdigest()

        self.assertEqual([QUOTED], [row["text"] for row in recalled],
                         "换说法没想起那句原话：%r" % (recalled,))
        self.assertEqual(before, after, "召回这一步动了脑里的长期记忆")

    def test_the_turn_never_rewrites_the_longterm_memory(self):
        """走完整一轮：这一句与她那一句落进 today 账，长期记忆一分不动。"""
        self._append_memory()
        before = hashlib.sha256((self.tmp / "brain" / "longterm.jsonl").read_bytes()).hexdigest()
        self._answer()
        after = hashlib.sha256((self.tmp / "brain" / "longterm.jsonl").read_bytes()).hexdigest()
        self.assertEqual(before, after, "这一轮改写了长期记忆")

    def test_a_broken_word_list_falls_back_to_the_plain_sentence(self):
        """词表坏了只该少召回一点：`_recall` 兜住 ValueError，用原句照查，这一轮不许哑。"""
        live = self.tmp / "home" / "mind"
        live.mkdir(parents=True)
        (live / "themes.json").write_text('{"version": 1, "max_terms": 8, "themes": []}',
                                          encoding="utf-8")
        brain = _RecordOnlyBrain()
        conversation._recall(brain, SAID, WHO)
        self.assertEqual([SAID], brain.queries, "词表坏了没退回原句")

    def test_a_brain_directory_is_not_written_by_the_recall_step(self):
        """只截参数的假脑：整个目录前后逐文件 sha256 相同（这一层没往脑里写过一个字节）。"""
        root = self.tmp / "fake-brain"
        root.mkdir()
        (root / "longterm.jsonl").write_text("", encoding="utf-8")   # 空记忆：一个字节都不许改
        before = _dir_sha(root)
        brain = _RecordOnlyBrain()
        conversation._recall(brain, SAID, WHO)
        self.assertEqual(before, _dir_sha(root), "召回这一步往脑里写了东西")
        self.assertEqual(themes.expand(SAID), brain.queries[0])


if __name__ == "__main__":
    unittest.main()
