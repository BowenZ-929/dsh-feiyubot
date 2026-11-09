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
from datetime import datetime, timedelta, timezone
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
    def test_structured_body_retrieves_topic_without_sender_prefix_crowding(self):
        from organs.mind import recent
        from organs.home import knowledge
        at=datetime.fromisoformat(brain_now_iso())
        speaker='测试用的长昵称同学('+WHO+')'
        body='天文馆怎么安排的'
        formatted=speaker+'：'+body
        for cross_day in (False,True):
            with self.subTest(cross_day=cross_day):
                brain=BrainStore(self.tmp/('speaker-query-'+str(cross_day)))
                start=at-timedelta(days=int(cross_day),hours=1)
                topic=brain.ingest(WHO,'天文馆周六下午去。',ts=start.isoformat())['entry']
                for i in range(7):
                    brain.ingest(WHO,speaker+'：好的'+str(i)+'。',ts=(start+timedelta(minutes=i+1)).isoformat())
                captured=[];root=self.tmp/('speaker-recent-'+str(cross_day))
                with mock.patch.object(knowledge,'search',return_value=[]) as search:
                    got=conversation.answer(formatted,WHO,store=self.store,brain=brain,now=at.isoformat(),
                        segment=[{'speaker':speaker,'who':WHO,'text':body}],session_id='private:query',recent_root=root,
                        transport=_transport('周六下午。',captured),config=CONFIG,env=ENV)
                self.assertEqual(body,got['_retrieval_query'])
                self.assertEqual(body,got['memory_recall']['query'])
                search.assert_called_once_with(body)
                self.assertEqual([topic['id']],[r['id'] for r in got['recalled']])
                self.assertIn(topic['quote'],self._memory_section(captured))
                stored=recent.load(root=root)['turns'][-1]
                self.assertEqual(formatted,stored['said'])
                self.assertEqual(body,stored['utterances'][0]['text'])
                self.assertEqual(WHO,stored['utterances'][0]['who'])

    def test_query_keeps_all_bodies_and_explicit_names_numbers(self):
        from organs.home import knowledge
        segment=[{'speaker':'甲(100001)','who':'100001','text':'林同学的号码是100002，去天文馆。'},
                 {'speaker':'乙('+WHO+')','who':WHO,'text':'我周六一起去。'}]
        formatted='\n'.join(r['speaker']+'：'+r['text'] for r in segment)
        expected='\n'.join(r['text'] for r in segment)
        with mock.patch.object(knowledge,'search',return_value=[]) as search:
            got=conversation.answer(formatted,WHO,store=self.store,brain=self.brain,segment=segment,
                session_id='',transport=_transport('嗯。'),config=CONFIG,env=ENV)
        self.assertEqual(expected,got['_retrieval_query'])
        search.assert_called_once_with(expected)
        self.assertIn('林同学',got['_retrieval_query'])
        self.assertIn('100002',got['_retrieval_query'])

    def test_group_private_source_is_not_read_or_added_to_excerpt_audit(self):
        self.brain.ingest(WHO, '医院的事情。'+'私人细节。'*200, audience='私密', ts=brain_now_iso())
        rows, _text, audit=conversation._recall(self.brain, '医院', WHO, 'group', cap=900,
            reader=lambda *args:self.fail('private source must not be sent to reader in group'))
        self.assertTrue(rows)
        self.assertFalse(rows[0]['retellable'])
        self.assertNotIn('source_read', audit['selected'][0])

    def test_source_read_is_bounded_and_failed_read_does_not_claim_no_memory(self):
        at = datetime.fromisoformat(brain_now_iso())
        for i in range(2):
            self.brain.ingest(WHO, '周末安排。' + '整理纸张。'*200 + f'最后改去图书馆{i}。',
                              ts=(at-timedelta(minutes=i+1)).isoformat())
        calls = []
        def read(item, query, budget):
            calls.append(item['id'])
            return {'status':'invalid'}
        rows, text, audit = conversation._recall(self.brain, '周末安排', WHO, cap=900, reader=read)
        self.assertEqual(1, len(calls))
        self.assertFalse(any(r['in_prompt'] for r in rows))
        self.assertIn('未能完整读取', text)
        self.assertNotIn('这次没想起什么', text)
        self.assertEqual('invalid', audit['selected'][0]['source_read']['status'])

    def test_source_read_skips_disabled_memory_fixed_receipt_and_short_original(self):
        at = brain_now_iso()
        for mode in ('disabled', 'fixed', 'short'):
            with self.subTest(mode=mode):
                brain = BrainStore(self.tmp/('read-mode-'+mode))
                brain.ingest(WHO, '周末安排：去书店。' + ('整理纸张。'*200 if mode!='short' else ''), ts=at)
                assembly = conversation.load_assembly()
                if mode=='disabled':
                    assembly['order'].remove('memory')
                captured=[]
                result={'kind':None, 'status':'ok', 'who':WHO, 'note':'普通提问', 'evidence':''}
                if mode=='fixed':
                    result['reminder']={'action':'create'}
                with mock.patch.object(conversation.remind, 'handle', return_value={'say':'已记下。','status':'ok'}):
                    conversation.answer('周末安排', WHO, store=self.store, brain=brain, now=at,
                        assessor=lambda **_:result, assembly=assembly, session_id='',
                        transport=_transport('嗯。', captured), config=CONFIG, env=ENV)
                self.assertEqual(0 if mode=='fixed' else 1, len(captured))

    def test_long_memory_is_read_once_as_grounded_excerpt_in_semantic_chat(self):
        from organs.mind import llm, memory_read, recent
        at = datetime.fromisoformat(brain_now_iso())
        correction = '最后决定去河岸图书馆，不去旧街书店。'
        quote = '周末原本想去旧街书店。' + '整理旧纸张。' * 200 + correction
        saved = self.brain.ingest(WHO, quote, ts=(at-timedelta(minutes=1)).isoformat())['entry']
        captured = []
        def send(url, headers, payload, timeout):
            captured.append(payload)
            content = (json.dumps({'quote':[correction], 'note':[]}, ensure_ascii=False)
                       if payload['messages'][0]['content'] == memory_read.PROMPT else '去河岸图书馆。')
            return 200, json.dumps({'choices':[{'message':{'content':content}}]}, ensure_ascii=False).encode()
        with mock.patch.dict(os.environ, {'QQAGENT_TRACE':'1','QQAGENT_HOME_ROOT':str(self.tmp/'read-trace')}), \
             llm.trace_round('a'*32, 'private:source-read'):
            got = conversation.answer('周末最后去哪儿', WHO, store=self.store, brain=self.brain,
                now=at.isoformat(), assessor=lambda **_: {'kind':None, 'status':'ok', 'who':WHO, 'note':'普通提问', 'evidence':''},
                session_id='private:source-read', recent_root=self.tmp/'read-recent',
                transport=send, config=CONFIG, env=ENV)
        trace=[json.loads(line) for p in (self.tmp/'read-trace/mind/trace').glob('*.jsonl') for line in p.read_text().splitlines()]
        self.assertEqual(['回读长记忆原文','生成回复'], [r['purpose'] for r in trace])
        self.assertEqual([1,2], [r['call_index'] for r in trace])
        self.assertTrue(all(r['turn_id']=='a'*32 for r in trace))
        self.assertEqual(2, len(captured), 'one read plus one main reply; no retries or second recall')
        self.assertEqual(quote, json.loads(captured[0]['messages'][1]['content'])['quote'])
        section = self._memory_section(captured[1:])
        self.assertIn(correction, section)
        self.assertIn('未展示全文', section)
        self.assertLessEqual(len(section), 900)
        row = next(r for r in got['recalled'] if r['id']==saved['id'])
        self.assertTrue(row['in_prompt'])
        self.assertEqual(quote, row['display_text'], 'original display is not replaced by excerpt')
        audit = got['memory_recall']['selected'][0]
        self.assertEqual('included_excerpt', audit['reason'])
        self.assertEqual('ok', audit['source_read']['status'])
        stored = recent.load(root=self.tmp/'read-recent')['turns'][-1]
        self.assertEqual(got['memory_recall'], stored['memory_recall'])
        self.assertEqual(quote, next(r for r in self.brain.today_entries() if r['id']==saved['id'])['quote'])

    @mock.patch(__name__ + ".brain_now_iso", new=lambda: "2026-11-08T12:00:00+09:00")
    @mock.patch("organs.brain.store.now_iso", new=lambda: "2026-11-08T12:00:00+09:00")
    def test_grounded_context_expands_one_retrieval_without_replacing_original_words(self):
        from organs.mind import appraisal, recent
        from organs.home import knowledge
        at = datetime.fromisoformat(brain_now_iso())
        for previous_who, valid in ((WHO, True), ('999000001', False)):
            with self.subTest(previous_who=previous_who):
                brain = BrainStore(self.tmp / ('brain-' + previous_who))
                old = brain.ingest(WHO, '无糖茶不加冰', ts=(at-timedelta(minutes=30)).isoformat())['entry']
                original = dict(old)
                root = self.tmp / ('recent-' + previous_who)
                recent.append({'ts':(at-timedelta(minutes=2)).isoformat(), 'who':previous_who,
                               'said':'我买无糖茶', 'reply':'嗯。', 'session_id':'private:context'}, root=root)
                captured = []
                def send(url, headers, payload, timeout):
                    captured.append(payload)
                    content = (json.dumps({'kind':None, 'evidence':'', 'reason':'承接同一人的饮料选择',
                        'memory_note':{'text':'继续买无糖茶', 'current':['饮料照旧'], 'previous':['我买无糖茶']}}, ensure_ascii=False)
                        if 'memory_note' in payload['messages'][0]['content'] else '好。')
                    return 200, json.dumps({'choices':[{'message':{'content':content}}]}, ensure_ascii=False).encode()
                with mock.patch.object(brain, 'recall', wraps=brain.recall) as recall, \
                     mock.patch.object(knowledge, 'search', return_value=[]) as search:
                    got = conversation.answer('饮料照旧', WHO, store=self.store, brain=brain,
                        now=at.isoformat(), assessor=appraisal.assess, session_id='private:context', recent_root=root,
                        config=CONFIG, env=ENV, transport=send)
                query = got['memory_recall']['query']
                self.assertEqual('饮料照旧\n继续买无糖茶' if valid else '饮料照旧', query)
                self.assertEqual(1, recall.call_count)
                search.assert_called_once_with(query)
                self.assertEqual(query, got['_retrieval_query'])
                self.assertEqual(valid, old['id'] in [r['id'] for r in got['recalled']])
                self.assertEqual(2, len(captured), 'only existing interpretation and reply calls')
                if valid:
                    self.assertIn('无糖茶不加冰', captured[1]['messages'][1]['content'])
                stored = recent.load(root=root)['turns'][-1]
                self.assertEqual('饮料照旧', stored['said'])
                human = next(r for r in brain.today_entries() if r['id']==stored['msg_id'])
                self.assertEqual('饮料照旧', human['quote'])
                self.assertEqual(valid, bool(human['note']))
                self.assertEqual(original['quote'], next(r for r in brain.today_entries() if r['id']==old['id'])['quote'])

    @mock.patch(__name__ + ".brain_now_iso", new=lambda: "2026-11-08T12:00:00+09:00")
    @mock.patch("organs.brain.store.now_iso", new=lambda: "2026-11-08T12:00:00+09:00")
    def test_recall_decisions_survive_turn_storage_without_reentering_history(self):
        from organs.mind import recent
        at = datetime.fromisoformat(brain_now_iso())
        for age in range(8, 0, -1):
            self.brain.ingest(WHO, '周六计划：' + ('很长的讨论。' * 150 if age == 1 else '去公园' + str(age)),
                              ts=(at - timedelta(minutes=age)).isoformat())
        data = conversation.load_assembly()
        data['sections']['memory']['cap'] = 420
        root = self.tmp / 'decisions'
        got = conversation.answer('周六计划', WHO, store=self.store, brain=self.brain,
                                  now=at.isoformat(), assembly=data, recent_root=root, session_id='private:test',
                                  transport=_transport('嗯。'), config=CONFIG, env=ENV)
        self.assertIn('memory_recall', got)
        audit = got['memory_recall']
        self.assertEqual(6, audit['slots_used'])
        self.assertEqual([1, 2, 3, 4, 5, 6], [r['rank'] for r in audit['selected']])
        self.assertEqual([False, True, True, True, False, False], [r['in_prompt'] for r in audit['selected']])
        self.assertTrue(audit['reply_model_called'])
        self.assertEqual([False, True, True, True, False, False], [r['sent_to_model'] for r in audit['selected']])
        self.assertEqual([False, False, False, True, True, True], [r['borrowed_slot'] for r in audit['selected']])
        self.assertEqual(['budget_full', 'budget_full'], [r['reason'] for r in audit['skipped']])
        self.assertEqual([7, 8], [r['rank'] for r in audit['skipped']])
        self.assertLessEqual(audit['injected_chars'], 420)
        self.assertEqual(['text'], audit['selected'][0]['matched_by'])
        stored = recent.load(root=root)['turns'][-1]
        self.assertEqual(got['turn_id'], stored['turn_id'])
        self.assertEqual(audit, stored['memory_recall'])
        recent.confirm_reply(stored, '确认发出。', root=root)
        self.assertEqual(audit, recent.load(root=root)['turns'][-1]['memory_recall'])
        from api import api
        with mock.patch('api.observe.recent.path_of', return_value=recent.path_of(root)):
            status, page = api.handle('GET', '/observe/records?source=chat', None, None)
        self.assertEqual(200, status)
        self.assertEqual(audit, page['rows'][0]['record']['memory_recall'])
        self.assertNotIn('quota_full', recent.render([stored]))
        self.assertNotIn('memory_recall', recent.render([stored]))

    def test_disabled_memory_section_is_distinct_from_no_matches(self):
        at = brain_now_iso()
        self.brain.ingest(WHO, '周六计划去公园', ts=at)
        data = conversation.load_assembly()
        data['order'].remove('memory')
        got = conversation.answer('周六计划', WHO, store=self.store, brain=self.brain,
                                  now=at, assembly=data, transport=_transport('嗯。'), config=CONFIG, env=ENV)
        self.assertIn('memory_recall', got)
        self.assertFalse(got['memory_recall']['enabled'])
        self.assertEqual('memory_disabled', got['memory_recall']['selected'][0]['reason'])

    def test_fixed_receipt_does_not_claim_assembled_memory_was_sent_to_reply_model(self):
        at = brain_now_iso()
        self.brain.ingest(WHO, '周六计划去公园', ts=at)
        captured = []
        with mock.patch('organs.mind.conversation.remind.handle', return_value={'say': '提醒已记下。'}):
            got = conversation.answer('周六计划', WHO, store=self.store, brain=self.brain,
                                      now=at, transport=_transport('嗯。', captured), config=CONFIG, env=ENV)
        self.assertEqual([], captured)
        self.assertIn('reply_model_called', got['memory_recall'])
        self.assertFalse(got['memory_recall']['reply_model_called'])
        row = got['memory_recall']['selected'][0]
        self.assertTrue(row['in_prompt'])
        self.assertFalse(row['sent_to_model'])
        self.assertEqual('fixed_receipt', row['reason'])

    def test_memory_budget_never_sends_half_a_quote_or_understanding(self):
        for long_field in ('quote', 'note'):
            with self.subTest(long_field=long_field):
                brain = BrainStore(self.tmp / long_field)
                at = datetime.fromisoformat(brain_now_iso())
                short = brain.ingest(WHO, '周六计划：去松林公园',
                                     ts=(at - timedelta(minutes=2)).isoformat())['entry']
                quote = '周六计划：之前提议图书馆。'
                note = '承接理解（可错，非原话）：'
                if long_field == 'quote':
                    quote += '讨论来回路线，' * 50 + '最终不去图书馆。'
                else:
                    note += '讨论来回路线，' * 50 + '最终不去图书馆。'
                large = brain.ingest(WHO, quote, note=note,
                                     ts=(at - timedelta(minutes=1)).isoformat())['entry']
                data = conversation.load_assembly()
                data['sections']['memory']['cap'] = 260
                captured = []
                got = conversation.answer('周六计划', WHO, store=self.store, brain=brain,
                                          now=at.isoformat(), assembly=data, session_id='',
                                          transport=_transport('去松林公园。', captured), config=CONFIG, env=ENV)
                section = self._memory_section(captured)
                self.assertIn(short['quote'], section)
                self.assertNotIn('之前提议图书馆', section, '不能把较长记录截成半个说法')
                self.assertLessEqual(len(section), 260)
                shown = {r['id']: r['in_prompt'] for r in got['recalled']}
                self.assertEqual({short['id']: True, large['id']: False}, shown)
                self.assertEqual(quote, next(r for r in got['recalled'] if r['id'] == large['id'])['display_text'])

    def test_memory_budget_accepts_exact_fit_and_reports_all_omitted(self):
        at = brain_now_iso()
        self.brain.ingest(WHO, '周六计划：去松林公园', ts=at)
        rows, full, _audit = conversation._recall(self.brain, '周六计划', WHO)
        for cap, expected in ((len(full), True), (len(full) - 1, False)):
            with self.subTest(cap=cap):
                brain = BrainStore(self.tmp / str(cap))
                brain.ingest(WHO, '周六计划：去松林公园', ts=at)
                data = conversation.load_assembly()
                data['sections']['memory']['cap'] = cap
                captured = []
                got = conversation.answer('周六计划', WHO, store=self.store, brain=brain,
                                          now=at, assembly=data, session_id='',
                                          transport=_transport('嗯。', captured), config=CONFIG, env=ENV)
                section = self._memory_section(captured)
                self.assertEqual(expected, '周六计划：去松林公园' in section)
                self.assertEqual(expected, got['recalled'][0]['in_prompt'])
                self.assertLessEqual(len(section), cap)

    def test_timezone_order_keeps_latest_correction_in_reply_model_budget(self):
        at = datetime.fromisoformat(brain_now_iso()).astimezone(timezone.utc)
        for i in range(6):
            self.brain.ingest(WHO, f'周六计划去图书馆，安排{i}',
                             ts=(at - timedelta(minutes=10-i)).astimezone(timezone(timedelta(hours=9))).isoformat())
        stamp = (at - timedelta(minutes=1)).isoformat()
        correction = self.brain.ingest(WHO, '周六计划改去公园', ts=stamp)['entry']
        captured = []
        got = conversation.answer('周六计划', WHO, store=self.store, brain=self.brain,
                                  now=at.isoformat(), transport=_transport('公园。', captured), config=CONFIG, env=ENV)
        section = self._memory_section(captured)
        self.assertIn('周六计划改去公园', section)
        self.assertIn(stamp, section)
        selected = got['memory_recall']['selected']
        self.assertEqual(6, len(selected))
        self.assertEqual(correction['id'], selected[0]['id'])
        self.assertTrue(selected[0]['sent_to_model'])
        self.assertLessEqual(len(section), conversation.load_assembly()['sections']['memory']['cap'])

    def test_spare_slot_correction_reaches_reply_model_with_original_evidence(self):
        at = datetime.fromisoformat(brain_now_iso())
        texts = ['周六去河岸图书馆，下午两点出发', '周六去河岸图书馆，带上笔记本',
                 '周六去河岸图书馆，在门口碰面', '计划改了，周六改去松林公园，不去图书馆了']
        for age, text in zip((4, 3, 2, 1), texts):
            self.brain.ingest(WHO, text, ts=(at - timedelta(minutes=age)).isoformat())
        captured = []
        got = conversation.answer('周六去河岸图书馆', WHO, store=self.store, brain=self.brain,
                                  now=at.isoformat(), transport=_transport('去公园。', captured),
                                  config=CONFIG, env=ENV)
        section = self._memory_section(captured)
        for text in texts:
            self.assertIn(text, section)
        self.assertLessEqual(len(section), conversation.load_assembly()['sections']['memory']['cap'])
        selected = got['memory_recall']['selected']
        self.assertEqual(4, len(selected))
        self.assertTrue(selected[-1]['borrowed_slot'])
        self.assertTrue(selected[-1]['sent_to_model'])

    def test_correction_with_repeated_words_reaches_model_with_both_dates(self):
        at = datetime.fromisoformat(brain_now_iso())
        for age, note in ((60, '承接理解（可错，非原话）：周六改去河岸图书馆'),
                         (1, '承接理解（可错，非原话）：周六再次改去松林公园，原计划取消')):
            self.brain.ingest(WHO, '改去那里吧', ts=(at - timedelta(minutes=age)).isoformat(), note=note)
        captured = []
        got = conversation.answer('周六去河岸图书馆还是哪里', WHO, store=self.store,
                                  brain=self.brain, now=at.isoformat(),
                                  transport=_transport('去松林公园。', captured), config=CONFIG, env=ENV)
        section = self._memory_section(captured)
        self.assertIn('周六改去河岸图书馆', section)
        self.assertIn('周六再次改去松林公园，原计划取消', section)
        for age in (60, 1):
            self.assertIn((at - timedelta(minutes=age)).isoformat(), section)
        self.assertEqual(2, len(got['recalled']))

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
        system = captured[0]["messages"][1]["content"]
        spec = conversation.load_assembly()["sections"]["memory"]
        body = _section(system, spec["label"])
        notice = spec.get("notice", "")
        self.assertTrue(body.startswith(notice + "\n"))
        return body[len(notice) + 1:]

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
        self.assertIn("] %s（来源：" % NO_QUOTE, section,
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
