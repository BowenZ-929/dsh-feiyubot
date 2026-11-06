"""素材盒不进装配：自动跑，不需要模型 key。

判据（人裁 2026-11-06）：草稿**不许**被自动塞进提示词——她不需要每轮都念自己的草稿。
做法：拿素材盒里真实卡的正文当探针，去撞 mind 的读口与装配配置；
再补一条源码级断言（mind 里根本不该出现「素材盒」）。两条都不碰模型，无 key 也能跑。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organs.home import cards, rooms

PLUGIN = Path(cards.__file__).resolve().parents[2]
MATERIALS = "素材盒"


class MaterialsNotInjectedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "home"
        for name in rooms.ROOM_NAMES:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / "access.json").write_text(
            json.dumps({"version": 1, "writable": list(rooms.DEFAULT_WRITABLE)}),
            encoding="utf-8")
        # 判据文件也从出厂那份抄过来：素材盒的 space_ready 依赖它
        (self.root / cards.SPACE_RULES_NAME).write_text(
            cards.space_rules_path(None).read_text(encoding="utf-8")
            if cards.space_rules_path(None).exists() else "{}", encoding="utf-8")

    def _draft_probe(self) -> str:
        text = "探针草稿：今天把窗台上的灰擦了一遍，抹布是凉的。"
        cards.add_note(root=self.root, role="agent", by="她", text=text, mood="平静")
        got = cards.list_room(root=self.root, room=MATERIALS)
        return got["items"][0]["params"]["text"]

    def test_draft_text_never_reaches_the_assembly_inputs(self) -> None:
        """撞 mind 的读口与装配配置：探针正文一个字都不该出现。"""
        probe = self._draft_probe()
        surfaces = []
        try:
            from organs.mind import conversation
            surfaces.append(json.dumps(conversation.load_assembly(), ensure_ascii=False))
        except Exception as exc:                       # 读不到就如实记，不静默跳过
            surfaces.append("assembly 读不到：%s: %s" % (type(exc).__name__, exc))
        from organs.home import anchors
        surfaces.append(json.dumps(anchors.read_all(), ensure_ascii=False))
        blob = "\n".join(surfaces)
        self.assertNotIn(probe, blob, "草稿正文出现在装配输入里了")
        self.assertNotIn(MATERIALS, blob, "素材盒整体都不该被装配读到")

    def test_mind_never_mentions_the_materials_room(self) -> None:
        """源码级：说话那条线里根本不该出现「素材盒」。"""
        mind = PLUGIN / "organs" / "mind"
        hits = []
        for path in mind.rglob("*.py"):
            if "tests" in path.parts:
                continue
            if MATERIALS in path.read_text(encoding="utf-8", errors="replace"):
                hits.append(str(path.relative_to(PLUGIN)))
        self.assertEqual([], hits, "mind 里提到了素材盒：%s" % hits)

    def test_chat_module_never_mentions_the_materials_room(self) -> None:
        for rel in ("api/chat.py",):
            text = (PLUGIN / rel).read_text(encoding="utf-8")
            self.assertNotIn(MATERIALS, text, rel)


if __name__ == "__main__":
    unittest.main()
