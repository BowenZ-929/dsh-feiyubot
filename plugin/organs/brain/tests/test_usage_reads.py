"""列表读取随条目增长时不反复读取整本 usage；保留最后一条及实时可见语义。"""
import json
import unittest
from unittest.mock import patch
from organs.brain import store as module
from organs.brain.tests.helpers import fresh_store

class UsageReadsTest(unittest.TestCase):
    def test_one_usage_read_per_active_list_and_next_read_sees_new_usage(self):
        brain, tmp = fresh_store()
        self.addCleanup(tmp.cleanup)
        rows = [{'id':str(i),'status':'active','text':'虚构记忆'} for i in range(5)]
        rows.append({'id':'retired','status':'retired'})
        (brain.root / module.LONGTERM_FILE).write_text('\n'.join(json.dumps(r) for r in rows))
        brain._append_usage({'id':'0','hit_count':1,'recall_count':1,'last_recalled_at':'old'})
        brain._append_usage({'id':'0','hit_count':3,'recall_count':2,'last_recalled_at':'new'})
        with patch.object(module,'_usage_rows',wraps=module._usage_rows) as reads:
            first = brain.longterm_active_entries()
            self.assertEqual(1, reads.call_count)
        self.assertEqual(5,len(first))
        self.assertEqual(3,first[0]['hit_count'])
        brain._append_usage({'id':'0','hit_count':4,'recall_count':3,'last_recalled_at':'later'})
        self.assertEqual(4,brain.longterm_active_entries()[0]['hit_count'])
        self.assertEqual(3,first[0]['hit_count'])
