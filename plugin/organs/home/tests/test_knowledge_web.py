"""网页卡与字幕卡共用同一个容器：这批用例看住"旧记录照旧、新来源不越界"。

每条断言都对着任务书里的判据，不按实现反推：旧件不许被迁移、缺 `fetched_at` 不许入库、
网页卡的说法不许变成"她的记忆"、加了网页卡不许搅乱字幕原有的排序与文案。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from organs.home import knowledge

META = {"bvid": "BV1xx411c7mD", "page": 1, "title": "行空间是什么", "owner": "示例作者",
        "url": "https://www.bilibili.com/video/BV1xx411c7mD"}
SUBTITLE_WORDING = "B站字幕提炼，视频观点未独立核验"
WEB_URL = "https://example.org/t1-schedule"


def subtitle_card(ident="card-a", title="行空间", claim="行空间是向量空间的一种直觉说法",
                  tags=("行空间", "线性代数")):
    return {"id": ident, "title": title, "claim": claim, "tags": list(tags),
            "evidence": [{"t": 1, "quote": claim, "url": META["url"] + "&t=1"}]}


def subtitle_job(ident, created_at, cards=None, meta=None):
    return {"id": ident, "created_at": created_at, "status": "done",
            "source": {"meta": dict(meta or META), "lines": []},
            "cards": [subtitle_card()] if cards is None else cards}


def web_card(ident="web-1", claim="T1 下一场是 11 月 10 日 15:00 对 Gen.G", tags=("T1", "赛程")):
    return {"id": ident, "claim": claim, "tags": list(tags), "evidence": [{"url": WEB_URL}]}


def web_job(ident, *, fetched_at="2026-11-09T10:00:00+09:00", published_at="2026-11-08",
            url=WEB_URL, cards=None, created_at=None):
    return {"id": ident, "created_at": created_at or fetched_at, "fetched_at": fetched_at,
            "source": {"kind": "web", "url": url, "site_name": "示例站",
                       "published_at": published_at, "query": "T1 赛程"},
            "cards": [web_card()] if cards is None else cards}


class KnowledgeLibraryMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "home"
        self.root.mkdir()
        self.library = knowledge.library_root(self.root)
        self.library.mkdir(parents=True)

    def put_raw(self, ident: str, payload: dict) -> Path:
        path = self.library / (ident + ".json")
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def stored_ids(self):
        return sorted(p.stem for p in self.library.glob("*.json"))


class LegacySubtitleTest(KnowledgeLibraryMixin):
    def test_record_without_kind_is_still_subtitle_and_keeps_old_wording(self):
        self.put_raw("a" * 32, subtitle_job("a" * 32, "2026-11-08T12:00:00+09:00"))

        hits = knowledge.search("行空间 是什么", root=self.root)
        self.assertEqual(1, len(hits))
        hit = hits[0]
        self.assertEqual("subtitle", hit["kind"])
        self.assertEqual("字幕", hit["source_type"])
        self.assertEqual(SUBTITLE_WORDING, hit["source"])
        self.assertEqual("示例作者", hit["owner"])
        self.assertEqual("行空间是什么", hit["video_title"])
        self.assertEqual(META["url"], hit["url"])
        self.assertEqual("card-a", hit["card_id"])
        self.assertIsNone(hit["fetched_at"])
        self.assertIsNone(hit["published_at"])

    def test_reading_old_record_does_not_rewrite_the_file(self):
        path = self.put_raw("a" * 32, subtitle_job("a" * 32, "2026-11-08T12:00:00+09:00"))
        before = path.read_bytes()

        self.assertEqual("subtitle", knowledge.read("a" * 32, root=self.root)["kind"])
        self.assertEqual("subtitle", knowledge.jobs(root=self.root)[0]["kind"])
        self.assertEqual(before, path.read_bytes(), "旧件只能在读侧补缺省，不许被就地改写")

    def test_written_subtitle_job_carries_explicit_kind(self):
        job = subtitle_job("b" * 32, "2026-11-08T12:00:00+09:00")
        knowledge.save(job, root=self.root)

        on_disk = json.loads((self.library / ("b" * 32 + ".json")).read_text(encoding="utf-8"))
        self.assertEqual("subtitle", on_disk["kind"])
        self.assertNotIn("kind", job, "写侧补的缺省不许反写调用方的对象")
        self.assertEqual(SUBTITLE_WORDING, knowledge.search("行空间", root=self.root)[0]["source"])


class WebTaskTest(KnowledgeLibraryMixin):
    def test_web_card_saves_into_the_same_container_and_is_recalled(self):
        knowledge.save_web(web_job("c" * 32), root=self.root)

        on_disk = json.loads((self.library / ("c" * 32 + ".json")).read_text(encoding="utf-8"))
        self.assertEqual("web", on_disk["kind"])
        self.assertEqual("web", on_disk["source"]["kind"])
        self.assertEqual("2026-11-09T10:00:00+09:00", on_disk["fetched_at"])
        self.assertNotIn("revision", on_disk)

        hits = knowledge.search("T1 赛程", root=self.root)
        self.assertEqual(1, len(hits))
        hit = hits[0]
        self.assertEqual("web", hit["kind"])
        self.assertEqual("网页", hit["source_type"])
        self.assertEqual(WEB_URL, hit["url"])
        self.assertEqual("2026-11-08", hit["published_at"])
        self.assertEqual("2026-11-09T10:00:00+09:00", hit["fetched_at"])
        self.assertEqual("web-1", hit["card_id"])
        self.assertEqual("示例站", hit["site_name"])
        self.assertIn("网页公开资料", hit["source"])
        self.assertIn("2026-11-08", hit["source"], "对外日期要报页面自己的时间")
        self.assertNotIn("我记得", hit["source"])

    def test_web_card_without_fetched_at_is_rejected_and_nothing_lands(self):
        job = web_job("c" * 32)
        job.pop("fetched_at")
        with self.assertRaises(ValueError):
            knowledge.save_web(job, root=self.root)
        self.assertEqual([], self.stored_ids(), "缺抓取时间的卡一张也不许入库")

        blank = web_job("d" * 32, fetched_at="   ")
        with self.assertRaises(ValueError):
            knowledge.save_web(blank, root=self.root)
        self.assertEqual([], self.stored_ids())

    def test_missing_url_or_cards_are_rejected(self):
        no_url = web_job("c" * 32)
        no_url["source"] = dict(no_url["source"], url="")
        with self.assertRaises(ValueError):
            knowledge.save_web(no_url, root=self.root)

        no_cards = web_job("c" * 32, cards=[])
        with self.assertRaises(ValueError):
            knowledge.save_web(no_cards, root=self.root)
        self.assertEqual([], self.stored_ids())

    def test_web_card_without_published_at_says_so_instead_of_borrowing_fetch_time(self):
        knowledge.save_web(web_job("c" * 32, published_at=None), root=self.root)

        hit = knowledge.search("T1 赛程", root=self.root)[0]
        self.assertIsNone(hit["published_at"])
        self.assertIn("页面未注明时间", hit["source"])
        self.assertNotIn("2026-11-09", hit["source"], "抓取时刻不许冒充页面时间说给人听")

    def test_newest_web_fetch_wins_and_old_one_is_not_recalled_twice(self):
        knowledge.save_web(web_job("c" * 32, fetched_at="2026-11-09T10:00:00+09:00",
                                   cards=[web_card(claim="T1 赛程是 11 月 10 日 15:00")]), root=self.root)
        knowledge.save_web(web_job("d" * 32, fetched_at="2026-11-09T12:00:00+09:00",
                                   cards=[web_card(claim="T1 赛程改成 11 月 11 日 15:00")]), root=self.root)

        hits = knowledge.search("T1 赛程", root=self.root)
        self.assertEqual(1, len(hits), "同一页重抓只留最新一张，不排两遍")
        self.assertEqual("d" * 32, hits[0]["job_id"])


class UnifiedViewTest(KnowledgeLibraryMixin):
    UNIFIED = ("kind", "source_type", "url", "published_at", "fetched_at", "card_id", "card",
               "job_id", "score", "matches", "source")

    def test_both_sources_answer_the_same_view_keys(self):
        knowledge.save(subtitle_job("a" * 32, "2026-11-08T12:00:00+09:00"), root=self.root)
        knowledge.save_web(web_job("c" * 32), root=self.root)

        by_kind = {hit["kind"]: hit for hit in knowledge.search("行空间 T1 赛程 线性代数", root=self.root, limit=5)}
        self.assertEqual({"subtitle", "web"}, set(by_kind))
        for kind, hit in by_kind.items():
            with self.subTest(kind=kind):
                for key in self.UNIFIED:
                    self.assertIn(key, hit)
                self.assertTrue(hit["card_id"])
                self.assertTrue(hit["card"]["claim"])

    def test_record_use_accepts_both_sources_and_keeps_the_old_row_shape(self):
        knowledge.save(subtitle_job("a" * 32, "2026-11-08T12:00:00+09:00"), root=self.root)
        knowledge.save_web(web_job("c" * 32), root=self.root)
        items = knowledge.search("行空间 T1 赛程 线性代数", root=self.root, limit=5)
        self.assertEqual({"subtitle", "web"}, {row["kind"] for row in items})

        knowledge.record_use(items, session_id="s1", delivery_id="d1", root=self.root)

        row = json.loads((self.library / "usage.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual("offered_to_model", row["kind"])
        self.assertEqual(("s1", "d1"), (row["session_id"], row["delivery_id"]))
        accounted = {card["source_kind"]: card for card in row["cards"]}
        self.assertEqual({"subtitle", "web"}, set(accounted))
        for item in items:
            # 旧账行的 job_id/card_id/matches 一格不改，新来源只多一格 source_kind。
            booked = accounted[item["kind"]]
            self.assertEqual(item["job_id"], booked["job_id"])
            self.assertEqual(item["card"]["id"], booked["card_id"])
            self.assertEqual(item["matches"], booked["matches"])

    def test_record_use_still_books_a_legacy_item_without_kind(self):
        legacy = {"job_id": "a" * 32, "card": subtitle_card(), "matches": ["行空"]}
        knowledge.record_use([legacy], session_id="s1", delivery_id="d1", root=self.root)

        row = json.loads((self.library / "usage.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual("subtitle", row["cards"][0]["source_kind"])
        self.assertEqual("card-a", row["cards"][0]["card_id"])


class BrokenFileTest(KnowledgeLibraryMixin):
    def test_web_job_without_url_is_skipped_not_fatal(self):
        self.put_raw("a" * 32, subtitle_job("a" * 32, "2026-11-08T12:00:00+09:00"))
        orphan = dict(web_job("c" * 32), kind="web",
                      source={"kind": "web", "site_name": "示例站", "query": "T1 赛程"})
        self.put_raw("c" * 32, orphan)

        hits = knowledge.search("行空间 T1 赛程 线性代数", root=self.root, limit=5)
        self.assertEqual(["a" * 32], [row["job_id"] for row in hits], "一张坏卡不许把整库召回拖陪")


class SubtitleUnaffectedTest(KnowledgeLibraryMixin):
    def setUp(self) -> None:
        super().setUp()
        knowledge.save(subtitle_job("a" * 32, "2026-11-08T12:00:00+09:00"), root=self.root)
        # 两条字幕任务来自两个视频：同 (bvid, page) 会被"只认最新那次"合并，那属于另一条判据。
        other = dict(META, bvid="BV1yy411c7mE", page=2, title="行空间与列空间")
        knowledge.save(subtitle_job("b" * 32, "2026-11-09T12:00:00+09:00", meta=other, cards=[
            subtitle_card("card-b", "行空间与列空间", "行空间和列空间是同一个矩阵的两种看法")]), root=self.root)

    def test_web_card_does_not_change_subtitle_order_or_wording(self):
        query = "行空间 列空间"
        before = knowledge.search(query, root=self.root, limit=5)

        knowledge.save_web(web_job("c" * 32, cards=[
            web_card(claim="行空间、列空间、线性代数在公开资料里的常见说法", tags=("行空间", "列空间"))]),
            root=self.root)
        after = knowledge.search(query, root=self.root, limit=5)

        def keep(rows):
            return [(row["job_id"], row["card_id"], row["score"], row["source"], row["owner"])
                    for row in rows if row["kind"] == "subtitle"]

        self.assertEqual(keep(before), keep(after), "加网页卡不许改字幕的分数、文案与排序")
        self.assertEqual(["b" * 32, "a" * 32], [row[0] for row in keep(after)])
        self.assertIn("web", {row["kind"] for row in after})

    def test_unrelated_web_card_is_not_recalled(self):
        before = len(knowledge.search("行空间", root=self.root, limit=5))
        knowledge.save_web(web_job("c" * 32), root=self.root)

        self.assertEqual(before, len(knowledge.search("行空间", root=self.root, limit=5)))
        self.assertEqual([], knowledge.search("今天吃什么", root=self.root))


if __name__ == "__main__":
    unittest.main()
