import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from api import api
from organs.mind import llm
from organs.mind.tests.test_llm import conf, ok


class TraceRootTest(unittest.TestCase):
    def test_custom_home_alone_controls_trace_write_and_both_readers(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ',
                {'QQAGENT_HOME':tmp,'QQAGENT_HOME_ROOT':'','QQAGENT_TRACE':'1'}):
            root=Path(tmp)
            # Fail before writing if this would escape the isolated root.
            self.assertEqual(root/'mind/trace',llm._trace_dir())
            with llm.trace_round('c'*32,'api:private:100001'):
                llm.complete([{'role':'user','content':'离线来源链验收'}],config=conf(retries=0),
                    env={'QQAGENT_LLM_KEY':'fixture'},transport=lambda *args:ok('离线固定回复'),purpose='离线验收')
            before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with patch.dict('os.environ',{'QQAGENT_TRACE':'0'}):
                code,data=api.handle('GET','/observe/records?source=model&turn_id='+'c'*32,None,None)
                self.assertEqual(200,code);self.assertEqual(1,data['total'])
                day=datetime.now().astimezone().date().isoformat()
                code,data=api.handle('GET','/observe/activity?source=model&date='+day,None,None)
                self.assertEqual(200,code);self.assertEqual(1,data['total'])
            self.assertEqual(before,{p:p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_explicit_legacy_trace_root_remains_supported(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ',
                {'QQAGENT_HOME':tmp+'/other','QQAGENT_HOME_ROOT':tmp,'QQAGENT_TRACE':'1'}):
            self.assertEqual(Path(tmp)/'mind/trace',llm._trace_dir())
