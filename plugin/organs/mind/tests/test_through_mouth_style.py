"""普通一轮的切泡按「谁触发」走：preference_of → style_policy.overlay → split。

改前：organs/mind/conversation.py 的 _bubbles 拿 warehouse.load("policy") 原样切，
普通一轮因此永远是默认旋钮（两条支路——边界话与冷却轮——早就按人走了，只有主路径没接上）。
改后：同一份默认 policy 先被这一轮 who 的偏好卡盖一次再切；覆盖规则不在 mind 里抄，
走 organs/mouth/style_policy.py 的 overlay（api/chat.py 也调它，判据只有一份）。

四条用例（每条对应验收里的一句话）：
1. **没卡 = 逐字没变**：切出来的每一泡与改前那条路（默认 policy 原样喂 split + pace）逐条相同，
   连间隔与发出时刻都比；这条是底线，接主路径不许顺手改了别人。
2. **max_chars=20**：每泡 ≤20 字，拼回去与原文逐字相等。
3. **chunks_per_reply=1**：同一段话恰好一泡，拼回去与原文逐字相等。
4. **两个都有**：字限赢（泡数 >1，不是 M=1 的一泡），且日志里如实留一句
   「本次忽略 chunks_per_reply：字限优先」——这一层没有回包，留痕只落日志，不塞进别处。

卡一律用家的真写口 add_preference 造在临时家根（HOME_ROOT）下，不 monkeypatch 家的读法；
一句话都不发网络、实盘一个字节不写。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List
from unittest import mock

from organs.home import add_preference
from organs.mind import conversation
from organs.mouth import warehouse
from organs.mouth.pace import fill as pace_fill
from organs.mouth.split import split as split_text

WHO = "10000000001"
AT = "2024-05-01T09:00:00"

# 112 字、八句：默认两句一泡是四泡，卡上每泡 20 字时能切出八泡（离防炸上限 12 还有余量）。
LONG = ("今天风大，河边的柳树一直往水里压。豆浆摊刚开锅，白汽一团团地冒。我买了一杯，烫得两手来回倒换。"
        "摊主说话慢，笑起来眼睛弯成缝。她说这河以前能看见鱼。现在只剩塑料瓶。"
        "太阳爬上来了，桥面亮得像薄铁皮。风一停，腥味就变成豆浆的甜味。")


class ThroughMouthStyleCase(unittest.TestCase):
    """一次一间空家：卡全造在临时家根里，一个人一张，后写的算。"""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="through-mouth-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {"HOME_ROOT": self.home})
        env.start()
        self.addCleanup(env.stop)

    def card(self, **knobs: Any) -> dict:
        """用家的真 API 造一张这个人的偏好卡（追加一行到 名片盒/prefs.jsonl）。"""
        return add_preference(root=None, role="human", by="human", who=WHO, **knobs)

    def texts(self, chunks: List[Dict[str, Any]]) -> List[str]:
        return [str(one["text"]) for one in chunks]

    def before(self, text: str) -> List[Dict[str, Any]]:
        """改前那条路逐字复刻：默认 policy 原样喂 split 与 pace，seed 同一个拼法。"""
        policy = warehouse.load("policy")
        batch = pace_fill(split_text(text, policy), policy,
                          "%s%s%s" % (WHO, conversation.SEED_SEP, AT))
        return [{"text": part.text, "gap_before": part.gap_before, "send_at": part.send_at}
                for part in batch.parts]

    def test_one_no_card_matches_the_path_before(self) -> None:
        """① 没卡：切出来的每一泡与改前逐条相同（段落、间隔、发出时刻都比）。"""
        chunks = conversation._bubbles(LONG, WHO, AT)
        self.assertEqual(chunks, self.before(LONG))
        self.assertGreater(len(chunks), 1)        # 默认两句一泡：四泡，不是一条

    def test_two_char_limit_caps_every_part(self) -> None:
        """② max_chars=20：每泡 ≤20 字，拼回去与原文逐字相等。"""
        self.card(max_chars=20)
        chunks = conversation._bubbles(LONG, WHO, AT)
        for text in self.texts(chunks):
            self.assertLessEqual(len(text), 20, text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(self.texts(chunks)), LONG)

    def test_three_chunks_per_reply_one_part(self) -> None:
        """③ chunks_per_reply=1：同一段话恰好一泡，字一个都不丢。"""
        self.card(chunks_per_reply=1)
        chunks = conversation._bubbles(LONG, WHO, AT)
        self.assertEqual(len(chunks), 1)
        self.assertEqual("".join(self.texts(chunks)), LONG)

    def test_four_char_limit_wins_and_leaves_a_log_line(self) -> None:
        """④ 两个都有：字限赢（泡数 >1）且日志里有一行「忽略 chunks_per_reply：字限优先」。"""
        self.card(max_chars=20, chunks_per_reply=1)
        with self.assertLogs("qqagent.mind", level="INFO") as logs:
            chunks = conversation._bubbles(LONG, WHO, AT)
        self.assertGreater(len(chunks), 1)                       # M=1 没生效，字限赢
        self.assertEqual("".join(self.texts(chunks)), LONG)
        rows = [row for row in logs.output if "chunks_per_reply" in row]
        self.assertEqual(len(rows), 1)
        self.assertIn("字限优先", rows[0])
        self.assertIn("忽略", rows[0])


if __name__ == "__main__":           # pragma: no cover
    unittest.main()
