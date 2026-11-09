import json
import unittest

from organs.mind import phone_reflection


class ReflectionTest(unittest.TestCase):
    def test_music_without_words_can_ground_a_tentative_impression(self):
        audio = {'status':'empty','sound_events':{'status':'ok','source':'local_yamnet',
                  'processed_seconds':4,'events':[{'label':'音乐','score':.8}]}}
        prompts=[]
        result=phone_reflection.reflect('喜欢音乐',{},[],audio,complete=lambda messages:
            prompts.append(messages) or json.dumps({'effect':'positive','impression':'想多听一会儿',
                                                   'evidence':'可能有音乐'}))
        self.assertEqual(result['evidence'],'可能有音乐')
        self.assertIn('可能有音乐',prompts[0][1]['content'])
        self.assertIn('不是情绪',prompts[0][0]['content'])

    def test_grounded_feeling_and_only_usable_audio_reach_model(self):
        captured=[]
        result=phone_reflection.reflect('喜欢猫',{'mood':{'now':60}},
            [{'status':'ok','observation':{'subject':'猫坐在纸箱旁'}}],
            {'status':'unclear','transcript':'字幕by某人','raw_transcript':'不能外推'},
            complete=lambda messages:captured.append(messages) or json.dumps(
                {'effect':'positive','impression':'这个小动作有点好玩','evidence':'猫坐在纸箱旁'}))
        self.assertEqual('positive',result['effect'])
        self.assertIn('猫坐在纸箱旁',captured[0][1]['content'])
        self.assertNotIn('字幕by',captured[0][1]['content'])
        self.assertNotIn('不能外推',captured[0][1]['content'])

    def test_invalid_or_invented_evidence_is_rejected(self):
        for value in (
            {'effect':'positive','impression':'很开心','evidence':'猫跳进了纸箱'},
            {'effect':'positive','impression':'很开心','evidence':''},
            {'effect':'positive','impression':'','evidence':'猫坐着'},
            {'effect':'excited','impression':'很开心','evidence':'猫坐着'},
            {'effect':'neutral','impression':'x'*161,'evidence':''},
            []):
            with self.subTest(value=value),self.assertRaises(ValueError):
                phone_reflection.reflect('她',{},[{'status':'ok','observation':{'subject':'猫坐着'}}],{},
                    complete=lambda messages:json.dumps(value))

    def test_neutral_can_have_no_impression(self):
        result=phone_reflection.reflect('她',{},[{'status':'ok','observation':{'subject':'一张桌子'}}],{},
            complete=lambda messages:'{"effect":"neutral","impression":"","evidence":""}')
        self.assertEqual('neutral',result['effect'])

    def test_evidence_matches_observed_text_before_json_escaping(self):
        for quote in ('字幕写着"晚安"', '第一行\n第二行', '路径 C:\\猫'):
            with self.subTest(quote=quote):
                result = phone_reflection.reflect('她', {},
                    [{'status':'ok', 'observation':{'visible_text':quote}}], {},
                    complete=lambda messages: json.dumps({'effect':'neutral', 'impression':'记住了这个细节', 'evidence':quote}))
                self.assertEqual(quote, result['evidence'])

    def test_schema_names_and_json_between_fields_are_not_observation_evidence(self):
        seen = [{'status':'ok', 'observation':{'subject':'猫', 'scene':'窗台', 'sensitive':False}}]
        for quote in ('subject', 'sensitive', 'false', '猫", "scene": "窗台'):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                phone_reflection.reflect('她', {}, seen, {}, complete=lambda messages:
                    json.dumps({'effect':'positive', 'impression':'有意思', 'evidence':quote}))

    def test_usable_audio_can_supply_evidence_but_failed_observations_cannot(self):
        quote = '它说"过来"'
        result = phone_reflection.reflect('她', {}, [], {'status':'partial', 'transcript':quote},
            complete=lambda messages:json.dumps({'effect':'neutral', 'impression':'这句话有些亲切', 'evidence':quote}))
        self.assertEqual(quote,result['evidence'])
        with self.assertRaises(ValueError):
            phone_reflection.reflect('她', {}, [{'status':'failed','observation':{'subject':'猫'}}], {},
                complete=lambda messages:'{"effect":"positive","impression":"有意思","evidence":"猫"}')
