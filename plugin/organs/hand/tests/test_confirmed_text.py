import unittest
from organs.hand import channel
from organs.hand.tests.helpers import FakeService, json_reply


class ConfirmedTextTest(unittest.TestCase):
    def test_confirmed_text_is_not_replaced_by_requested_draft(self):
        for sent, text, reason, stopped in [(2,'第一句\n第二句','sent',None),
                (1,'第一句','sent','ack失败'),(1,'我在。','replaced_too_long',None),
                (0,'','outbound_failed','ack失败')]:
            with self.subTest(text=text), FakeService(json_reply({'ok':sent>0,'sent':sent,
                    'confirmed_text':text,'reason_code':reason,'stopped':stopped})) as server:
                result=channel.say_text(scene='private',target=100001,text='完整草稿',url=server.url)
                self.assertEqual(text,result['confirmed_text'])
                self.assertEqual(sent,result['sent'])
                self.assertEqual(stopped,result['stopped'])

    def test_old_channel_without_confirmed_text_remains_unknown(self):
        with FakeService(json_reply({'ok':True,'sent':1,'reason_code':'sent'})) as server:
            result=channel.say_text(scene='private',target=100001,text='不要猜我已发出',url=server.url)
            self.assertEqual('OK',result['code'])
            self.assertIsNone(result.get('confirmed_text'))
