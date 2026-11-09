import json
import unittest

from organs.mind import memory_read


class SourceReadTest(unittest.TestCase):
    def test_entire_source_reaches_reader_and_excerpts_keep_exact_offsets(self):
        correct = '最后改去河岸图书馆，不去旧街书店。'
        for position in (0, 20, 40):
            with self.subTest(position=position):
                sentences = ['整理抽屉里的旧纸张。'] * 40
                sentences.insert(position, correct)
                source = ''.join(sentences)
                calls = []
                def complete(messages):
                    calls.append(messages)
                    return json.dumps({'quote': [correct], 'note': []}, ensure_ascii=False)
                item = {'full_text': source, 'note_full': '', 'who': '100001', 'ts': '2026-11-07T10:00:00+09:00'}
                result = memory_read.read(item, '最后去哪儿', 180, complete=complete)
                self.assertEqual('ok', result['status'])
                self.assertEqual(source, json.loads(calls[0][1]['content'])['quote'])
                self.assertIn(correct, result['display_text'])
                self.assertIn('摘录', result['display_text'])
                span = result['spans'][0]
                self.assertEqual(correct, source[span['start']:span['end']])
                self.assertEqual('quote', span['field'])
                self.assertLessEqual(len(result['display_text']) + len(result['note_display']), 180)
                self.assertEqual(source, item['full_text'])

    def test_rejects_fabrication_partial_negation_and_over_budget(self):
        item = {'full_text': '不去旧街书店。最后去河岸图书馆。', 'note_full': '她理解为计划已更改。'}
        for payload, budget in (({'quote':['去旧街书店。'],'note':[]}, 180),
                                ({'quote':['最后去游乐园。'],'note':[]}, 180),
                                ({'quote':['最后去河岸图书馆。'],'note':[]}, 5),
                                ({'quote':['最后去河岸图书馆。'],'note':['去游乐园。']}, 180),
                                ({'quote':[],'note':['她理解为计划已更改。']}, 180),
                                ([], 180)):
            with self.subTest(payload=payload, budget=budget):
                result = memory_read.read(item, '去哪儿', budget, complete=lambda _: json.dumps(payload, ensure_ascii=False))
                self.assertEqual('invalid', result['status'])
                self.assertNotIn('display_text', result)

    def test_quote_and_interpretation_never_share_evidence_fields(self):
        item = {'full_text':'周末去书店。', 'note_full':'她以为是河岸图书馆。'}
        result = memory_read.read(item, '去哪儿', 200,
            complete=lambda _: json.dumps({'quote':['周末去书店。'],'note':['她以为是河岸图书馆。']}, ensure_ascii=False))
        self.assertEqual('ok', result['status'])
        self.assertNotIn('河岸图书馆', result['display_text'])
        self.assertIn('非原话', result['note_display'])

    def test_oversize_source_is_not_silently_truncated_or_sent(self):
        result = memory_read.read({'full_text':'字'*12001}, '查一下', 200,
            complete=lambda _: self.fail('oversize source must not be sent'))
        self.assertEqual('source_too_large', result['status'])

    def test_invalid_json_is_a_visible_read_failure(self):
        result = memory_read.read({'full_text':'去书店。'}, '去哪儿', 200, complete=lambda _: 'bad json')
        self.assertEqual('invalid', result['status'])

    def test_network_failure_is_not_reported_as_success(self):
        from organs.mind.llm import LLMError
        def fail(_):
            raise LLMError('timeout', 'private provider details')
        result = memory_read.read({'full_text':'去书店。'}, '去哪儿', 200, complete=fail)
        self.assertEqual('unavailable', result['status'])
        self.assertNotIn('private provider details', json.dumps(result))

    def test_excerpts_preserve_source_order_even_if_model_reorders_them(self):
        old, new = '原来去旧街书店。', '最后改去河岸图书馆。'
        result = memory_read.read({'full_text':old+new}, '去哪儿', 200,
            complete=lambda _: json.dumps({'quote':[new,old], 'note':[]}, ensure_ascii=False))
        self.assertLess(result['display_text'].index(old), result['display_text'].index(new))

    def test_line_break_cannot_strip_condition_or_negation(self):
        for source, excerpt in (('如果不下雨，\n周末去河岸图书馆。','周末去河岸图书馆。'),
                                ('我不\n去旧街书店。','去旧街书店。')):
            with self.subTest(source=source):
                result = memory_read.read({'full_text':source}, '去哪儿', 200,
                    complete=lambda _: json.dumps({'quote':[excerpt], 'note':[]}, ensure_ascii=False))
                self.assertEqual('invalid', result['status'])

    def test_repeated_sentence_must_be_located_uniquely(self):
        source='周末去书店。临时改去公园。周末去书店。'
        for excerpts, status in ((['临时改去公园。','周末去书店。'],'invalid'),
                                 (['临时改去公园。周末去书店。'],'ok')):
            with self.subTest(excerpts=excerpts):
                result=memory_read.read({'full_text':source}, '去哪儿', 200,
                    complete=lambda _: json.dumps({'quote':excerpts,'note':[]},ensure_ascii=False))
                self.assertEqual(status,result['status'])
                if status=='ok':
                    self.assertEqual(len('周末去书店。'),result['spans'][0]['start'])


if __name__ == '__main__':
    unittest.main()
