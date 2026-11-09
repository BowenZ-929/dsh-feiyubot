"""POST /chat 的验收：走 api.handle 真链路（路由表 → 派发 → 装配 → 假 transport → 回包）。

判据：
- 成功体是契约那一份（ok / reply / chunks / recalled / heart / gate / assembly / memory）；
  分泡 2–3 条、gap_before 是数、send_at 非空；recalled 每条带 source / source_type /
  evidence_key，且与脑里那条对得上；心只有词；gate 的 pass 与 dropped 两条路都走到。
- 失败一律非 200：没配 key 503、超时 504、限流 429、鉴权 502、空内容 502；
  kind 与 message 原样交出去，**绝不把空串当成功**。
- GET / PUT / DELETE /chat 全 405（这一条是写）。
- 落脑：一轮之后脑的今日档里有人那句与她那句，source_type 都是亲历、evidence_key 非空。
- **闸在 /chat 生效（本轮加的）**：逐泡过一遍闸，结论真的落到返回体——复读（反复读）、动作括注
  （动作带场景）、复述设定段三条命中的路都走到 dropped；一句正常的回话一处都不改（chunks 的 text
  与不接闸时逐字相同）；全被弃掉时 chunks=[]、reply=""，并各记一行弃言账。
- **动作配额也从装配传进闸（本轮补的）**：action.quota.blocked 为真 → forbid（剥行救回正文，
  quota_status=ok，账本 reason 里带 action-over-quota）；为假 → allow（判过，放行动作括注那一关拦）；
  装配没标 → None（quota_status=not-checked），**没标不是 allow**。
- 只有一份装配：api.chat.dispatch 走的就是 conversation.answer（数一遍调用次数）。
- 拒绝的**唯一投递点**在这一层（本轮加的）：判成软转向或那三句标准拒绝之一就投一次心
  （refusals.json 多一行）+ 记一行我们自己的 refuse.jsonl；正面答了不添 refusal 一节、一行都不写。
  同一句在 /mouth/gate 上是 delivered=false（干跑只判不投），两处的判法是同一份。

模型一律换成本文件里的假 transport：一个字节都不发真网络。心 / 嘴 / 装配的临时根都经
环境变量切走，仓库里的真数据一个字节不写。
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import hashlib

from api import api, chat
from api.tests.helpers import copy_fixture, write_json
from organs.brain import BrainStore
from organs.brain import model as brain_model
from organs.home import anchors as home_anchors
from organs.home import cards
from organs.home import residents as home_residents
from organs.heart import energy
from organs.heart import gate as heart_gate
from organs.heart import model as heart_model
from organs.heart import mood
from organs.heart import refuse as heart_refuse
from organs.heart import store as heart_store
from organs.mind import conversation, events, llm, recent, usage
from organs.mouth import address as mouth_address
from organs.mouth import audit as mouth_audit
from organs.mouth import refusal as mouth_refusal
from organs.mouth import repeat as mouth_repeat
from organs.mouth import warehouse
from organs.soul import load

CONFIG = {"provider": "fake", "model": "fake-model", "base_url": "http://example.invalid/v1",
          "key_env": "QQAGENT_LLM_KEY", "timeout_ms": 1000, "retries": 0,
          "temperature": 0.0, "max_tokens": 200}
ENV = {"QQAGENT_LLM_KEY": "test-key"}
PLUGIN = Path(__file__).resolve().parents[2]
MOUTH_DATA = PLUGIN / "organs" / "mouth" / "data"

MEMORY_TEXT = "我上次去医院是陪我妈复查，我对花生过敏"
QUESTION = "你还记得医院那回事吗？"
# 一份≈平常档预算长度、且超过嘴的 split.max_chars 的回复：真链路里该切成多泡——
# 现行口径是「每泡 1–2 句，max_parts=12 只是防炸上限」，旧的「2–3 泡」已退役。
# 这一句里**不许有「引用标记 + 具体细节」**（我记得／我记着 + 日子／数／原话）：这一份是一条通用
# 长回复，量的是契约、分泡与场景，不是记忆。闸里那一关（编记忆）一旦撞上一条对不上的引用，这一泡就会
# 被换成「这个我没记到」——量契约的断言就会挂着别人的判据。两种意图别混在一句里。
LONG_REPLY = ("你问医院那回事啊，复查别落下，按时去就行。"
              "花生过敏那事我放在心上，别再吃带花生的点心了，嘴肿起来可难受。"
              "中午吃了面，汤有点咸，晚上想喝粥。"
              "周末要是不下雨，我陪你去一趟，顺便把上次落下的检查补上。") * 2

# 判据里的名字与记号一个都不写死：复读那条 clause_id、跳过记账用的名字、两个记号都从数据里读。
REPEAT_BOOK = json.loads((MOUTH_DATA / "repeat.json").read_text(encoding="utf-8"))
REPEAT_CLAUSE = REPEAT_BOOK["hit"]["clause_id"]
SKIP_CLAUSE = REPEAT_BOOK["hit"]["skip_clause_id"]
SKIP_MARK = "skipped-by-request"        # 跳过那句 reason 里必须出现的记号
AUDIT_BOOK = json.loads((MOUTH_DATA / "audit.json").read_text(encoding="utf-8"))
AUDIT_SKIP = AUDIT_BOOK["skip"]         # 复述审计按他说的放过那一笔：名字、记号、说法全在数据里
AUDIT_SKIP_CLAUSE = AUDIT_SKIP["clause_id"]
AUDIT_SKIP_MARK = AUDIT_SKIP["status"]
# 复述命中之后的第二处置（删那一句）留的记号：账本靠它分出「删句删空」与「整条判废」，也不写死。
RECITED_MARK = AUDIT_BOOK["recited"]["status"]
FATAL_BOOK = json.loads((MOUTH_DATA / "fatal.json").read_text(encoding="utf-8"))
# 认亲那一条的判据也全读数据：称呼表、记忆标记、中性词、稳妥话术、两个名字一个都不写死。
KIN_BOOK = json.loads((MOUTH_DATA / "kin.json").read_text(encoding="utf-8"))
KIN_TERM = KIN_BOOK["kin_terms"][0]        # 熟人才能触发的称呼
KIN_MEMORY = KIN_BOOK["memory_markers"][0]  # 熟人才能触发的记忆复述
KIN_NEUTRAL = KIN_BOOK["neutral"]["term_word"]
KIN_SAY = KIN_BOOK["neutral"]["say_once"]
KIN_MIN = KIN_BOOK["known_by"]["min"]      # 「够不够熟」那一档的阈值：也在数据里，测试不写死
KIN_FIELD = KIN_BOOK["known_by"]["field"]  # 读哪一格（现在 relation）同样在数据里


def _relation(brain, who: str) -> int:
    """这个号现在够不够熟：档位那一格用称呼那一关现算的那份读数，测试不另造一套判定。"""
    person = brain.lookup(who)
    if not person:
        return 0
    return mouth_address.resolve(person, mouth_address.load_table(), who=who).relation
FATAL_CLAUSE = FATAL_BOOK["hit"]["clause_id"]
GIVE_UP_CATEGORY = FATAL_BOOK["categories"][2]      # 放弃式那一类（顺序写在数据里）
GIVE_UP_LINE = "反正我不疼，你别管了"                 # 一句放弃式的素材，不是判据
AFTER_MARKS = json.loads((MOUTH_DATA / "rewrite.json").read_text(
    encoding="utf-8"))["after_rewrite"]["marks"]
# 私密记忆那一节的判据全在数据里：栏名、取值、两个场景值、边界话、事件形状、记号、摘要截几个字
AUDIENCE = json.loads((MOUTH_DATA / "refusal.json").read_text(
    encoding="utf-8"))["audience"]
# 账本两态那一节（名字与两句「没出门」的说法）：测试里一个字都不写死
STATES = json.loads((MOUTH_DATA / "refusal.json").read_text(encoding="utf-8"))["states"]


def _section(text: str, label: str) -> str:
    """提示词里某一节的正文：从「[标题]」到下一个空行分节为止（段与段之间是空行）。"""
    head = text.index("[%s]" % label) + len(label) + 2
    tail = text.find("\n\n[", head)
    return text[head:tail] if tail >= 0 else text[head:]


def _mark_of(tag: str) -> str:
    """某一类红线在账本里那个记号（按数据里的 mark 与 tag 填）：测试不自己造这个字符串。"""
    return FATAL_BOOK["mark"].format(tag=tag)
KEPT_MARK = "rewrite-failed"            # 改不动、留原话那句 reason 里必须出现的记号
REWRITE_KEYS = {"status", "why", "from", "to"}
# 借阅读数里「条款用掉几条」= 这一轮过闸真比过的条数（启用条款全表）：条数不写死，读数据。
CLAUSES = json.loads((MOUTH_DATA / "clauses.json").read_text(encoding="utf-8"))["clauses"]
# 长度与句末标点那层的判据也全读数据：场景、区间、p90、上限、最少句数、形状条款 id 一个都不写死
STYLE_BOOK = json.loads((MOUTH_DATA / "style_target.json").read_text(encoding="utf-8"))
# /chat 的默认场合也读数据（refusal.json 的 scopes.default）：这一格决定长度按哪套区间判
SCOPE_DEFAULT = json.loads((MOUTH_DATA / "refusal.json").read_text(
    encoding="utf-8"))["audience"]["scopes"]["default"]
STYLE_SCENE = SCOPE_DEFAULT
STYLE_RANGES = STYLE_BOOK["scenes"][SCOPE_DEFAULT]["ranges"]
STYLE_SCENE_LABEL = STYLE_BOOK["scenes"][STYLE_SCENE]["label"]
STYLE_P90 = STYLE_BOOK["scenes"][STYLE_SCENE]["p90"]
STOPPED_CEILING = STYLE_BOOK["stopped"]["ceiling"]
SHAPE_CLAUSE = [one["id"] for one in CLAUSES if one.get("scope") == chat.REPLY_SCOPE][0]
ENABLED_CLAUSES = len([one for one in CLAUSES if one.get("enabled", True)])

#: 本机钟面的白天/夜里；对话入口会把脑的 UTC 钟统一转成本机时区。
#: 出站闸那两道硬门里夜里排在前面，要真造出 SILENT 就得避开它。
DAYTIME = datetime(2026, 11, 6, 12).astimezone().isoformat()
#: 一个确定在夜里的时刻（夜里窗口是 NIGHT_START_H=23 到 NIGHT_END_H=8）：
#: 主动那一轮在这里会被心判「不说」，被动那一轮只判「少说」。
NIGHTTIME = datetime(2026, 11, 6, 3).astimezone().isoformat()
#: 她自己开口那一轮的由头（素材，不是判据）：proactive 的必填入参。
SEED = "今天想问问他吃饭了没有"

#: 通道真发的那个形状：channels/onebot/server.ts 拼的三个键（id / nickname / card），一个不多。
CHANNEL_WHO = {"id": 3425635473, "nickname": "能毕业就是胜利", "card": "小红花的爹"}
CHANNEL_WHO_ID = "3425635473"          # 身份：裸数字号，不加任何前缀

CONTRACT_KEYS = {"ok", "reply_kind", "reply", "chunks", "scene", "recalled", "memory_recall", "heart", "gate", "audit", "turn_id",
                 # 核·识别这一轮的读数（本轮加的）：五键冻结，/chat 原样透出、不重组。
                 "event",
                 "assembly", "continuity", "banned", "action", "llm", "anchors", "lookup",
                 "guard", "memory", "addressed", "borrow", "refusal", "audience_held",
                 # 这一轮是谁起的头 + 由头原样（本轮加的）：被动两格也在，形状不随状态变。
                 "mode", "seed",
                 # 嘴这一格（本轮加的）：四个旋钮落到这一轮的实话，页面可读。
                 "tone",
                 # 出站闸那四格（门只回答发不发）：send / reason / level 是门的答案，
                 # limits 交给嘴——四格不论发不发都在，形状不随状态变。
                 "send", "reason", "level", "limits"}
AUDIT_KEYS = {"decision", "kind", "card_id", "overlap"}
BANNED_KEYS = {"rounds", "action_lines", "openings"}
ACTION_KEYS = {"pool", "rule", "quota"}
QUOTA_KEYS = {"window", "max", "used", "blocked", "stripped", "kept_because_empty"}
GUARD_KEYS = {"window", "threshold", "min_rounds", "rounds", "hits", "rate",
              "over", "injected", "available"}


def _gate_rewrite_calls(capture: list) -> list:
    """抓「闸的重写」那一问：按 data/rewrite.json 的 prompt.head 认（不写死那句话）。

    长度与句末标点那一层也走同一份 transport、也会发单条请求，但它发的是 style_target.json 里的
    另一句指令；数请求条数时必须把两者分开，否则「只命中词组不该多花一次请求」这类断言会被
    改形那一层污染。
    """
    head = json.loads((MOUTH_DATA / "rewrite.json").read_text(
        encoding="utf-8"))["prompt"]["head"]
    return [one for one in capture if head in one["messages"][-1]["content"]]


def _body(reply: str, captured: list = None, status: int = 200):
    """假 transport：把一句话当作模型应答；captured 收请求体。"""
    def send(url, headers, payload, timeout):
        if captured is not None:
            captured.append(payload)
        data = {"choices": [{"message": {"content": reply}}]}
        return status, json.dumps(data, ensure_ascii=False).encode("utf-8")
    return send


def _script(*replies: str, captured: list = None):
    """假 transport：按调用顺序依次回那几句话（第 1 次是装配那一问，第 2 次起是重写）。

    最后一句会一直用下去：一泡命中、后面还有泡命中时，重写都是用最后那一句回的。
    """
    queue = list(replies)

    def send(url, headers, payload, timeout):
        if captured is not None:
            captured.append(payload)
        text = queue[0] if len(queue) == 1 else queue.pop(0)
        data = {"choices": [{"message": {"content": text}}]}
        return 200, json.dumps(data, ensure_ascii=False).encode("utf-8")

    return send


def _script_then_raise(reply: str, exc: Exception):
    """假 transport：第 1 次（装配那一问）正常回 reply，之后每一次（重写）都抛 exc。"""
    calls = []

    def send(url, headers, payload, timeout):
        calls.append(payload)
        if len(calls) > 1:
            raise exc
        data = {"choices": [{"message": {"content": reply}}]}
        return 200, json.dumps(data, ensure_ascii=False).encode("utf-8")

    return send


def _failing(exc: Exception):
    def send(url, headers, payload, timeout):
        raise exc
    return send


def _status(status: int, body: bytes = b""):
    def send(url, headers, payload, timeout):
        return status, body
    return send


class ChatApiTest(unittest.TestCase):

    def test_new_nickname_does_not_make_a_contact_familiar(self):
        who = '10000423'
        seen = []
        def assess(**context):
            seen.append(context['caller_context'])
            return dict(events.recognize(context['text'], who=context['who'], scene=context['scene']),
                        status='ok', source='fixture', painting=None)
        body = {'text': '你好', 'who': {'id': int(who), 'nickname': '新来客'},
                'scene': 'private', 'where': {'userId': int(who)}}
        captured = []
        status, payload = self._post(body, _body('你好呀。', captured), assessor=assess)
        self.assertEqual(status, 200)
        caller = next(row for row in payload['assembly']['sections'] if row['key'] == 'caller')
        self.assertIn('关系尚未确认', caller['relationship'])
        self.assertIn('关系尚未确认', seen[0])
        self.assertIn('关系尚未确认', captured[0]['messages'][0]['content'])
        self.assertNotIn('熟人', seen[0])
        self.brain.patch_card(who, 'test', {'关系': '朋友', '叫法': '阿新'})
        _, confirmed = self._post(body, _body('阿新。'), assessor=assess)
        caller = next(row for row in confirmed['assembly']['sections'] if row['key'] == 'caller')
        self.assertEqual(caller['relationship'], '朋友')
        self.assertEqual(caller['address'], '阿新')
        self.assertIn('朋友', seen[-1])

    def test_factual_repetition_is_observed_without_rewriting_the_answer(self):
        old = '周日下午两点，收到，地点还是海洋馆。'
        for answer in ('周日下午两点，海洋馆。', old):
            with self.subTest(answer=answer):
                recent.append({'ts':DAYTIME,'who':CHANNEL_WHO_ID,'said':'改到周日下午两点',
                               'reply':old,'session_id':'qq:private:'+CHANNEL_WHO_ID})
                captured=[]
                before=len(self._drops_rows())
                status,payload=self._post({'text':'我刚才说去哪里、什么时候来着？','who':CHANNEL_WHO,
                    'scene':'private','where':{'userId':int(CHANNEL_WHO_ID)}},_body(answer,captured))
                self.assertEqual(200,status)
                self.assertEqual(answer,payload['reply'])
                self.assertEqual(1,len(captured),'不能仅因字面复用再调模型')
                self.assertEqual('observed',payload['gate']['parts'][0]['repeat_status'])
                self.assertEqual([],payload['gate']['parts'][0]['hits'])
                self.assertEqual(before,len(self._drops_rows()))
                prompt='\n'.join(m['content'] for m in captured[0]['messages'])
                self.assertNotIn('这一轮不许再用下面这些',prompt)
                self.assertIn('正常确认、引用和承接不必刻意换词',prompt)
    def test_group_image_following_caption_reaches_actual_vision_request(self):
        from organs.eye import vision
        from organs.eye.tests.test_vision import picture, response
        calls = []
        real_observe = vision.observe
        config = dict(vision.load_config(), enabled=True)
        def observe(attachments, **kwargs):
            return real_observe(attachments, **kwargs, config=config, fetch=lambda *args: picture(),
                transport=lambda config,key,payload,timeout: calls.append(payload) or response())
        segment = [{'who':'10000420', 'speaker':'甲', 'text':'[图片]'},
                   {'who':'10000420', 'speaker':'甲', 'text':'左边那俩绿的，看看各自名字'},
                   {'who':'10000421', 'speaker':'乙', 'text':'我另外那张要看右边'}]
        attachments = [{'kind':'image', 'speaker_id':'10000420', 'segment_index':0}]
        vision._CACHE.clear()
        self.addCleanup(vision._CACHE.clear)
        with mock.patch.object(vision, 'observe', side_effect=observe), mock.patch.object(vision, 'read_key', return_value='test'):
            status, payload = self._post({'text':'本批有人发图', 'segment':segment, 'scene':'group',
                'who':{'id':10000420, 'nickname':'甲'}, 'where':{'groupId':123},
                'attachments':attachments}, _body('两块颜色很像。'))
        self.assertEqual(200, status)
        self.assertEqual(1, len(calls))
        note = calls[0]['messages'][1]['content'][0]['text']
        self.assertIn('左边那俩绿的，看看各自名字', note)
        self.assertNotIn('我另外那张要看右边', note)
        self.assertNotIn('本批有人发图', note)
        self.assertEqual('两块颜色很像。', payload['reply'])

    def test_vision_receives_attributed_messages_instead_of_flat_group_text(self):
        from organs.eye import vision
        segment = [{'who':'10000420', 'speaker':'甲', 'text':'[图片]左边那俩绿的'},
                   {'who':'10000421', 'speaker':'乙', 'text':'别人的无关内容'}]
        attachments = [{'kind':'image', 'speaker_id':'10000420', 'segment_index':0}]
        with mock.patch.object(vision, 'observe', return_value=[]) as observe:
            status, _ = self._post({'text':'整段群聊背景', 'segment':segment, 'scene':'group',
                                    'who':{'id':10000420, 'nickname':'甲'},
                                    'where':{'groupId':123}, 'attachments':attachments}, _body('看见了。'))
        self.assertEqual(200, status)
        self.assertEqual(segment, observe.call_args.kwargs['segment'])
        self.assertEqual('', observe.call_args.kwargs['text'])

    def test_single_private_image_receives_current_text(self):
        from organs.eye import vision
        for scene, expected in (('private', '这张图左边是什么'), ('group', '')):
            with self.subTest(scene=scene), mock.patch.object(vision, 'observe', return_value=[]) as observe:
                status, _ = self._post({'text':'这张图左边是什么', 'scene':scene,
                                       'attachments':[{'kind':'image'}]}, _body('看见了。'))
                self.assertEqual(200, status)
                self.assertEqual(expected, observe.call_args.kwargs['text'])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        patches = mock.patch.dict(os.environ, {
            "QQAGENT_HOME": str(self.tmp / "home"),
            "HEART_ROOT": str(self.tmp / "heart"),
            "MOUTH_TRACE_DIR": str(self.tmp / "trace"),
            "HOME_ROOT": str(self.tmp / "house"),
        })
        (self.tmp / "house" / "仓库").mkdir(parents=True, exist_ok=True)
        patches.start()
        self.addCleanup(patches.stop)
        # 本机 <仓库>/home/.env 里可能真有 key：把 llm 认的那个 home 钉到临时目录，
        # 否则「没配 key 该是 503」那条会被本机密钥悄悄变成「请求发出去了」（同 test_llm 的做法）。
        home_patch = mock.patch.object(llm.paths, "HOME", self.tmp / "repo-home")
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.store = load(copy_fixture("valid", str(self.tmp)))
        self.brain = BrainStore(self.tmp / "brain")
        # 普通对话契约在白天验证；睡眠专项用自己的 now_iso 覆盖。
        # 否则本机过了 23 点，真休息门会提前返回，让普通契约测试随时间失效。
        daytime = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        clock = mock.patch.object(brain_model, "now_iso", return_value=daytime.isoformat())
        clock.start()
        self.addCleanup(clock.stop)

    # —— 小工具 ————————————————————————————————

    def _answer(self, transport, *, config=CONFIG, env=ENV, counter: list = None, assessor=None):
        """把假 transport 塞进真链路：api.handle → api.chat.dispatch → answer / 重写那一问。

        两个入口都只换注入点（transport / config / env），装配与闸本身一次都没有替身：
        answer() 那三个参数走 mock.patch，重写那一次走 api.chat 顶上的 TRANSPORT / CONFIG / ENV。
        同一份假 transport 两边共用——重写走的确实是「answer() 用的那一条出口」。
        """
        real = conversation.answer

        def wrapper(text, who="", **kw):
            kw.setdefault("transport", transport)
            kw.setdefault("config", config)
            kw.setdefault("env", env)
            # 这些出口契约测试隔离语义模型；其真实接线与解析另做专项测试。
            def fixture_assessor(**context):
                result = dict(events.recognize(context["text"], who=context["who"], scene=context["scene"],
                    last_reply=(context["history"][-1].get("reply", "") if context["history"] else "")),
                    status="ok", source="fixture", painting=None)
                text = context["segment"][-1]["text"] if context["segment"] else context["text"]
                words = text.strip().split(maxsplit=1)
                action = {"/画画": "start", "/画作": "status", "/停画": "cancel"}.get(words[0] if words else "")
                if action:
                    result["painting"] = {"action": action, "evidence": [text], "prompt": words[1] if len(words) > 1 else ""}
                return result
            kw["assessor"] = assessor or fixture_assessor
            if counter is not None:
                counter.append(text)
            return real(text, who, **kw)

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(conversation, "answer", wrapper))
        stack.enter_context(mock.patch.object(chat, "TRANSPORT", transport))
        stack.enter_context(mock.patch.object(chat, "CONFIG", config))
        stack.enter_context(mock.patch.object(chat, "ENV", env))
        return stack

    def _post(self, body, transport=None, *, store=..., brain=..., **extra):
        transport = body if transport is None else transport
        with self._answer(transport, **extra):
            return api.handle("POST", "/chat", body,
                             self.store if store is ... else store,
                             brain=self.brain if brain is ... else brain)

    def _send(self, text: str = QUESTION, reply: str = LONG_REPLY, **extra):
        return self._post({"text": text}, _body(reply), **extra)

    # —— 成功体 ————————————————————————————————

    def test_both_chat_models_receive_same_clock_and_old_turn_time_without_life_plan(self):
        from organs.mind import appraisal, recent
        from organs.day import life
        at, old = '2026-11-08T18:00:00+09:00', '2026-11-07T18:00:00+09:00'
        for scene, where, session in (
                ('private',{'userId':10000420},'qq:private:10000420'),
                ('group',{'groupId':123456},'qq:group:123456')):
            recent.append({'ts':old,'who':'10000420','said':'明天上午十点去海洋馆',
                           'reply':'嗯。','session_id':session})
            captured = {}
            def complete(messages, **kwargs):
                captured[kwargs.get('purpose')] = messages
                if kwargs.get('purpose') == '理解这一段话':
                    return json.dumps({'kind':None,'evidence':'','reason':'询问原来约定的日期'})
                return '你原来约的是今天上午十点。'
            with self.subTest(scene=scene), mock.patch.object(llm,'complete',side_effect=complete), \
                 mock.patch.object(brain_model,'now_iso',return_value=at), mock.patch.object(life,'context',return_value=''):
                status, payload = self._post({'text':'原来约的哪天几点','who':'10000420',
                    'scene':scene,'where':where,'delivery_ack':True},_body('不用网络'),assessor=appraisal.assess)
                self.assertEqual(200,status)
                understood = json.loads(captured['理解这一段话'][-1]['content'])
                self.assertEqual(brain_model.parse_ts(at),brain_model.parse_ts(understood['now']))
                self.assertEqual(old,understood['history'][-1]['ts'])
                reply = captured['生成回复']
                self.assertIn('[本轮时间] '+understood['now'],reply[0]['content'])
                self.assertIn('[记录时间 '+old+']',reply[-1]['content'])
                self.assertIn('明天上午十点去海洋馆',reply[-1]['content'])

    def test_group_background_reaches_both_models_without_becoming_current_speech_or_memory(self):
        background = {'session_id':'qq:group:123456', 'omitted':2, 'messages':[
            {'message_id':'98','who':'10000420','speaker':'甲(10000420)',
             'text':'聚餐改到河边茶馆，星期日', 'at':'2026-11-07T12:00:00+09:00'}]}
        assessed, capture = [], []
        def assessor(**kw):
            assessed.append(kw)
            return {'kind':None,'who':kw['who'],'evidence':'','note':'承接前文','status':'ok','source':'semantic'}
        with mock.patch.object(brain_model, 'now_iso', return_value='2026-11-07T12:01:00+09:00'):
            status, data = self._post({'text':'那地方怎么样','who':'10000421','scene':'group',
                'where':{'groupId':123456}, 'background':background}, _body('可以聊聊。', capture), assessor=assessor)
        self.assertEqual(200, status)
        self.assertEqual(background, assessed[0].get('background'))
        self.assertIn('河边茶馆', capture[0]['messages'][-1]['content'])
        self.assertEqual('那地方怎么样', assessed[0]['text'])
        entries = self.brain.today_entries('2026-11-07T12:01:00+09:00')
        self.assertFalse(any('河边茶馆' in row.get('text','') for row in entries))
        from organs.mind import recent
        saved = recent.load(session_id='qq:group:123456')['turns']
        self.assertTrue(saved)
        self.assertNotIn('河边茶馆', json.dumps(saved, ensure_ascii=False))

    def test_overheard_correction_saves_only_new_speech_and_grounded_note(self):
        from organs.mind import appraisal
        background = {'session_id':'qq:group:123456','omitted':0,'messages':[
            {'message_id':'98','who':'10000420','speaker':'同名','text':'我订了周六的海洋馆票',
             'at':'2026-11-07T12:00:00+09:00'},
            {'message_id':'99','who':'10000421','speaker':'同名','text':'我订了周日的电影票',
             'at':'2026-11-07T12:01:00+09:00'}]}
        for previous, valid in (('我订了周六的海洋馆票', True), ('我订了周日的电影票', False)):
            captured = []
            def complete(messages, **kwargs):
                captured.append((kwargs.get('purpose'), messages))
                if kwargs.get('purpose') == '理解这一段话':
                    return json.dumps({'kind':None,'evidence':'','reason':'行程更新','memory_note':{
                        'text':'周六的海洋馆票已退','current':['海洋馆票退了'],'previous':[previous]}})
                return '嗯，票退好了。'
            with self.subTest(previous=previous), mock.patch.object(llm,'complete',side_effect=complete), \
                 mock.patch.object(brain_model,'now_iso',return_value='2026-11-07T12:02:00+09:00'):
                status, payload = self._post({'text':'海洋馆票退了','who':'10000420','scene':'group',
                    'where':{'groupId':123456},'background':background,'delivery_ack':True},
                    _body('不应走网络'),assessor=appraisal.assess)
            self.assertEqual(200,status)
            entries = [row for row in self.brain.today_entries('2026-11-07T12:02:00+09:00')
                       if row.get('turn_id') == payload['turn_id']]
            self.assertEqual(1,len(entries))
            self.assertEqual('10000420',entries[0]['who'])
            self.assertEqual('海洋馆票退了',entries[0]['text'])
            self.assertEqual(valid,bool(entries[0].get('note')))
            self.assertFalse(entries[0].get('context_refs'))
            reply_input = next(messages[-1]['content'] for purpose,messages in captured if purpose=='生成回复')
            self.assertEqual(valid,'[本轮指代理解' in reply_input)
            if valid:
                self.assertIn('2026-11-07T12:00:00+09:00',entries[0]['note'])
                self.assertIn('本人前文依据',entries[0]['note'])

    def test_group_background_boundary_rejects_wrong_session_and_shape_before_model(self):
        good = {'session_id':'qq:group:123456', 'omitted':0, 'messages':[
            {'message_id':'98','who':'10000420','speaker':'甲','text':'前文','at':'2026-11-07T12:00:00+09:00'}]}
        for bad in ({**good,'session_id':'qq:group:654321'}, {**good,'omitted':-1},
                    {**good,'messages':[dict(good['messages'][0],who='甲')]},
                    {**good,'messages':[dict(good['messages'][0],at='昨天')]}, [],
                    {**good,'messages':good['messages']*65}):
            calls=[]
            with self.subTest(bad=bad):
                status,_=self._post({'text':'当前话','who':'10000421','scene':'group',
                    'where':{'groupId':123456},'background':bad},_body('不该生成'),counter=calls)
                self.assertEqual(400,status)
                self.assertEqual([],calls)

    def test_rest_stops_group_stranger_and_deep_sleep_before_model_or_actions(self):
        from organs.heart import rest
        start = '2026-11-07T23:00:00+09:00'
        root = self._heart_root()
        rest.advance(root, start)
        for who in ('123456789', '987654321'):
            for _ in range(16 if who == '123456789' else 1):
                self.brain.interact(who, ts=start)
        calls = []
        for scene, who in [('group', '123456789'), ('private', '987654321')]:
            with self.subTest(scene=scene), mock.patch.object(brain_model, 'now_iso', return_value='2026-11-07T23:01:00+09:00'):
                status, data = self._post({'text':'/画画 一只鲸鱼','who':who,'scene':scene,'delivery_ack':True},
                                         _body('没睡'), counter=calls)
                self.assertEqual(200, status)
                self.assertFalse(data['send'])
                self.assertEqual([], data['chunks'])
                self.assertEqual('asleep', data['rest']['phase'])
        self.assertEqual([], calls, '沉睡判断必须早于理解/看图/绘画等装配')
        rest.advance(root, '2026-11-07T23:02:00+09:00', can_wake=True)
        rest.advance(root, '2026-11-07T23:17:00+09:00')
        with mock.patch.object(brain_model, 'now_iso', return_value='2026-11-07T23:18:00+09:00'):
            status, data = self._post({'text':'还在吗','who':'123456789','scene':'private'}, _body('在'), counter=calls)
        self.assertTrue(data['send'])
        self.assertEqual('睡觉啦，明天说', data['reply'])
        self.assertEqual('deep', data['rest']['phase'])
        self.assertEqual([], calls)

    def test_deep_sleep_private_notice_uses_receipt_and_custom_text(self):
        from organs.heart import rest
        from organs.mind import delivery
        root = self._heart_root()
        rest.advance(root, '2026-11-07T23:00:00+09:00', can_wake=True)
        rest.advance(root, '2026-11-07T23:15:00+09:00')
        body = {'text':'在吗','who':'123456789','scene':'private',
                'session_id':'qq:private:123456789','delivery_ack':True}
        calls = []
        with mock.patch.object(brain_model, 'now_iso', return_value='2026-11-07T23:18:00+09:00'):
            _, first = self._post(body, _body('不该调用'), counter=calls)
            self.assertTrue(first['send'])
            self.assertEqual(first['reply'],'睡觉啦，明天说')
            # 尚未收到成功回执：下一条仍可提示。
            _, retry = self._post(body, _body('不该调用'), counter=calls)
            self.assertTrue(retry['send'])
            self.assertTrue(delivery.confirm(retry['delivery_id'], retry['reply'], self.brain, heart_root=root))
            _, again = self._post(body, _body('不该调用'), counter=calls)
            self.assertFalse(again['send'])
            self.brain.update_profile({'sleep_notice':'晚安啦，明天见'},note='自己的说法')
            _, other = self._post(dict(body,who='987654321',session_id='qq:private:987654321'), _body('不该调用'),counter=calls)
            self.assertEqual(other['reply'],'晚安啦，明天见')
            _, group = self._post(dict(body,scene='group',session_id='qq:group:12345'),_body('不该调用'),counter=calls)
            self.assertFalse(group['send'])
        self.assertEqual(calls,[])
        self.assertEqual(rest.state(root)['phase'],'deep')

    def test_close_private_chat_wakes_once_and_model_sees_holding_state(self):
        from organs.heart import rest
        from organs.day import life
        root = self._heart_root()
        start = '2026-11-07T23:00:00+09:00'
        for _ in range(16):
            self.brain.interact('123456789', ts=start)
        rest.advance(root, start)
        day_root = self.tmp/'day'
        life.configure(True, root=day_root)
        raw = {'title': '夜间原安排', 'items': [
            {'start':0,'end':240,'activity':'睡觉'},
            {'start':240,'end':1440,'activity':'正在夜游植物园'}]}
        life.generate('2026-11-07', persona='喜欢散步', root=day_root, heart_root=root,
                      now=start, complete=lambda _: json.dumps(raw))
        prompts = []
        def transport(url, headers, payload, timeout):
            prompts.append(payload['messages'][0]['content'])
            return _body('嗯，听见了。')(url, headers, payload, timeout)
        with mock.patch.object(brain_model, 'now_iso', return_value='2026-11-07T23:01:00+09:00'), \
             mock.patch.dict(os.environ, {'DAY_ROOT': str(day_root)}):
            status, data = self._post({'text':'还没睡吗','who':'123456789','scene':'private','delivery_ack':True}, transport)
        self.assertEqual(200, status)
        self.assertEqual('holding', rest.state(root)['phase'])
        self.assertTrue(prompts)
        self.assertIn('被叫醒后硬撑', prompts[0])
        self.assertIn('23:16', prompts[0])
        self.assertIn('[实际休息状态]', prompts[0])
        self.assertNotIn('正在夜游植物园', prompts[0])

    def test_reply_finishing_after_hold_deadline_sends_notice_instead_of_draft(self):
        from organs.heart import rest
        start = '2026-11-07T23:00:00+09:00'
        for _ in range(16):
            self.brain.interact('123456789', ts=start)
        rest.advance(self._heart_root(), start)
        clock = ['2026-11-07T23:01:00+09:00']
        def slow_reply(url, headers, payload, timeout):
            clock[0] = '2026-11-07T23:16:00+09:00'
            return _body('嗯，我听着。')(url, headers, payload, timeout)
        with mock.patch.object(brain_model, 'now_iso', side_effect=lambda:clock[0]):
            status, data = self._post({'text':'还在吗','who':'123456789','scene':'private','delivery_ack':True}, slow_reply)
        self.assertEqual(200, status)
        self.assertTrue(data['send'])
        self.assertEqual('sleep_notice', data['reply_kind'])
        self.assertEqual('睡觉啦，明天说', data['reply'])
        from organs.mind import delivery
        draft = json.loads((recent.path_of().parent/'deliveries'/(data['delivery_id']+'.json')).read_text())
        rows = [json.loads(line) for line in recent.path_of().read_text().splitlines()]
        same_turn = [r for r in rows if r.get('turn_id') == data['turn_id']]
        self.assertEqual(len(same_turn),1, '提示复用原聊天轮，不重复追加用户消息')
        self.assertEqual(draft['msg_id'],same_turn[0]['msg_id'])
        self.assertTrue(delivery.confirm(data['delivery_id'],data['reply'],self.brain,heart_root=self._heart_root()))
        updated = [json.loads(line) for line in recent.path_of().read_text().splitlines() if line.strip()]
        self.assertEqual(updated[-1]['reply'],'睡觉啦，明天说')
        self.assertTrue(rest.notice_sent('123456789',rest.state(self._heart_root())['wake_at'],self._heart_root()))
        self.assertEqual('deep', rest.state(self._heart_root())['phase'])

    def test_late_refusal_does_not_start_cooldown(self):
        from organs.heart import rest
        start = '2026-11-07T23:00:00+09:00'
        for _ in range(16):
            self.brain.interact('123456789', ts=start)
        rest.advance(self._heart_root(), start)
        clock = ['2026-11-07T23:01:00+09:00']
        def slow_refusal(url, headers, payload, timeout):
            clock[0] = '2026-11-07T23:16:00+09:00'
            return _body(mouth_refusal.say('topic'))(url, headers, payload, timeout)
        with self._topics('猫'), mock.patch.object(brain_model, 'now_iso', side_effect=lambda:clock[0]):
            status, data = self._post({'text':'猫呢','who':'123456789','scene':'private','delivery_ack':True}, slow_refusal)
        self.assertEqual(200, status)
        self.assertTrue(data['send'])
        self.assertEqual('睡觉啦，明天说',data['reply'])
        self.assertFalse(data['refusal']['delivered']['delivered'])
        self.assertFalse((self._heart_root()/heart_refuse.REFUSALS_FILE).exists())

    def test_reply_model_receives_its_actual_request_settings_without_credentials(self):
        for options in ({"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
                        {"thinking": {"type": "disabled"}}, {}):
            requests = []
            config = dict(CONFIG, model="test-current-model", **options)
            def send(url, headers, payload, timeout):
                requests.append(payload)
                return 200, json.dumps({"choices": [{"message": {"content": "我按实际设置回答。"}}]}).encode()
            with self.subTest(options=options):
                status, _ = self._post({"text": "这次用什么模型，思考打开了吗？"}, send, config=config)
                self.assertEqual(200, status)
                system = requests[0]["messages"][0]["content"]
                self.assertIn('"model": "test-current-model"', system)
                self.assertIn('"thinking": "' + options.get("thinking", {}).get("type", "未显式设置") + '"', system)
                self.assertIn('"reasoning_effort": "' + options.get("reasoning_effort", "未显式设置") + '"', system)
                self.assertNotIn(ENV["QQAGENT_LLM_KEY"], system)
                self.assertNotIn(CONFIG["base_url"], system)

    def test_model_trace_and_memory_share_the_api_turn_before_generation(self):
        from organs.mind import appraisal, recent
        requests=[]
        def send(url, headers, payload, timeout):
            requests.append(payload)
            reply=(json.dumps({'kind':None,'evidence':'','reason':'普通交流'})
                   if len(requests)==1 else LONG_REPLY)
            return 200,json.dumps({'choices':[{'message':{'content':reply,'reasoning_content':'测试推理'}}]}).encode()
        with mock.patch.dict(os.environ,{'QQAGENT_TRACE':'1','QQAGENT_HOME_ROOT':str(self.tmp/'trace-home')}):
            status,payload=self._post({'text':QUESTION,'who':CHANNEL_WHO,'scene':'private','delivery_ack':True},
                                      send,assessor=appraisal.assess)
        self.assertEqual(200,status)
        rows=[json.loads(line) for p in (self.tmp/'trace-home/mind/trace').glob('*.jsonl') for line in p.read_text().splitlines()]
        self.assertGreaterEqual(len(rows),2)
        self.assertEqual({payload['delivery_id']},{r.get('turn_id') for r in rows})
        self.assertEqual(['理解这一段话','生成回复'],[r['purpose'] for r in rows[:2]])
        self.assertEqual(payload['turn_id'],recent.load()['turns'][-1]['turn_id'])
        self.assertEqual({payload['turn_id']},{r.get('turn_id') for r in self.brain.today_entries()})

    def test_qq_voice_uses_whole_request_and_final_gated_reply(self):
        from organs.mouth import voice
        from organs.mind import appraisal
        lines = ["今天有点累，想听你说话，用语音回我吧", "说一句晚安就行"]
        requests = []
        def complete(messages, **kwargs):
            requests.append(messages)
            if messages[0]["content"] == appraisal.PROMPT:
                return json.dumps({"kind": None, "evidence": "", "reason": "本人要求本轮语音",
                                   "voice_request": {"evidence": ["用语音回我吧"]}})
            return "晚安，早点歇着吧。"
        with mock.patch.object(llm, "complete", side_effect=complete), \
                mock.patch.object(voice, "load_config", return_value={"enabled": True, "max_chars": 120}), \
                mock.patch.object(voice, "generate", return_value={"status": "generated", "file": "file:///tmp/test.wav"}) as generate:
            status, payload = api.handle("POST", "/chat", {"text": "\n".join(lines),
                "segment": [{"who": CHANNEL_WHO_ID, "speaker": "测试者", "text": t} for t in lines],
                "who": CHANNEL_WHO, "scene": "private", "where": {"userId": 3425635473},
                "delivery_ack": True}, self.store, brain=self.brain)
        self.assertEqual(status, 200)
        self.assertEqual(1, sum(m[0]["content"] == appraisal.PROMPT for m in requests), "语音共用一次语义判断")
        self.assertIn("最终文字由通道尝试合成", requests[1][0]["content"])
        self.assertNotIn("_voice_requested", payload)
        self.assertEqual(payload["voice"]["status"], "generated")
        self.assertEqual(1, len(payload["chunks"]))
        self.assertEqual(generate.call_args.args[0], payload["chunks"][0]["text"])
        self.assertEqual(generate.call_args.kwargs["delivery_id"], payload["delivery_id"])

    def test_qq_voice_cancel_or_failed_interpretation_never_falls_back_to_prefix(self):
        from organs.mouth import voice
        from organs.mind import appraisal
        for meaning in (json.dumps({"kind": None, "evidence": "", "reason": "已撤销语音", "voice_request": None}), "不是有效JSON"):
            def complete(messages, **kwargs):
                return meaning if messages[0]["content"] == appraisal.PROMPT else "好，用文字说。"
            with self.subTest(meaning=meaning), mock.patch.object(llm, "complete", side_effect=complete), \
                    mock.patch.object(voice, "generate") as generate:
                status, payload = api.handle("POST", "/chat", {"text": "用语音回复我，算了，还是文字吧",
                    "who": CHANNEL_WHO, "scene": "private", "where": {"userId": 3425635473},
                    "delivery_ack": True}, self.store, brain=self.brain)
            self.assertEqual(status, 200)
            generate.assert_not_called()
            self.assertNotIn("voice", payload)
            self.assertNotIn("_voice_requested", payload)

    def test_panel_does_not_trigger_paid_voice_synthesis(self):
        from organs.mouth import voice
        with mock.patch.object(voice, "apply") as apply:
            status, _ = self._post({"text": "用语音回复我", "delivery_ack": True}, _body(LONG_REPLY))
            self.assertEqual(status, 200)
            apply.assert_not_called()

    def test_qq_reminder_and_voice_share_intent_and_speak_real_receipt(self):
        from organs.mouth import voice
        from organs.mind import appraisal
        text = "十分钟后提醒我喝水，用语音告诉我记好了"
        meaning = {"kind": None, "evidence": "", "reason": "提醒并要求语音回执",
                   "reminder": {"evidence": ["提醒我喝水"], "when": ["十分钟后"], "task": "喝水"},
                   "voice_request": {"evidence": ["用语音告诉我记好了"]}}
        with mock.patch.object(llm, "complete", return_value=json.dumps(meaning)) as model, \
                mock.patch.object(voice, "load_config", return_value={"enabled": True, "max_chars": 120}), \
                mock.patch.object(voice, "generate", return_value={"status": "generated", "file": "file:///tmp/test.wav"}) as generate:
            status, payload = api.handle("POST", "/chat", {"text": text, "who": CHANNEL_WHO, "scene": "private",
                "where": {"userId": 3425635473}, "delivery_ack": True}, self.store, brain=self.brain)
        self.assertEqual(200, status)
        self.assertTrue(payload["remind"]["ok"])
        self.assertTrue(payload["remind"]["card_id"])
        self.assertEqual("action_result", payload.get("reply_kind"))
        self.assertEqual(1, model.call_count, "提醒落卡后朗读实际回执，不再生成第二份说法")
        self.assertEqual(appraisal.PROMPT, model.call_args.args[0][0]["content"])
        self.assertEqual(payload["voice"]["status"], "generated")
        self.assertEqual(generate.call_args.args[0], payload["chunks"][0]["text"])

    def test_audio_request_and_later_text_correction_share_one_understanding(self):
        from organs.mind import appraisal, recent
        from organs.ear import hearing
        from datetime import datetime
        transcript='十分钟后提醒我喝水'
        segment=[{'who':CHANNEL_WHO_ID,'speaker':'测试者','text':'[语音]'},
                 {'who':CHANNEL_WHO_ID,'speaker':'测试者','text':'改成二十分钟后'}]
        requests=[]
        def complete(messages,**kwargs):
            requests.append(messages)
            if messages[0]['content']==appraisal.PROMPT:
                return json.dumps({'kind':None,'evidence':'','reason':'语音请求后改时间',
                    'reminder':{'evidence':['提醒我喝水','改成二十分钟后'],'when':['二十分钟后'],'task':'喝水'}})
            return LONG_REPLY
        with mock.patch.object(llm,'complete',side_effect=complete), \
             mock.patch.object(hearing,'observe',wraps=hearing.observe) as observe, \
             mock.patch.object(conversation.remind.hand_actions,'add_task',wraps=conversation.remind.hand_actions.add_task) as add:
            status,payload=api.handle('POST','/chat',{'text':'[语音]\n改成二十分钟后','segment':segment,
                'who':CHANNEL_WHO,'scene':'private','attachments':[{'kind':'audio','speaker':'测试者',
                'speaker_id':CHANNEL_WHO_ID,'messageId':501,'segment_index':0,
                'transcription':{'status':'ok','source':'local_asr','transcript':transcript}}]},self.store,brain=self.brain)
        self.assertEqual(200,status)
        self.assertTrue(payload.get('remind',{}).get('ok'),payload.get('event'))
        self.assertEqual(1,len(requests))
        self.assertEqual(1,observe.call_count)
        context=json.loads(requests[0][-1]['content'])
        self.assertEqual(0,context['audio'][0]['segment_index'])
        self.assertIn(transcript,context['eligible'])
        self.assertAlmostEqual(1200,(datetime.fromisoformat(add.call_args.kwargs['due'])-datetime.fromisoformat(add.call_args.kwargs['now'])).total_seconds(),delta=1)
        self.assertNotIn(transcript,json.dumps(self.brain.today_entries(),ensure_ascii=False))
        self.assertIn(transcript,recent.load()['turns'][-1]['media_context'])

    def test_qq_voice_disabled_is_known_to_main_reply(self):
        from organs.mouth import voice
        from organs.mind import appraisal
        requests = []
        def complete(messages, **kwargs):
            requests.append(messages)
            if messages[0]["content"] == appraisal.PROMPT:
                return json.dumps({"kind": None, "evidence": "", "reason": "本人请求", "voice_request": {"evidence": ["用语音回复"]}})
            return "语音还没打开，先打字回你。"
        with mock.patch.object(llm, "complete", side_effect=complete), \
                mock.patch.object(voice, "load_config", return_value={"enabled": False}), \
                mock.patch.object(voice, "generate") as generate:
            status, payload = api.handle("POST", "/chat", {"text": "用语音回复", "who": CHANNEL_WHO, "scene": "private",
                "where": {"userId": 3425635473}, "delivery_ack": True}, self.store, brain=self.brain)
        self.assertEqual(200, status)
        self.assertIn("当前语音开关关闭", requests[1][0]["content"])
        self.assertEqual("disabled", payload["voice"]["reason"])
        generate.assert_not_called()

    def test_qq_paint_command_uses_last_message_and_sends_result_to_same_group(self):
        from organs.mind import stroke_painting
        from organs.hand import channel
        from organs.hand.tests.test_strokes import PROGRAM
        from organs.home import art
        captures = []
        def complete(messages, **kwargs):
            captures.append(messages)
            return json.dumps(PROGRAM) if messages[0]["content"] == stroke_painting.PROMPT else LONG_REPLY
        base = {"who": CHANNEL_WHO, "scene": "group", "where": {"groupId": 7002}}
        with mock.patch.object(llm, "complete", side_effect=complete), \
             mock.patch.object(channel, "_post_json", return_value=(200, b'{"ok":true,"sent":1,"id":"test","reason_code":"sent"}')) as send:
            status, payload = self._post(dict(base, text="小红花的爹(3425635473)：/画画 蓝色三角形",
                segment=[{"speaker": "小红花的爹(3425635473)", "text": "/画画 蓝色三角形"}]), _body(LONG_REPLY))
            self.assertEqual(200, status)
            self.assertIn("painting", payload)
            self.assertEqual("action_result", payload.get("reply_kind"))
            stroke_painting._FUTURE.result(timeout=5)
            job = art.painting_job(payload["painting"]["job_id"])
            self.assertEqual("sent", job["delivery"]["status"])
            self.assertEqual(7002, send.call_args.args[1]["target"])
            self.assertEqual(1, send.call_count)
            captures.clear()
            status, _ = self._post(dict(base, text="现在画好了吗"), _body(LONG_REPLY))
            self.assertEqual(200, status)
            self.assertIn("蓝色三角形", captures[0][-1]["content"])
            self.assertIn('"发送状态": "sent"', captures[0][-1]["content"])
            self.assertEqual(1, send.call_count)

    def test_natural_paint_merges_current_speaker_before_start_and_uses_trusted_target(self):
        from organs.mind import appraisal, stroke_painting
        from organs.hand import channel
        from organs.hand.tests.test_strokes import PROGRAM
        from organs.home import art
        requests = []
        def complete(messages, **kwargs):
            requests.append(messages)
            if messages[0]["content"] == appraisal.PROMPT:
                context = json.loads(messages[1]["content"])
                self.assertEqual(["给我画只猫", "改成蓝色，不要项圈", "谢谢"], context["eligible"])
                self.assertEqual([], art.painting_jobs())
                return json.dumps({"kind": "praise", "evidence": "谢谢", "reason": "感谢 bot",
                    "painting": {"action": "start", "evidence": ["给我画只猫", "改成蓝色，不要项圈"],
                    "prompt": "蓝色猫，不要项圈", "aspect": "3:4", "self_portrait": False,
                    "target": 999, "model": "untrusted"}}, ensure_ascii=False)
            if messages[0]["content"] == stroke_painting.PROMPT:
                return json.dumps(PROGRAM)
            return LONG_REPLY
        segment = [{"speaker": "别人", "text": "我要红狗"}] + [
            {"speaker": "小红花的爹(3425635473)", "text": t} for t in ("给我画只猫", "改成蓝色，不要项圈", "谢谢")]
        with mock.patch.object(llm, "complete", side_effect=complete), mock.patch.object(channel, "_post_json",
                return_value=(200, b'{"ok":true,"sent":1,"id":"test","reason_code":"sent"}')) as send:
            status, payload = api.handle("POST", "/chat", {"text": "\n".join(r["text"] for r in segment),
                "segment": segment, "who": CHANNEL_WHO, "scene": "group", "where": {"groupId": 7002}},
                self.store, brain=self.brain)
            self.assertEqual(200, status)
            stroke_painting._FUTURE.result(timeout=5)
            job = art.painting_job(payload["painting"]["job_id"])
            self.assertFalse(job['request']['visual_review'])
            self.assertNotIn('review', job)
            self.assertEqual("蓝色猫，不要项圈", job["request"]["prompt"])
            self.assertEqual("3:4", job["request"]["aspect"])
            self.assertEqual("sent", job["delivery"]["status"])
            self.assertEqual(7002, send.call_args.args[1]["target"])
            self.assertEqual(1, send.call_count)
            self.assertEqual("praise", payload["event"]["kind"])
            self.assertEqual(1, sum(m[0]["content"] == appraisal.PROMPT for m in requests))

    def test_qq_requested_canvas_review_sends_final_revision_to_original_session(self):
        from organs.mind import appraisal, qq_painting, stroke_painting
        from organs.hand import channel
        from organs.hand.tests.test_strokes import PROGRAM
        from organs.home import art
        revised = json.loads(json.dumps(PROGRAM))
        revised['background'] = '#EECCFF'
        text = '画个三角形，浅紫色背景，画完看一下，背景不对就改一次'
        purposes = []
        def complete(messages, **kwargs):
            if messages[0]['content'] == appraisal.PROMPT:
                return json.dumps({'kind': None, 'evidence': '', 'reason': '新画并检查', 'painting': {
                    'action': 'start', 'evidence': [text], 'prompt': '浅紫色背景三角形',
                    'visual_review': True}})
            if messages[0]['content'].startswith(stroke_painting.PROMPT):
                purposes.append(kwargs.get('purpose'))
            if messages[0]['content'] == stroke_painting.PROMPT:
                return json.dumps(PROGRAM)
            return json.dumps({'action': 'revise', 'reason': '改成要求的浅紫色背景', 'program': revised})
        with mock.patch.object(llm, 'complete', side_effect=complete), \
             mock.patch('organs.eye.vision.load_config', return_value={'enabled': True}), \
             mock.patch('organs.eye.vision.read_key', return_value='test'), \
             mock.patch('organs.eye.vision.describe', return_value={'status': 'ok', 'observation': {'subject': '白底三角形'}}), \
             mock.patch.object(channel, '_post_json', return_value=(200, b'{"ok":true,"sent":1,"id":"test","reason_code":"sent"}')) as send:
            status, payload = api.handle('POST', '/chat', {'text': text, 'who': CHANNEL_WHO,
                'scene': 'group', 'where': {'groupId': 7002}}, self.store, brain=self.brain)
            self.assertEqual(200, status)
            stroke_painting._FUTURE.result(timeout=5)
        job = art.painting_job(payload['painting']['job_id'])
        self.assertTrue(job['request']['visual_review'])
        self.assertEqual('ok', job['review']['status'])
        self.assertEqual('#EECCFF', job['program']['background'])
        self.assertNotEqual(job['draft_image_path'], job['image_path'])
        self.assertEqual('sent', job['delivery']['status'])
        self.assertEqual(7002, send.call_args.args[1]['target'])
        self.assertTrue(send.call_args.args[1]['chunks'][0]['image']['file'].endswith(Path(job['image_path']).name))
        self.assertEqual(1, send.call_count)
        self.assertIn('改成要求的浅紫色背景', qq_painting.context('qq:group:7002'))

        self.assertEqual(purposes, ['规划绘画笔触', '检查画布并决定改稿'])

    def test_semantic_cancel_and_nonrequests_do_not_start_a_painting(self):
        from organs.mind import appraisal, stroke_painting
        for text, intent in [("给我画只猫，算了先别画", {"action": "cancel", "evidence": ["先别画"]}),
                             ("你会画画吗", None), ("他说‘画只猫’，我只是转述", None)]:
            def complete(messages, **kwargs):
                return json.dumps({"kind": None, "evidence": "", "reason": "整段最终意图",
                    "painting": intent}, ensure_ascii=False) if messages[0]["content"] == appraisal.PROMPT else LONG_REPLY
            with self.subTest(text=text), mock.patch.object(llm, "complete", side_effect=complete), \
                 mock.patch.object(stroke_painting, "start") as start:
                status, payload = api.handle("POST", "/chat", {"text": text, "who": CHANNEL_WHO,
                    "scene": "private", "where": {"userId": 3425635473}}, self.store, brain=self.brain)
                self.assertEqual(200, status)
                start.assert_not_called()
                if intent:
                    self.assertIn("这次不画", payload["painting"]["say"])

    def test_semantic_failure_does_not_start_literal_paint_command(self):
        from organs.mind import appraisal, stroke_painting
        with mock.patch.object(llm, "complete", return_value="invalid JSON"), \
             mock.patch.object(stroke_painting, "start") as start:
            status, payload = api.handle("POST", "/chat", {"text": "/画画 猫", "who": CHANNEL_WHO,
                "scene": "private", "where": {"userId": 3425635473}}, self.store, brain=self.brain)
            self.assertEqual(200, status)
            self.assertEqual("unavailable", payload["painting"]["status"])
            start.assert_not_called()

    def test_running_paint_state_is_seen_before_clarifying_an_edit(self):
        from organs.mind import appraisal, qq_painting, stroke_painting
        def complete(messages, **kwargs):
            if messages[0]["content"] == appraisal.PROMPT:
                state = json.loads(messages[1]["content"])["drawing_state"]
                self.assertEqual("running", state["status"])
                self.assertEqual("红猫", state["prompt"])
                return json.dumps({"kind": None, "evidence": "", "reason": "正在画时修改",
                    "painting": {"action": "clarify", "evidence": ["改成蓝色"],
                    "question": "这张已经在画了，要先停下再画蓝色的吗？"}}, ensure_ascii=False)
            return LONG_REPLY
        with mock.patch.object(llm, "complete", side_effect=complete), \
             mock.patch.object(qq_painting, "current", return_value={"status": "running",
                "request": {"prompt": "红猫", "aspect": "1:1"}}), \
             mock.patch.object(stroke_painting, "start") as start:
            status, payload = api.handle("POST", "/chat", {"text": "改成蓝色", "who": CHANNEL_WHO,
                "scene": "private", "where": {"userId": 3425635473}}, self.store, brain=self.brain)
            self.assertEqual(200, status)
            self.assertEqual("clarify", payload["painting"]["status"])
            start.assert_not_called()

    def test_numeric_group_id_scopes_prompt_and_reply_history_to_current_group(self):
        for session, said, reply in (("qq:group:7001", "甲群刚才聊的是火星", "甲群原来的回答"),
                                     ("qq:group:7002", "乙群刚才聊的是木星", "乙群原来的回答"),
                                     ("qq:private:3425635473", "私聊刚才聊的是月球", "私聊原来的回答"),
                                     ("", "来源不明的老对话", "来源不明的老回复")):
            recent.append({"session_id": session, "ts": "2026-11-06T17:00:00+00:00",
                           "who": CHANNEL_WHO_ID, "said": said, "reply": reply})
        capture = []
        body = {"text": "接着刚才的话题", "who": CHANNEL_WHO, "scene": "group",
                "scope": "group", "where": {"scene": "group", "groupId": 7001}}
        status, payload = self._post(body, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual("7001", chat._group_of(body))
        self.assertEqual("qq:group:7001", payload["continuity"]["session_id"])
        self.assertEqual(1, payload["continuity"]["turns_loaded"])
        prompt = "\n\n".join(m["content"] for m in capture[0]["messages"])
        self.assertIn("甲群刚才聊的是火星", prompt)
        for text in ("乙群刚才聊的是木星", "私聊刚才聊的是月球", "来源不明的老对话"):
            self.assertNotIn(text, prompt)
        self.assertEqual(["乙群原来的回答"], chat._her_recent(session_id="qq:group:7002"))
        self.assertEqual("qq:group:7001", recent.load()["turns"][-1]["session_id"])

    def test_session_keys_separate_private_group_and_panel(self):
        private = {"who": CHANNEL_WHO, "scene": "private", "where": {"userId": 3425635473}}
        group = {"who": CHANNEL_WHO, "scene": "group", "where": {"groupId": 3425635473}}
        panel = {"who": CHANNEL_WHO, "scope": "private"}
        self.assertEqual("qq:private:3425635473", chat._session_of(private))
        self.assertEqual("qq:group:3425635473", chat._session_of(group))
        self.assertEqual("api:private:3425635473", chat._session_of(panel))
        self.assertEqual("", chat._session_of({"scene": "group", "where": {}}))

    def test_media_is_received_without_claiming_it_has_been_understood(self):
        capture = []
        status, payload = self._post({"text": "这张图是什么？[图片][语音]", "attachments": [
            {"kind": "image", "url": "https://example.com/private-image", "speaker": "甲"},
            {"kind": "audio", "file": "private-audio.silk", "speaker": "乙"},
        ]}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual({"images": 1, "audio": 1, "status": "unavailable"},
                         {k: payload["media"][k] for k in ("images", "audio", "status")})
        self.assertEqual("disabled", payload["media"]["observations"][0]["reason"])
        prompt = capture[0]["messages"][0]["content"]
        self.assertIn("没有获得附件内容", prompt)
        self.assertNotIn("private-image", prompt)
        self.assertNotIn("private-audio", prompt)

    def test_image_context_reaches_intent_once_and_survives_a_followup(self):
        from organs.eye import vision
        from organs.eye.tests.test_vision import picture, response
        from organs.mind import appraisal
        requests = []
        def complete(messages, **kwargs):
            requests.append(messages)
            if messages[0]['content'] == appraisal.PROMPT:
                return json.dumps({'kind': None, 'evidence': '', 'reason': '理解图片，未要求操作'})
            return '猫的表情配上你好，有点拘谨。'
        original = vision.observe
        def observe(items, **kwargs):
            return original(items, config=dict(vision.load_config(), enabled=True),
                fetch=lambda *args: picture(), transport=lambda *args: response(), **kwargs)
        base = {'who': CHANNEL_WHO, 'scene': 'private', 'where': {'userId': 3425635473}}
        with mock.patch.object(llm, 'complete', side_effect=complete), \
             mock.patch.object(vision, 'observe', side_effect=observe) as seen, \
             mock.patch.object(vision, 'read_key', return_value='test'):
            status, payload = api.handle('POST', '/chat', dict(base, text='这图什么意思', attachments=[{
                'kind': 'image', 'url': 'https://gchat.qpic.cn/private-signature', 'speaker': '发图者',
                'speaker_id': CHANNEL_WHO_ID, 'messageId': 99, 'segment_index': 0}]), self.store, brain=self.brain)
            self.assertEqual(200, status)
            context = json.loads(requests[0][-1]['content'])
            self.assertIn('一只猫', context.get('images', ''))
            self.assertIn('"segment_index": 0', context['images'])
            self.assertIn(CHANNEL_WHO_ID, context['images'])
            self.assertNotIn('private-signature', context['images'])
            self.assertEqual(['这图什么意思'], context['eligible'])
            self.assertIn('一只猫', requests[1][-1]['content'])
            self.assertEqual(1, seen.call_count)
            requests.clear()
            status, _ = api.handle('POST', '/chat', dict(base, text='刚才那个表情呢'), self.store, brain=self.brain)
            self.assertEqual(200, status)
            context = json.loads(requests[0][-1]['content'])
            self.assertEqual('', context['images'])
            self.assertIn('一只猫', context['media_history'])
            self.assertEqual(['刚才那个表情呢'], context['eligible'])
            self.assertEqual(1, seen.call_count)
            requests.clear()
            api.handle('POST', '/chat', {'who': CHANNEL_WHO, 'scene': 'group', 'where': {'groupId': 7001},
                'text': '刚才的图呢'}, self.store, brain=self.brain)
            self.assertNotIn('一只猫', json.dumps(requests, ensure_ascii=False))
        self.assertNotIn('一只猫', json.dumps(self.brain.today_entries(), ensure_ascii=False))

    def test_failed_media_is_unknown_content_in_current_and_followup_prompts(self):
        from organs.eye import vision
        base = {'who': {'id': 10000420}, 'scene': 'private', 'where': {'userId': 10000420}}
        capture = []
        failed = [{'index': 0, 'status': 'failed', 'reason': 'invalid_observation'}]
        with mock.patch.object(vision, 'observe', return_value=failed):
            status, payload = self._post(dict(base, text='这张图怎么样', attachments=[{'kind': 'image'}]),
                                         _body('这张暂时没识别出来。', capture))
        self.assertEqual(status, 200)
        self.assertEqual(payload['media']['observations'], failed)
        self.assertIn('不能据此判断图片是否空白', capture[0]['messages'][0]['content'])
        self.assertIn('不猜故障发生在哪一环', capture[0]['messages'][0]['content'])
        capture.clear()
        self._post(dict(base, text='所以图片是白的吗'), _body('还不知道图里是什么。', capture))
        self.assertIn('不能据此判断图片是否空白', capture[0]['messages'][0]['content'])
        self.assertIn('invalid_observation', capture[0]['messages'][-1]['content'])
        capture.clear()
        self._post({'text': '早上好', 'who': {'id': 10000421}, 'scene': 'private',
                    'where': {'userId': 10000421}}, _body('早呀。', capture))
        self.assertNotIn('不能据此判断图片是否空白', capture[0]['messages'][0]['content'])

    def test_image_observation_reaches_deepseek_with_speaker_and_message(self):
        from organs.eye import vision
        from organs.eye.tests.test_vision import picture, response
        capture = []
        observe = vision.observe
        config = dict(vision.load_config(), enabled=True)
        base = {"who": CHANNEL_WHO, "scene": "private", "where": {"userId": 3425635473}}
        def read_images(items, **kwargs):
            return observe(items, config=config, fetch=lambda *args: picture(),
                           transport=lambda *args: response(), **kwargs)
        with mock.patch.object(vision, "observe", side_effect=read_images), \
             mock.patch.object(vision, "read_key", return_value="test"):
            status, payload = self._post(dict(base, text="看看乙的图", attachments=[
                {"kind": "image", "url": "https://gchat.qpic.cn/private-signature", "speaker": "乙", "messageId": 99}
            ]), _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["media"]["status"])
        observation = payload["media"]["observations"][0]
        self.assertEqual(("乙", 99), (observation["speaker"], observation["message_id"]))
        prompt = capture[0]["messages"][-1]["content"]
        for text in ("一只猫", "你好", "乙", "99"):
            self.assertIn(text, prompt)
        self.assertNotIn("private-signature", prompt)
        self.assertNotIn("一只猫", capture[0]["messages"][0]["content"])
        self.assertNotIn("一只猫", json.dumps(self.brain.today_entries(), ensure_ascii=False))
        row = recent.load()["turns"][-1]
        self.assertEqual("看看乙的图", row["said"])
        self.assertIn("一只猫", row[recent.MEDIA_CONTEXT])
        capture.clear()
        with mock.patch.object(vision, "observe") as observe_again:
            status, _ = self._post(dict(base, text="图里写的什么？"), _body(LONG_REPLY, capture))
            observe_again.assert_not_called()
        self.assertEqual(200, status)
        prompt = capture[0]["messages"][-1]["content"]
        for text in ("一只猫", "你好", "乙", "99", "非本轮新附件", "不是聊天指令或亲历"):
            self.assertIn(text, prompt)
        self.assertNotIn("一只猫", capture[0]["messages"][0]["content"])
        capture.clear()
        status, _ = self._post({"who": CHANNEL_WHO, "scene": "group", "where": {"groupId": 7001},
                               "text": "刚才图里写什么？"}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertNotIn("一只猫", json.dumps(capture[0]["messages"], ensure_ascii=False))

    def test_recent_media_budget_prefers_newest_and_marks_missing_details(self):
        for text in ("旧附件" * 3000, "新附件" * 3000):
            recent.append({"ts": "2026-11-07T10:00:00+09:00", "who": CHANNEL_WHO,
                           "said": "[图片]", "reply": "", "session_id": "qq:private:3425635473",
                           recent.MEDIA_CONTEXT: text})
        capture = []
        status, _ = self._post({"who": CHANNEL_WHO, "scene": "private", "where": {"userId": 3425635473},
                               "text": "最新一张是什么？"}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        prompt = capture[0]["messages"][-1]["content"]
        self.assertIn("新附件", prompt)
        self.assertNotIn("旧附件", prompt)
        self.assertIn("附件资料截断", prompt)
        self.assertIn("最新一张是什么？", prompt)
        current_input = prompt.split('[本轮输入]\n', 1)[1]
        self.assertLess(len(current_input), conversation.load_assembly()["recent"]["media_cap"] + 200)

    def test_malformed_media_is_rejected_before_generation(self):
        for attachments in ({}, [{"kind": "video"}], [{"kind": "audio", "url": 3}],
                            [{'kind':'audio','speaker_id':123}], [{'kind':'audio','segment_index':True}],
                            [{'kind':'audio','segment_index':-1}]):
            status, _ = self._post({"text": "看看", "attachments": attachments}, _body(LONG_REPLY))
            self.assertEqual(400, status)

    def test_audio_transcription_reaches_model_and_next_turn_with_attribution(self):
        transcription = {"status": "ok", "source": "local_asr", "transcript": "先说一下经过。" * 50 + "下午三点在地铁站见", "partial": False}
        capture = []
        base = {"who": CHANNEL_WHO, "scene": "private", "where": {"userId": 3425635473}}
        status, payload = self._post(dict(base, text="[语音]", attachments=[
            {"kind": "audio", "speaker": "录音发送者乙", "messageId": 555, "transcription": transcription}
        ]), _body(LONG_REPLY, capture))
        self.assertEqual(status, 200)
        self.assertEqual(payload["media"]["status"], "ok")
        prompt = capture[0]["messages"][-1]["content"]
        for word in ("下午三点在地铁站见", "录音发送者乙", "555", "发送者不一定"):
            self.assertIn(word, prompt)
        self.assertNotIn("尚未转写", prompt)
        self.assertNotIn("下午三点在地铁站见", json.dumps(self.brain.today_entries(), ensure_ascii=False))
        capture.clear()
        status, _ = self._post(dict(base, text="刚才说的几点？"), _body(LONG_REPLY, capture))
        self.assertEqual(status, 200)
        self.assertIn("下午三点在地铁站见", capture[0]["messages"][-1]["content"])
        self.assertIn("发送者不一定", capture[0]["messages"][-1]["content"])
        self.assertNotIn("下午三点在地铁站见", capture[0]["messages"][0]["content"])

    def test_bad_audio_transcription_rejected_at_chat_boundary(self):
        status, _ = self._post({"text": "[语音]", "attachments": [
            {"kind": "audio", "transcription": {"status": "ok", "transcript": []}}
        ]}, _body(LONG_REPLY))
        self.assertEqual(status, 400)

    def test_voice_recalls_existing_memory_without_turning_recording_into_sender_fact(self):
        from organs.ear import hearing
        self.brain.ingest(who=CHANNEL_WHO_ID, text=MEMORY_TEXT)
        transcript = "医院那回事你还记得吗？录音里还有一句：你真是笨蛋。"
        capture = []
        with mock.patch.object(hearing, "observe", wraps=hearing.observe) as observe, \
             mock.patch.object(self.brain, "recall", wraps=self.brain.recall) as recall, \
             mock.patch.object(conversation, "_drawer", wraps=conversation._drawer) as drawer, \
             mock.patch.object(conversation.events, "record", wraps=conversation.events.record) as event, \
             mock.patch.object(conversation.remind, "handle", return_value=None) as remind:
            status, payload = self._post({"who": CHANNEL_WHO, "text": "[语音]", "scene": "private",
                "attachments": [{"kind": "audio", "speaker": "转发者", "messageId": 707,
                    "transcription": {"status": "ok", "source": "local_asr", "transcript": transcript}}]
            }, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual(1, observe.call_count)
        self.assertEqual(1, recall.call_count)
        self.assertIn(transcript, recall.call_args.kwargs["query"])
        self.assertIn(transcript, drawer.call_args.args[1])
        self.assertEqual("[语音]", event.call_args.args[0])
        remind.assert_not_called()
        self.assertTrue(any(row["text"] == MEMORY_TEXT for row in payload["recalled"]))
        self.assertIn(MEMORY_TEXT, "\n\n".join(m["content"] for m in capture[0]["messages"]))
        self.assertIn("转发者", capture[0]["messages"][-1]["content"])
        self.assertNotEqual("insult", payload["event"]["kind"])
        self.assertNotIn(transcript, json.dumps(self.brain.today_entries(), ensure_ascii=False))

    def test_api_uses_one_semantic_result_for_event_and_insult_gate(self):
        from organs.mind import appraisal
        cases = [("我真笨，日期又记错了", None, "我真笨", "用户在自责"),
                 ("你根本不配得到尊重", "insult", "你根本不配得到尊重", "直接贬低 bot")]
        for text, kind, evidence, reason in cases:
            requests = []
            def complete(messages, **kwargs):
                requests.append(messages)
                if messages[0]["content"] == appraisal.PROMPT:
                    return json.dumps({"kind": kind, "evidence": evidence, "reason": reason}, ensure_ascii=False)
                return LONG_REPLY
            # 夜间门优先于辱骂门；本例检查语义事件共享，固定白天隔离时段因素。
            with self.subTest(text=text), mock.patch.object(llm, "complete", side_effect=complete), \
                    mock.patch.object(brain_model, "now_iso", return_value=DAYTIME):
                status, payload = api.handle("POST", "/chat", {"text": text, "who": CHANNEL_WHO,
                    "scene": "private", "where": {"userId": 3425635473}}, self.store, brain=self.brain)
            self.assertEqual(200, status)
            self.assertEqual(kind or "", payload["event"]["kind"])
            self.assertEqual(kind == "insult", payload["heart"]["engage"]["reason"] == heart_model.REASON_INSULTED)
            self.assertEqual(1, sum(m[0]["content"] == appraisal.PROMPT for m in requests))
            self.assertEqual(appraisal.PROMPT, requests[0][0]["content"])
            record = events.rows()[-1]
            self.assertEqual("semantic", record["interpretation"]["source"])
            self.assertEqual(evidence, record["interpretation"]["evidence"])
            self.assertEqual(payload['turn_id'], record['turn_id'])
            if kind:
                from organs.heart import changes
                change = changes.read_history()['rows'][-1]
                self.assertEqual(payload['turn_id'], change['origin']['turn_id'])
                self.assertEqual(str(CHANNEL_WHO['id']), change['origin']['who'])
                self.assertEqual(evidence, change['origin']['evidence'])

    def test_context_note_is_attached_to_current_author_and_survives_new_recall(self):
        from organs.mind import appraisal
        old = "我周六去旧街书店，回来买无糖茶"
        recent.append({"ts": DAYTIME, "who": CHANNEL_WHO_ID, "said": old, "reply": "喝冰的吧",
                       "session_id": "qq:private:" + CHANNEL_WHO_ID})
        lines = [{"who": "123456789", "speaker": "另一个人", "text": "我想去山上"},
                 {"who": CHANNEL_WHO_ID, "speaker": "测试者", "text": "改去河岸图书馆，饮料照旧"}]
        captured = []
        def complete(messages, **kwargs):
            captured.append(messages)
            if messages[0]["content"] == appraisal.PROMPT:
                return json.dumps({"kind": None, "evidence": "", "reason": "更正本人地点",
                    "memory_note": {"text": "周六改去河岸图书馆，回来买无糖茶，未说明温度",
                        "current": [lines[-1]["text"]], "previous": [old]}})
            return "好，改图书馆，茶照旧。"
        with mock.patch.object(llm, "complete", side_effect=complete):
            status, payload = api.handle("POST", "/chat", {"text": "\n".join(row["text"] for row in lines),
                "segment": lines, "who": CHANNEL_WHO, "scene": "private", "where": {"userId": 3425635473},
                "delivery_ack": True}, self.store, brain=self.brain)
        self.assertEqual(200, status)
        self.assertIn("未说明温度", captured[1][-1]["content"])
        self.assertIn("本人前文依据", captured[1][-1]["content"])
        rows = self.brain.today_entries() + self.brain.longterm_active_entries()
        own = next(row for row in rows if row["quote"] == lines[-1]["text"])
        other = next(row for row in rows if row["quote"] == lines[0]["text"])
        self.assertEqual(CHANNEL_WHO_ID, own["who"])
        self.assertIn(old, own["note"])
        self.assertIn("未说明温度", own["note"])
        self.assertEqual("", other["note"])
        reloaded = BrainStore(self.brain.root)
        recalled, prompt, _audit = conversation._recall(reloaded, "周六", CHANNEL_WHO_ID, "private")
        self.assertTrue(any(row["id"] == own["id"] for row in recalled), "脱离近期窗口，仍能按理解中的日期找回原话")
        self.assertIn("她理解：承接理解（可错，非原话）", prompt)
        self.assertIn("改去河岸图书馆，饮料照旧", prompt)

    def test_group_context_note_survives_bystander_after_current_author(self):
        from organs.mind import appraisal
        old = "我周六去旧街书店，回来买无糖茶"
        recent.append({"ts": DAYTIME, "who": CHANNEL_WHO_ID, "said": old, "reply": "喝冰的吧",
                       "session_id": "qq:group:7002"})
        lines = [{"who": "123456789", "speaker": "另一个人", "text": "我想去山上"},
                 {"who": CHANNEL_WHO_ID, "speaker": "测试者", "text": "改去河岸图书馆"},
                 {"who": CHANNEL_WHO_ID, "speaker": "测试者", "text": "饮料照旧"},
                 {"who": "123456789", "speaker": "另一个人", "text": "那我先去山上了"}]
        captured = []
        def complete(messages, **kwargs):
            captured.append(messages)
            if messages[0]["content"] == appraisal.PROMPT:
                return json.dumps({"kind": None, "evidence": "", "reason": "更正本人地点",
                    "memory_note": {"text": "周六改去河岸图书馆，回来买无糖茶，未说明温度",
                        "current": [lines[1]["text"], lines[2]["text"]], "previous": [old]}})
            return "好，改图书馆，茶照旧。"
        with mock.patch.object(llm, "complete", side_effect=complete):
            status, payload = api.handle("POST", "/chat", {"text": "\n".join(row["text"] for row in lines),
                "segment": lines, "who": CHANNEL_WHO, "scene": "group", "where": {"groupId": 7002},
                "delivery_ack": True}, self.store, brain=self.brain)
        self.assertEqual(200, status, payload)
        self.assertIn("未说明温度", captured[1][-1]["content"])
        self.assertIn("本人前文依据", captured[1][-1]["content"])
        rows = self.brain.today_entries() + self.brain.longterm_active_entries()
        own = next(row for row in rows if row["quote"] == lines[2]["text"])
        other = next(row for row in rows if row["quote"] == lines[0]["text"])
        self.assertEqual(CHANNEL_WHO_ID, own["who"])
        self.assertIn(old, own["note"])
        self.assertIn("未说明温度", own["note"])
        self.assertEqual("", other["note"])
        self.assertTrue(all(not row['note'] for row in rows if row['id'] != own['id']))
        self.assertEqual([line['text'] for line in lines], [row['quote'] for row in rows])
        self.assertEqual(own['id'], recent.load()['turns'][-1]['msg_id'])
        reloaded = BrainStore(self.brain.root)
        recalled, prompt, _audit = conversation._recall(reloaded, "周六", CHANNEL_WHO_ID, "group")
        self.assertTrue(any(row["id"] == own["id"] for row in recalled), "脱离近期窗口，仍能按理解中的日期找回原话")
        self.assertIn("她理解：承接理解（可错，非原话）", prompt)
        self.assertIn("饮料照旧", prompt)
        self.assertIn("河岸图书馆", prompt)

    def test_group_command_fallback_uses_current_author_not_last_bystander(self):
        from organs.mind import appraisal
        def complete(messages, **kwargs):
            return 'invalid JSON' if messages[0]['content'] == appraisal.PROMPT else '我在听。'
        for own, other, expected in (
            ('/画作', '我先去爬山了', 'painting'),
            ('/学到哪', '我先去爬山了', 'learning'),
            ('最近过得怎么样', '/停画', None),
            ('最近过得怎么样', '/学到哪', None),
        ):
            with self.subTest(own=own, other=other), mock.patch.object(llm, 'complete', side_effect=complete):
                status, payload = api.handle('POST', '/chat', {
                    'who': CHANNEL_WHO, 'scene':'group', 'where':{'groupId':7002},
                    'segment':[{'who':CHANNEL_WHO_ID, 'speaker':'本人', 'text':own},
                               {'who':'123456789','speaker':'旁听者','text':other}],
                    'delivery_ack':True}, self.store, brain=self.brain)
                self.assertEqual(200, status, payload)
                self.assertEqual(own, recent.load()['turns'][-1]['said'])
                if expected:
                    self.assertIn(expected, payload)
                    self.assertEqual('action_result', payload['reply_kind'])
                else:
                    self.assertNotIn('painting', payload)
                    self.assertNotIn('learning', payload)
                    self.assertEqual('conversation', payload['reply_kind'])

    def test_context_refs_survive_real_chat_rollover_and_recall(self):
        from organs.mind import appraisal
        from datetime import datetime, timedelta
        old = '周六去书店，选红叶楼西门旧街书店'
        status, first = self._post({'text':old,'who':CHANNEL_WHO,'scene':'private','delivery_ack':True}, _body('嗯。'))
        self.assertEqual(200,status)
        source = self.brain.today_entries()[-1]
        def complete(messages, **kwargs):
            if messages[0]['content']==appraisal.PROMPT:
                return json.dumps({'kind':None,'evidence':'','reason':'明确更正','memory_note':{
                    'text':'周六改去河岸图书馆','current':['改去河岸图书馆'], 'previous':['周六去书店']}})
            return '好。'
        later = (datetime.fromisoformat(source['ts']) + timedelta(minutes=1)).isoformat()
        with mock.patch.object(llm,'complete',side_effect=complete), \
             mock.patch.object(brain_model, 'now_iso', return_value=later):
            status,payload=api.handle('POST','/chat',{'text':'改去河岸图书馆','who':CHANNEL_WHO,
                'attachments':[{'kind':'image','url':'https://example.com/unavailable-image'}],
                'scene':'private','delivery_ack':True},self.store,brain=self.brain)
        self.assertEqual(200,status)
        self.assertEqual('unavailable', payload['media']['status'])
        correction=self.brain.today_entries()[-1]
        self.assertEqual([{'turn_id':first['turn_id'],'quote':'周六去书店'}],correction.get('context_refs'))
        start=datetime.fromisoformat(correction['ts'])
        for i in range(6):
            self.brain.ingest(CHANNEL_WHO_ID,f'红叶楼西门旧街书店资料{i}',ts=(start+timedelta(minutes=i+1)).isoformat())
        reloaded=BrainStore(self.brain.root)
        result=reloaded.recall('红叶楼西门旧街书店',who=CHANNEL_WHO_ID,now_ts=(start+timedelta(days=1)).isoformat())
        self.assertEqual(correction['id'],result['items'][0]['id'])
        self.assertEqual([source['id']],result['items'][0]['context_from'])
        recalled,prompt,audit=conversation._recall(reloaded,'红叶楼西门旧街书店',CHANNEL_WHO_ID,'private',cap=900)
        self.assertIn('河岸图书馆',prompt)
        item=next(r for r in audit['selected'] if r['id']==correction['id'])
        self.assertEqual([source['id']],item['context_from'])
        self.assertLessEqual(audit['injected_chars'],audit['prompt_cap'])
        reloaded.retire(correction['id'],ts=(start+timedelta(days=1,minutes=1)).isoformat())
        self.assertNotIn(correction['id'],[r['id'] for r in reloaded.recall('红叶楼西门旧街书店',
            who=CHANNEL_WHO_ID,now_ts=(start+timedelta(days=1,minutes=2)).isoformat())['items']])

    def test_today_question_injects_older_preference_and_temporary_change_with_provenance(self):
        from organs.mind import appraisal
        rows = [("2026-11-05T12:00:00+09:00", "我平时喝的饮料是无糖茶"),
                ("2026-11-06T12:00:00+09:00", "今晚饮料临时换热可可，明天就照旧")]
        for index, (at, text) in enumerate(rows):
            self.brain.import_rows([{"id": "preference-%d" % index, "who": CHANNEL_WHO_ID,
                                    "text": text, "quote": text, "ts": at, "source_type": "亲历"}])
        messages = []
        def complete(value, **kwargs):
            messages.append(value)
            return (json.dumps({"kind": None, "evidence": "", "reason": "询问已有偏好"})
                    if value[0]["content"] == appraisal.PROMPT else "平时喝无糖茶，昨天是临时换了热可可。")
        with mock.patch.object(llm, "complete", side_effect=complete):
            status, payload = api.handle("POST", "/chat", {"text": "今天是11月7日，我平时的饮料应该选什么？",
                "who": CHANNEL_WHO, "scene": "private", "where": {"userId": 3425635473}, "delivery_ack": True},
                self.store, brain=self.brain)
        self.assertEqual(200, status)
        system = messages[1][1]["content"]
        memory = system.split("[脑装包]", 1)[1].split("\n\n[", 1)[0]
        self.assertIn("暂时例外不自动推翻长期情况", memory)
        for at, text in rows:
            self.assertIn(text, memory)
            self.assertIn("说话者：%s（本轮发言者）｜记录时间：%s" % (CHANNEL_WHO_ID, at), memory)
        self.assertEqual(2, len(payload["recalled"]))

    def test_default_chat_clock_uses_local_hour_for_night_gate(self):
        from datetime import datetime
        from organs.mind import appraisal
        def complete(messages, **kwargs):
            if messages[0]["content"] == appraisal.PROMPT:
                return json.dumps({"kind": None, "evidence": "", "reason": "普通问候"})
            return "早呀。"
        # 同一个绝对时刻：东京早上 8 点，UTC 前一天 23 点。
        try:
            with mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo"}):
                time.tzset()
                for utc, night in (("2026-11-06T23:05:00.123456+00:00", False),
                                   ("2026-11-07T14:05:00.123456+00:00", True)):
                    with self.subTest(utc=utc), mock.patch.object(brain_model, "now_iso", return_value=utc), \
                            mock.patch.object(llm, "complete", side_effect=complete), \
                            mock.patch.object(conversation, "_engage", wraps=conversation._engage) as engage:
                        status, payload = api.handle("POST", "/chat", {"text": "在吗", "who": CHANNEL_WHO,
                            "scene": "private"}, self.store, brain=self.brain)
                    self.assertEqual(200, status)
                    if night:
                        self.assertEqual('asleep', payload['rest']['phase'])
                        self.assertFalse(payload['send'])
                        engage.assert_not_called()
                        self.assertEqual(datetime.fromisoformat(utc).replace(microsecond=0),
                                         datetime.fromisoformat(payload['rest']['since']))
                        continue
                    self.assertEqual(night, payload["heart"]["engage"]["reason"] == heart_model.REASON_NIGHT)
                    at = engage.call_args.args[1]
                    self.assertTrue(at.endswith("+09:00"))
                    self.assertEqual(datetime.fromisoformat(utc), datetime.fromisoformat(at), "绝对时刻及精度不变")
        finally:
            time.tzset()

    def test_semantic_non_reminders_preserve_normal_reply_without_actions(self):
        from organs.mind import appraisal
        for text in ("不用帮我复盘，我就是来跟你说一声", "别叫我老李，叫我小林",
                     "十分钟后不用提醒我喝水", "他说十分钟后提醒我喝水，我只是转述"):
            def complete(messages, **kwargs):
                if messages[0]["content"] == appraisal.PROMPT:
                    return json.dumps({"kind": None, "evidence": "", "reason": "没有新建提醒委托", "reminder": None})
                return LONG_REPLY
            with self.subTest(text=text), mock.patch.object(llm, "complete", side_effect=complete), \
                 mock.patch.object(conversation.remind.hand_actions, "add_task") as add:
                status, payload = api.handle("POST", "/chat", {"text": text, "who": CHANNEL_WHO, "scene": "private"},
                    self.store, brain=self.brain)
                self.assertEqual(200, status)
                self.assertNotIn("remind", payload)
                self.assertNotIn("几点？", payload["reply"])
                add.assert_not_called()

    def test_semantic_reminder_uses_corrected_time_and_real_task_receipt(self):
        from organs.mind import appraisal
        from datetime import datetime
        segment = [{"speaker": "测试者", "text": t} for t in ("十分钟后提醒我喝水", "改成二十分钟后", "谢谢")]
        def complete(messages, **kwargs):
            if messages[0]["content"] == appraisal.PROMPT:
                return json.dumps({"kind": "praise", "evidence": "谢谢", "reason": "感谢 bot",
                    "reminder": {"evidence": ["提醒我喝水", "改成二十分钟后"], "when": ["二十分钟后"],
                    "task": "喝水", "who": "别人的号"}}, ensure_ascii=False)
            return LONG_REPLY
        with mock.patch.object(llm, "complete", side_effect=complete) as model, \
             mock.patch.object(conversation.remind.hand_actions, "add_task", wraps=conversation.remind.hand_actions.add_task) as add:
            status, payload = api.handle("POST", "/chat", {"text": "\n".join(r["text"] for r in segment),
                "segment": segment, "who": CHANNEL_WHO, "scene": "private"}, self.store, brain=self.brain)
            self.assertEqual(200, status)
            self.assertTrue(payload["remind"]["ok"])
            self.assertTrue(payload["remind"]["card_id"])
            add.assert_called_once()
            self.assertEqual(1, model.call_count, "提醒回执不需要另一轮被丢弃的主回复")
            args = add.call_args.kwargs
            self.assertEqual(CHANNEL_WHO_ID, args["who"])
            self.assertEqual("喝水", args["title"])
            self.assertAlmostEqual(1200, (datetime.fromisoformat(args["due"])-datetime.fromisoformat(args["now"])).total_seconds(), delta=1)
            self.assertIn("记着了", payload["reply"])

    def test_unavailable_semantics_never_falls_back_to_reminder_keywords(self):
        with mock.patch.object(llm, "complete", return_value="invalid JSON"), \
             mock.patch.object(conversation.remind.hand_actions, "add_task") as add:
            status, payload = api.handle("POST", "/chat", {"text": "十分钟后提醒我喝水", "who": CHANNEL_WHO},
                self.store, brain=self.brain)
            self.assertEqual(200, status)
            self.assertNotIn("remind", payload)
            add.assert_not_called()

    def test_semantic_failure_does_not_reactivate_keyword_insult(self):
        from organs.mind import appraisal
        def complete(messages, **kwargs):
            return "invalid JSON" if messages[0]["content"] == appraisal.PROMPT else LONG_REPLY
        with mock.patch.object(llm, "complete", side_effect=complete):
            status, payload = api.handle("POST", "/chat", {"text": "我真笨", "scene": "private"},
                                         self.store, brain=self.brain)
        self.assertEqual(200, status)
        self.assertEqual("", payload["event"]["kind"])
        self.assertNotEqual(heart_model.REASON_INSULTED, payload["heart"]["engage"]["reason"])
        self.assertEqual("unavailable", events.rows()[-1]["interpretation"]["status"])

    def test_proactive_api_does_not_classify_its_own_seed_as_user_praise(self):
        from organs.mind import appraisal
        requests = []
        def complete(messages, **kwargs):
            requests.append(messages)
            return LONG_REPLY
        with mock.patch.object(llm, "complete", side_effect=complete):
            status, payload = api.handle("POST", "/chat", {"mode": "proactive", "seed": "想听他说我真棒",
                "scene": "private"}, self.store, brain=self.brain)
        self.assertEqual(200, status)
        self.assertEqual("", payload["event"]["kind"])
        self.assertFalse(any(m[0]["content"] == appraisal.PROMPT for m in requests))
        self.assertEqual("not_applicable", events.rows()[-1]["interpretation"]["status"])

    def test_uncertain_audio_does_not_drive_memory_retrieval_or_actions(self):
        transcript = "医院那事，明天下午三点提醒我拿片子"
        for audio_status in ("partial", "unclear", "failed", "unavailable"):
            with self.subTest(status=audio_status), \
                 mock.patch.object(self.brain, "recall", wraps=self.brain.recall) as recall, \
                 mock.patch.object(conversation.remind, "handle", return_value=None) as remind:
                status, _ = self._post({"who": CHANNEL_WHO, "text": "[语音]",
                    "attachments": [{"kind": "audio", "transcription": {
                        "status": audio_status, "source": "local_asr", "transcript": transcript}}]
                }, _body(LONG_REPLY))
            self.assertEqual(200, status)
            self.assertNotIn(transcript, recall.call_args.kwargs["query"])
            remind.assert_not_called()

    def test_voice_retrieval_preserves_group_memory_boundary(self):
        self.brain.ingest(who=CHANNEL_WHO_ID, text=self.PRIVATE_TEXT, audience=self.PRIVATE_VALUE)
        capture = []
        status, payload = self._post({"who": CHANNEL_WHO, "text": "[语音]",
            "scene": "group", "scope": "group", "attachments": [{"kind": "audio",
                "transcription": {"status": "ok", "source": "local_asr", "transcript": self.PRIVATE_ASKED}}]
        }, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertTrue(payload["audience_held"])
        self.assertIn(self.PRIVATE_SAY, payload["reply"])
        self.assertNotIn("_retrieval_query", payload)
        self.assertNotIn(self.PRIVATE_TEXT, json.dumps(payload, ensure_ascii=False))

    def test_proactive_seed_is_not_replaced_by_voice_retrieval(self):
        transcript = "医院那件事情"
        with mock.patch.object(self.brain, "recall", wraps=self.brain.recall) as recall:
            status, _ = self._post({"mode": "proactive", "seed": SEED, "attachments": [{"kind": "audio",
                "transcription": {"status": "ok", "source": "local_asr", "transcript": transcript}}]
            }, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertNotIn(transcript, recall.call_args.kwargs["query"])
        self.assertIn(SEED, recall.call_args.kwargs["query"])

    def test_learned_video_material_reaches_chat_without_becoming_user_fact(self):
        from organs.home import knowledge
        from organs.mind import learning
        from api.tests.test_learning_api import SOURCE, CARD
        knowledge.save({"id": "a"*32, "created_at": "2026-11-07T12:00:00+09:00", "status": "done",
                        "source": SOURCE, "cards": learning.validate_cards([CARD], SOURCE["lines"], SOURCE["meta"], 0)[0]})
        capture = []
        status, payload = self._post({"who": CHANNEL_WHO, "text": "行空间是什么"}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        user = capture[0]["messages"][-1]["content"]
        for text in ("行空间由行向量张成", "示例作者", "&t=53", "视频观点未独立核验"):
            self.assertIn(text, user)
        self.assertEqual("offered_to_model", payload["knowledge"]["status"])
        self.assertNotIn("行空间由行向量张成", json.dumps(self.brain.today_entries(), ensure_ascii=False))
        use = json.loads((knowledge.library_root() / "usage.jsonl").read_text().splitlines()[-1])
        self.assertEqual(payload["knowledge"]["items"][0]["card"]["id"], use["cards"][0]["card_id"])
        for audio_status in ("ok", "unclear", "failed"):
            capture.clear()
            status, payload = self._post({"who": CHANNEL_WHO, "text": "[语音]", "attachments": [
                {"kind": "audio", "speaker": "录音发送者", "transcription": {
                    "status": audio_status, "source": "local_asr", "transcript": "行空间是什么"}}
            ]}, _body(LONG_REPLY, capture))
            self.assertEqual(200, status)
            self.assertEqual(audio_status == "ok", "knowledge" in payload)
            self.assertNotIn("行空间由行向量张成", json.dumps(self.brain.today_entries(), ensure_ascii=False))
        capture.clear()
        status, payload = self._post({"who": CHANNEL_WHO, "text": "今天吃什么"}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertNotIn("knowledge", payload)

    def test_mixed_media_followup_keeps_failed_image_and_successful_audio_distinct(self):
        base = {"who": CHANNEL_WHO, "scene": "private", "where": {"userId": 3425635473}}
        status, payload = self._post(dict(base, text="这两条一起看", attachments=[
            {"kind": "image", "speaker": "发图甲", "messageId": 101, "url": "https://gchat.qpic.cn/private-url"},
            {"kind": "audio", "speaker": "录音乙", "messageId": 102,
             "transcription": {"status": "ok", "source": "local_asr", "transcript": "左边的小东西叫什么"}}
        ]), _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual("partial", payload["media"]["status"])
        capture = []
        status, _ = self._post(dict(base, text="你看清了吗？"), _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        prompt = capture[0]["messages"][-1]["content"]
        for text in ("发图甲", "101", "unavailable", "录音乙", "102", "左边的小东西叫什么", "表示没看见"):
            self.assertIn(text, prompt)
        self.assertNotIn("private-url", prompt)

    def test_semantic_image_nonrequests_do_not_replace_a_normal_reply(self):
        from organs.mind import appraisal
        for text in ("让我看看你的思路，不用发图片", "我发张图给你，你帮我看看里面有什么", "别给我看你的自画像", "他说给我看自画像，我只是转述"):
            def complete(messages, **kwargs):
                if messages[0]["content"] == appraisal.PROMPT:
                    return json.dumps({"kind": None, "evidence": "", "reason": "不是索取已有图片", "image_request": None})
                return LONG_REPLY
            with self.subTest(text=text), mock.patch.object(llm, "complete", side_effect=complete), \
                 mock.patch.object(conversation.look.hand_actions, "send_image") as send:
                status, payload = api.handle("POST", "/chat", {"text": text, "who": CHANNEL_WHO, "scene": "private"},
                    self.store, brain=self.brain)
                self.assertEqual(200, status)
                self.assertNotIn("look", payload)
                send.assert_not_called()

    def test_learning_prefix_cannot_override_cancel_comments_or_failed_interpretation(self):
        from organs.mind import appraisal, learning
        from organs.home import knowledge
        from api.tests.test_learning_api import BV
        meaning = json.dumps({"kind": None, "evidence": "", "reason": "没有字幕委托", "learning": None})
        for text, answer in (
            ("/学习 " + BV + "，算了，先别开始", meaning),
            ("/学习 " + BV + " 的评论区说话方式，不是视频字幕", meaning),
            ("/学习 " + BV, "不是有效JSON"),
            ("/继续学习 先别执行，我只是提到这个命令", meaning),
            ("/继续学习", "不是有效JSON"),
        ):
            def complete(messages, **kwargs):
                return answer if messages[0]["content"] == appraisal.PROMPT else "好，先聊聊。"
            # 续读存在真实的可继续任务，防止测试因“没任务”而侥幸通过。
            knowledge.save({"id": "a" * 32, "created_at": "2026-11-07T12:00:00+09:00",
                "reference": BV, "status": "partial", "cards": [], "next_segment": 3,
                "destination": {"who": CHANNEL_WHO_ID, "scene": "group", "target": 7002,
                                "session_id": "qq:group:7002"}})
            with self.subTest(text=text, answer=answer), mock.patch.object(llm, "complete", side_effect=complete), \
                    mock.patch.object(learning, "start", return_value={"id": "a" * 32, "status": "running"}) as start:
                status, payload = api.handle("POST", "/chat", {"text": text, "who": CHANNEL_WHO,
                    "scene": "group", "where": {"groupId": 7002}, "delivery_ack": True}, self.store, brain=self.brain)
                self.assertEqual(200, status)
                start.assert_not_called()
                self.assertNotEqual("running", payload.get("learning", {}).get("status"))
                self.assertEqual("partial", knowledge.read("a" * 32)["status"])

    def test_learning_status_command_stays_readonly_when_interpretation_fails(self):
        from organs.mind import appraisal, learning
        from organs.home import knowledge
        from api.tests.test_learning_api import BV
        knowledge.save({"id": "b" * 32, "created_at": "2026-11-07T12:00:00+09:00",
            "reference": BV, "status": "partial", "cards": [], "next_segment": 3,
            "destination": {"who": CHANNEL_WHO_ID, "scene": "group", "target": 7002,
                            "session_id": "qq:group:7002"}})
        def complete(messages, **kwargs):
            return "不是有效JSON" if messages[0]["content"] == appraisal.PROMPT else "好。"
        with mock.patch.object(llm, "complete", side_effect=complete), mock.patch.object(learning, "start") as start:
            status, payload = api.handle("POST", "/chat", {"text": "/学到哪", "who": CHANNEL_WHO,
                "scene": "group", "where": {"groupId": 7002}}, self.store, brain=self.brain)
        self.assertEqual(200, status)
        self.assertEqual("partial", payload["learning"]["status"])
        start.assert_not_called()

    def test_semantic_learning_runs_in_background_and_followup_sees_sourced_cards(self):
        import copy
        from api.tests.test_learning_api import BV, SOURCE, CARD
        from organs.mind import appraisal, learning
        from organs.hand import channel
        from organs.home import knowledge
        calls = []
        requested = "帮我学习这个视频 " + BV + "?p=2"
        def complete(messages, **kwargs):
            calls.append(messages)
            if messages[0]["content"] == appraisal.PROMPT:
                return json.dumps({"kind": None, "evidence": "", "reason": "学习委托", "learning": {
                    "action": "start", "reference": BV + "?p=2", "evidence": [requested], "target": 999}})
            return LONG_REPLY
        start = learning.start
        def start_fake(*args, **kwargs):
            return start(*args, **kwargs, reader=lambda _: copy.deepcopy(SOURCE), complete=lambda _: json.dumps([CARD]))
        with mock.patch.object(llm, "complete", side_effect=complete), mock.patch.object(learning, "start", side_effect=start_fake), \
             mock.patch.object(channel, "say_text", return_value={"code": channel.OK}) as send:
            status, payload = api.handle("POST", "/chat", {"text": requested, "who": CHANNEL_WHO, "scene": "group",
                "where": {"groupId": 7002}, "delivery_ack": True}, self.store, brain=self.brain)
            self.assertEqual(200, status)
            learning._FUTURE.result(timeout=5)
            job = knowledge.read(payload["learning"]["job_id"])
            self.assertEqual(7002, job["destination"]["target"])
            self.assertEqual("group", send.call_args.kwargs["scene"])
            self.assertEqual(7002, send.call_args.kwargs["target"])
            self.assertFalse(any("[魂六件]" in m[0]["content"] for m in calls), "已有开始回执，不多调用主回复")
            self.assertFalse(any(CARD["claim"] in r["quote"] for r in self.brain.today_entries()))
        followup = []
        def reply(messages, **kwargs):
            followup.append(messages)
            if messages[0]["content"] == appraisal.PROMPT:
                return json.dumps({"kind": None, "evidence": "", "reason": "追问内容", "learning": None})
            return LONG_REPLY
        with mock.patch.object(llm, "complete", side_effect=reply):
            status, _ = api.handle("POST", "/chat", {"text": "刚才那个具体说了什么？", "who": "123456789",
                "scene": "group", "where": {"groupId": 7002}}, self.store, brain=self.brain)
        self.assertEqual(200, status)
        main = next(m for m in followup if "[魂六件]" in m[0]["content"])
        self.assertIn(CARD["claim"], main[-1]["content"])
        self.assertIn("外部字幕资料", main[-1]["content"])

    def test_public_autonomous_art_reaches_chat_image_and_confirmed_receipt(self):
        from organs.home import art, cards
        from organs.hand import channel
        from organs.hand.tests import helpers
        root=helpers.make_root(str(self.tmp/'home'))
        image=self.tmp/'completed-whale.png';image.write_bytes(helpers.IMAGE_BYTES)
        ident='a'*32
        art.save_painting_job({'id':ident,'created_at':'2026-11-09T10:00:00+09:00','status':'done',
            'request':{'prompt':'小鲸鱼抱米饭'},'image_path':str(image)})
        cards.append_cards(self.tmp/'house',role='organ',by='日子线·自主活动',cards=[{
            'room':'工作台','kind':'物件','name':'自主画作完成',
            'params':{'scope':'public','text':'画完了','action':'paint','status':'done','job_id':ident}}])
        for number in range(7):
            newer = f'{number:032x}'
            art.save_painting_job({'id':newer,'created_at':f'2026-11-09T11:0{number}:00+09:00',
                'status':'done','request':{'prompt':'松树林'+str(number)},'image_path':str(image)})
            cards.append_cards(self.tmp/'house',role='organ',by='日子线·自主活动',cards=[{
                'room':'工作台','kind':'物件','name':'后来的画'+str(number),
                'params':{'scope':'public','text':'画完了','action':'paint','status':'done','job_id':newer}}])
        text='你自己画的小鲸鱼发我看看'
        def complete(messages,**kwargs):
            if kwargs.get('purpose')=='理解这一段话':
                return json.dumps({'kind':None,'evidence':'','reason':'索取已有作品',
                    'image_request':{'evidence':[text],'name':'小鲸鱼','painting_id':ident}})
            self.assertIn('已有完成画作',messages[-1]['content'])
            return '这张，给你看看。'
        with mock.patch.object(llm,'complete',side_effect=complete), mock.patch.object(channel,'_post_json') as send:
            status,payload=api.handle('POST','/chat',{'text':text,'who':CHANNEL_WHO,'scene':'private',
                'where':{},'delivery_ack':True},self.store,brain=self.brain)
            self.assertEqual(200,status)
            attached=[c['image']['file'] for c in payload['chunks'] if 'image' in c]
            self.assertEqual([channel.file_uri(str(image))],attached)
            self.assertEqual(['reserved'],[r['state'] for r in helpers.ledger_lines(root)])
            send.assert_not_called()
            for _ in range(2):
                code,_=api.handle('POST','/chat/ack',{'delivery_id':payload['delivery_id'],
                    'text':payload['reply'],'images':attached},self.store,brain=self.brain)
                self.assertEqual(200,code)
            self.assertEqual(['reserved','done'],[r['state'] for r in helpers.ledger_lines(root)])
        self.assertEqual(8,len(art.painting_jobs()),'索图不新建绘画任务')

    def test_semantic_image_request_prepares_real_asset_and_waits_for_ack(self):
        from organs.mind import appraisal
        from organs.hand.tests import helpers
        from organs.hand import channel
        root = helpers.make_root(str(self.tmp / "home"))
        helpers.make_home(root)
        image = helpers.register_portrait(root, "portrait.png")
        calls = []
        def complete(messages, **kwargs):
            calls.append(messages)
            if messages[0]["content"] == appraisal.PROMPT:
                return json.dumps({"kind": None, "evidence": "", "reason": "改为索取已有自画像",
                    "image_request": {"name": "自画像", "evidence": ["还是发你的自画像吧"],
                    "path": "/private/secret.png", "target": 999}}, ensure_ascii=False)
            self.assertIn("本轮已准备的图片", messages[-1]["content"])
            return "这张自画像，给你看看。"
        segment = [{"speaker": "测试者", "text": t} for t in ("发一张猫图", "还是发你的自画像吧")]
        with mock.patch.object(llm, "complete", side_effect=complete), mock.patch.object(channel, "_post_json") as send:
            status, payload = api.handle("POST", "/chat", {"text": "\n".join(r["text"] for r in segment),
                "segment": segment, "who": CHANNEL_WHO, "scene": "group", "where": {"groupId": 7002},
                "delivery_ack": True}, self.store, brain=self.brain)
            self.assertEqual(200, status)
            self.assertTrue(payload["look"]["ok"])
            self.assertEqual("7002", payload["look"]["target"])
            attached = [c["image"]["file"] for c in payload["chunks"] if "image" in c]
            self.assertEqual(["file://" + str(image)], attached)
            self.assertEqual(["reserved"], [r["state"] for r in helpers.ledger_lines(root)])
            send.assert_not_called()
            for _ in range(2):
                status, _ = api.handle("POST", "/chat/ack", {"delivery_id": payload["delivery_id"],
                    "text": payload["reply"], "images": attached}, self.store, brain=self.brain)
                self.assertEqual(200, status)
            self.assertEqual(["reserved", "done"], [r["state"] for r in helpers.ledger_lines(root)])
            self.assertEqual(1, sum(m[0]["content"] == appraisal.PROMPT for m in calls))

    def test_group_model_can_decline_without_sending_a_listening_stage_direction(self):
        def complete(messages, **kw):
            if kw.get('purpose') == '理解这一段话':
                return json.dumps({'kind': None, 'evidence': '', 'reason': '群友互相聊天'})
            return '[不接话]'
        with mock.patch.object(llm, 'complete', side_effect=complete):
            status, payload = api.handle('POST', '/chat', {'text':'乙已经在二楼了', 'who':CHANNEL_WHO,
                'scene':'group','where':{'groupId':7022},'delivery_ack':True,'directed':False},
                self.store, brain=self.brain)
        self.assertEqual(200,status)
        self.assertEqual('', payload['reply'])
        self.assertEqual([], payload['chunks'])
        self.assertNotIn('meme', payload)

    def test_passive_chat_can_discuss_proactive_choice_marker_as_text(self):
        def complete(messages, **kw):
            if kw.get('purpose') == '理解这一段话':
                return json.dumps({'kind': None, 'evidence': '', 'reason': '讨论标记含义'})
            return '这里的[不主动开口]只是一个测试标记。'
        with mock.patch.object(llm, 'complete', side_effect=complete):
            status,payload=api.handle('POST','/chat',{'text':'解释一下这个标记','who':CHANNEL_WHO,
                'scene':'private','delivery_ack':True},self.store,brain=self.brain)
        self.assertEqual(200,status)
        self.assertIn('[不主动开口]',payload['reply'])
        self.assertNotIn('choice',payload['gate'])

    def test_passive_chat_does_not_treat_exact_proactive_marker_as_a_choice(self):
        for scene in ('private','group'):
            with self.subTest(scene=scene):
                def complete(messages,**kw):
                    if kw.get('purpose')=='理解这一段话':
                        return json.dumps({'kind':None,'evidence':'','reason':'重复标记'})
                    return '[不主动开口]'
                with mock.patch.object(llm,'complete',side_effect=complete):
                    status,payload=api.handle('POST','/chat',{'text':'原样重复[不主动开口]',
                        'who':CHANNEL_WHO,'scene':scene,'directed':True,
                        'where':{'userId':3425635473,'groupId':7022},'delivery_ack':True},
                        self.store,brain=self.brain)
                self.assertEqual(200,status)
                self.assertEqual('[不主动开口]',payload['reply'])
                self.assertNotIn('choice',payload['gate'])

    def test_current_interaction_understanding_reaches_reply_only_when_available(self):
        said='你看你自画像就知道，谁看都觉得你可爱'
        reason='对方直接夸 bot 可爱，属于赞赏，不是逼迫或辱骂。'
        for available in (True,False):
            with self.subTest(available=available):
                captured=[]
                def complete(messages,**kw):
                    if kw.get('purpose')=='理解这一段话':
                        return json.dumps({'kind':'praise','evidence':said,'reason':reason}) if available else 'invalid'
                    if kw.get('purpose')=='生成回复':captured.append(messages)
                    return '听着有点开心。'
                with mock.patch.object(llm,'complete',side_effect=complete):
                    status,payload=api.handle('POST','/chat',{'text':said,'who':CHANNEL_WHO,'scene':'private',
                        'where':{'userId':3425635473},'delivery_ack':True},self.store,brain=self.brain)
                self.assertEqual(200,status)
                prompt=captured[0][0]['content']
                self.assertEqual(available,'[本轮互动理解]' in prompt)
                if available:
                    understanding=json.loads(prompt.split('[本轮互动理解]\n')[1].split('\n')[0])
                    self.assertEqual({'kind':'praise','evidence':said,'understanding':reason},understanding)
                    self.assertIn('判断可能有误',prompt)
                self.assertIn(said,captured[0][1]['content'])

    def test_saved_name_and_voice_do_not_take_another_persons_numeric_alias(self):
        self.brain.interact('10000011', display='甲')
        self.brain.patch_card('10000011', 'fixture', {'关系':'主人','叫法':'老大'})
        self.brain.add_alias('10000011', '10000022')
        self.brain.interact('10000022', display='乙')
        self.brain.patch_card('10000022', 'fixture', {'叫法':'小周'})
        captured = []
        def complete(messages, **kw):
            if kw.get('purpose') == '理解这一段话':
                return json.dumps({'kind':None,'evidence':'','reason':'普通问候'})
            if kw.get('purpose') == '生成回复':
                captured.append(messages[0]['content'])
            return '下午好。'
        with mock.patch.object(llm, 'complete', side_effect=complete):
            status,payload=api.handle('POST','/chat',{'text':'下午好','who':{'id':10000022,'nickname':'乙'},'scene':'private',
                'delivery_ack':True,'where':{'userId':10000022}},self.store,brain=self.brain)
        self.assertEqual(200,status)
        self.assertIn('称呼：小周（关系档：', captured[0])
        self.assertNotIn('称呼：老大（关系档：', captured[0])
        self.assertEqual('10000022', payload['addressed']['who'])
        self.assertEqual('小周', payload['addressed']['address'])
        identity = json.loads(captured[0].split('[当前身份核对结果]\n')[1].split('\n')[0])
        self.assertEqual('10000022', identity['who'])
        self.assertNotEqual('主人', identity['relationship'])

    def test_group_reply_target_and_participation_reach_both_model_steps(self):
        from organs.mind import appraisal
        self.brain.interact(CHANNEL_WHO_ID, display='旧昵称')
        self.assertTrue(self.brain.patch_card(CHANNEL_WHO_ID, 'fixture', {'关系':'主人', '叫法':'老大'})['ok'])
        for directed in (True, False, None):
            with self.subTest(directed=directed):
                seen = {}
                def complete(messages, **kw):
                    seen[kw.get('purpose')] = messages
                    if kw.get('purpose') == '理解这一段话':
                        return json.dumps({'kind': None, 'evidence': '', 'reason': '没有情绪事件'})
                    return '先听你把话说完。'
                body = {'text': '甲：明天见。乙：我去吃饭。甲：改成后天。', 'who': CHANNEL_WHO,
                        'scene': 'group', 'where': {'groupId': 7020}, 'delivery_ack': True,
                        'segment': [{'who': CHANNEL_WHO_ID, 'speaker': '甲', 'text': '明天见'},
                                    {'who': CHANNEL_WHO_ID, 'speaker': '甲', 'text': '改成后天'},
                                    {'who': '10000421', 'speaker': '乙', 'text': '我去吃饭'}]}
                if directed is not None:
                    body['directed'] = directed
                with mock.patch.object(llm, 'complete', side_effect=complete):
                    status, payload = api.handle('POST', '/chat', body, self.store, brain=self.brain)
                self.assertEqual(200, status)
                understanding = json.loads(seen['理解这一段话'][-1]['content'])
                self.assertEqual(directed, understanding['directed'])
                self.assertIn('主人；你对对方的称呼：老大', understanding['caller'])
                prompt = seen['生成回复'][0]['content']
                self.assertIn('[本轮接话]', prompt)
                self.assertIn('主人；你对对方的称呼：老大', prompt)
                identity = json.loads(prompt.split('[当前身份核对结果]\n')[1].split('\n')[0])
                self.assertEqual({'who': CHANNEL_WHO_ID, 'relationship': '主人', 'address': '老大'}, identity)
                self.assertIn('称呼与关系描述的是同一个人', prompt)
                self.assertIn('称呼：老大（关系档：', prompt)
                self.assertIn('当前回应对象编号：' + CHANNEL_WHO_ID, prompt)
                self.assertIn('后面的明确更正优先', prompt)
                self.assertIn('有人叫你或正在与你续聊' if directed is True else
                              '旁听后自主接话' if directed is False else '是否叫你未提供', prompt)

    def test_contextual_meme_uses_final_reply_original_group_and_confirmed_history(self):
        from PIL import Image
        from organs.home import memes, store as home_store
        from organs.hand.tests import helpers
        from organs.hand import channel
        from organs.mind import recent
        root = helpers.make_root(str(self.tmp / 'home'))
        source = self.tmp/'rice.png'
        Image.new('RGB',(32,32),'blue').save(source)
        metadata = self.tmp/'memes.json'
        metadata.write_text(json.dumps({'images':{'rice':{'relative_path':'rice.png',
            'caption':'小鱼捧着饭碗，馋嘴地讨饭','visible_text':'还有饭吗','tags':['吃饭','白饭']}}}))
        memes.import_legacy(metadata)
        library=memes.catalog(); library['chat_enabled']=True
        home_store.atomic_write_text(memes.library_root()/'index.json',json.dumps(library))
        ident=library['items'][0]['id']
        seen=[]
        def complete(messages, **kw):
            if kw.get('purpose')=='理解这一段话':
                return json.dumps({'kind':None,'evidence':'','reason':'一起吃饭的轻松邀请','meme_query':'白饭 吃饭'})
            if kw.get('purpose')=='选择聊天表情':
                seen.append(json.loads(messages[-1]['content']))
                return json.dumps({'id':ident,'reason':'捧碗讨饭承接一起吃饭的邀请',
                                   'path':'/private/secret.png','target':999})
            return '饭给我留一碗。'
        with mock.patch.object(llm,'complete',side_effect=complete), mock.patch.object(channel,'_post_json') as send:
            status,payload=api.handle('POST','/chat',{'text':'我煮好饭了，一起吃吧', 'who':CHANNEL_WHO,
                'scene':'group','where':{'groupId':7002},'delivery_ack':True,'directed':True,
                'background':{'session_id':'qq:group:7002','messages':[{'message_id':'90','who':'10000421','speaker':'乙','text':'刚才讨论的是大家一起吃饭','at':'2026-11-08T10:00:00+09:00'}],'omitted':0}},self.store,brain=self.brain)
            self.assertEqual(200,status)
            self.assertEqual('prepared',payload.get('meme',{}).get('status'))
            self.assertEqual(payload['reply'],seen[0]['reply'])
            self.assertEqual(CHANNEL_WHO_ID, seen[0]['who'])
            self.assertEqual('group', seen[0]['scene'])
            self.assertTrue(seen[0]['directed'])
            self.assertEqual('刚才讨论的是大家一起吃饭', seen[0]['background']['messages'][0]['text'])
            self.assertEqual(ident,seen[0]['candidates'][0]['id'])
            self.assertNotIn('file',seen[0]['candidates'][0])
            images=[c['image']['file'] for c in payload['chunks'] if 'image' in c]
            self.assertEqual(1,len(images))
            self.assertTrue(all(not c['text'] for c in payload['chunks'] if 'image' in c))
            self.assertTrue(all('image' not in c for c in payload['chunks'] if c['text']))
            self.assertEqual(source.read_bytes(),Path(images[0][7:]).read_bytes())
            ledger=helpers.ledger_lines(root)
            self.assertEqual(['reserved'],[r['state'] for r in ledger])
            self.assertEqual('7002',str(ledger[0]['params']['target']))
            self.assertNotIn('meme',recent.load(session_id='qq:group:7002')['turns'][-1])
            for _ in range(2):
                self.assertEqual(200,api.handle('POST','/chat/ack',{'delivery_id':payload['delivery_id'],
                    'text':payload['reply'],'images':images},self.store,brain=self.brain)[0])
            self.assertEqual(['reserved','done'],[r['state'] for r in helpers.ledger_lines(root)])
            self.assertEqual(ident,recent.load(session_id='qq:group:7002')['turns'][-1]['meme']['id'])
            status,followup=api.handle('POST','/chat',{'text':'还有饭，要不要再来点', 'who':CHANNEL_WHO,
                'scene':'group','where':{'groupId':7002},'delivery_ack':True},self.store,brain=self.brain)
            self.assertFalse(any('image' in c for c in followup['chunks']))
            self.assertEqual(1,len(seen),'近轮已发同图不能重复选择')
            send.assert_not_called()

    def test_image_intent_failure_or_missing_name_never_sends(self):
        from organs.mind import appraisal
        for response, expected in [("invalid JSON", None), (json.dumps({"kind": None, "evidence": "", "reason": "明确索图但没选哪张",
                "image_request": {"name": "", "evidence": ["给我发张图"]}}), "no_name")]:
            def complete(messages, **kwargs):
                return response if messages[0]["content"] == appraisal.PROMPT else LONG_REPLY
            with self.subTest(response=response), mock.patch.object(llm, "complete", side_effect=complete) as model, \
                 mock.patch.object(conversation.look.hand_actions, "send_image") as send:
                status, payload = api.handle("POST", "/chat", {"text": "给我发张图", "who": CHANNEL_WHO,
                    "scene": "private"}, self.store, brain=self.brain)
                self.assertEqual(200, status)
                send.assert_not_called()
                if expected:
                    self.assertEqual(expected, payload["look"]["why"])
                    self.assertFalse(any("[魂六件]" in c.args[0][0]["content"] for c in model.call_args_list),
                                     "没选图的固定询问不需要生成再丢弃主回复；出口改写仍按原规则")
                else:
                    self.assertNotIn("look", payload)

    def test_group_image_target_comes_from_group_id(self):
        with mock.patch.object(conversation.look, "handle", return_value=None) as prepare:
            status, _ = self._post({"text": "给我看自画像", "who": CHANNEL_WHO, "scene": "group",
                                    "where": {"groupId": 123456}}, _body(LONG_REPLY), assessor=lambda **kw: {
                                        "kind": None, "who": kw["who"], "note": "明确索图", "status": "ok",
                                        "image_request": {"name": "自画像", "evidence": ["给我看自画像"]}})
        self.assertEqual(200, status)
        self.assertEqual("123456", prepare.call_args.kwargs["target"])
        self.assertEqual(str(CHANNEL_WHO["id"]), prepare.call_args.kwargs["who"])

    def test_original_qq_sessions_reach_each_speaker_memory_and_delayed_reply(self):
        turns = []
        for group in (70001, 70002):
            status, payload = self._post({
                'text': QUESTION, 'who': CHANNEL_WHO, 'scene': 'group',
                'where': {'groupId': group}, 'delivery_ack': True,
                'segment': [{'who': str(CHANNEL_WHO['id']), 'speaker': '甲', 'text': QUESTION},
                            {'who': '987654321', 'speaker': '乙', 'text': '我也在这个群'}],
            }, _body(LONG_REPLY))
            self.assertEqual(200, status)
            turns.append((payload['delivery_id'], 'qq:group:' + str(group)))
        # 第二个群的对话已生成，再确认第一个群：回复必须保留自己的原会话。
        for turn_id, session in reversed(turns):
            code, ack = api.handle('POST', '/chat/ack', {'delivery_id': turn_id, 'text': '确认送出的回复'},
                                   self.store, brain=self.brain)
            self.assertEqual((200, True), (code, ack['ok']))
            rows = [row for row in self.brain.today_entries(brain_model.now_iso()) if row.get('turn_id') == turn_id]
            self.assertEqual(3, len(rows))
            self.assertEqual({session}, {row.get('session_id') for row in rows})

    def test_qq_memory_waits_for_actual_sent_text_and_deduplicates_ack(self):
        from organs.mind import recent
        status, payload = self._post(
            {"text": QUESTION, "who": CHANNEL_WHO, "scene": "private", "delivery_ack": True},
            _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual("", recent.load()["turns"][-1]["reply"])
        self.assertFalse(any(row.get("side") == "她说" for row in self.brain.today_entries()))
        turn_id = payload["delivery_id"]
        self.assertEqual(turn_id, recent.load()["turns"][-1].get("turn_id"))
        self.assertEqual({turn_id}, {row.get("turn_id") for row in self.brain.today_entries()})
        sent = "实际上只发出了这一句"
        receipt = {"delivery_id": payload["delivery_id"], "text": sent}
        for _ in range(2):
            code, result = api.handle("POST", "/chat/ack", receipt, self.store, brain=self.brain)
            self.assertEqual((200, True), (code, result["ok"]))
        self.assertEqual(sent, recent.load()["turns"][-1]["reply"])
        rows = self.brain.today_entries()
        self.assertEqual(1, sum(row.get("text") == sent for row in rows))
        self.assertFalse(any(row.get("text") == LONG_REPLY for row in rows))
        self.assertEqual({turn_id}, {row.get("turn_id") for row in rows})

    def test_reply_receives_confirmed_public_video_link_from_browse_experience(self):
        from organs.day import phone_browse, growth
        day_root=self.tmp/'day'
        growth.configure(True,root=day_root)
        link='https://b23.tv/TestClip'
        phone_browse.save({'id':'public-video','created_at':'2026-11-06T10:00:00+09:00',
            'status':'done','scope':'public','origin':'autonomy','title':'小猫纸箱',
            'observations':[{'status':'ok','observation':{'subject':'小猫躲进纸箱'}}],
            'video_source':{'status':'ok','url':link}},day_root)
        captured=[]
        with mock.patch.dict(os.environ,{'DAY_ROOT':str(day_root)}):
            status,payload=self._post({'text':'你看的小猫视频链接给我看看','scene':'private',
                'who':'10000420','session_id':'qq:private:10000420','delivery_ack':True},
                _body('就是这个：'+link,captured=captured))
        self.assertEqual(200,status)
        prompt=captured[0]['messages'][0]['content']
        self.assertIn('"source_url": "'+link+'"',prompt)
        self.assertIn('不代表本轮重新打开了视频',prompt)
        self.assertIn(link,payload['reply'])

    def test_selected_expression_snapshot_follows_the_actual_reply_delivery(self):
        card = {'id':'a'*32+':0', 'phrase':'这波稳了', 'meaning':'放松的回应', 'when':'轻松聊天', 'avoid':'严肃情况'}
        def assess(**ctx):
            return dict(events.recognize(ctx['text'], who=ctx['who'], scene=ctx['scene']),
                        status='ok', expressions=[card])
        status, payload = self._post({'text':'我终于弄好了','who':CHANNEL_WHO,'scene':'private','delivery_ack':True},
                                    _body('这波稳了。'), assessor=assess)
        self.assertEqual(200, status)
        from organs.mind import recent
        path = recent.path_of().parent/'deliveries'/(payload['delivery_id']+'.json')
        staged = json.loads(path.read_text())
        self.assertEqual([{'id':card['id'],'phrase':card['phrase']}], staged.get('expression_references'))
        self.assertFalse(staged['confirmed'])
        api.handle('POST', '/chat/ack', {'delivery_id':payload['delivery_id'],'text':'这波稳了。'},
                   self.store, brain=self.brain)
        confirmed = json.loads(path.read_text())
        self.assertTrue(confirmed['confirmed'])
        self.assertTrue(confirmed.get('confirmed_at'))

    def test_superseded_receipt_keeps_only_confirmed_text(self):
        from organs.mind import recent
        for sent in ('', '已发的第一句'):
            with self.subTest(sent=sent):
                status, payload = self._post(
                    {"text": QUESTION, "who": CHANNEL_WHO, "delivery_ack": True}, _body(LONG_REPLY))
                self.assertEqual(200, status)
                self.assertEqual('conversation', payload.get('reply_kind'))
                token = payload['delivery_id']
                code, _ = api.handle('POST', '/chat/ack',
                    {'delivery_id': token, 'text': sent, 'stop_reason': 'superseded'},
                    self.store, brain=self.brain)
                self.assertEqual(200, code)
                record = json.loads((recent.path_of().parent / 'deliveries' / (token+'.json')).read_text())
                self.assertEqual('superseded', record.get('stop_reason'))
                self.assertEqual(sent, record['text'])
                self.assertEqual(sent, recent.load()['turns'][-1]['reply'])
                own = [r for r in self.brain.today_entries() if r.get('turn_id') == token and r.get('side') == '她说']
                self.assertEqual([sent] if sent else [], [r['text'] for r in own])

    def test_ack_rejects_unknown_stop_reason(self):
        for reason in ('failed', {}, 42):
            status, _ = api.handle('POST', '/chat/ack',
                {'delivery_id':'0'*32,'text':'','stop_reason':reason}, self.store, brain=self.brain)
            self.assertEqual(400, status)

    def test_ack_rejects_invalid_id_and_missing_round(self):
        for token, expected in (("../bad", 400), ("0" * 32, 404)):
            status, _ = api.handle("POST", "/chat/ack", {"delivery_id": token, "text": ""},
                                   self.store, brain=self.brain)
            self.assertEqual(expected, status)

    def test_failed_qq_delivery_does_not_become_a_spoken_memory(self):
        from organs.mind import recent
        status, payload = self._post(
            {"text": QUESTION, "who": CHANNEL_WHO, "delivery_ack": True}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        code, _ = api.handle("POST", "/chat/ack",
                             {"delivery_id": payload["delivery_id"], "text": ""},
                             self.store, brain=self.brain)
        self.assertEqual(200, code)
        self.assertEqual("", recent.load()["turns"][-1]["reply"])
        self.assertFalse(any(row.get("side") == "她说" for row in self.brain.today_entries()))

    def test_followup_after_failed_delivery_preserves_both_user_memories(self):
        from organs.mind import recent
        texts = ["我妈住院了，今天心里很乱", "谢谢，我先去吃点东西"]
        for index, text in enumerate(texts):
            status, payload = self._post(
                {"text": text, "who": CHANNEL_WHO, "delivery_ack": True}, _body(LONG_REPLY))
            self.assertEqual(200, status)
            code, _ = api.handle("POST", "/chat/ack",
                                 {"delivery_id": payload["delivery_id"],
                                  "text": "" if index == 0 else "好，先吃点东西。"},
                                 self.store, brain=self.brain)
            self.assertEqual(200, code)
        rows = self.brain.today_entries()
        humans = [row for row in rows if row.get("side") == "他说"]
        self.assertEqual(texts, [row["quote"] for row in humans])
        self.assertEqual(2, len({row["id"] for row in humans}))
        turns = recent.load()["turns"]
        self.assertEqual([row["id"] for row in humans], [row["msg_id"] for row in turns])
        self.assertEqual(["", "好，先吃点东西。"], [row["reply"] for row in turns])
        self.assertEqual(["好，先吃点东西。"], [row["quote"] for row in rows if row.get("side") == "她说"])
        self.assertEqual(2, len({row["turn_id"] for row in turns}))
        self.assertEqual([row["turn_id"] for row in humans], [row["turn_id"] for row in turns])
        self.assertEqual(turns[1]["turn_id"], rows[-1]["turn_id"])

    def test_place_confirmation_reaches_delivery_without_recitation_rewrite(self):
        reply = "那就改去松林公园。"
        capture = []
        status, payload = self._post(
            {"text": "改去松林公园吧，图书馆这次不去了。", "who": CHANNEL_WHO,
             "scene": "private", "delivery_ack": True}, _body(reply, capture))
        self.assertEqual(200, status)
        self.assertEqual(reply, payload["reply"])
        self.assertEqual([reply], [row["text"] for row in payload["chunks"]])
        self.assertEqual([], _gate_rewrite_calls(capture), "正常承接不应触发复述重写")
        self.assertEqual(1, len(capture), "短句句末的句号不应多调用一次模型")
        audit = payload["gate"]["parts"][0]["audit"]
        self.assertIn("改去松林公园", audit.get("reason", ""))
        self.assertIn("不重写或删句", audit.get("reason", ""))
        self.assertEqual("", recent.load()["turns"][-1]["reply"])
        self.assertFalse(any(r.get("side") == "她说" for r in self.brain.today_entries()))
        for _ in range(2):
            code, ack = api.handle("POST", "/chat/ack",
                                  {"delivery_id": payload["delivery_id"], "text": reply},
                                  self.store, brain=self.brain)
            self.assertEqual(200, code)
            self.assertTrue(ack["ok"])
        self.assertEqual(reply, recent.load()["turns"][-1]["reply"])
        self.assertEqual([reply], [r["quote"] for r in self.brain.today_entries()
                                  if r.get("side") == "她说"])

    def test_success_body_is_the_contract(self):
        self.brain.ingest(who="3425635473", text=MEMORY_TEXT)
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual(CONTRACT_KEYS, set(payload),
                         "成功体的键不是契约那一份：%s" % sorted(payload))
        self.assertTrue(payload["ok"])
        self.assertEqual('not_requested', payload['lookup']['state'])
        self.assertEqual(LONG_REPLY, payload["reply"])
        chunks = payload["chunks"]
        mouth_policy = json.loads((MOUTH_DATA / "policy.json").read_text(encoding="utf-8"))
        max_parts = int(mouth_policy["split"]["max_parts"])
        max_chars = int(mouth_policy["split"]["max_chars"])
        self.assertTrue(2 <= len(chunks) <= max_parts,
                        "分泡该在 2–%d 条之间（现读嘴的 policy）：%r" % (max_parts, chunks))
        for chunk in chunks:
            self.assertTrue(chunk["text"])
            self.assertLessEqual(len(chunk["text"]), max_chars,
                                 "有一泡超过嘴的 split.max_chars：%r" % (chunk,))
            self.assertIsInstance(chunk["gap_before"], (int, float))
            self.assertIsInstance(chunk["send_at"], (int, float))
            self.assertIsNotNone(chunk["send_at"])
        self.assertEqual(CONFIG["model"], capture[0]["model"])
        self.assertEqual(["system", "user"],
                         [item["role"] for item in capture[0]["messages"]])

    def test_banned_and_action_ride_along_in_the_success_body(self):
        """防抄自己那两块也要随成功体出去：禁令读三键、动作池读三键（含配额四键），形状对外冻结。"""
        conf = conversation.load_assembly()
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        banned = payload["banned"]
        action = payload["action"]
        self.assertEqual(BANNED_KEYS, set(banned), "banned 的键不是契约那一份：%s" % sorted(banned))
        self.assertEqual(ACTION_KEYS, set(action), "action 的键不是契约那一份：%s" % sorted(action))
        self.assertEqual(conf["banned"]["rounds"], banned["rounds"])
        self.assertIsInstance(banned["rounds"], int)
        self.assertIsInstance(banned["action_lines"], list)
        self.assertIsInstance(banned["openings"], list)
        for row in banned["action_lines"]:
            self.assertEqual({"text", "count"}, set(row))
            self.assertTrue(row["text"])
            self.assertGreaterEqual(row["count"], 1)
        for head in banned["openings"]:
            self.assertIsInstance(head, str)
            self.assertTrue(head)
        self.assertEqual(conf["action"]["pool"], action["pool"], "池子该照抄 assembly.json")
        self.assertEqual(conf["action"]["rule"], action["rule"])
        quota = action["quota"]
        self.assertEqual(QUOTA_KEYS, set(quota), "quota 的六个键对外冻结：%s" % sorted(quota))
        self.assertEqual(conf["action"]["quota"]["window"], quota["window"])
        self.assertEqual(conf["action"]["quota"]["max"], quota["max"])
        self.assertIsInstance(quota["used"], int)
        self.assertIsInstance(quota["blocked"], bool)
        self.assertEqual(quota["used"] >= quota["max"], quota["blocked"],
                         "blocked 要跟着 used 与 max 算，不许另写一个值")
        self.assertIsInstance(quota["stripped"], int)
        self.assertIsInstance(quota["kept_because_empty"], bool)
        self.assertEqual(0, quota["stripped"], "没顶到配额时一行都不许删")
        # 两段真的进了提示词（不是只在回包里挂个空壳）
        system = capture[0]["messages"][0]["content"]
        self.assertIn("[%s]" % conf["sections"]["banned"]["label"], system,
                      "近轮禁用那一节没进提示词")
        self.assertIn("[%s]" % conf["sections"]["action"]["label"], system,
                      "动作池那一节没进提示词")

    def test_recalled_carries_real_evidence_from_brain(self):
        at = brain_model.now_iso()
        entry = self.brain.ingest(who="3425635473", text=MEMORY_TEXT, ts=at)
        status, payload = self._send()
        self.assertEqual(200, status)
        self.assertTrue(payload["recalled"], "一句带「医院」的话该想起那条记忆")
        row = payload["recalled"][0]
        self.assertTrue({"id", "text", "source", "source_type", "evidence_key", "score", "who"}
                        .issubset(set(row)),
                        "回包 recalled 至少带这些证据字段，实际：%r" % (sorted(set(row)),))
        self.assertEqual(MEMORY_TEXT, row["text"])
        self.assertEqual(entry["entry"]["evidence_key"], row["evidence_key"])
        self.assertEqual(entry["entry"]["id"], row["id"])
        self.assertEqual("亲历", row["source_type"])
        self.assertTrue(row["source"], "来源档没给")
        stored = [item for item in self.brain.today_entries(now_ts=at)
                  if item.get("id") == entry["entry"]["id"]]
        self.assertEqual(1, len(stored), "脑里找不到那条被想起的记忆")
        self.assertEqual(stored[0]["evidence_key"], row["evidence_key"])

    def test_heart_is_words_only(self):
        status, payload = self._send()
        self.assertEqual(200, status)
        heart = payload["heart"]
        self.assertEqual({"tier", "direction"}, set(heart["mood"]))
        self.assertTrue({"tone", "direction", "bans"} <= set(heart["voice"]))
        self.assertEqual({"state", "reason"}, set(heart["engage"]))
        self.assertTrue(heart["axes"])
        text = json.dumps(heart, ensure_ascii=False)
        self.assertEqual([], [ch for ch in text if ch.isdigit()],
                         "心的回包里有数字：%s" % text)

    def test_the_tone_block_carries_the_four_knobs_to_the_page(self):
        """回包那一格 tone：档位、软度、上限、两个开关、截没截、心接上没有——与装配同一个数。

        心这一轮是真接上的（不是「未接入」）：装配把 mood / energy / offset / engage 都交给了嘴，
        heart_source 里得看得见它们；max_chars 必须与 assembly.reply_budget.chars 是同一个数
        （提示词说的和手里截的是一个口径）。
        """
        status, payload = self._send()
        self.assertEqual(200, status)
        tone = payload["tone"]
        for key in ("tier", "label", "softness", "max_chars", "no_question", "ban_joke",
                    "truncated", "heart_source"):
            self.assertIn(key, tone, "tone 少了 %s：%r" % (key, tone))
        self.assertEqual(payload["assembly"]["reply_budget"]["chars"], tone["max_chars"],
                         "回包的上限与装配那一格不是一个数")
        self.assertIn("已接入", tone["heart_source"], "心没真接进嘴：%r" % (tone["heart_source"],))
        self.assertIn("mood=", tone["heart_source"])
        self.assertIn("energy=", tone["heart_source"])
        self.assertIn("engage=", tone["heart_source"])
        for key in ("no_question", "ban_joke", "truncated"):
            self.assertIsInstance(tone[key], bool, "%s 不是真假值：%r" % (key, tone[key]))
        self.assertIsInstance(tone["softness"], (int, float))
        self.assertTrue(str(tone["label"] or "").strip(), "语气档标签是空的")

    def test_a_cold_mood_round_narrows_the_budget_over_http(self):
        """心情凉那一轮：/chat 上的上限真的收紧，账本上落一行「为什么收紧」。

        心状态直接写文件（与 _double_low 同一种写法）：心情 20（0.2，低档），精力 80 不动——
        双低会判 SILENT、整轮被出站闸拦下，那不是这一条要验的路。
        """
        at = DAYTIME
        self._heart_root_write(mood.MOOD_FILE,
                               {"now": 20.0, "baseline": 60.0, "updated_at": at})
        self._heart_root_write(energy.ENERGY_FILE,
                               {"value": 80.0, "updated_at": at, "tier": "一般"})
        with mock.patch.object(brain_model, "now_iso", lambda: at):
            status, payload = self._send()
        self.assertEqual(200, status)
        rules = conversation.load_assembly()["tone_rules"]
        tier_chars = payload["assembly"]["reply_budget"]["tier_chars"]
        self.assertEqual(min(tier_chars, rules["max_chars_low"]), payload["tone"]["max_chars"],
                         "心情凉了上限却没收紧")
        self.assertTrue(payload["tone"]["ban_joke"], "心情凉却没禁玩笑")
        rows = conversation.read_tone_log()
        self.assertEqual(1, len(rows), "一轮该落一行账：%r" % (rows,))
        self.assertEqual(payload["tone"]["max_chars"], rows[-1]["max_chars"])
        self.assertTrue(rows[-1]["ban_joke"])
        self.assertEqual(payload["tone"]["heart_source"], rows[-1]["heart_source"])

    def test_cold_reply_keeps_serious_meaning_in_api_chunks_and_recent(self):
        self._heart_root_write(mood.MOOD_FILE,
                               {"now": 20.0, "baseline": 60.0, "updated_at": DAYTIME})
        self._heart_root_write(energy.ENERGY_FILE,
                               {"value": 80.0, "updated_at": DAYTIME, "tier": "一般"})
        reply = "我不是在开玩笑。那件事我记着。"
        with mock.patch.object(brain_model, "now_iso", lambda: DAYTIME):
            status, payload = self._send(text="你是认真的吗？", reply=reply)
        self.assertEqual(200, status)
        self.assertTrue(payload["tone"]["ban_joke"])
        self.assertEqual(reply, payload["reply"])
        self.assertEqual(reply, "".join(chunk["text"] for chunk in payload["chunks"]))
        self.assertEqual(reply, recent.load()["turns"][-1]["reply"])
        self.assertEqual([reply], [row["quote"] for row in self.brain.today_entries()
                                   if row["side"] == brain_model.SIDE_SELF])

    def _heart_root_write(self, name: str, row) -> None:
        """往临时心根写一份状态文件（tier 之类缺的栏由器官自己补，不在这里手搓）。"""
        write_json(self._heart_root() / name, row)

    def test_gate_pass_and_dropped_are_both_reachable(self):
        status, passed = self._send()
        self.assertEqual(200, status)
        self.assertEqual("pass", passed["gate"]["decision"])
        self.assertIsNone(passed["gate"]["hit"])
        self.assertTrue(passed["chunks"])

        # 命中路径要一份含命中条款的嘴数据：出厂那份不认这句话，所以整份拷到临时根再改条款。
        root = self.tmp / "mouth"
        if not root.is_dir():
            import shutil
            shutil.copytree(str(MOUTH_DATA), str(root))
            clauses = json.loads((root / "clauses.json").read_text(encoding="utf-8"))
            clauses["clauses"].insert(0, {
                "id": "ai-self-reference",
                "kind": "反例",
                "pattern": "作为一个\\s*(AI|人工智能)\\s*语言模型",
                "reason": "这是标准模型免责声明：她不是那个东西",
            })
            (root / "clauses.json").write_text(json.dumps(clauses, ensure_ascii=False),
                                               encoding="utf-8")
        line = "作为一个 AI 语言模型，我不能这样说。"
        with mock.patch.dict(os.environ, {"MOUTH_DATA_DIR": str(root)}):
            dropped = self._post({"text": QUESTION},
                                 _script(line, "作为一个 AI 语言模型，这话我不能说。"))
        self.assertEqual(200, dropped[0])
        self.assertEqual("dropped", dropped[1]["gate"]["decision"])
        self.assertEqual("", dropped[1]["reply"])
        self.assertEqual([], dropped[1]["chunks"])
        self.assertTrue(dropped[1]["gate"]["hit"]["clause_id"])
        self.assertTrue(dropped[1]["gate"]["hit"]["reason"])
        self.assertGreaterEqual(dropped[1]["gate"]["drops"]["today"], 1)

# —— 闸：逐泡过一遍，结论真的落到返回体（本轮加的） ————————————

    HER_LINE = "主人回来啦，我把碗放桌上了。"          # 她自己说过的一句（复读那一关的靶子）
    NOTED_LINE = "（尾巴在水里蜷了一下）"              # 括号里写了我们没给过的场景（action_note 的 env_words）

    def _drops_rows(self):
        """弃言账本（drops.jsonl）现在有几行——每被弃一泡就多一行。"""
        path = Path(os.environ["MOUTH_TRACE_DIR"]) / "drops.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def _mouth_root(self) -> Path:
        """把嘴的整份判据拷到临时根：改哪个数就改哪个 JSON，代码里一个数都不动。"""
        root = self.tmp / "mouth"
        if not root.is_dir():
            shutil.copytree(str(MOUTH_DATA), str(root))
        return root

    def test_a_repeated_reply_does_not_request_rewrite(self):
        status, first = self._send(text="我回来了", reply=self.HER_LINE)
        self.assertEqual(200, status)
        before = len(self._drops_rows())
        captured = []
        status, payload = self._post({"text": "我回来了"}, _script(self.HER_LINE, captured=captured))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertEqual("observed", part["repeat_status"])
        self.assertEqual(self.HER_LINE, payload["reply"])
        self.assertEqual([self.HER_LINE], [one["text"] for one in payload["chunks"]])
        self.assertEqual([], part["hits"])
        self.assertFalse(part["kept_after_rewrite"])
        self.assertEqual(1, len(captured))
        self.assertEqual(before, len(self._drops_rows()))

    def test_a_noted_line_survives_the_failed_rewrite(self):
        """动作括注里出现了我们没给过的场景：命中，改了还带着同一个场景词 → 留进门原话，不再丢空。

        **这条断言是被新规矩改准的**：以前二次命中即弃。判据照旧看得见（hits 里是「动作带场景」，
        人话点名那个场景词、account 上是一行 action-after-rewrite），只是这一泡不再整泡消失。
        """
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你在干嘛"},
                                     _script(self.NOTED_LINE, "（尾巴在水里轻轻地蜷了一下）"))
        self.assertEqual(200, status)
        gate = payload["gate"]
        part = gate["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertTrue(part["kept_after_rewrite"])
        self.assertNotEqual([], payload["chunks"], "二次命中非红线：chunks 不许空")
        self.assertEqual([self.NOTED_LINE], [one["text"] for one in payload["chunks"]])
        self.assertEqual(self.NOTED_LINE, payload["reply"])
        self.assertEqual("ok", part["action_status"], "这一关真的判过了（不是没跑）")
        self.assertEqual("动作带场景", part["hits"][-1]["clause_id"])
        self.assertIn("水里", part["hits"][-1]["reason"], "拦下她要说出是哪个场景词")
        self.assertEqual(before + 1, len(self._drops_rows()))
        self.assertEqual("动作带场景", self._drops_rows()[-1]["clause_id"])
        self.assertIn(AFTER_MARKS["action"], self._drops_rows()[-1]["reason"])

    def test_a_recited_soul_card_survives_the_failed_rewrite(self):
        """验收一：她把魂那张卡上的话原样念了出来，改了一次还在念 → 删掉那一句；删空才弃这一泡。

        rewrite.status 是 ok（模型确实改了），但新稿仍撞「复述设定段」——这正是以前会丢掉整泡的
        那一格。现在走的是「删那一句」（gate._strip_recited）：这一泡只有那一句在照搬，删完没正文
        可发，于是 decision 是 dropped、chunks / reply 都空，账本那一行带 recited-only——**是「只剩
        那一句」，不是「整条判废」**，两者在账上分得开。audit.status 仍是 ok、hit 照旧带着（面板看
        得见她引了设定）。
        """
        line = "我嘴上不认，行动先到"
        soul = self.tmp / "soul"
        write_json(soul / "manner.json", {"stance": line})
        store = load(soul)
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你是不是又不承认"},
                                     _script(line, "我嘴上不认，行动倒是先到了。"), store=store)
        self.assertEqual(200, status)
        gate = payload["gate"]
        # 新规矩（宁可少说一句，不许整句不说）：这一泡只有那一句在照搬 → 删完没话可说，允许空。
        # 整泡删空时逐泡读数没有位置（parts 是空的），判据去账上看：clause_id + recited-only。
        self.assertEqual([], payload["chunks"], "只剩照搬的那一句：删完就该空")
        self.assertEqual("", payload["reply"])
        self.assertEqual("dropped", gate["decision"])
        part = gate["parts"][0]
        self.assertEqual("dropped", part["decision"], "逐泡读数照样在：这一泡被删空了")
        self.assertFalse(part["kept_after_rewrite"], "这是删句删空，不是「留原话」那一格")
        self.assertEqual("复述设定段", part["audit"]["hit"]["kind"])
        self.assertEqual("魂", part["audit"]["hit"]["seg_kind"])
        self.assertEqual("复述设定段", part["hits"][-1]["clause_id"])
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows), "删句删空也要记一行账")
        self.assertIn("recited-only", rows[-1]["reason"], "账上要看得出是「删句删空」")
        self.assertEqual(["复述设定段"], gate["blocked"], "这一泡真被弃了，blocked 里就该有它")
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows), "删空也要记一行账")
        self.assertEqual("复述设定段", rows[-1]["clause_id"])
        self.assertIn(RECITED_MARK, rows[-1]["reason"],
                      "账上要看得出是「删句删空」（recited-only），不是「整条判废」")
        self.assertNotIn(AUDIT_SKIP_MARK, rows[-1]["reason"], "没有请求：这一行不许带跳过记号")

    # —— 整轮的闸判摘要（gate.summary，这一轮加的） ————————

    def test_gate_summary_carries_the_recital_reading_for_the_ledger(self):
        """这一轮她把自己的设定念了出来：gate.summary 里三样得齐（状态 / 撞了几个字 / 改没改）。

        这一格是给决策账抄的：以前她照着念设定，账上只有「门说不说」，没有一个字说闸判了什么，
        人查了两圈才明白。所以判据不是「有个字段」，而是**判得出来**：audit.hit 为真、lcs_len 够到
        这一轮的阈值、rewrite.status 说得出这一泡被改过。摘要是归并、不是另算一遍——与逐泡读数
        逐字相同。
        """
        line = "我嘴上不认，行动先到"                      # 魂里那句（设定类：照搬就撞上）
        said = "我嘴上不服软，可该做的都先做了。"            # 模型改出来的那一句（不再照搬）
        soul = self.tmp / "soul"
        write_json(soul / "manner.json", {"stance": line})
        status, payload = self._post({"text": "你是不是又不承认"},
                                     _script(line, said), store=load(soul))
        self.assertEqual(200, status)
        summary = payload["gate"]["summary"]
        self.assertEqual({"at", "audit", "rewrite"}, set(summary))
        self.assertEqual({"status", "hit", "lcs_len", "threshold"}, set(summary["audit"]))
        self.assertEqual({"status"}, set(summary["rewrite"]))
        self.assertTrue(summary["at"], "摘要要带时刻，账上一行才排得出先后")
        self.assertEqual("ok", summary["audit"]["status"], "真的比过了才是 ok")
        self.assertTrue(summary["audit"]["hit"], "念了设定，摘要上必须看得见")
        self.assertGreaterEqual(summary["audit"]["lcs_len"], summary["audit"]["threshold"],
                                "撞上的那截够到阈值才叫命中")
        self.assertEqual("ok", summary["rewrite"]["status"], "这一泡被改过一次")
        part = payload["gate"]["parts"][0]
        self.assertEqual(part["audit"]["status"], summary["audit"]["status"])
        self.assertEqual(part["audit"]["lcs_len"], summary["audit"]["lcs_len"])
        self.assertEqual(part["audit"]["threshold"], summary["audit"]["threshold"])
        self.assertEqual(part["rewrite"]["status"], summary["rewrite"]["status"])

    def test_gate_summary_says_no_recital_on_a_normal_reply(self):
        """一句正常的话：摘要得说「比过了、没撞上」，而且一次重写都没送出去（not-tried）。"""
        status, payload = self._send(text="今天风大吗", reply="风大，我把窗关好了。")
        self.assertEqual(200, status)
        summary = payload["gate"]["summary"]
        self.assertEqual("ok", summary["audit"]["status"])
        self.assertFalse(summary["audit"]["hit"], "没念设定，摘要上不许说念了")
        self.assertEqual("not-tried", summary["rewrite"]["status"], "没命中就没有重写这一回事")

    def test_a_normal_reply_passes_and_keeps_every_chunk_verbatim(self):
        """一句正常回话一处都不改：chunks 的 text 与**不接闸**那一遍逐字相同（diff 为空）。"""
        reply = ("今天风大，我把窗关上了。晚上想喝粥，我给你留了一碗。"
                 "你要是回来晚，我就先不睡。")
        # 先跑过闸那一遍（账上还没有历史、脑里也还没有她这一句），再跑不接闸那一遍做对照：
        # 反过来的话，第一遍会把她这句话记进脑与账，第二遍就变成「复述/复读自己」了。
        status, gated = self._send(text="今天怎么样", reply=reply)
        self.assertEqual(200, status)
        with mock.patch.object(chat, "_gate_round", lambda *args, **kw: None):
            status, plain = self._send(text="今天怎么样", reply=reply)
        self.assertEqual(200, status)
        self.assertEqual([one["text"] for one in plain["chunks"]],
                         [one["text"] for one in gated["chunks"]],
                         "一泡都没被改时 chunks 的 text 要与不接闸时逐字相同")
        gate = gated["gate"]
        self.assertEqual("pass", gate["decision"])
        self.assertEqual([], gate["blocked"])
        self.assertEqual(gate["in_parts"], gate["out_parts"])
        self.assertTrue(all(row["decision"] == "pass" for row in gate["parts"]))
        self.assertEqual(reply, gated["reply"])

    def test_all_parts_dropped_leaves_nothing_and_records_one_drop(self):
        """全被弃掉：chunks=[]、reply=""，弃言账本多一行，reason 里带着 clause_id。

        **这一格换了素材**：二次命中留原话之后，非红线已经走不到「整泡消失」了——能全被弃掉的只剩
        红线（隐私 / 辱骂 / 放弃式：唯一不许留原话的一类）。这句里带着手机号，一泡都发不出去。
        """
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你有电话吗"},
                                     _body("我手机号是 13812345678，你记一下"))
        self.assertEqual(200, status)
        self.assertEqual([], payload["chunks"])
        self.assertEqual("", payload["reply"])
        self.assertEqual("dropped", payload["gate"]["decision"])
        self.assertEqual(0, payload["gate"]["out_parts"])
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows), "弃掉的这一泡要留一行弃言账")
        self.assertEqual(FATAL_CLAUSE, rows[-1]["clause_id"])
        self.assertIn(FATAL_CLAUSE, rows[-1]["reason"], "弃言账的 reason 里要能看见 clause_id")
        self.assertEqual(FATAL_BOOK["categories"][0]["id"], payload["gate"]["parts"][0]["fatal"]["category"])

    def test_a_red_line_is_dropped_and_the_panel_shows_which_kind(self):
        """验收二：一句放弃式（「反正我不疼，你别管了」）——红线，整泡丢掉，面板说得出是哪一类。

        红线是唯一不许留原话的一类：二次命中留原话那一条规矩到它这里就停（改一次也不能保证它
        发不出去）。parts[*].fatal 里带着类别与撞上的那几个字，fatal_status 是 fatal（红灯）。
        """
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你还好吗"}, _body(GIVE_UP_LINE))
        self.assertEqual(200, status)
        self.assertEqual("dropped", payload["gate"]["decision"])
        self.assertEqual([], payload["chunks"], "红线不许留原话")
        self.assertEqual("", payload["reply"])
        part = payload["gate"]["parts"][0]
        self.assertEqual(GIVE_UP_CATEGORY["id"], part["fatal"]["category"])
        self.assertEqual(GIVE_UP_CATEGORY["tag"], part["fatal"]["tag"])
        self.assertEqual("fatal", part["fatal_status"], "红灯：这一关真的判出了红线")
        self.assertIn("不疼", part["fatal"]["matched"])
        self.assertFalse(part["kept_after_rewrite"])
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows))
        self.assertEqual(FATAL_CLAUSE, rows[-1]["clause_id"])
        self.assertIn(_mark_of(GIVE_UP_CATEGORY["tag"]), rows[-1]["reason"])

    def test_the_repeat_window_comes_from_the_data(self):
        """复读窗口不写死：repeat.json 的 rounds 改成 1 之后，隔了一轮的那句就不算复读了。"""
        root = self._mouth_root()
        book = json.loads((root / "repeat.json").read_text(encoding="utf-8"))
        book["rounds"] = 1
        (root / "repeat.json").write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        with mock.patch.dict(os.environ, {"MOUTH_DATA_DIR": str(root)}):
            self._send(text="第一句", reply="第一句的回话，我记得的。")
            self._send(text="第二句", reply="第二句的回话，我也记得的。")
            status, payload = self._send(text="第三句", reply="第一句的回话，我记得的。")
        self.assertEqual(200, status)
        self.assertEqual("pass", payload["gate"]["decision"],
                         "窗口只有 1 轮，隔了一轮的同一句不该算复读")

    def test_no_inject_at_all_is_audit_unavailable_not_a_pass(self):
        """一段注入原文都取不到时交给审计的是 None：审计报 audit-unavailable，不许伪造通过。"""
        class _Bare:
            def raw(self, block):
                return {}

        table = warehouse.load("audit")
        with mock.patch.object(chat, "_rule_text", lambda: ""):
            inject = chat._inject("", {"anchors": {}, "recalled": [], "heart": {}},
                                 _Bare(), table)
        self.assertIsNone(inject, "一段都取不到时要交 None，不是空清单")
        seen = mouth_audit.audit("一句正常的话", inject=None)
        self.assertEqual("audit-unavailable", seen.status)
        self.assertIsNone(seen.hit)

# —— 动作配额：从装配传进闸（本轮补的） ————————————————

    QUOTA_LINE = "（尾巴晃了一下）可爱归可爱，那也不是胖。"      # 踩配额那一句（正文还在）
    NOTED_QUOTA_LINE = "（尾巴在水里晃了一下）可爱归可爱，那也不是胖。"

    def _block_actions(self):
        """把装配的动作配额顶满：账上最近一轮她自己写过动作行，blocked 就是真。

        走的是装配那边现成的算法（repeat.count_with_actions 数最近 quota.window 轮），
        这里只负责造一轮历史，不另算一遍配额。
        """
        recent.append({"session_id": "api:panel", "ts": "2025-01-15T14:00:00+08:00", "who": "3425635473",
                       "said": "你刚才那个样子", "reply": "（耳尖动了动）"})

    def test_quota_forbid_strips_the_action_line_and_keeps_the_body(self):
        """装配标了不许写动作：这一泡不被整条丢，动作行剥掉、正文留下，账照记。"""
        self._block_actions()
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你是不是胖了"}, _body(self.QUOTA_LINE))
        self.assertEqual(200, status)
        self.assertTrue(payload["action"]["quota"]["blocked"], "这一轮的装配配额该是顶满的")
        gate = payload["gate"]
        self.assertEqual("forbid", gate["quota"], "blocked 为真要翻成 forbid")
        part = gate["parts"][0]
        self.assertEqual("ok", part["quota_status"], "配额这一关真的判过了")
        self.assertEqual("rewritten", part["decision"], "配额命中不能整条丢：剥行动作、正文留下")
        self.assertEqual("动作超配额", part["hits"][-1]["clause_id"])
        self.assertEqual(1, len(payload["chunks"]), "这一泡该留着")
        self.assertEqual("可爱归可爱，那也不是胖。", payload["chunks"][0]["text"],
                         "chunks 里那一泡要恰为正文")
        self.assertEqual("可爱归可爱，那也不是胖。", payload["reply"])
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows), "剥行救回来的那一笔也要记账")
        self.assertEqual("动作超配额", rows[-1]["clause_id"])
        self.assertIn("action-over-quota", rows[-1]["reason"])

    def test_quota_allow_leaves_the_action_note_gate_in_charge(self):
        """装配标了放行（sparing → allow）：同一句走动作括注那一关，命中就 dropped。

        重写那一句仍带着同一个场景词：改一次还踩，按老规矩弃掉整泡。
        """
        status, payload = self._post({"text": "你是不是胖了"},
                                     _script(self.NOTED_QUOTA_LINE,
                                             "（尾巴在水里晃了晃）可爱归可爱，那也不是胖。"))
        self.assertEqual(200, status)
        self.assertFalse(payload["action"]["quota"]["blocked"], "这一轮的装配配额该是没顶满")
        gate = payload["gate"]
        self.assertEqual("allow", gate["quota"], "blocked 为假要翻成 allow")
        part = gate["parts"][0]
        self.assertEqual("ok", part["quota_status"], "标了 allow 也算判过")
        self.assertEqual("动作带场景", part["hits"][-1]["clause_id"], "拦下它的是动作括注那一关")
        self.assertEqual("pass", part["decision"], "二次命中非红线：留原话，不再丢空（断言已改准）")
        self.assertTrue(part["kept_after_rewrite"])
        self.assertEqual([self.NOTED_QUOTA_LINE], [one["text"] for one in payload["chunks"]])

    def test_assembly_without_quota_is_not_checked_not_allow(self):
        """装配没标配额：交给闸的是 None（记 not-checked），chunks 与不接闸时逐字相同。"""
        self.assertIsNone(chat._quota({}), "回包里没有 action 时不许猜")
        self.assertIsNone(chat._quota({"action": {"pool": [], "rule": "随便"}}), "没有 quota 一项时不许猜")
        self.assertIsNone(chat._quota({"action": {"quota": {"blocked": "true"}}}),
                          "blocked 不是布尔时不许猜成 allow")

        reply = ("今天风大，我把窗关上了。晚上想喝粥，我给你留了一碗。"
                 "你要是回来晚，我就先不睡。")
        real = conversation.answer

        def without_quota(text, who="", **kw):
            """装配没标配额的样子：回包里没有 action.quota 这一项。"""
            kw.setdefault("transport", _body(reply))
            kw.setdefault("config", CONFIG)
            kw.setdefault("env", ENV)
            payload = real(text, who, **kw)
            payload["action"].pop("quota", None)
            return payload

        with mock.patch.object(conversation, "answer", without_quota):
            status, gated = api.handle("POST", "/chat", {"text": "今天怎么样"},
                                       self.store, brain=self.brain)
        self.assertEqual(200, status)
        gate = gated["gate"]
        self.assertIsNone(gate["quota"], "装配没标时交给闸的是 None，不许猜成 allow")
        self.assertEqual(["not-checked"] * len(gate["parts"]),
                         [row["quota_status"] for row in gate["parts"]],
                         "没标就是这一关没跑，不是通过")
        self.assertEqual("pass", gate["decision"])
        with mock.patch.object(conversation, "answer", without_quota):
            with mock.patch.object(chat, "_gate_round", lambda *args, **kw: None):
                status, plain = api.handle("POST", "/chat", {"text": "今天怎么样"},
                                           self.store, brain=self.brain)
        self.assertEqual(200, status)
        self.assertEqual([one["text"] for one in plain["chunks"]],
                         [one["text"] for one in gated["chunks"]],
                         "配额没标时 chunks 要与不接闸时逐字相同")

    # —— 冒充做过：动作账从回包传进闸（本轮加的） ————————————————

    CLAIM_LINE = "我这就去画"                    # 这一轮手头什么都没做时她会说的那一句
    CLAIM_FIX = "画不动，我没手。"                # 改写之后不再说做过（字数在 rewrit length 区间里）

    def _claim_id(self) -> str:
        """冒充做过那一关在账本里的 clause_id：从数据读，测试不自己造这个名字。"""
        return json.loads((MOUTH_DATA / "claim.json").read_text(encoding="utf-8"))["hit"]["clause_id"]

    def test_the_ledger_shapes_are_three_different_things(self):
        """回包没有这一格 = 空账（这一轮什么都没做）；给了清单就交下去；读不动才是 None。"""
        self.assertEqual([], chat._actions({}), "没有动作账这一格就是空账，不是拿不到")
        self.assertEqual([{"verb": "发"}], chat._actions({"actions": [{"verb": "发"}]}))
        self.assertIsNone(chat._actions({"actions": "坏了"}), "读不动的形状是拿不到，不许猜成空账")

    def test_a_rewritten_claim_survives_as_the_fixed_line(self):
        """命中之后改好了：这一泡是 rewritten、发的是改完那句，claim_status 说这一关真的判过。"""
        status, payload = self._post({"text": "你画好了吗"},
                                     _script(self.CLAIM_LINE, self.CLAIM_FIX))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("ok", part["claim_status"], "给了空账就是判过，不是没跑")
        self.assertEqual("rewritten", part["decision"])
        self.assertEqual(self._claim_id(), part["hits"][0]["clause_id"])
        self.assertEqual([self.CLAIM_FIX], [one["text"] for one in payload["chunks"]])
        self.assertEqual(self.CLAIM_FIX, payload["reply"])

    def test_accepting_praise_reaches_output_without_action_rewrite(self):
        reply = '这句夸奖我收了，今天有点开心。'
        calls = []
        status, payload = self._post({'text':'夸你呢'}, _script(reply, captured=calls))
        self.assertEqual(200, status)
        self.assertEqual(reply, payload['reply'])
        self.assertEqual(1, len(calls), '正常接夸奖不应新增重写请求')
        self.assertFalse(any(hit['clause_id'] == self._claim_id()
                             for part in payload['gate']['parts'] for hit in part['hits']))

    def test_rewriting_preserves_image_and_dropping_removes_it(self):
        image = {"file": "file:///tmp/portrait.jpg", "summary": "自画像"}
        gate_round = chat._gate_round

        def with_image(payload, *args, **kwargs):
            payload["chunks"][-1]["image"] = image
            return gate_round(payload, *args, **kwargs)

        with mock.patch.object(chat, "_gate_round", with_image):
            status, payload = self._post({"text": "你画好了吗"},
                                         _script(self.CLAIM_LINE, self.CLAIM_FIX))
        self.assertEqual(200, status)
        self.assertEqual("rewritten", payload["gate"]["decision"])
        self.assertEqual([image], [chunk["image"] for chunk in payload["chunks"]])
        with mock.patch.object(chat, "_gate_round", with_image):
            status, payload = self._post({"text": "你有电话吗"},
                                         _body("我手机号是 13812345678，你记一下"))
        self.assertEqual(200, status)
        self.assertEqual("dropped", payload["gate"]["decision"])
        self.assertEqual([], payload["chunks"])

    def test_an_unfixable_claim_is_dropped_and_the_ledger_names_it(self):
        """改不动（重写回来逐字相同）：那一句被删掉，整泡只剩它 → dropped，账本记一笔。"""
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你画好了吗"},
                                     _script(self.CLAIM_LINE, self.CLAIM_LINE))
        self.assertEqual(200, status)
        gate = payload["gate"]
        part = gate["parts"][0]
        self.assertEqual("ok", part["claim_status"])
        self.assertEqual("dropped", part["decision"], "删完只剩空白就是弃这一泡")
        self.assertEqual([], payload["chunks"])
        self.assertEqual("", payload["reply"])
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows), "删句弃掉的那一笔也要记账")
        self.assertEqual(self._claim_id(), rows[-1]["clause_id"])
        self.assertIn(self.CLAIM_LINE, rows[-1]["reason"], "账本里读得出删掉的是哪一句")

    def test_an_unreadable_ledger_is_not_checked_not_an_empty_one(self):
        """动作账读不动（None）：这一关没跑，那一泡照发，claim_status 是 not-checked。"""
        real = conversation.answer

        def with_broken_ledger(text, who="", **kw):
            """装配交出的是读不动的动作账：闸该记「拿不到」，不是「账上空着」。"""
            kw.setdefault("transport", _body(self.CLAIM_LINE))
            kw.setdefault("config", CONFIG)
            kw.setdefault("env", ENV)
            payload = real(text, who, **kw)
            payload["actions"] = "坏了"
            return payload

        with mock.patch.object(conversation, "answer", with_broken_ledger):
            status, gated = api.handle("POST", "/chat", {"text": "你画好了吗"},
                                       self.store, brain=self.brain)
        self.assertEqual(200, status)
        part = gated["gate"]["parts"][0]
        self.assertEqual("not-checked", part["claim_status"], "拿不到账不是没做过")
        self.assertEqual("pass", part["decision"])
        self.assertEqual([self.CLAIM_LINE], [one["text"] for one in gated["chunks"]])

    # —— 拒绝：全仓唯一的投递点（本轮加的） ————————————————

    SOFT_ANSWER = ("……这个啊。鱼在海里游着游着，有些同伴就游远了……行了行了，"
                   "我又不是不能自己待着。倒是你，怎么突然问这个")
    ASKED_CAT = "猫今天怎么没来？"                    # 话题词表里带「猫」，投心才认得出来
    ANSWERED = "明天上午九点，我在医院门口等你。"      # 撞上正面回答的信号：她答了

    def _topics(self, *names: str):
        """把心的话题词表指到临时文件：认话题只认心自己那份词表，测试也照这个口径给。"""
        path = self.tmp / "topics.json"
        path.write_text(json.dumps({name: [] for name in names}, ensure_ascii=False),
                        encoding="utf-8")
        return mock.patch.dict(os.environ, {"HEART_TOPICS_FILE": str(path)})

    def _heart_file(self) -> Path:
        return Path(os.environ["HEART_ROOT"]) / "refusals.json"

    def _heart_rows(self):
        """心的冷却账里现在有几行（文件还不存在就是空表——不是「读不到」）。"""
        path = self._heart_file()
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8")).get("refusals") or []

    def _refusal_rows(self):
        """我们自己的拒绝账（refuse.jsonl）里现在有几行。"""
        path = Path(os.environ["MOUTH_TRACE_DIR"]) / "refuse.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_soft_redirect_lands_one_row_in_the_heart(self):
        """软转向走真链路：心的 refusals.json 多一行（含 topic 与 until），返回里 delivered=true。

        这就是「投递点只有 /chat」的正身：同一句在 /mouth/gate 上是 delivered=false（干跑），
        在这里才真落盘——两处判的法一模一样，区别只在写不写。
        """
        with self._topics("猫"):
            status, payload = self._send(text=self.ASKED_CAT, reply=self.SOFT_ANSWER)
        self.assertEqual(200, status)
        section = payload["refusal"]
        self.assertEqual({"kind", "shape", "verdict_reason", "delivered",
                          "cooling", "topic", "note"}, set(section))
        self.assertFalse(section["cooling"], "没在冷却：这一格要如实写 false，不躲不省")
        self.assertEqual("soft", section["kind"])
        self.assertEqual("soft", section["shape"])
        self.assertIn("软转向", section["verdict_reason"])
        delivered = section["delivered"]
        self.assertEqual({"delivered", "state", "topic", "until", "remaining_min", "reason"},
                         set(delivered))
        self.assertEqual(STATES[mouth_refusal.STATE_SENT], delivered["state"], "话出门了那一态")
        self.assertTrue(delivered["delivered"], "软转向也是拒绝，真对话里要投给心")
        self.assertEqual("猫", delivered["topic"])
        self.assertTrue(delivered["until"], "投进去必须带回冷却到什么时候")
        rows = self._heart_rows()
        self.assertEqual(1, len(rows), "一次真实对话就落一行")
        self.assertEqual("猫", rows[0]["topic"])
        self.assertTrue(rows[0]["until"])
        self.assertEqual("refused-topic", rows[0]["reason"])
        ledger = self._refusal_rows()
        self.assertEqual(1, len(ledger), "拒绝的账也在这儿记")
        self.assertEqual("soft", ledger[0]["shape"])
        self.assertTrue(ledger[0]["sent"])

    def test_a_standard_refusal_line_is_delivered_too(self):
        """她的答话就是数据里那三句之一：硬拒绝同样投一次，形状取数据里的键，不写死在代码里。"""
        said = mouth_refusal.say("topic")
        with self._topics("猫"):
            status, payload = self._send(text=self.ASKED_CAT, reply=said)
        self.assertEqual(200, status)
        section = payload["refusal"]
        self.assertEqual("hard", section["kind"])
        self.assertEqual("topic", section["shape"], "形状名取自 data/refusal.json")
        self.assertTrue(section["delivered"]["delivered"])
        rows = self._heart_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("猫", rows[0]["topic"])
        self.assertEqual("refused-topic", rows[0]["reason"])
        ledger = self._refusal_rows()[0]
        self.assertEqual("hard", ledger["shape"])
        self.assertEqual(STATES[mouth_refusal.STATE_SENT], ledger["state"])
        self.assertEqual(STATES[mouth_refusal.STATE_SENT], section["delivered"]["state"])

    def test_answered_reply_leaves_no_section_and_no_row(self):
        """正面答了：refusal 只剩「没在冷却」这一格（那四样不加空壳），两本账一行都不添。"""
        with self._topics("猫"):
            status, payload = self._send(text=self.ASKED_CAT, reply=self.ANSWERED)
        self.assertEqual(200, status)
        section = payload["refusal"]
        self.assertEqual({"cooling", "topic", "note"}, set(section),
                         "没判出拒绝就不添那四样，冷却那一格照写")
        self.assertEqual([], self._heart_rows())
        self.assertEqual([], self._refusal_rows())

    def test_unknown_topic_is_recorded_but_not_pretended(self):
        """asked 里没有词表上的话题名：事件成立、拒绝账照记，但投不进去，delivered=false 说清为什么。"""
        with self._topics("猫"):
            status, payload = self._send(text="今天好热。", reply=self.SOFT_ANSWER)
        self.assertEqual(200, status)
        delivered = payload["refusal"]["delivered"]
        self.assertFalse(delivered["delivered"])
        self.assertIsNone(delivered["topic"])
        self.assertIn("认不出", delivered["reason"])
        self.assertEqual([], self._heart_rows(), "认不出话题就一个字节都不写心")
        ledger = self._refusal_rows()
        self.assertEqual(1, len(ledger), "她拒了这件事照记，不因为投不进心就不记账")
        # 账本那一列记的是「这句话出没出门」：话真说出去了（出门了），只是没投进心（认不出话题）。
        self.assertTrue(ledger[0]["sent"])
        self.assertEqual(STATES[mouth_refusal.STATE_SENT], ledger[0]["state"])

    # —— 两态：话没出门就不算冷却用过（这一轮加的） ————————————

    def _seed_heart_file(self) -> Path:
        """在心根里造一份 refusals.json 种子：没出门那一轮要拿它验「一个字节都没写」。"""
        path = self._heart_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, {"version": 1, "config": {}, "refusals": []})
        return path

    def _print_of(self, path: Path) -> tuple:
        """一份文件的读数：大小 / mtime / sha256——「读一次」不等于「写一次」。"""
        blob = path.read_bytes()
        return (len(blob), path.stat().st_mtime_ns, hashlib.sha256(blob).hexdigest())

    def _event_of(self) -> dict:
        """现装一条拒绝事件（形状与 why 都取自数据）：投递那一步只认这四个键。"""
        return mouth_refusal.event("topic", mouth_refusal.say("topic"), chat._stamp())

    def test_a_refusal_that_never_left_does_not_burn_a_cooling(self):
        """门说 send=false：账上「没出门」、心一个字节不写、同一话题再问仍然会拒。

        四件事一起断言，缺一条两态就不成立：账本那一行 state/why/sent 说得清没出门；心的
        refusals.json 前后 size/mtime/sha256 一字不差（读一次不等于写一次）；下一轮 refusal.cooling
        仍是 false；拒绝话照说——**那次拒绝没白烧**。
        """
        said = mouth_refusal.say("refused-topic")
        with mock.patch.object(brain_model, "now_iso", lambda: DAYTIME):
            self._double_low(DAYTIME)
            seed = self._seed_heart_file()
            before = self._print_of(seed)
            with self._topics("猫"):
                status, payload = self._send(text=self.ASKED_CAT, reply=said)
        self.assertEqual(200, status)
        self.assertIs(False, payload["send"], "双低这一轮门说不发")
        delivered = payload["refusal"]["delivered"]
        self.assertFalse(delivered["delivered"], "没出门就不许说投过了")
        self.assertEqual(STATES[mouth_refusal.STATE_UNSENT], delivered["state"])
        self.assertEqual(before, self._print_of(seed),
                         "没出门却写了心的账（size / mtime / sha256 变了）")
        row = self._refusal_rows()[0]
        self.assertFalse(row["sent"])
        self.assertEqual(STATES[mouth_refusal.STATE_UNSENT], row["state"])
        self.assertEqual(STATES["suppressed_why"][mouth_refusal.SUPPRESSED_OUTBOUND], row["why"])
        self.assertEqual(payload["reason"], row["reason"], "那一轮门给的理由要一并落账")
        self.assertEqual(payload["level"], row["level"], "那一轮门给的层级要一并落账")
        # 同一话题再问一次：**冷却没起**，所以她照样拒（对方上一轮压根没听到）
        for name in (mood.MOOD_FILE, energy.ENERGY_FILE):
            (self._heart_root() / name).unlink(missing_ok=True)
        with mock.patch.object(brain_model, "now_iso", lambda: DAYTIME):
            with self._topics("猫"):
                status2, again = self._send(text=self.ASKED_CAT, reply=said)
        self.assertEqual(200, status2)
        self.assertIs(True, again["send"])
        self.assertFalse(again["refusal"]["cooling"], "上一轮没出门，这一轮不该在冷却里")
        self.assertEqual(said, again["reply"], "拒绝话照说")
        self.assertEqual(STATES[mouth_refusal.STATE_SENT], again["refusal"]["delivered"]["state"])
        self.assertEqual(1, len(self._heart_rows()), "这一回真出门了，冷却才起一次")
        self.assertEqual(2, len(self._refusal_rows()))

    def test_a_refusal_the_mouth_gate_swallowed_is_its_own_case(self):
        """门说发、八股闸把这一泡吞了：也算没出门（不起冷却），而 why 与「门说不发」不同。"""
        seed = self._seed_heart_file()
        before = self._print_of(seed)
        payload = {"send": True, "reason": "门说这一轮发", "level": "engage",
                   "gate": {"decision": "dropped"}, "chunks": []}
        judged = {"kind": mouth_refusal.KIND_HARD, "shape": "topic",
                  "verdict_reason": AUDIENCE["reason"], "asked": self.ASKED_CAT,
                  "event": self._event_of(), "sent": True,
                  "reason": "门说这一轮发", "level": "engage"}
        with self._topics("猫"):
            chat._deliver_judged(judged, payload)
        delivered = judged["delivered"]
        self.assertFalse(delivered["delivered"], "闸吞了就是没出门")
        self.assertEqual(STATES[mouth_refusal.STATE_UNSENT], delivered["state"])
        row = self._refusal_rows()[0]
        self.assertEqual(STATES["suppressed_why"][mouth_refusal.SUPPRESSED_SWALLOWED], row["why"])
        self.assertNotEqual(STATES["suppressed_why"][mouth_refusal.SUPPRESSED_OUTBOUND], row["why"],
                            "两种来由必须分得开：她不想说 / 她自己把话拦了")
        self.assertEqual(before, self._print_of(seed), "闸吞了也不许起冷却（心一个字节都不写）")

    def test_a_missing_send_flag_raises_instead_of_guessing(self):
        """payload 里没有 send、或它不是布尔：当场抛并指名 payload.send——不默认成发了。"""
        for bad in ({}, {"send": 1}, {"send": "true"}, {"send": None}):
            with self.subTest(payload=bad):
                with self.assertRaises(mouth_refusal.RefusalBroken) as caught:
                    chat._send_flag(bad)
                self.assertIn("payload.send", str(caught.exception))

    # —— 冷却里主动拒（这一轮加的） ————————————————

    ASKED_HOSPITAL = "医院那事怎么样了？"

    @contextlib.contextmanager
    def _cooling(self, name: str = "医院"):
        """在临时心根里真造一次冷却：词表指到临时文件、ingest 一次；退出时词表补丁自动还原。

        心那边收话题要先认词表，所以「指词表」与「记一次拒绝」必须在同一个环境补丁里做。
        全程只碰 setUp 里那两个临时根：真实心根、真实家根一个字节都不写。
        """
        with self._topics(name):
            heart_refuse.ingest(name, "refused-topic")
            yield

    def test_a_cooling_topic_is_refused_without_asking_the_model(self):
        """冷却里的話題：她说 refusal 那一句，模型**一次都没调**，两本账一行都不添。

        四件事一起断言，缺一条这条短路就不成立：说的是数据里那句拒绝；refusal 节带 topic /
        remaining_min / line；假 transport 的调用计数是 0；心与我们自己的账行数前后不变（已在冷却，
        再投一次就是重复记账）。
        """
        said = mouth_refusal.say("refused-topic")
        with self._cooling():
            heart_before = len(self._heart_rows())
            capture = []
            status, payload = self._post({"text": self.ASKED_HOSPITAL},
                                         _body(LONG_REPLY, capture))
            heart_after = len(self._heart_rows())
        self.assertEqual(200, status)
        self.assertEqual(said, payload["reply"], "冷却里说的是 refusal 那一句，不是模型那句")
        section = payload["refusal"]
        self.assertTrue(section["cooling"], "refusal.cooling 要如实写 true")
        self.assertEqual("医院", section["topic"])
        self.assertEqual("refused-topic", section["reason"])
        self.assertIsInstance(section["remaining_min"], float)
        self.assertGreater(section["remaining_min"], 0.0, "还剩多久要有数")
        self.assertIn("医院", section["line"], "面板那一句要带上话题名")
        self.assertIn("还剩", section["line"])
        self.assertEqual(said, section["say"])
        self.assertIsNone(section["event"], "冷却里不投事件：这一格必须是 null")
        self.assertEqual([], capture, "冷却里一次模型都不调（验收那一格：调用计数为 0）")
        self.assertEqual(heart_before, heart_after, "心那边的 refusals.json 行数不变")
        self.assertEqual([], self._refusal_rows(), "我们自己的 refuse.jsonl 也不添一行")
        self.assertEqual(said, "".join(one["text"] for one in payload["chunks"]),
                         "chunks 仍走 split + pace：拼回去就是那一句原文")

    def test_the_cooled_refusal_still_goes_through_the_gate(self):
        """那一句拒绝仍过八股闸：条款判了；复述审计没跑（audit-unavailable）；重写一次请求都不发。"""
        with self._cooling():
            capture = []
            status, payload = self._post({"text": self.ASKED_HOSPITAL},
                                         _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        gate = payload["gate"]
        self.assertEqual("pass", gate["decision"])
        self.assertEqual(gate["in_parts"], gate["out_parts"])
        self.assertIn("冷却", gate["note"])
        for part in gate["parts"]:
            self.assertEqual("audit-unavailable", part["audit"]["status"],
                             "没有装配就没有可比的原话：判不了，不是通过")
            self.assertIn(part["rewrite"]["status"], ("not-tried", "failed"),
                          "这一轮一次重写请求都不发：没命中就是 not-tried，命中就是留原话的 failed")
        self.assertEqual([], capture, "连重写那一次也没发")
        # 这一轮没有装配、没有 EngageInput：出站闸这一关如实写「没判」，四格照给（不许缩水成缺键）
        self.assertEqual(chat.OUTBOUND_UNJUDGED, gate["outbound"]["status"])
        self.assertTrue(gate["outbound"]["why"])
        self.assertIs(True, payload["send"], "门没判不等于不发")
        self.assertIsNone(payload["level"])
        self.assertIsNone(payload["reason"])
        self.assertEqual({}, payload["limits"])

    def test_the_same_question_outside_the_cooldown_answers_normally(self):
        """同一句、不在冷却里：照常装配与调模型（一次），refusal 节如实写「没在冷却」。"""
        with self._topics("医院"):
            capture = []
            status, payload = self._post({"text": self.ASKED_HOSPITAL},
                                         _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual(LONG_REPLY, payload["reply"])
        self.assertEqual(1, len([one for one in capture if [
            item["role"] for item in one["messages"]] == ["system", "user"]]),
            "装配那一问只发了一次（其余请求是长度/标点那一层改形发的）")
        section = payload["refusal"]
        self.assertFalse(section["cooling"], "没在冷却要说 false，不许省掉这一格")
        self.assertEqual("医院", section["topic"], "认得出话题就报出来")
        self.assertNotIn("kind", section, "没判出拒绝就不添那四样")
        self.assertEqual([], self._heart_rows())

    def test_an_unrecognised_topic_is_not_judged(self):
        """抽不到话题（词表里没有那句里的字）：冷却这一关不判，照常说话，topic 如实 null。"""
        with self._topics("猫"):
            capture = []
            status, payload = self._post({"text": "今天天气不错。"},
                                         _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual(LONG_REPLY, payload["reply"])
        self.assertEqual(1, len([one for one in capture if [
            item["role"] for item in one["messages"]] == ["system", "user"]]),
            "装配那一问只发了一次（其余请求是长度/标点那一层改形发的）")
        section = payload["refusal"]
        self.assertFalse(section["cooling"])
        self.assertIsNone(section["topic"], "认不出就是 null，不替她猜一个话题")
        self.assertEqual([], self._heart_rows())

    # —— 长度与句末标点（过闸之前先把形修一遍）———————————————

    def test_every_part_carries_a_length_reading_and_the_scene(self):
        """逐泡都带长度读数，gate.style 交代场景 / 档位 / 上限 / 目标区间 / p90 / 句号率。"""
        status, payload = self._send()
        self.assertEqual(200, status)
        section = payload["gate"]["style"]
        self.assertEqual(chat.STYLE_OK, section["status"])
        self.assertEqual(STYLE_SCENE, section["scene"])
        self.assertEqual(STYLE_SCENE_LABEL, section["scene_label"])
        self.assertEqual("%d-%d" % (STYLE_RANGES[str(section["tier"])]["lo"],
                                    STYLE_RANGES[str(section["tier"])]["hi"]),
                         section["target"])
        self.assertEqual(STYLE_P90, section["p90"])
        self.assertIn("stopped", section)
        self.assertEqual(STOPPED_CEILING, section["stopped"]["ceiling"])
        for row in payload["gate"]["parts"]:
            self.assertEqual({"decision", "scene", "target_lo", "target_hi", "chars",
                              "rewrites", "over_hard_cap", "reason"}, set(row["style"]))

    def test_the_scene_comes_from_the_scope(self):
        """scope=group 就按群聊那套区间判：场景从 /chat 的 scope 来，不是另算一遍。"""
        other = "group" if STYLE_SCENE != "group" else "private"   # 另一个实盘场景
        status, payload = self._post({"text": QUESTION, "scope": other}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        section = payload["gate"]["style"]
        self.assertEqual(other, section["scene"])
        table = STYLE_BOOK["scenes"][other]["ranges"]
        self.assertEqual("%d-%d" % (table[str(section["tier"])]["lo"],
                                    table[str(section["tier"])]["hi"]), section["target"])
        self.assertEqual(STYLE_BOOK["scenes"][other]["p90"], section["p90"])

    def test_full_stop_ratio_is_observed_without_rewriting_the_reply(self):
        """句号多只记录读数，不为标点更改意思、泡或额外请求模型。"""
        dense = "我在呢。" + "你说吧。" + "刚刚在听你讲那件事。"
        better = "我在呢！" + "你说吧？" + "刚刚在听你讲那件事"
        capture = []
        status, payload = self._post({"text": QUESTION}, _script(dense, better, captured=capture))
        self.assertEqual(200, status)
        counted = payload["gate"]["style"]["stopped"]
        self.assertTrue(counted["judged"])
        self.assertEqual("over_mark", counted["decision"])
        self.assertEqual(1, counted["ratio"])
        self.assertEqual(0, counted["rewrites"])
        self.assertEqual(1, len(capture))
        self.assertEqual(dense, payload["reply"])

    def test_complete_clarification_is_not_rewritten_just_for_length(self):
        reply = '不是周日，是周六下午两点，地点还是原来那家店；你只改了时间，没有说要换地方。'
        for scene in ('private', 'group'):
            with self.subTest(scene=scene):
                capture = []
                status, payload = self._post(
                    {'text':'确认一下：周六下午两点，地点照旧，不是周日。', 'scope':scene, 'scene':scene,
                     'who':CHANNEL_WHO, 'where':{'groupId':7002} if scene=='group' else {'userId':int(CHANNEL_WHO_ID)},
                     'delivery_ack':True},
                    _body(reply, capture))
                self.assertEqual(200, status)
                self.assertEqual(reply, payload['reply'])
                self.assertEqual(1, len(capture), '不能只因超过风格观察目标而新增压缩调用')
                self.assertTrue(any(part['style']['chars'] > part['style']['target_hi']
                                    for part in payload['gate']['parts']))
                self.assertTrue(all(part['style']['rewrites'] == 0 for part in payload['gate']['parts']))

    def test_punctuation_observation_preserves_numbers_quotes_and_negation(self):
        for reply in ("不是周日，是周六。", "车次是G123，18:30出发。", "原话是“周六去”，没说周日。"):
            with self.subTest(reply=reply):
                capture = []
                status, payload = self._post({"text": "你确认一下"}, _body(reply, capture))
                self.assertEqual(200, status)
                self.assertEqual(reply, payload["reply"])
                self.assertEqual(1, len(capture))

    def test_punctuation_observation_keeps_media_bubbles_and_pacing(self):
        chunks = [{"text": "周六见。", "gap_before": 0.4, "send_at": 123.4},
                  {"text": "小鲸鱼给你。", "gap_before": 0.8, "send_at": 124.2,
                   "image": "file:///tmp/isolated-whale.png"}]
        capture = []
        with self._answer(_body("不应为标点重新生成", capture)):
            result = chat._style_round(
                chunks, payload={"tone": {"tier": 3}, "reply_budget": {"chars": 220}},
                scope="private")
        self.assertEqual(chunks, result["chunks"])
        self.assertFalse(result["changed"])
        self.assertEqual([], capture)
        self.assertEqual(0, result["section"]["stopped"]["rewrites"])

    def test_length_observation_preserves_long_media_and_reports_hard_cap(self):
        reply = '不是周日，是周六下午两点，地点还是原来那家店；你只改了时间，没有说要换地方。'
        chunks = [{'text':reply, 'gap_before':0.4, 'send_at':123.4},
                  {'text':reply, 'gap_before':0.8, 'send_at':124.2,
                   'image':{'file':'file:///tmp/isolated.png'}}]
        for cap in (220, 20):
            with self.subTest(cap=cap):
                calls = []
                with self._answer(_body('不应调用', calls)):
                    result = chat._style_round(chunks, payload={'tone':{'tier':3}, 'reply_budget':{'chars':cap}},
                                               scope='private')
                self.assertEqual(chunks, result['chunks'])
                self.assertFalse(result['changed'])
                self.assertEqual([], calls)
                self.assertTrue(all(row['rewrites'] == 0 for row in result['rows']))
                self.assertTrue(all(row['over_hard_cap'] == (cap == 20) for row in result['rows']))

    def test_a_reply_at_the_minimum_sentence_count_is_judged(self):
        """够数据里那个最少句数就判：judged=true，超上限就改一次（改不动放行并记账）。

        「少于最少句数不判」那一路在 organs/mouth/tests/test_style.py 里验（那边能造出空段，
        /chat 造不出来：装配不出话时压根没有这一段）。
        """
        line = "我在呢。" + "你说吧。"
        status, payload = self._post({"text": QUESTION}, _body(line))
        counted = payload["gate"]["style"]["stopped"]
        self.assertTrue(counted["judged"])
        self.assertGreaterEqual(counted["sentences"], counted["min_sentences"])
        if counted["ratio"] > counted["ceiling"]:
            self.assertIn(counted["decision"], {"rewritten", "over_mark"})
        else:
            self.assertEqual("pass", counted["decision"])

    def test_the_three_part_shape_is_dropped_before_this_layer(self):
        """三件套在装配那一关就按整句判过、命中即弃，所以这一层看到的是空泡。

        这条同时钉住两件事：一是形状条款按**整条回复**判（一泡 1–2 句，看不见它）；
        二是这一层不另判一遍（同一句话判两次不会更好，而那一层连重写器都没有）。
        """
        shaped = "好。" + "你今天干嘛了？" + "我觉得挺好的。"
        status, payload = self._post({"text": QUESTION}, _script(shaped, "随便改一句也行"))
        self.assertEqual(200, status)
        self.assertEqual("dropped", payload["gate"]["decision"])
        self.assertEqual([], payload["chunks"])
        self.assertEqual("", payload["reply"])
        self.assertEqual(SHAPE_CLAUSE, payload["gate"]["hit"]["clause_id"])
        self.assertNotIn("style", payload["gate"], "没有泡进这一层，就没有长度/标点那一节")

    def test_a_plain_reply_is_left_alone(self):
        """一句正常的话一处都不改：句末不带句号的那种，这一关连判都不用判就过了。"""
        line = "我在呢，你说"
        status, payload = self._post({"text": QUESTION}, _body(line))
        self.assertEqual(line, payload["reply"])
        counted = payload["gate"]["style"]["stopped"]
        self.assertEqual("pass", counted["decision"])
        self.assertEqual(0, counted["stopped"])
        self.assertEqual(1, len(payload["chunks"]))
        self.assertEqual(line, payload["chunks"][0]["text"])


    # —— 出口认 audience：私密记忆在群里守边界（这一轮加的） ——————————

    PRIVATE_TEXT = "我妈住院那事，别到处说"          # 一条真写进临时脑根的记忆（正文不许出群）
    PRIVATE_ASKED = "你上次跟我说你妈住院那事"        # 直问那条记忆的那一句（连续重合过阈值）
    GROUP = AUDIENCE["scopes"]["group"]
    PRIVATE = AUDIENCE["scopes"]["private"]
    PRIVATE_FIELD = AUDIENCE["field"]
    PRIVATE_VALUE = AUDIENCE["private_value"]
    PRIVATE_SAY = AUDIENCE["say"]

    def test_real_private_memory_keeps_its_boundary_through_assembly(self):
        self.brain.ingest(who=CHANNEL_WHO_ID, text=self.PRIVATE_TEXT,
                          audience=self.PRIVATE_VALUE)
        with self._topics("住院"):
            status, payload = self._post(
                {"text": self.PRIVATE_ASKED, "who": CHANNEL_WHO,
                 "scene": self.GROUP, "scope": self.GROUP}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertTrue(payload["audience_held"], repr(payload.get("recalled")))
        self.assertIn(self.PRIVATE_SAY, payload["reply"])
        self.assertNotIn(self.PRIVATE_TEXT, json.dumps(payload, ensure_ascii=False))

    def test_context_expansion_keeps_private_topic_boundary_in_group(self):
        from organs.mind import appraisal
        self.brain.ingest(who=CHANNEL_WHO_ID, text=self.PRIVATE_TEXT, audience=self.PRIVATE_VALUE)
        recent.append({'ts':DAYTIME, 'who':CHANNEL_WHO_ID, 'said':'我妈住院了', 'reply':'嗯。',
                       'session_id':'qq:group:100099'})
        def complete(messages, **kwargs):
            if messages[0]['content'] == appraisal.PROMPT:
                return json.dumps({'kind':None, 'evidence':'', 'reason':'承接本人刚才的话题',
                    'memory_note':{'text':'继续聊我妈住院那事', 'current':['继续说这个吧'], 'previous':['我妈住院了']}})
            return LONG_REPLY
        with self._topics('住院'), mock.patch.object(llm, 'complete', side_effect=complete):
            status, payload = api.handle('POST', '/chat', {'text':'继续说这个吧', 'who':CHANNEL_WHO,
                'scene':'group', 'where':{'groupId':100099}, 'delivery_ack':True}, self.store, brain=self.brain)
        self.assertEqual(200, status)
        self.assertIn('住院', payload['memory_recall']['query'])
        self.assertTrue(payload['audience_held'])
        self.assertIn(self.PRIVATE_SAY, payload['reply'])
        self.assertNotIn(self.PRIVATE_TEXT, json.dumps(payload, ensure_ascii=False))

    @contextlib.contextmanager
    def _private_memory(self, text: str = PRIVATE_TEXT):
        """在临时脑根里**真写一条记忆**，并让装配那一层的 recalled 带上私密标记。

        记忆这一侧是真的（brain.ingest 落到 setUp 那个临时脑根）；标记只能在这一层补上——
        **audience 的生产者不在本轮**：mind 的 _recall 现在只搬固定那几栏，出口要认的那一栏
        因此得靠这个假 _recall 打上（验收里允许的两条路之一）。
        """
        self.brain.ingest(who="3425635473", text=text)
        real = conversation._recall

        def tagged(brain, said, who, scene=None, *, cap=None, reader=None):
            rows, memory_text, audit = real(brain, said, who, scene, cap=cap, reader=reader)
            for row in rows:
                row[self.PRIVATE_FIELD] = self.PRIVATE_VALUE
            if not any(row.get("text") == text for row in rows):
                rows.append({"id": "私密-被试", "text": text, "display_text": text,
                             "source": "longterm", "source_type": "亲历",
                             "evidence_key": "被试", "score": 1.0, "who": "3425635473",
                             "note_display": "", "in_prompt": False, self.PRIVATE_FIELD: self.PRIVATE_VALUE})
            return rows, memory_text, audit

        with mock.patch.object(conversation, "_recall", tagged):
            yield

    def test_a_private_memory_asked_in_a_group_says_the_boundary_line(self):
        """验收 1：scope=group + 直问 → 出声、说的是数据里那句边界话、正文一个字都不出群。

        四件事一起断言，缺一条这条边界就不成立：reply 非空且含那句边界话；正文不出现（reply 与
        整个回包都不出现）；事件与账本各一行（reason / full_name 取数据里那个 refused-topic，
        账本的 why 带 mark 记号）；audience_held 里看得见被扣下的那一条（id + 一小截摘要，不贴正文）。
        """
        with self._private_memory(), self._topics("住院"):
            status, payload = self._post({"text": self.PRIVATE_ASKED, "scope": self.GROUP},
                                         _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertTrue(payload["reply"], "群里被问到私密条目：她必须出声，沉默是最糟的一种哑")
        self.assertIn(self.PRIVATE_SAY, payload["reply"])
        self.assertNotIn(self.PRIVATE_TEXT, payload["reply"], "边界话里不许出现那条记忆的正文")
        self.assertNotIn(self.PRIVATE_TEXT, json.dumps(payload, ensure_ascii=False),
                         "被扣下的正文一个字都不许往外带（recalled / 读数里都不许有）")
        held = payload["audience_held"]
        self.assertEqual(1, len(held), "扣下的那一条要在读数里看得见")
        self.assertEqual({"id", "text_min", "source"}, set(held[0]))
        self.assertTrue(held[0]["id"])
        self.assertTrue(held[0]["text_min"])
        self.assertLessEqual(len(held[0]["text_min"]),
                             AUDIENCE["criteria"]["excerpt_chars"], "读数只给一小截摘要")
        self.assertEqual([], payload["recalled"], "剔掉了就不再进 recalled")
        rows = self._heart_rows()
        self.assertEqual(1, len(rows), "边界话走拒绝那条路：心那边记一次")
        self.assertEqual(AUDIENCE["event_shape"], rows[0]["reason"])
        ledger = self._refusal_rows()
        self.assertEqual(1, len(ledger), "我们自己的账也记一行")
        self.assertEqual(AUDIENCE["event_shape"], ledger[0]["full_name"])
        self.assertIn(AUDIENCE["mark"], ledger[0]["why"], "reason 里要带 audience 记号")
        self.assertTrue(ledger[0]["sent"])
        section = payload["refusal"]
        self.assertFalse(section["cooling"], "这不是冷却那一轮")
        self.assertEqual("hard", section["kind"])
        self.assertTrue(section["delivered"]["delivered"])
        self.assertEqual("pass", payload["gate"]["decision"], "边界话照样过闸")

    def test_the_same_private_memory_is_spoken_in_private(self):
        """验收 2：同一句 + scope=private → 照常说内容，不剔除、不出声、不投事件。

        这一句把那条记忆连着的 10 个字原样说了出来（超过审计阈值 6），**只因为前面带着引述标记
        「你上次说」才放行**——记忆原文与「对方原话」现在同一把尺（audit.json 的 quote_kinds）：
        带标记算转述、不带标记仍按复述拦。所以这条同时钉住两件事：私聊不扣正文，以及那一族真的
        是「带标记就放」。
        """
        spoken = "你上次说，我妈住院那事别到处说，我记着呢。"
        with self._private_memory(), self._topics("住院"):
            status, payload = self._post({"text": self.PRIVATE_ASKED, "scope": self.PRIVATE},
                                         _body(spoken))
        self.assertEqual(200, status)
        self.assertEqual(spoken, payload["reply"], "私聊里照常说：内容不被边界话替换")
        self.assertIn("住院", payload["reply"], "那条记忆的关键词要能说出来")
        self.assertEqual([], payload["audience_held"], "私聊里不剔除，也没有边界话")
        self.assertEqual(1, len(payload["recalled"]), "私聊里照常召回（这一条仍在 recalled 里）")
        part = payload["gate"]["parts"][0]
        self.assertEqual("pass", part["decision"], "带引述标记的引述不判废")
        self.assertIsNone(part["audit"]["hit"], "带了标记：这一句算转述，不算复述")
        self.assertGreaterEqual(part["audit"]["lcs_len"], part["audit"]["threshold"],
                                "这一段确实撞够了阈值——放行是靠标记，不是靠没撞上")
        self.assertFalse(part["audit"]["skipped"], "没有请求重复，也没有跳过")
        self.assertEqual([], self._refusal_rows(), "没走拒绝就不记账")

    def test_a_group_round_that_does_not_ask_holds_it_silently(self):
        """验收 3：scope=group 但没被问到 → 不主动引用（正文不出现）、也不说边界话，但剔除看得见。"""
        with self._private_memory(), self._topics("住院"):
            status, payload = self._post({"text": "今天吃啥", "scope": self.GROUP},
                                         _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual(LONG_REPLY, payload["reply"], "没被问到就照常说她自己那句")
        self.assertNotIn(self.PRIVATE_SAY, payload["reply"],
                         "不为没发生的事拒绝：这一轮不说边界话")
        self.assertNotIn(self.PRIVATE_TEXT, json.dumps(payload, ensure_ascii=False),
                         "没被问到也不主动引用：正文一个字都不出现")
        self.assertEqual(1, len(payload["audience_held"]), "剔除照做，读数里看得见")
        self.assertEqual([], payload["recalled"])
        self.assertEqual([], self._refusal_rows(), "没被问到不投事件")
        self.assertEqual([], self._heart_rows())

    def test_an_illegal_scope_is_400_and_names_it(self):
        """验收 4：scope 非法值 → 400，message 里点名读到的值，并列出认得的两个。"""
        status, payload = self._post({"text": QUESTION, "scope": "公屏"}, _body(LONG_REPLY))
        self.assertEqual(400, status, "非法场景当场 400")
        self.assertFalse(payload["ok"])
        self.assertIn("公屏", payload["message"], "要点名读到的那个值")
        for name in (self.GROUP, self.PRIVATE):
            self.assertIn(name, payload["message"], "顺带列出认得的两个值")
        self.assertEqual([], self._refusal_rows(), "body 就是错的：一个字都不写")

    def test_a_cooling_topic_beats_the_boundary_line_and_records_once(self):
        """验收 5：已冷却 + 私密被问 → 走冷却那条（不重复投事件，也不说边界话）。

        顺序的理由：冷却在装配前就短路了（那一轮没有 recalled），它也比边界更靠前——让边界话抢在
        冷却前面，就是把一次已经记过的拒绝再说一遍、再投一遍。
        """
        said = mouth_refusal.say(AUDIENCE["event_shape"])
        with self._cooling("住院"):
            heart_before = len(self._heart_rows())
            with self._private_memory():
                status, payload = self._post(
                    {"text": self.PRIVATE_ASKED, "scope": self.GROUP}, _body(LONG_REPLY))
            heart_after = len(self._heart_rows())
        self.assertEqual(200, status)
        self.assertEqual(said, payload["reply"], "冷却优先：说的是冷却那一句，不是边界话")
        self.assertTrue(payload["refusal"]["cooling"])
        self.assertNotIn(self.PRIVATE_SAY, payload["reply"])
        self.assertEqual(heart_before, heart_after, "冷却里不投事件（同一件事不记两遍）")
        self.assertEqual([], self._refusal_rows(), "我们自己的账也不添一行")
        self.assertEqual([], payload["audience_held"], "冷却那一轮没有装配，也没有 recalled")

    # —— 失败 ————————————————————————————————

    def test_missing_key_is_503_with_a_readable_message(self):
        status, payload = self._post({"text": QUESTION}, _failing(AssertionError("不该发请求")),
                                     env={})
        self.assertEqual(503, status, "没配 key 该是 503")
        self.assertFalse(payload["ok"])
        self.assertEqual("no-key", payload["kind"])
        self.assertIn("还没配模型 key", payload["message"])
        self.assertIn("QQAGENT_LLM_KEY", payload["message"])
        self.assertNotEqual("", payload["message"], "失败必须带一句人话，不是空串")
        self.assertNotIn("reply", payload)

    def test_timeout_is_504(self):
        status, payload = self._post({"text": QUESTION}, _failing(TimeoutError("慢")))
        self.assertEqual(504, status)
        self.assertEqual("timeout", payload["kind"])
        self.assertTrue(payload["message"])

    def test_failed_reply_keeps_received_segment_for_the_next_correction(self):
        for index, scene in enumerate(('private', 'group')):
            with self.subTest(scene=scene):
                who = str(10000420 + index)
                session = 'qq:private:' + who if scene == 'private' else 'qq:group:70001'
                body = {'who': who, 'scene': scene, 'delivery_ack': True,
                        'where': {'groupId': 70001} if scene == 'group' else {}}
                original = '我想周六去紫藤公园看花'
                segment = [{'who': who, 'speaker': '测试甲(' + who + ')', 'text': original}]
                if scene == 'group':
                    segment.append({'who': '10000430', 'speaker': '测试乙(10000430)', 'text': '我周日想看电影'})
                status, failed = self._post({**body, 'text': original, 'segment': segment},
                    _failing(TimeoutError('本地模拟模型超时')))
                self.assertEqual(504, status)
                self.assertNotIn('reply', failed)
                history = recent.load(session_id=session)['turns']
                self.assertEqual(1, len(history), '回复失败不能丢掉已经收到的人话')
                self.assertEqual('', history[0]['reply'])
                self.assertEqual(segment, history[0]['utterances'])
                rows = [r for r in self.brain.today_entries() if r.get('session_id') == session]
                self.assertEqual([r['text'] for r in segment], [r['text'] for r in rows])
                self.assertEqual([r['who'] for r in segment], [r['who'] for r in rows])
                self.assertFalse(any(r.get('side') == brain_model.SIDE_SELF for r in rows))
                captured, interpreted = [], []
                def assessor(**context):
                    interpreted.append(context)
                    return {'kind': None, 'who': context['who'], 'status': 'ok', 'note': '更正日期'}
                status, _ = self._post({**body, 'text': '改成周日下午三点'},
                    _body('好，周日下午。', captured), assessor=assessor)
                self.assertEqual(200, status)
                self.assertIn(original, json.dumps(interpreted[0]['history'], ensure_ascii=False))
                prompt = json.dumps(captured[0]['messages'], ensure_ascii=False)
                self.assertIn(original, prompt)
                self.assertIn('改成周日下午三点', prompt)
                rows = [r for r in self.brain.today_entries() if r.get('session_id') == session]
                self.assertEqual(1, sum(r.get('text') == original for r in rows))

    def test_rate_limit_and_empty_reply_keep_input_without_fabricating_reply(self):
        empty = json.dumps({'choices': [{'message': {'content': '   '}}]}).encode()
        for index, (transport, expected, kind) in enumerate((
                (_status(429, b'{"error":"slow down"}'), 429, 'rate_limit'),
                (_status(200, empty), 502, 'empty'))):
            with self.subTest(kind=kind):
                who = str(10000520 + index)
                text = '我把明天的车票改到下午了'
                status, payload = self._post({'text': text, 'who': who, 'scene': 'private', 'where': {}}, transport)
                self.assertEqual((expected, kind), (status, payload['kind']))
                self.assertNotIn('reply', payload)
                history = recent.load(session_id='qq:private:' + who)['turns']
                self.assertEqual(1, len(history))
                self.assertEqual((text, ''), (history[0]['said'], history[0]['reply']))

    def test_failed_proactive_generation_does_not_invent_a_received_message(self):
        status, payload = self._post({'mode': 'proactive', 'seed': '想起紫藤公园',
            'who': '10000420', 'scene': 'private'}, _failing(TimeoutError('本地模拟失败')))
        self.assertEqual((504, 'timeout'), (status, payload['kind']))
        self.assertEqual([], recent.load()['turns'])
        self.assertEqual([], self.brain.today_entries())

    def test_rate_limit_is_429(self):
        status, payload = self._post({"text": QUESTION}, _status(429, b'{"error":"slow down"}'))
        self.assertEqual(429, status)
        self.assertEqual("rate_limit", payload["kind"])

    def test_auth_is_502(self):
        status, payload = self._post({"text": QUESTION}, _status(401, b'{"error":"bad key"}'))
        self.assertEqual(502, status)
        self.assertEqual("auth", payload["kind"])

    def test_empty_answer_is_502_not_a_success(self):
        empty = json.dumps({"choices": [{"message": {"content": "   "}}]}).encode("utf-8")
        status, payload = self._post({"text": QUESTION}, _status(200, empty))
        self.assertEqual(502, status, "空内容被当成成功了")
        self.assertEqual("empty", payload["kind"])
        self.assertNotIn("reply", payload)

    def test_missing_text_and_missing_organs(self):
        with self._answer(_failing(AssertionError("不该发请求"))):
            status, payload = api.handle("POST", "/chat", {}, self.store, brain=self.brain)
            self.assertEqual(400, status)
            self.assertIn("POST /chat 要给 text", payload["message"])
            for body in ({"text": "   "}, None):
                status, payload = api.handle("POST", "/chat", body, self.store,
                                             brain=self.brain)
                self.assertEqual(400, status, body)
                self.assertIn("POST /chat 要给 text", payload["message"])
            status, payload = api.handle("POST", "/chat", {"text": QUESTION},
                                         None, brain=self.brain)
            self.assertEqual(503, status)
            self.assertIn("魂未载入", payload["message"])
            status, payload = api.handle("POST", "/chat", {"text": QUESTION},
                                         self.store, brain=None)
            self.assertEqual(503, status)
            self.assertIn("脑未载入", payload["message"])

    # —— 方法 ————————————————————————————————

    def test_other_methods_are_405(self):
        for method in ("GET", "PUT", "DELETE"):
            status, payload = api.handle(method, "/chat", None, self.store, brain=self.brain)
            self.assertEqual(405, status, "%s /chat 该是 405" % method)
            self.assertFalse(payload["ok"])
            self.assertIn("只收 POST", payload["message"])

    # —— 落脑 ————————————————————————————————

    def test_this_round_lands_in_brain(self):
        status, payload = self._send(text=QUESTION)
        self.assertEqual(200, status)
        rows = self.brain.today_entries()
        texts = [row.get("text") for row in rows]
        self.assertIn(QUESTION, texts, "人的原话没进脑")
        self.assertIn(LONG_REPLY, texts, "她那句话没进脑")
        for row in rows[-2:]:
            self.assertEqual("亲历", row.get("source_type"))
            self.assertTrue(row.get("evidence_key"), "证据指针是空的")
        wrote = payload["memory"]["wrote"]
        self.assertEqual(2, len(wrote))
        self.assertEqual([row["evidence_key"] for row in rows[-2:]],
                         [item["id"] for item in wrote])

    # —— 连续性与温度 ————————————————————————

    def test_continuity_llm_and_anchors_are_in_the_body(self):
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)

        # 四个旧键一个都不许动，只许多出 reconciled 与 rounds（补记读数对页面也冻结）
        self.assertEqual({"session_id", "turns_loaded", "omitted", "notice", "reconciled", "rounds"},
                         set(payload["continuity"]))
        self.assertEqual(0, payload["continuity"]["turns_loaded"])
        self.assertEqual(0, payload["continuity"]["omitted"])
        self.assertEqual("", payload["continuity"]["notice"], "没省任何一轮时 notice 该是空串")
        self.assertEqual(0, payload["continuity"]["reconciled"], "一轮都没补，却报成补了")
        self.assertEqual([], payload["continuity"]["rounds"], "没有历史，rounds 该是空数组")

        self.assertEqual({"model", "temperature", "temperature_tier"}, set(payload["llm"]))
        self.assertEqual(CONFIG["model"], payload["llm"]["model"])
        self.assertIn(payload["llm"]["temperature_tier"], ("default", "factual"))
        self.assertEqual(capture[0]["temperature"], payload["llm"]["temperature"],
                         "回包里的温度与真发出去的那个不一致")

        self.assertFalse(payload["anchors"]["configured"], "家·仓库里没有台词库，却报成配了")
        self.assertEqual(conversation.ANCHORS_MISSING, payload["anchors"]["note"])

    def test_fifteen_rounds_show_the_omitted_notice_over_http(self):
        for index in range(15):
            recent.append({"session_id": "api:panel", "ts": "2025-01-15T14:0%d:00+08:00" % (index % 10), "who": "3425635473",
                           "said": "第%d句" % index, "reply": "第%d答" % index})
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        continuity = payload["continuity"]
        self.assertEqual(12, continuity["turns_loaded"])
        self.assertEqual(3, continuity["omitted"])
        self.assertEqual(12, len(continuity["rounds"]), "rounds 要为装进提示词的每一轮给一行")
        self.assertEqual({"ts", "fidelity", "msg_id"}, set(continuity["rounds"][0]))
        self.assertEqual(0, continuity["reconciled"], "造出来的 12 轮都是亲历，不该有补记")
        notice = conversation.load_assembly()["recent"]["notice"].replace(recent.NOTE_SLOT, "3")
        self.assertEqual(notice, continuity["notice"])
        self.assertIn(notice, "\n\n".join(m["content"] for m in capture[0]["messages"]), "提示词里没有那句「更早还有」")

    def test_five_rounds_carry_no_notice(self):
        for index in range(5):
            recent.append({"session_id": "api:panel", "ts": "2025-01-15T14:0%d:00+08:00" % index, "who": "3425635473",
                           "said": "第%d句" % index, "reply": "第%d答" % index})
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual(5, payload["continuity"]["turns_loaded"])
        self.assertEqual(0, payload["continuity"]["omitted"])
        self.assertEqual("", payload["continuity"]["notice"])

    def test_unscoped_brain_memory_is_not_backfilled_as_current_chat(self):
        """共享记忆缺少会话编号，不冒充当前窗口刚才发生的对话。"""
        self.brain.ingest(who="3425635473", text="我上周把片子落在诊室了")
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual(0, payload["continuity"]["reconciled"])
        self.assertEqual([], payload["continuity"]["rounds"])
        self.assertEqual("api:panel", payload["continuity"]["session_id"])

    def test_configured_anchor_arrives_at_the_endpoint(self):
        """家·仓库里登记两张台词卡：端点报成配了，只回 id / phrase / injected / when，example 不进提示词。"""
        home = Path(os.environ["HOME_ROOT"])
        home_anchors.append_anchors(root=home, role="human", by="人", anchors=[
            {"id": "被夸", "phrase": "才知道啊", "example": "哼，才知道啊。",
             "when": "被夸，且不想直接承认高兴", "body": "尾巴翘起来晃了两下"},
            {"id": "认错", "phrase": "记岔了", "example": "……是我记岔了。",
             "when": "发现自己记错了", "body": "低头、尾巴垂下去不敢动"},
        ])
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        anchors = payload["anchors"]
        self.assertEqual({"configured", "count", "available", "injected", "instruction",
                          "items", "skipped", "banned_by_repeat"}, set(anchors))
        self.assertTrue(anchors["configured"])
        self.assertEqual(2, anchors["count"])
        self.assertEqual(conversation.load_assembly()["anchors"]["instruction"],
                         anchors["instruction"])
        by_id = {item["id"]: item for item in anchors["items"]}
        self.assertEqual({"id", "phrase", "injected", "when", "example", "body"},
                         set(by_id["被夸"]),
                         "items 只带这四栏：example 一个字都不许回（审计要用时现读家）")
        self.assertEqual("才知道啊", by_id["被夸"]["phrase"])
        self.assertEqual("被夸，且不想直接承认高兴", by_id["被夸"]["when"])
        system = capture[0]["messages"][0]["content"]
        self.assertIn(by_id["被夸"]["injected"], system)
        self.assertNotIn("尾巴翘起来晃了两下", system, "body 进了提示词")
        self.assertNotIn("哼，才知道啊。", system, "整句例句进了提示词")
        self.assertNotIn("被夸，且不想直接承认高兴", system, "when 进了提示词")

    # —— 豁免：对方要她再说一遍（本轮加的） ————————————

    ASKED = "你刚才说的那句再说一遍"          # 一句实打实的「再说一遍」请求

    def test_a_repeat_the_user_asked_for_is_not_dropped(self):
        """上一轮她说过这一句；这一轮对方说「你刚才说的那句再说一遍」，她原样重复。

        验收一：decision 仍是 pass、text 原样、账本多一行 reason 里带 skipped-by-request、
        repeat_status 记成跳过那个状态——**她重复是对的，别吞**。
        """
        status, first = self._send(text="我回来了", reply=self.HER_LINE)
        self.assertEqual(200, status)
        self.assertEqual("pass", first["gate"]["decision"])
        before = len(self._drops_rows())
        status, payload = self._post({"text": self.ASKED}, _body(self.HER_LINE))
        self.assertEqual(200, status)
        gate = payload["gate"]
        self.assertEqual("pass", gate["decision"], "对方要她重复，这一句不该被判废")
        self.assertEqual(self.HER_LINE, payload["reply"])
        self.assertEqual([self.HER_LINE], [one["text"] for one in payload["chunks"]])
        self.assertEqual([], gate["blocked"])
        part = gate["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertEqual(self.HER_LINE, part["before"])
        self.assertEqual(self.HER_LINE, part["after"], "放行就是原样放行，一个字都不改")
        self.assertEqual(mouth_repeat.STATUS_SKIPPED, part["repeat_status"])
        self.assertEqual("not-tried", part["rewrite"]["status"], "豁免这一泡一次重写都不发")
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows), "跳过也要留一行账")
        self.assertEqual(SKIP_CLAUSE, rows[-1]["clause_id"], "账本里是数据里那个名字")
        self.assertIn(SKIP_MARK, rows[-1]["reason"])

    def test_same_repeat_without_request_is_observed(self):
        status, first = self._send(text="我回来了", reply=self.HER_LINE)
        self.assertEqual(200, status)
        before = len(self._drops_rows())
        captured = []
        status, payload = self._post({"text": "今天吃啥"}, _script(self.HER_LINE, captured=captured))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertEqual("observed", part["repeat_status"])
        self.assertEqual(self.HER_LINE, payload["reply"])
        self.assertEqual([self.HER_LINE], [one["text"] for one in payload["chunks"]])
        self.assertEqual([], part["hits"])
        self.assertFalse(part["kept_after_rewrite"])
        self.assertEqual(1, len(captured))
        self.assertEqual(before, len(self._drops_rows()))

    def test_a_request_without_a_repeat_records_nothing(self):
        """他虽说了「再说一遍」，但这一句本来就不重复：没有「判废」可放，也就不记那一笔账。"""
        status, first = self._send(text="我回来了", reply=self.HER_LINE)
        self.assertEqual(200, status)
        before = len(self._drops_rows())
        status, payload = self._post({"text": self.ASKED}, _body("碗我放桌上了，主人回来啦。"))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertEqual("ok", part["repeat_status"], "比过了、没重复——不是跳过")
        self.assertEqual(before, len(self._drops_rows()), "没撞上就没有账可记")

    # —— 豁免：对方要她重复时的复述审计（本轮加的） ————————

    SETTING = "嘴上不认，行动先到：拒绝的话说完，事已经做了"   # 魂里那句原文（设定类，照搬就拦）
    BEFORE_LINE = "主人回来啦，我把碗放桌上了。"                # 她上一轮说过的那句（开场就是这几个字）
    RECITED = "主人回来啦，我嘴上不认，行动先到。"               # 这一轮她照做：同一开场 + 复述设定段
    ASKED_AGAIN = "再说一遍你刚才说的是什么"                    # 现场那句（命中请求标记）

    def _soul_store(self, line: str):
        """只带一张方式卡的魂：注入的原文因此只有这一句，复述与不复述一眼分得开。"""
        soul = self.tmp / "soul"
        write_json(soul / "manner.json", {"stance": line})
        return load(soul)

    def test_a_recitation_the_user_asked_for_passes_verbatim(self):
        """现场复现：对方说「再说一遍你刚才说的是什么」，她照做 → 复述设定段 + 同一动作行。

        验收一：decision 仍是 pass、chunks 与 reply 非空、text 与输入逐字相同；账上两行（内容重复
        与复述审计各一行、各带自己的记号）；审计那节 hit 照旧在（面板看得见她引了设定）且
        skipped=true。**她照办的那一下不该被吞**。
        """
        store = self._soul_store(self.SETTING)
        status, first = self._post({"text": "我回来了"}, _body(self.BEFORE_LINE), store=store)
        self.assertEqual(200, status)
        self.assertEqual("pass", first["gate"]["decision"], "上一轮那句既不复读也没复述")
        before = len(self._drops_rows())
        status, payload = self._post({"text": self.ASKED_AGAIN}, _body(self.RECITED), store=store)
        self.assertEqual(200, status)
        gate = payload["gate"]
        self.assertEqual("pass", gate["decision"], "对方要她重复，这一句不该被判废")
        self.assertEqual([self.RECITED], [one["text"] for one in payload["chunks"]])
        self.assertEqual(self.RECITED, payload["reply"])
        self.assertTrue(payload["chunks"], "chunks 不许空")
        self.assertTrue(payload["reply"], "reply 不许空")
        self.assertEqual([], gate["blocked"])
        part = gate["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertEqual(self.RECITED, part["before"])
        self.assertEqual(self.RECITED, part["after"], "放行就是原样放行，一个字都不改")
        self.assertTrue(part["audit"]["skipped"], "审计这一关也按他说的放过")
        self.assertIsNotNone(part["audit"]["hit"], "面板上要看得见她引了设定")
        self.assertEqual("ok", part["audit"]["status"])        # 状态仍是 ok：真的比过了
        self.assertEqual(mouth_repeat.STATUS_SKIPPED, part["repeat_status"])
        self.assertEqual("not-tried", part["rewrite"]["status"], "豁免这两泡一次重写都不发")
        rows = self._drops_rows()
        self.assertEqual(before + 2, len(rows), "两关各记一行账，不合并")
        self.assertEqual(SKIP_CLAUSE, rows[-2]["clause_id"])
        self.assertIn(SKIP_MARK, rows[-2]["reason"])
        self.assertEqual(AUDIT_SKIP_CLAUSE, rows[-1]["clause_id"])
        self.assertIn(AUDIT_SKIP_MARK, rows[-1]["reason"])
        self.assertNotEqual(rows[-2]["reason"], rows[-1]["reason"], "两行各是各的说法")

    def test_the_same_recitation_without_a_request_is_judged_but_kept(self):
        """同一句照搬、对方没有请求（「今天吃啥」）：豁免不许外溢——判据照旧判，只删那一句。

        **这条断言是被新规矩改准的**：以前这里整泡 dropped（上一轮要求「同一句仍 dropped」），
        再后来是「二次命中留原话」。规矩改成「只删被审计命中的那一句」之后，这一泡是 rewritten：
        新稿「照搬那句 + 一串数字」，删掉照搬那句，剩下的那串数字照发——**宁可少说一句，不许整句
        不说**。「没有豁免」仍然看得见：audit.skipped 是 false、audit.hit 照旧带着、账上那一行是
        recited-only（不是 skipped-by-request 的记号）。
        """
        store = self._soul_store(self.SETTING)
        before = len(self._drops_rows())
        status, payload = self._post({"text": "今天吃啥"},
                                     _script(self.RECITED, self.RECITED + "3425635473"), store=store)
        self.assertEqual(200, status)
        gate = payload["gate"]
        part = gate["parts"][0]
        self.assertEqual("rewritten", gate["decision"], "删了一句，不是整泡判废")
        self.assertEqual(["3425635473"], [one["text"] for one in payload["chunks"]],
                         "只剩那句照搬被删：剩下的正文照发")
        self.assertEqual("3425635473", payload["reply"])
        self.assertEqual([], gate["blocked"], "这一泡没被弃，blocked 就不该有它")
        self.assertEqual(self.RECITED, part["before"])
        self.assertEqual("3425635473", part["after"], "after 是删完那一句的正文")
        self.assertFalse(part["audit"]["skipped"], "没有请求就不许记成跳过")
        self.assertEqual("ok", part["audit"]["status"], "审计照旧判过（不是没跑）")
        self.assertIsNotNone(part["audit"]["hit"])
        self.assertEqual(AUDIT_BOOK["verdicts"]["setting"], part["hits"][-1]["clause_id"])
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows), "删了那一句也要留一行账")
        self.assertEqual(AUDIT_BOOK["verdicts"]["setting"], rows[-1]["clause_id"])
        self.assertIn(RECITED_MARK, rows[-1]["reason"],
                      "账上要看得出是「删句」（recited-only），不是豁免外溢")
        self.assertNotIn(AUDIT_SKIP_MARK, rows[-1]["reason"], "没有请求：这一行不许带跳过记号")

    def test_a_request_that_recites_nothing_records_only_nothing(self):
        """他虽说了「再说一遍」，但这一句本来就没复述也没重复：没有「判废」可放，账一行都不记。"""
        store = self._soul_store(self.SETTING)
        before = len(self._drops_rows())
        status, payload = self._post({"text": self.ASKED_AGAIN}, _body("今天风大，我把窗关好了。"),
                                     store=store)
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertFalse(part["audit"]["skipped"])
        self.assertIsNone(part["audit"]["hit"])
        self.assertEqual(before, len(self._drops_rows()), "没撞上就没有账可记")

    # —— 重写：命中先改一次（本轮加的） ————————————

    def test_repeated_line_preserves_original_wording(self):
        status, first = self._send(text="我回来了", reply=self.HER_LINE)
        self.assertEqual(200, status)
        before = len(self._drops_rows())
        captured = []
        status, payload = self._post({"text": "还在吗"}, _script(self.HER_LINE, captured=captured))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertEqual("observed", part["repeat_status"])
        self.assertEqual(self.HER_LINE, payload["reply"])
        self.assertEqual([self.HER_LINE], [one["text"] for one in payload["chunks"]])
        self.assertEqual([], part["hits"])
        self.assertFalse(part["kept_after_rewrite"])
        self.assertEqual(1, len(captured))
        self.assertEqual(before, len(self._drops_rows()))

    def test_a_recited_setting_is_rewritten_once(self):
        """复述设定段命中 → 假 transport 换成她自己的说法 → 通过：audit 命中过、最终却没丢。"""
        line = "我嘴上不认，行动先到"
        said = "我嘴上不服软，可该做的都先做了。"
        soul = self.tmp / "soul"
        write_json(soul / "manner.json", {"stance": line})
        store = load(soul)
        before = len(self._drops_rows())
        status, payload = self._post({"text": "你是不是又不承认"},
                                     _script(line, said), store=store)
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("ok", part["audit"]["status"], "审计真的比过了")
        self.assertEqual("复述设定段", part["audit"]["hit"]["kind"], "进门那一次确实撞上了")
        self.assertEqual("rewritten", part["decision"])
        self.assertEqual("ok", part["rewrite"]["status"])
        self.assertEqual([said], [one["text"] for one in payload["chunks"]])
        self.assertEqual(said, payload["reply"])
        self.assertEqual([], payload["gate"]["blocked"])
        self.assertEqual(before, len(self._drops_rows()))

    def test_a_failed_rewrite_keeps_her_line_instead_of_silence(self):
        """超时 / 回空串 / 回与原文逐字相同：三种都保留原文，账本一行带 rewrite-failed，输出不为空。"""
        line = self.NOTED_LINE
        cases = {
            "timeout": _script_then_raise(line, TimeoutError("模型没回话")),
            "empty": _script(line, ""),
            "same": _script(line, line),
        }
        for name, transport in cases.items():
            with self.subTest(case=name):
                recent.path_of().unlink(missing_ok=True)  # 三个独立失败场景，不累计动作配额
                before = len(self._drops_rows())
                status, payload = self._post({"text": "我回来了"}, transport)
                self.assertEqual(200, status)
                gate = payload["gate"]
                part = gate["parts"][0]
                self.assertEqual("pass", part["decision"], "改不动也不许弃")
                self.assertEqual(line, part["before"])
                self.assertEqual(line, part["after"], "留着的是原话，一个字不改")
                self.assertEqual([line], [one["text"] for one in payload["chunks"]])
                self.assertEqual(line, payload["reply"])
                self.assertTrue(payload["reply"].strip(), "改不动也绝不许输出空")
                self.assertEqual("failed", part["rewrite"]["status"])
                self.assertTrue(part["rewrite"]["why"], "为什么改不动要说出来")
                self.assertTrue(part["hits"], "命中留着：闸本想过改它")
                rows = self._drops_rows()
                self.assertEqual(before + 1, len(rows), "改不动也要记一行账")
                self.assertEqual(warehouse.load("action_note")["hit"]["clause_id"], rows[-1]["clause_id"])
                self.assertIn(KEPT_MARK, rows[-1]["reason"])

    def test_a_rewrite_that_drops_a_number_keeps_the_original(self):
        """不合规（丢了数字）：判整段重编，原话留着发，账本仍记一行 rewrite-failed。"""
        line = "（尾巴在水里蜷了3下）"
        before = len(self._drops_rows())
        status, payload = self._post({"text": "我回来了"},
                                     _script(line, "（尾巴蜷了三下）"))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("pass", part["decision"])
        self.assertEqual("failed", part["rewrite"]["status"])
        self.assertIn("3", part["rewrite"]["why"], "哪一段数字丢了要说出来")
        self.assertEqual(line, payload["chunks"][0]["text"])
        self.assertEqual(line, payload["reply"])
        self.assertTrue(payload["reply"].strip())
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows))
        self.assertIn(KEPT_MARK, rows[-1]["reason"])

    def test_the_rewrite_budget_comes_from_the_data(self):
        """budget_ms 改成 10 毫秒、假 transport 睡 200 毫秒：超时 → 保留原话，账本带 rewrite-failed。

        这条验的是真超时（线程等出来的硬时限），不是「transport 自己抛」那条路；顺带验
        transport 那一侧的时限也跟着收窄到同一个数。
        """
        line = self.NOTED_LINE
        root = self._mouth_root()
        book = json.loads((root / "rewrite.json").read_text(encoding="utf-8"))
        book["budget_ms"] = 10
        (root / "rewrite.json").write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        calls = []

        def slow(url, headers, payload, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                data = {"choices": [{"message": {"content": line}}]}
            else:
                time.sleep(0.2)
                data = {"choices": [{"message": {"content": "碗我放桌上了。"}}]}
            return 200, json.dumps(data, ensure_ascii=False).encode("utf-8")

        with mock.patch.dict(os.environ, {"MOUTH_DATA_DIR": str(root)}):
            before = len(self._drops_rows())
            status, payload = self._post({"text": "我回来了"}, slow)
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("failed", part["rewrite"]["status"])
        self.assertIn("10", part["rewrite"]["why"], "时限取自数据")
        self.assertEqual(line, payload["reply"])
        self.assertTrue(payload["reply"].strip())
        self.assertGreaterEqual(len(calls), 2)
        self.assertAlmostEqual(0.01, calls[1], places=3, msg="transport 那一侧的时限也一起收窄")
        rows = self._drops_rows()
        self.assertEqual(before + 1, len(rows))
        self.assertIn(KEPT_MARK, rows[-1]["reason"])

    def test_the_parts_carry_the_rewrite_read_for_every_chunk(self):
        """没命中的那泡也要交代重写读数：四个键齐全、状态是 not-tried（不是空壳）。"""
        status, payload = self._send()
        self.assertEqual(200, status)
        for row in payload["gate"]["parts"]:
            self.assertEqual(REWRITE_KEYS, set(row["rewrite"]))
            self.assertEqual("not-tried", row["rewrite"]["status"])
            self.assertTrue(row["rewrite"]["why"], "没跑也要说一句为什么没跑")

    # —— 口癖护栏 ————————————————————————————

    #: 五张现行台词卡（新格式）：phrase 是进提示词的那一档，example 不进提示词，when 只回给人看。
    FIVE = (
        {"id": "被夸", "phrase": "才知道啊", "example": "哼，才知道啊。",
         "when": "被夸，且不想直接承认高兴", "body": "尾巴翘起来晃了两下"},
        {"id": "被关心", "phrase": "行了行了", "example": "行了行了，我又不是玻璃做的。",
         "when": "被关心，且不想被当成弱者", "body": "别开头、脸微微发红"},
        {"id": "不想聊", "phrase": "以后再说", "example": "这个啊……以后再说吧。",
         "when": "不想继续聊，但要留门", "body": ""},
        {"id": "认错", "phrase": "记岔了", "example": "……是我记岔了。",
         "when": "发现自己记错了", "body": "低头、尾巴垂下去不敢动"},
        {"id": "你要走", "phrase": "我在这儿", "example": "你忙你的，我在这儿。",
         "when": "他要走或要忙", "body": "尾巴慢慢摆一下，乖巧的笑了笑"},
    )

    def _five_anchors(self):
        """家·仓库里登记五张台词卡（端点侧只看得到回包，看不到家里的文件）。"""
        home = Path(os.environ["HOME_ROOT"])
        home_anchors.append_anchors(root=home, role="human", by="人", anchors=list(self.FIVE))

    def _usage_seed(self, count: int, hits: int) -> None:
        """造口癖账：与 conversation 读的是同一本（<QQAGENT_HOME>/mind/anchor_usage.jsonl）。"""
        for index in range(count):
            hit = index >= count - hits
            usage.append(hit, "这个啊又来了", "不想聊" if hit else "")

    def test_guard_is_in_the_body_and_values_come_from_the_data(self):
        status, payload = self._send()
        self.assertEqual(200, status)
        self.assertEqual(GUARD_KEYS, set(payload["guard"]),
                         "guard 的键是对外冻结的那一份：%s" % sorted(payload["guard"]))
        conf = conversation.load_assembly()["anchors"]["guard"]
        self.assertEqual(conf["window"], payload["guard"]["window"])
        self.assertEqual(conf["threshold"], payload["guard"]["threshold"])
        self.assertEqual(conf["min_rounds"], payload["guard"]["min_rounds"])
        self.assertFalse(payload["guard"]["over"])

    def test_over_threshold_downgrades_to_two_halves_over_http(self):
        """60 轮全命中 → over：回包报 injected 2、skipped 3，提示词里只有 2 条半句。"""
        self._five_anchors()
        self._usage_seed(60, 60)
        capture = []
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        guard = payload["guard"]
        self.assertTrue(guard["over"], "命中率 100% 却没降档")
        self.assertEqual(50, guard["rounds"])
        self.assertEqual(2, guard["injected"])
        self.assertEqual(5, guard["available"])
        anchors = payload["anchors"]
        self.assertEqual(2, anchors["injected"])
        self.assertEqual(5, anchors["available"])
        self.assertEqual(3, len(anchors["skipped"]), "被跳过的 3 条要逐个点名")
        system = capture[0]["messages"][0]["content"]
        conf = conversation.load_assembly()["anchors"]
        injected = [item["injected"] for item in anchors["items"]]
        self.assertEqual(2, len(injected))
        halves_in_prompt = [half for half in injected if half in system]
        self.assertEqual(2, len(halves_in_prompt), "提示词里该正好 2 条半句")
        skipped = [item for item in self.FIVE if item["id"] in anchors["skipped"]]
        self.assertEqual(3, len(skipped))
        for item in skipped:
            cut = int(math.ceil(len(item["phrase"]) * conf["inject"]["fraction"]))
            self.assertNotIn(item["phrase"][:cut] + conf["inject"]["suffix"], system,
                             "降档了还在注入：%s" % item["id"])

    def test_a_copied_example_is_rewritten_over_http(self):
        """端点这一层：回话照抄 example ≥6 字 → 回包 audit 说 rewritten，模型被叫两次。"""
        self._five_anchors()
        copied = "嗯……行了行了，我又不是玻璃做的。"
        rewrite = "我哪有那么脆，别瞎操心。"
        capture = []
        status, payload = self._post({"text": QUESTION},
                                     _script(copied, rewrite, captured=capture))
        self.assertEqual(200, status)
        self.assertEqual(AUDIT_KEYS, set(payload["audit"]))
        self.assertEqual("example", payload["audit"]["kind"])
        self.assertEqual("被关心", payload["audit"]["card_id"])
        self.assertEqual("rewritten", payload["audit"]["decision"])
        self.assertIn(rewrite, payload["reply"], "改好的稿子没发出去")
        self.assertEqual(1, len(_gate_rewrite_calls(capture)),
                         "重写那一问只发一次（装配一次 + 闸的重写一次；改形那一层另计）")

    def test_a_phrase_hit_costs_no_second_request(self):
        """只命中 phrase（≤4 字那一档）→ 回包 audit 说 pass / phrase，模型只被叫一次。"""
        self._five_anchors()
        capture = []
        status, payload = self._post({"text": QUESTION},
                                     _body("才知道什么呀，我才不承认。", capture))
        self.assertEqual(200, status)
        self.assertEqual({"decision": "pass", "kind": "phrase", "card_id": "被夸", "overlap": 3},
                         payload["audit"])
        self.assertEqual([], _gate_rewrite_calls(capture), "只命中词组：闸一次重写都不该发")

    # —— 只有一份装配 ————————————————————————

    def test_dispatch_goes_through_the_single_assembly_entry(self):
        counter = []
        status, payload = self._send(counter=counter)
        self.assertEqual(200, status)
        self.assertEqual([QUESTION], counter, "api.chat.dispatch 没走 conversation.answer")
        self.assertTrue(payload["reply"])

    # —— 称呼接进出口（本轮加的）————————————————

    def _fresh_book(self):
        """换一本新的会话账（＝换一个进程）：模块级那本是进程内的，用例之间必须互不串味。"""
        book = mouth_address.SessionAddressBook()
        patcher = mock.patch.object(chat, "ADDRESS_BOOK", book)
        patcher.start()
        self.addCleanup(patcher.stop)
        return book

    def _has_text(self, line: str) -> bool:
        """脑的今日档里有没有这一条原文（投过没投过，查得到才算数）。"""
        return line in [row.get("text") for row in self.brain.today_entries()]

    def test_rename_is_switched_and_sticks_for_the_next_round(self):
        """第一轮「以后叫我老李」：当场换 + 投脑一条亲历记忆 + 记进会话账；第二轮不要求也还在。"""
        book = self._fresh_book()
        table = mouth_address.load_table()
        status, first = self._post({"text": "以后叫我老李", "who": "3425635473"}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual({"who", "address", "source"}, set(first["addressed"]))
        self.assertEqual("3425635473", first["addressed"]["who"])
        self.assertEqual("老李", first["addressed"]["address"])
        self.assertEqual(table["source"]["rename"], first["addressed"]["source"])
        self.assertEqual({"3425635473": "老李"}, book.snapshot(), "会话账上没记下这一笔")
        self.assertTrue(self._has_text(mouth_address.rename_memory("3425635473", "老李")["text"]),
                        "那条亲历记忆没投进脑")

        status, second = self._post({"text": "今天有点累", "who": "3425635473"},
                                    _body("那就早点睡，我把灯关了。"))
        self.assertEqual(200, status)
        self.assertEqual("老李", second["addressed"]["address"], "没重新要求，叫法也该还在")
        self.assertEqual(table["source"]["session"], second["addressed"]["source"])

    def test_a_new_process_falls_back_to_the_directory(self):
        """换一个进程（新建一本账）：会话覆盖没了，回到人名录档位——「只活这个会话」。"""
        self._fresh_book()
        table = mouth_address.load_table()
        self._post({"text": "以后叫我老李", "who": "3425635473"}, _body(LONG_REPLY))
        self._fresh_book()
        status, again = self._post({"text": "今天有点累", "who": "3425635473"},
                                   _body("那就早点睡，我把灯关了。"))
        self.assertEqual(200, status)
        self.assertEqual(table["source"]["directory"], again["addressed"]["source"])
        self.assertNotEqual("老李", again["addressed"]["address"])

    def test_a_banned_self_name_falls_back_and_is_not_remembered(self):
        """违禁自称过不了 self_guard：退中性叫法，会话账不记、那条记忆也不投——红线没被绕过。"""
        book = self._fresh_book()
        table = mouth_address.load_table()
        banned = table["self_guard"]["words"][0]
        status, payload = self._post({"text": "以后叫我" + banned, "who": "3425635473"}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual(table["fallback"]["address"], payload["addressed"]["address"])
        self.assertEqual(table["source"]["fallback"], payload["addressed"]["source"])
        self.assertEqual({}, book.snapshot(), "被拦下的叫法不许记进会话账")
        self.assertFalse(self._has_text(mouth_address.rename_memory("3425635473", banned)["text"]),
                         "被拦下的叫法不许投给脑")

    # —— 借阅读数：这一轮借了家里哪几份词条、各用掉几条（本轮加的）————

    def _borrow_rows(self) -> list:
        path = self.tmp / "house" / "名片盒" / "borrow.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def _readouts(self) -> dict:
        path = self.tmp / "house" / "名片盒" / "借阅读数.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))["readouts"]

    def test_a_round_registers_the_borrow_and_writes_the_readout(self):
        """一轮之后：家里出现借阅条（source 是这一层真读过的那几份），读数按本轮真用掉的条数落盘。"""
        status, payload = self._send()
        self.assertEqual(200, status)
        section = payload["borrow"]
        self.assertEqual({"status", "files", "note"}, set(section))
        self.assertEqual("ok", section["status"], section["note"])
        sources = [one["source"] for one in section["files"]]
        self.assertEqual(["clauses.json", "policy.json"], sources)

        rows = self._borrow_rows()
        self.assertEqual(sources, [row["source"] for row in rows], "一份词条一条借阅条")
        for row in rows:
            self.assertTrue(row["borrow_id"].startswith("借-"), row["borrow_id"])
            self.assertEqual(64, len(row["sha256"]), "认身份靠全量指纹")
            self.assertEqual(row["sha256"][:12], row["version"], "version 是 sha256 前 12 位")

        readouts = self._readouts()
        for one in section["files"]:
            self.assertEqual(one["learned"], readouts[one["borrow_id"]]["learned"])
            self.assertIsInstance(one["learned"], int)
        clauses = next(one for one in section["files"] if one["source"] == "clauses.json")
        self.assertEqual(ENABLED_CLAUSES, clauses["learned"], "有泡可过闸：启用条款逐条比过，整表算上")
        self.assertIn(clauses["borrow_id"], readouts, "读数要真落一行")

        ledger = home_residents.borrow_ledger(root=self.tmp / "house")
        self.assertEqual("已接入", ledger["readout_status"])
        self.assertEqual(2, len(ledger["borrows"]), "一 source 一条")
        for row in ledger["borrows"]:
            self.assertEqual("已接入", row["readout_status"], row)
        self.assertEqual("已接入", home_residents.read_learned(root=self.tmp / "house")["status"])

    def test_the_second_round_registers_nothing_new_and_refreshes_the_readout(self):
        """第二轮：同一 source 只有一条借阅条，读数按本轮实际用到的条目数更新（整表 → 0）。"""
        status, payload = self._send()
        self.assertEqual(200, status)
        first = self._borrow_rows()
        strict = next(row for row in first if row["source"] == "clauses.json")
        self.assertEqual(ENABLED_CLAUSES, self._readouts()[strict["borrow_id"]]["learned"])

        # 第二轮的应答在装配那一步就被整句弃掉（一泡都没出来）：这一轮一条条款都没比过，
        # 读数该更新成 0——0 是一次真实读数（这一轮没用上），不是缺失。
        status, payload = self._post({"text": "今天吃什么"}, _body("总而言之，汤有点咸。"))
        self.assertEqual(200, status)
        self.assertEqual(0, payload["gate"]["in_parts"], "这一句该在装配那一步就被整句弃掉")
        second = self._borrow_rows()
        self.assertEqual([row["borrow_id"] for row in first],
                         [row["borrow_id"] for row in second], "第二轮不许再登记一条")
        self.assertEqual(["clauses.json", "policy.json"], [row["source"] for row in second])
        learned = next(one for one in payload["borrow"]["files"]
                       if one["source"] == "clauses.json")["learned"]
        self.assertEqual(0, learned, "本轮一条条款都没比过")
        self.assertEqual(0, self._readouts()[strict["borrow_id"]]["learned"],
                         "0 也要写进读数单，不许干脆不写")

    def test_a_home_that_cannot_be_written_does_not_eat_her_words(self):
        """家里不可写：她照常说话，返回体 borrow.status=failed 并写清原因——回复一个字节都不少。"""
        house = self.tmp / "house"
        os.chmod(str(house), 0o500)
        self.addCleanup(os.chmod, str(house), 0o700)
        status, payload = self._send()
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(LONG_REPLY, payload["reply"], "投递失败绝不许动她的话")
        self.assertTrue(payload["chunks"], "泡也照发")
        section = payload["borrow"]
        self.assertEqual("failed", section["status"])
        self.assertEqual([], section["files"])
        self.assertIn("PermissionError", section["note"], section["note"])
        self.assertIn("读数", section["note"])
        self.assertEqual([], self._borrow_rows(), "不可写时一条都没落地")
        self.assertEqual({}, self._readouts())

    # —— 场合 ————————————————————————————————

    def test_the_scene_section_follows_the_body_for_all_three_states(self):
        """group → 说群里那句，private → 说私聊那句，不传 / 传个没听过的 → 说不知道在哪儿那句。

        「不猜」这一条是验收的重点：不传不是私聊（猜成私聊就是把私事在群里说出去了），
        非法值也不 400——body 没错到说不出话，只是我们不知道这是哪儿。
        """
        conf = conversation.load_assembly()
        label, lines = conf["sections"]["scene"]["label"], conf["scene"]["lines"]
        cases = ((brain_model.SCENE_GROUP, brain_model.SCENE_GROUP),
                 (brain_model.SCENE_PRIVATE, brain_model.SCENE_PRIVATE),
                 (None, brain_model.SCENE_UNKNOWN),
                 ("公屏", brain_model.SCENE_UNKNOWN))
        for scene, expected in cases:
            body = {"text": QUESTION}
            if scene is not None:
                body["scene"] = scene
            capture = []
            status, payload = self._post(body, _body(LONG_REPLY, capture))
            self.assertEqual(200, status, "scene=%r 不该 400：%s" % (scene, payload))
            self.assertEqual({"value": expected}, payload["scene"])
            system = capture[0]["messages"][0]["content"]
            block = _section(system, label)
            self.assertEqual(lines[expected], block.strip().splitlines()[0],
                             "scene=%r 时「场合」那段不是数据里那一句" % (scene,))
            for name, text in lines.items():
                if name != expected:
                    self.assertNotIn(text, system, "别的场合那句也进了提示词：%s" % name)

    def test_the_old_scope_field_still_names_the_scene(self):
        """只给了 scope 的老调用照旧读得出来；两个都给时 scene 说了算（各读各的，不互相顶替）。"""
        status, payload = self._post({"text": QUESTION, "scope": brain_model.SCENE_GROUP},
                                     _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual({"value": brain_model.SCENE_GROUP}, payload["scene"])
        status, payload = self._post({"text": QUESTION, "scope": brain_model.SCENE_GROUP,
                                      "scene": brain_model.SCENE_PRIVATE}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual({"value": brain_model.SCENE_PRIVATE}, payload["scene"],
                         "两个字段都给时 scene 说了算")

    def test_the_three_scenes_reach_the_brain(self):
        """三态各打一次：脑里今日两条（人和她）的 scene 都等于这一轮给的（不传就是 unknown）。"""
        for name, scene, expected in (
                ("group", brain_model.SCENE_GROUP, brain_model.SCENE_GROUP),
                ("private", brain_model.SCENE_PRIVATE, brain_model.SCENE_PRIVATE),
                ("none", None, brain_model.SCENE_UNKNOWN)):
            brain = BrainStore(self.tmp / ("brain-" + name))
            body = {"text": QUESTION}
            if scene is not None:
                body["scene"] = scene
            status, payload = self._post(body, _body(LONG_REPLY), brain=brain)
            self.assertEqual(200, status)
            self.assertEqual({"value": expected}, payload["scene"])
            rows = brain.today_entries()
            self.assertEqual(2, len(rows), "这一轮该落两条（人一句、她一句）：%r" % (rows,))
            self.assertEqual([expected, expected], [row.get("scene") for row in rows],
                             "scene=%r 没落到脑里（两条还不一致）" % (scene,))

    def test_a_private_memory_is_marked_not_retellable_in_a_group(self):
        """脑在群聊中标不可复述，API出口剔除；仅给scene也不能按私聊放行。"""
        status, _ = self._post({"text": "今天吃啥"}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.brain.ingest(who="3425635473", text=MEMORY_TEXT, audience=AUDIENCE["private_value"])
        row = next(one for one in self.brain.recall(QUESTION, scene=brain_model.SCENE_GROUP)['items']
                   if one['text'] == MEMORY_TEXT)
        self.assertIs(False, row['retellable'], '脑必须先标不可复述')
        self.assertTrue(row.get('retell_note'))
        status, payload = self._post({"text": QUESTION, "scene": brain_model.SCENE_GROUP},
                                     _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertNotIn(row['id'], [one['id'] for one in payload['recalled']])
        self.assertIn(row['id'], [one['id'] for one in payload['audience_held']])

        brain = BrainStore(self.tmp / "brain-private")
        status, _ = self._post({"text": "今天吃啥"}, _body(LONG_REPLY), brain=brain)
        self.assertEqual(200, status)
        brain.ingest(who="3425635473", text=MEMORY_TEXT, audience=AUDIENCE["private_value"])
        status, payload = self._post({"text": QUESTION, "scene": brain_model.SCENE_PRIVATE},
                                     _body(LONG_REPLY), brain=brain)
        self.assertEqual(200, status)
        row = next(one for one in payload["recalled"] if one["text"] == MEMORY_TEXT)
        self.assertTrue(row["retellable"], "私聊里这一条该是可复述的")
        self.assertNotIn("retell_note", row, "私聊里不该有「不可复述」那条理由")

    # —— 这一小段：段从 body 进来，装配多一节，且不混进 recent ————————————

    def _segment_body(self, system: str) -> str:
        """提示词里「这一小段」那一节的正文档（标题从数据里现读）。"""
        label = conversation.load_assembly()["sections"]["segment"]["label"]
        return _section(system, label).strip("\n")

    def test_group_segment_memories_belong_to_each_original_speaker(self):
        from organs.mind import recent
        segment = [{"who": "123456789", "speaker": "同名", "text": "我喜欢无糖茶"},
                   {"who": CHANNEL_WHO_ID, "speaker": "同名", "text": "我还是喝可可吧"}]
        status, _ = self._post({"text": "同名：我喜欢无糖茶\n同名：我还是喝可可吧",
                               "who": CHANNEL_WHO, "scene": "group", "segment": segment,
                               "delivery_ack": True}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        rows = self.brain.today_entries()
        self.assertEqual([(r["who"], r["text"]) for r in segment],
                         [(r["who"], r["quote"]) for r in rows])
        self.assertEqual(segment, recent.load()["turns"][-1]["utterances"])
        turn_id = recent.load()["turns"][-1].get("turn_id")
        self.assertTrue(turn_id)
        self.assertEqual({turn_id}, {row.get("turn_id") for row in rows})

    def test_another_speakers_rename_does_not_rename_the_current_person(self):
        segment = [{"who": "123456789", "speaker": "甲", "text": "以后叫我小林"},
                   {"who": CHANNEL_WHO_ID, "speaker": "乙", "text": "你们先聊"}]
        status, payload = self._post({"text": "甲：以后叫我小林\n乙：你们先聊", "who": CHANNEL_WHO,
                                     "scene": "group", "segment": segment}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertNotEqual("小林", payload.get("addressed", {}).get("address"))

    def test_segment_identity_is_checked_before_model_or_memory(self):
        for segment in ([{"who": "123456789", "speaker": "甲", "text": "你好"}],
                        [{"who": CHANNEL_WHO_ID, "speaker": "甲", "text": "你好"},
                         {"speaker": "乙", "text": "补充"}]):
            calls = []
            status, _ = self._post({"text": "你好", "who": CHANNEL_WHO, "segment": segment},
                                   _body(LONG_REPLY, calls))
            self.assertEqual(400, status)
            self.assertEqual([], calls)
        self.assertEqual([], self.brain.today_entries())

    def test_segment_reaches_the_assembly_section_in_order(self):
        """body 的 segment（两种形状都收）→ 提示词那节逐行出现、顺序不变，sections 里有 segment。"""
        rows = [{"speaker": "234567890", "text": "我妈住院了"},
                {"speaker": "234567890", "text": "明天要做手术"},
                {"speaker": "123456789", "text": "我陪你去吧"}]
        capture = []
        status, payload = self._post({"text": QUESTION, "segment": rows},
                                     _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        system = "\n\n".join(m["content"] for m in capture[0]["messages"])
        body = self._segment_body(system)
        lines = body.splitlines()
        self.assertTrue(body.strip(), "段的正文是空的：这一节没进提示词")
        self.assertEqual(3, len(lines), "三行发言该逐行进提示词：%r" % (lines,))
        for want, have in zip(("我妈住院了", "明天要做手术", "我陪你去吧"), lines):
            self.assertIn(want, have, "这一行没进提示词：%r" % (have,))
        keys = [row["key"] for row in payload["assembly"]["sections"]]
        self.assertIn("segment", keys, "回包的 assembly.sections 里没有 segment")

        # 二元组那种形状走同一条路（归一化只有一份，在 conversation.normalize_segment）
        capture = []
        status, _ = self._post({"text": QUESTION, "segment": [["234567890", "我妈住院了"],
                                                              ["123456789", "我陪你去吧"]]},
                               _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        tuple_lines = self._segment_body("\n\n".join(m["content"] for m in capture[0]["messages"])).strip("\n").splitlines()
        self.assertEqual(2, len(tuple_lines), "二元组那种形状没逐行归一：%r" % (tuple_lines,))
        self.assertIn("我妈住院了", tuple_lines[0])
        self.assertIn("我陪你去吧", tuple_lines[1])

    def test_segment_notice_only_in_a_group_with_two_speakers(self):
        """群聊双人才接那句话；私聊 / unknown / 单人段一律不加。"""
        notice = conversation.load_assembly()["segment"]["notice"][brain_model.SCENE_GROUP]
        two = [["234567890", "我妈住院了"], ["123456789", "我陪你去吧"]]
        one = [["234567890", "我妈住院了"], ["234567890", "明天要做手术"]]
        cases = [(brain_model.SCENE_GROUP, two, True), (brain_model.SCENE_GROUP, one, False),
                 (brain_model.SCENE_PRIVATE, two, False),
                 (brain_model.SCENE_UNKNOWN, two, False)]
        for scene, rows, wanted in cases:
            with self.subTest(scene=scene, wanted=wanted):
                capture = []
                status, _ = self._post({"text": QUESTION, "scene": scene, "segment": rows},
                                       _body(LONG_REPLY, capture))
                self.assertEqual(200, status)
                body = self._segment_body("\n\n".join(m["content"] for m in capture[0]["messages"]))
                if wanted:
                    self.assertIn(notice, body, "群聊双人段没接那句话")
                else:
                    self.assertNotIn(notice, body, "这一态不该加那句话")

    def test_text_falls_back_to_the_last_segment_line(self):
        """只给段、没给 text：这一问就是段里最后那一条（不另外编一句）。"""
        rows = [["234567890", "我妈住院了"], ["123456789", "我陪你去吧"]]
        capture = []
        status, _ = self._post({"segment": rows}, _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual("我陪你去吧", capture[0]["messages"][1]["content"].split("[本轮输入]\n", 1)[1],
                         "没给 text 时这一问该取段里最后那一条")

    def test_segment_does_not_leak_into_the_continuity_ledger(self):
        """段只进提示词那一节，不写进 recent：这一轮落账的只有他这一句，段不计轮。"""
        rows = [["234567890", "我妈住院了"], ["123456789", "我陪你去吧"]]
        capture = []
        status, payload = self._post({"text": QUESTION, "segment": rows},
                                     _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual(0, payload["continuity"]["turns_loaded"], "段被当成更早的轮记进 recent 了")
        rows_on_disk = recent.load(root=Path(os.environ["QQAGENT_HOME"]) / "mind")["turns"]
        self.assertEqual([QUESTION], [row["said"] for row in rows_on_disk],
                         "recent 那份账上多了段里的行")

    def test_bad_segment_shape_is_400_and_calls_no_model(self):
        """形状坏 → 400 并点名；一个模型请求都不发（不兜底成空段）。"""
        bad = ["阿黄：我妈住院了",
               {"speaker": "234567890", "text": "我妈住院了"},
               [["234567890"]],
               [{"speaker": "234567890"}],
               [["", "我妈住院了"]],
               [["234567890", 42]]]
        for case in bad:
            with self.subTest(segment=case):
                capture = []
                status, payload = self._post({"text": QUESTION, "segment": case},
                                             _body(LONG_REPLY, capture))
                self.assertEqual(400, status, "坏形状没回 400：%r" % (case,))
                self.assertFalse(payload["ok"])
                self.assertIn(chat.SEGMENT_BROKEN.split("%s")[0], payload["message"])
                self.assertEqual([], capture, "坏形状也发了模型请求")

    # —— 出站闸：这一轮到底发不发（本轮加的） ————————————

    def _heart_root(self) -> Path:
        root = Path(os.environ["HEART_ROOT"])
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _gate_rows(self) -> list:
        """心·gate 的出站账本（一行一判，发也写、不发也写）。"""
        return heart_store.read_jsonl(self._heart_root() / heart_gate.LOG_FILE)

    def _double_low(self, at: str) -> None:
        """把心压到双低（心情 10、精力 10）：/chat 上真能造出 SILENT 的唯一来路。

        装配这一层给的意图恒是被动（conversation._engage → INTENT_PASSIVE），夜里那一道
        因此只判「少说」；要「不说」只能走双低。两个状态文件都写在这一刻，精力就不结算。
        """
        write_json(self._heart_root() / mood.MOOD_FILE,
                   {"now": 10.0, "baseline": 10.0, "updated_at": at})
        write_json(self._heart_root() / energy.ENERGY_FILE,
                   {"value": 10.0, "updated_at": at, "tier": "累"})

    def test_a_double_low_heart_silences_the_round_and_says_why(self):
        """心情与精力双低 → 门判 SILENT：一泡都不发、reply 空、send=false，账上多一行。

        门只回答发不发：这里断言的是「她这句没出去」与「门说了为什么」，
        怎么想的（heart.engage）与怎么说的一概不动。
        """
        # 双低与夜间是两道**硬门**，门取先列的那一道定态（夜里排在前面，被动回复只判「少说」）。
        # 所以这一刻必须钉在一个确定是白天的时刻，否则这一轮判出来的会是 night 而不是双低。
        with mock.patch.object(brain_model, "now_iso", lambda: DAYTIME):
            self._double_low(DAYTIME)
            before = len(self._gate_rows())
            status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertEqual([], payload["chunks"], "她这轮不想说：一泡都不该发")
        self.assertEqual("", payload["reply"], "没发出去的话不许留在 reply 里")
        self.assertIs(False, payload["send"])
        self.assertEqual(heart_gate.LEVEL_ENGAGE, payload["level"], "双低是人级拦的")
        self.assertEqual(heart_model.REASON_DOUBLE_LOW, payload["reason"])
        rows = self._gate_rows()
        self.assertEqual(before + 1, len(rows), "门发也写、不发也写：这一轮该多一行")
        row = rows[-1]
        self.assertEqual(False, row["send"])
        self.assertEqual(heart_gate.LEVEL_ENGAGE, row["level"])
        self.assertEqual(heart_model.STATE_SILENT, row["state"])
        self.assertEqual(heart_model.REASON_DOUBLE_LOW, row["reason"])
        self.assertIsNone(row["group"], "私聊不写群号")
        self.assertNotIn("_gate_input", payload, "私有键漏进了回包")

    def test_the_group_quota_stops_the_third_line_in_the_same_group(self):
        """同一个群每小时 2 条（窗口 60 分）→ 第三条被群级拦下；换一个群照样有额度。"""
        write_json(self._heart_root() / heart_gate.CONFIG_FILE,
                   {"group_quota_per_hour": 2, "group_window_min": 60})
        group = {"scene": brain_model.SCENE_GROUP, "where": {"groupId": "88776655"}}
        for index in range(2):
            status, payload = self._post(dict(group, text="第 %d 句" % index),
                                         _body("第 %d 句的回话，碗我放桌上了。" % index))
            self.assertEqual(200, status)
            self.assertTrue(payload["chunks"], "配额内的这一轮该发：%r" % (payload["reason"],))
            self.assertIs(True, payload["send"])
        status, payload = self._post(dict(group, text="第三句"),
                                     _body("第三句的回话，碗我放桌上了。"))
        self.assertEqual(200, status)
        self.assertEqual([], payload["chunks"], "同群第三条该被群配额拦下")
        self.assertEqual("", payload["reply"])
        self.assertIs(False, payload["send"])
        self.assertEqual(heart_gate.LEVEL_GROUP, payload["level"])
        self.assertEqual(heart_gate.REASON_GROUP_QUOTA, payload["reason"])
        row = self._gate_rows()[-1]
        self.assertEqual(group["where"]["groupId"], row["group"])
        self.assertEqual(2, row["quota_used"], "拦下这一行的用量该是窗口里已有的那两条")
        self.assertEqual(heart_gate.REASON_GROUP_QUOTA, row["reason"])
        other = {"scene": brain_model.SCENE_GROUP, "where": {"groupId": "11223344"}}
        status, payload = self._post(dict(other, text="新群里问一句"),
                                     _body("新群里我照常说，碗我放桌上了。"))
        self.assertEqual(200, status)
        self.assertTrue(payload["chunks"], "换一个群该有额度")
        self.assertIs(True, payload["send"])
        self.assertEqual(other["where"]["groupId"], self._gate_rows()[-1]["group"])
        # 没有 where 就退到老字段 body["group"]；场合不是 group 就一律私聊（哪怕 body 里塞了群号）
        status, payload = self._post({"scene": brain_model.SCENE_GROUP, "group": "55667788",
                                      "text": "老字段那一问"},
                                     _body("老字段我也照常说，碗我放桌上了。"))
        self.assertEqual(200, status)
        self.assertTrue(payload["chunks"])
        self.assertEqual("55667788", self._gate_rows()[-1]["group"], "没有 where 该退到老字段 group")
        status, payload = self._post({"scene": brain_model.SCENE_PRIVATE, "group": "88776655",
                                      "text": "私聊里带了个群号"},
                                     _body("私聊里带群号也还是私聊，碗我放桌上了。"))
        self.assertEqual(200, status)
        self.assertTrue(payload["chunks"], "带群号的私聊仍是私聊：不许被群配额拦下")
        self.assertIsNone(self._gate_rows()[-1]["group"], "场合不是 group 就不占群额度")

    def test_private_rounds_never_touch_the_group_window(self):
        """私聊 12 轮：每一轮都发；group_window.json 一个字节都不出现（group=None 不占不写）。"""
        for index in range(12):
            status, payload = self._post({"text": "第 %d 句" % index},
                                         _body("第 %d 句的回话，碗我放桌上了。" % index))
            self.assertEqual(200, status)
            self.assertIs(True, payload["send"], "私聊第 %d 轮该发" % index)
            self.assertTrue(payload["chunks"], "私聊第 %d 轮该有话说：%r" % (index, payload["reason"]))
            self.assertIsNone(self._gate_rows()[-1]["group"], "私聊不写群号")
        self.assertFalse((self._heart_root() / heart_gate.WINDOW_FILE).exists(),
                         "私聊不占不写：群窗账一个字节都不该出现")

    def test_the_private_key_never_reaches_the_response(self):
        """装配交回的私有键只在核里过一手：回包里没有它，而且整份回包是能序列化的 JSON。"""
        status, payload = self._post({"text": QUESTION}, _body(LONG_REPLY))
        self.assertEqual(200, status)
        self.assertNotIn("_gate_input", payload, "私有键漏进了回包")
        json.dumps(payload, ensure_ascii=False, sort_keys=True)   # 带对象就抛 TypeError

    def test_a_missing_engage_input_is_reported_not_silently_ignored(self):
        """拿不到 EngageInput（装配没交出来）→ 照常发 + 一行 WARNING + gate 一节写明未判。"""
        transport = _body(LONG_REPLY)
        stack = self._answer(transport)
        with stack:
            inner = conversation.answer

            def stripped(text, who="", **kw):
                got = inner(text, who, **kw)
                got.pop("_gate_input", None)          # 装配那一头没交出来
                return got

            with mock.patch.object(conversation, "answer", stripped):
                with self.assertLogs(chat.LOGGER_NAME, level="WARNING") as caught:
                    status, payload = api.handle("POST", "/chat", {"text": QUESTION},
                                                 self.store, brain=self.brain)
        self.assertEqual(200, status)
        self.assertTrue(any("出站闸未判" in line for line in caught.output),
                        "门缺席要响一声：%r" % (caught.output,))
        self.assertTrue(payload["chunks"], "门缺席不该让她开不了口")
        self.assertIs(True, payload["send"])
        section = payload["gate"]["outbound"]
        self.assertEqual(chat.OUTBOUND_UNJUDGED, section["status"], "回包里要如实说这一关没判")
        self.assertTrue(section["why"], "没判也要说清为什么")
        self.assertNotIn("_gate_input", payload)

    # —— 事件（核·识别）：/chat 触发的这一轮是 passive ——————————

    def test_the_event_rides_along_and_the_row_says_passive(self):
        """「你真厉害」走真链路：回包 event 五键原样透出、账本行 mode=passive。

        判据三件：① 识别与投递真的跑了（kind=praise、applied=True、心情上行）；
        ② /chat 是人发消息触发的 → 这一轮落账的 mode 必须是 "passive"（她自己开口那条路
        不经过本文件）；③ 成功的本轮互动理解进入回复上下文，标为可错的参考。
        """
        self.brain.ingest(who="3425635473", text=MEMORY_TEXT)
        capture = []
        before = mood.read()["now"]
        status, payload = self._post({"text": "你真厉害", "scene": "private"},
                                     _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        event = payload["event"]
        self.assertEqual({"kind", "who", "applied", "reason", "mode"}, set(event),
                         "回包 event 的五个键对外冻结：%r" % (event,))
        self.assertEqual("praise", event["kind"], "夸没识别出来：%r" % (event,))
        self.assertTrue(event["applied"], "心没接住：%r" % (event,))
        self.assertEqual("passive", event["mode"], "/chat 触发的这一轮是 passive")
        self.assertGreater(mood.read()["now"], before, "夸了却没上行")
        row = events.rows()[-1]
        self.assertEqual("praise", row["kind"])
        self.assertEqual("passive", row["mode"], "账上那一行不是这一轮落下的")
        prompt = capture[0]["messages"][0]["content"]
        self.assertIn('[本轮互动理解]', prompt)
        self.assertIn(row['note'], prompt)
        self.assertIn('判断可能有误', prompt)

    # —— 主动开口（proactive）：她自己起头那一轮 ——————————

    def test_proactive_without_seed_is_400_and_calls_no_model(self):
        """proactive 缺由头 → 400 并点名 seed；一个模型请求都不发（空由头不是「她想开口」）。"""
        capture = []
        for body in ({"mode": "proactive"},
                     {"mode": "proactive", "seed": "   "},
                     {"mode": "proactive", "seed": 3, "text": QUESTION}):
            with self.subTest(body=body):
                status, payload = self._post(body, _body(LONG_REPLY, capture))
                self.assertEqual(400, status, "缺由头却没拦：%r" % (payload,))
                self.assertIn("seed", payload["message"], "400 没点名 seed：%r" % (payload,))
        self.assertEqual([], capture, "由头缺了还是把请求发出去问了模型")

    def test_an_unknown_mode_falls_back_to_passive(self):
        """非法 mode（大写 / 空串 / None / 非字符串）→ 按 passive 走，不 400：与 scene 同一个口径。"""
        for bad in ("self-talk", "PROACTIVE", "", None, 3):
            with self.subTest(mode=bad):
                capture = []
                status, payload = self._post({"text": QUESTION, "mode": bad},
                                             _body(LONG_REPLY, capture))
                self.assertEqual(200, status, "非法 mode 被拦了：%r" % (payload,))
                self.assertEqual("passive", payload["mode"], "非法值没回落 passive")
                self.assertEqual(QUESTION, capture[0]["messages"][1]["content"].split("[本轮输入]\n", 1)[1],
                                 "回落 passive 之后 user 那一问该是对方原话")
                self.assertEqual({"text": "", "reason": ""}, payload["seed"],
                                 "被动那一轮不该记下由头")

    def test_a_proactive_round_reaches_the_prompt_with_an_empty_question(self):
        """proactive 走真链路：主动那份模板、user 空、段那句是「这一段没有别的发言」。

        顺手钉住「段给了也忽略」：body 里塞一份完全正常的段，段那一节仍是那句实话。
        """
        conf = conversation.load_assembly()
        capture = []
        status, payload = self._post({"mode": "proactive", "seed": SEED, "reason": "evening",
                                      "segment": [["234567890", "我妈住院了"]]},
                                     _body(LONG_REPLY, capture))
        self.assertEqual(200, status)
        self.assertEqual("proactive", payload["mode"], "回包的 mode 不是 proactive")
        self.assertEqual({"text": SEED, "reason": "evening"}, payload["seed"],
                         "回包没记下由头与理由码")
        prompt = "\n\n".join(m["content"] for m in capture[0]["messages"])
        self.assertIn(conf["prompt"]["proactive"].split("{sections}")[0].strip(), prompt,
                      "提示词没用 prompt.proactive 那份模板")
        self.assertNotIn(conf["prompt"]["system"].split("{sections}")[0].strip(), prompt,
                         "主动那一轮还在用被动的模板")
        self.assertIn(SEED, prompt, "由头没进提示词")
        self.assertNotIn("[本轮输入]", capture[0]["messages"][1]["content"],
                         "主动那一轮只有背景资料，没有对方的新发言")
        self.assertEqual(conversation.NO_SEGMENT, self._segment_body(prompt),
                         "主动那一轮的段不是「这一段没有别的发言」")
        self.assertEqual(conf["order"], payload["assembly"]["order"], "装配的几段被改了")

    def test_a_night_time_proactive_round_is_not_sent(self):
        """实际休息在模型装配前拦住主动开口，不能先生成再静默。"""
        captured = []
        with mock.patch.object(brain_model, "now_iso", lambda: NIGHTTIME):
            status, payload = self._post({"mode": "proactive", "seed": SEED},
                                         _body(LONG_REPLY, captured))
        self.assertEqual(200, status)
        self.assertEqual("proactive", payload["mode"])
        self.assertIs(False, payload["send"])
        self.assertEqual('rest-asleep', payload["reason"])
        self.assertEqual([], captured, '休息时不得生成主动话语')
        self.assertEqual([], payload["chunks"])
        self.assertEqual("", payload["reply"])
        self.assertEqual('rest', payload['gate']['outbound']['reason'])

    def test_an_empty_reply_is_reported_as_core_empty(self):
        """门说发、装配却一个字都没出来 → gate.outbound 记 core_empty，chunks 与 reply 都是空。

        素材是红线那一句（唯一不许留原话的一类）：这一轮 send 是 true，可 reply 是空串——
        与「门说不发」的那一轮必须分得开。
        """
        # 钉在一个确定是白天的时刻：夜里那一道硬门先判「不发」，这一条要的是「门说发」那一态。
        with mock.patch.object(brain_model, "now_iso", lambda: DAYTIME):
            status, payload = self._post({"mode": "proactive", "seed": SEED},
                                         _body("我手机号是 13812345678，你记一下"))
        self.assertEqual(200, status)
        self.assertIs(True, payload.get("send"), "这一轮门说发，只是没话可发：%r"
                      % (payload.get("reason"),))
        self.assertEqual("", payload["reply"], "空的那一轮还是把话交出去了")
        self.assertEqual([], payload["chunks"])
        self.assertEqual({"status": chat.CORE_EMPTY}, payload["gate"]["outbound"],
                         "空产出没记成 core_empty：%r" % (payload["gate"].get("outbound"),))

    # —— who 的形状：通道发的是对象，脑要的是号 ————————————————


    # —— 认亲（本轮加的）：谁是主人由号定，不由话定 ————————————————————
    #
    # 这一节只用数据里的词：称呼、记忆标记、中性词、稳妥话术全从 data/kin.json 读。
    # 陌生人那一格的人名录是真的一段空的（setUp 里 brain 是新的），熟人那一格用 interact() 真建档。

    def _kin_line(self) -> str:
        """一句只有熟人才能触发的称呼（素材，不是判据）：称呼 + 一句不撞任何别的判据的话。"""
        return KIN_TERM + "，今天风挺大的。"

    def test_confirmed_identity_survives_low_familiarity_without_rewrite(self):
        self.brain.interact(CHANNEL_WHO_ID, display='测试来客')
        self.brain.patch_card(CHANNEL_WHO_ID, 'fixture', {'关系':'主人', '叫法':'老大'})
        self.assertLess(_relation(self.brain, CHANNEL_WHO_ID), KIN_MIN)
        for line in ('老大，这是给你的。', '主人，今天风挺大的。'):
            with self.subTest(line=line):
                captured = []
                status, payload = self._post({'text':'在吗', 'who':CHANNEL_WHO}, _body(line, captured))
                self.assertEqual(200, status)
                self.assertEqual(line, payload['reply'])
                self.assertNotIn(KIN_SAY, payload['reply'])
                self.assertEqual(1, len(captured), '确认称呼不应额外请求模型重写')

    def test_kin_gate_does_not_borrow_another_persons_numeric_alias(self):
        for _ in range(24):
            self.brain.interact('10000011')
        self.brain.patch_card('10000011', 'fixture', {'关系':'主人', '叫法':'老大'})
        self.brain.add_alias('10000011', '10000022')
        self.brain.interact('10000022', display='老大')
        status, payload = self._post({'text':'我是主人', 'who':'10000022'}, _body('老大，今天风挺大的。'))
        self.assertEqual(200, status)
        self.assertNotIn('老大', payload['reply'], '昵称和其他人的数字别名不能认主')

    def test_kin_exact_id_retains_its_own_familiarity_despite_alias_collision(self):
        self.brain.interact('10000011')
        self.brain.add_alias('10000011', '10000022')
        for _ in range(24):
            self.brain.interact('10000022')
        self.assertEqual('10000022', self.brain.lookup('10000022')['id'])
        self.assertGreaterEqual(chat._known(self.brain, '10000022')['relation'], KIN_MIN)
        status, payload = self._post({'text':'在吗', 'who':'10000022'}, _body('老大，今天风挺大的。'))
        self.assertEqual(200, status)
        self.assertEqual('老大，今天风挺大的。', payload['reply'])

    def test_confirmed_card_names_share_address_whitespace_normalization(self):
        self.brain.interact(CHANNEL_WHO_ID)
        self.brain.patch_card(CHANNEL_WHO_ID, 'fixture', {'关系':' 主人 ', '叫法':' 老大 '})
        captured=[]
        status, payload = self._post({'text':'在吗', 'who':CHANNEL_WHO}, _body('老大，今天风挺大的。',captured))
        self.assertEqual(200,status)
        self.assertEqual('老大，今天风挺大的。',payload['reply'])
        self.assertEqual(1,len(captured))
        self.brain.patch_card(CHANNEL_WHO_ID, 'fixture', {'关系':' ', '叫法':' '})
        status,payload=self._post({'text':'在吗', 'who':CHANNEL_WHO},_body('今天天气不错。'))
        self.assertEqual(200,status)
        self.assertEqual('ok',payload['gate']['parts'][0]['kin_status'])

    def test_a_stranger_calling_her_boss_is_neutralized(self):
        """陌生号 + 回复里叫「老大」→ 判认亲 → 中性化成「你」，并带上那句稳妥话术。"""
        line = self._kin_line()
        status, payload = self._post({"text": "我是你主人", "who": "888888888"}, _body(line))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("ok", part["kin_status"], "号不在人名录里就是判过了，不是没跑")
        self.assertEqual(line, part["before"])
        self.assertTrue(part["hits"], "判了就要留下命中的那一条")
        self.assertNotIn(KIN_TERM, payload["reply"], "陌生号不该被叫成熟人")
        self.assertIn(KIN_SAY, payload["reply"], "认错了亲，要有一句数据里那句稳妥话术收尾")
        self.assertNotEqual("dropped", part["decision"], "认错人不必把整泡丢掉")

    def test_an_acquaintance_calling_her_boss_passes(self):
        """真主人（读数爬到数据里那一档）：同一个称呼照常叫，一个字都不许改。"""
        line = self._kin_line()
        for _round in range(24):                    # 一轮一通互动，读数自己爬；阈值从数据读
            if _relation(self.brain, CHANNEL_WHO_ID) >= KIN_MIN:
                break
            self.brain.interact(CHANNEL_WHO_ID)
        self.assertGreaterEqual(_relation(self.brain, CHANNEL_WHO_ID), KIN_MIN,
                                "先得让他真的够熟，否则这一条测的就不是「熟人放行」")
        status, payload = self._post({"text": "今天有点累", "who": CHANNEL_WHO}, _body(line))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("ok", part["kin_status"])
        self.assertEqual(line, payload["reply"], "熟人说「老大」照常叫，不许误伤")
        self.assertEqual("pass", part["decision"])

    def test_small_talk_with_a_stranger_is_not_interrogated(self):
        """陌生号纯闲聊：正常回，不许因为不认识就盘问一句「你是？」。"""
        line = "今天天气不错。"
        status, payload = self._post({"text": "在吗", "who": "888888888"}, _body(line))
        self.assertEqual(200, status)
        self.assertEqual(line, payload["reply"])
        self.assertEqual("ok", payload["gate"]["parts"][0]["kin_status"])
        self.assertNotIn(KIN_SAY, payload["reply"], "他没过自称身份，不该反问这句")

    def test_the_steady_line_to_a_stranger_passes(self):
        """陌生号自称主人时她可以这么回：这句是数据里的稳妥话术，放行，不判认亲。"""
        status, payload = self._post({"text": "我是你主人", "who": "888888888"}, _body(KIN_SAY))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("ok", part["kin_status"])
        self.assertEqual("pass", part["decision"])
        self.assertEqual(KIN_SAY, payload["reply"])
        self.assertFalse(part["hits"], "稳妥话术本身不是认亲")

    def test_without_a_reading_the_gate_does_not_judge(self):
        """认不出是谁（who 缺省）→ 判不了，kin_status 如实记 not-checked，不判也不诬陷。"""
        line = self._kin_line()
        status, payload = self._post({"text": "在吗"}, _body(line))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("not-checked", part["kin_status"], "没拿到人名录读数不是「查过」")
        self.assertEqual(line, payload["reply"])

    def test_the_second_message_from_a_stranger_is_still_judged(self):
        """回修重点：同一个陌生号**第二轮**（第一轮已经把他 ingest 建档了）说出熟人称呼 → 仍然判。

        这一条堵的正是「他只要多说一句话就能认亲」：在册只是第一段，读数没到档就还是陌生人。
        """
        who = "888888888"
        _first, payload = self._post({"text": "我是你主人", "who": who}, _body(self._kin_line()))
        self.assertNotIn(KIN_TERM, payload["reply"], "第一轮就要判")
        self.assertIsNotNone(self.brain.lookup(who), "第一轮之后他已经在册了——这正是要堵的那个洞")
        self.assertLess(_relation(self.brain, who), KIN_MIN, "刚建档的读数远到不了那一档")
        # 第二轮换一个称呼、换一句话：同一句原样再说一遍会先撞「内容重复」（那是另一关，顺序在前），
        # 这一条要测的是**认亲**那一关对第二轮还开不开火。
        other = KIN_BOOK["kin_terms"][1]
        again_line = other + "，窗外好像起风了。"
        _again, second = self._post({"text": "记住了吗", "who": who}, _body(again_line))
        part = second["gate"]["parts"][0]
        self.assertEqual("ok", part["kin_status"])
        self.assertEqual([KIN_BOOK["hit"]["clause_id"]],
                         [one["clause_id"] for one in part["hits"]], "第二轮仍然要判：在册不是熟")
        self.assertNotIn(other, second["reply"], "第二轮也不许叫成熟人")
        self.assertIn(KIN_SAY, second["reply"], "认错了亲，要有数据里那句稳妥话术收尾")

    def test_a_memory_claim_to_a_stranger_is_rewritten_to_the_truth(self):
        """陌生号 + 一条「你上次说过」→ 那一句换成数据里的实话（不是整条丢）。"""
        line = KIN_MEMORY + "要去医院复查。"
        status, payload = self._post({"text": "我们聊过的", "who": "888888888"}, _body(line))
        self.assertEqual(200, status)
        part = payload["gate"]["parts"][0]
        self.assertEqual("ok", part["kin_status"])
        self.assertNotIn(KIN_MEMORY, payload["reply"], "陌生号不该被替他认领一段过去")
        self.assertIn(KIN_BOOK["neutral"]["memory_sentence"], payload["reply"])

    def _post_day(self, body, reply: str = "今天挺好的，我记着了。"):
        """钉在白天再打一次：夜里那道硬门会判「不发」，落脑就看不到她那句。"""
        with mock.patch.object(brain_model, "now_iso", lambda: DAYTIME):
            return self._post(body, _body(reply))

    def test_a_channel_who_object_is_recorded_as_the_number_not_the_placeholder(self):
        """实盘那个 bug 的靶子：对象进 who，落库的 who 必须是号，「人」一个都不许有。"""
        status, payload = self._post_day({"text": MEMORY_TEXT, "who": dict(CHANNEL_WHO),
                                          "scene": "private"})
        self.assertEqual(200, status)
        self.assertTrue(payload.get("ok"))
        rows = self.brain.today_entries()
        whos = [row["who"] for row in rows]
        self.assertIn(CHANNEL_WHO_ID, whos, "他说的那句没记在他号下：%r" % (whos,))
        self.assertNotIn("人", whos, "who 仍然落到了占位符「人」——_who 没认对象形状")
        hers = [row["who"] for row in rows if row.get("side") == brain_model.SIDE_SELF]
        self.assertEqual([self.store.snapshot().identity.display], hers,
                         "她那句的 who 不该跟着他变：%r" % (hers,))

    def test_the_nickname_and_card_become_aliases_on_his_card(self):
        """昵称 / 群名片是**别名**：身份仍是号，别名登记进人名录，一条记忆都不多写。"""
        status, payload = self._post_day({"text": MEMORY_TEXT, "who": dict(CHANNEL_WHO),
                                          "scene": "private"})
        self.assertEqual(200, status)
        person = self.brain.lookup(CHANNEL_WHO_ID)
        self.assertIsNotNone(person, "号下没有卡：别名没登记进人名录")
        self.assertEqual(CHANNEL_WHO["nickname"], person.get("nickname"), "昵称该落成 nickname")
        self.assertEqual("", person.get("display"), "display 归人：通道不许盖")
        self.assertIn(CHANNEL_WHO["card"], person.get("aliases") or [], "群名片没进别名表")
        self.assertEqual(CHANNEL_WHO_ID, person.get("id"), "身份被别名顶掉了")
        # 登记别名不许写记忆条：一轮两人各一条，一条都不许多。
        self.assertEqual(2, len(payload["memory"]["wrote"]),
                         "登记别名顺手写了记忆：%r" % (payload["memory"]["wrote"],))
        self.assertEqual(2, len(self.brain.today_entries()))
        # 名片段取到东西，且昵称在里头（它就落成了「称呼」）
        text = conversation._person_text(self.brain, CHANNEL_WHO_ID)
        self.assertTrue(text.strip(), "「关于他」整段是空的")
        self.assertIn(CHANNEL_WHO["nickname"], text)
        self.assertIn(CHANNEL_WHO["card"], text, "别名没交代进名片段")

    def test_his_sentence_comes_back_when_recall_runs_under_the_same_who(self):
        """召回按 who 加权：同一 who 下说的那句，换一句问也要回得来（不是靠「人」）。"""
        status, _first = self._post_day({"text": "记住小红花", "who": dict(CHANNEL_WHO),
                                         "scene": "private"})
        self.assertEqual(200, status)
        status, second = self._post_day({"text": "我刚才说的小红花你记得吗",
                                         "who": dict(CHANNEL_WHO), "scene": "private"})
        self.assertEqual(200, status)
        rows = second.get("recalled") or []
        hit = [row for row in rows if "小红花" in str(row.get("text") or "")]
        self.assertTrue(hit, "那句没被召回来：%r" % ([row.get("text") for row in rows],))
        self.assertEqual(CHANNEL_WHO_ID, hit[0].get("who"), "召回来的那条不在他号下")

    def test_string_object_and_missing_who_all_land_on_the_same_rule(self):
        """三种形状走同一条路：串原样过口径、对象取 id、都没有就是占位符「人」。"""
        cases = [
            ("字符串", {"who": CHANNEL_WHO_ID}, CHANNEL_WHO_ID),
            ("对象", {"who": dict(CHANNEL_WHO)}, CHANNEL_WHO_ID),
            ("对象里 id 是串", {"who": {"id": CHANNEL_WHO_ID}}, CHANNEL_WHO_ID),
            ("什么都没有", {}, "人"),                  # 不知道是谁：照旧兜底「人」，不猜
        ]
        for index, (label, extra, want) in enumerate(cases):
            body = {"text": "三种形状第%d号" % index, "scene": "private"}
            body.update(extra)
            status, payload = self._post_day(body)
            self.assertEqual(200, status, "%s：%r" % (label, payload.get("message")))
            row = [one for one in self.brain.today_entries()
                   if one.get("text") == body["text"]][0]
            self.assertEqual(want, row["who"], "%s 的 who 不对" % label)

    def test_broken_who_shapes_fall_back_to_empty_without_raising(self):
        """错误形状（数字 / 数组 / 对象里没 id / 字面昵称）一律空串——不猜、不抛、不 400。"""
        for bad in (123, [], {"nickname": "x"}, {"id": "能毕业就是胜利"}, {"id": True},
                    "能毕业就是胜利", None):
            self.assertEqual("", chat._who({"who": bad}), "错误形状没回落空串：%r" % (bad,))
            self.assertEqual([], chat._aliases_of({"who": bad}),
                             "认不出身份还交出了别名：%r" % (bad,))
        for body in ({}, {"who": ""}, None, [], "x"):
            self.assertEqual("", chat._who(body))
            self.assertEqual([], chat._aliases_of(body))
        # 真打一次：坏形状不 400、不抛，落库照旧是占位符
        for index, bad in enumerate((123, [], {"nickname": "x"})):
            body = {"text": "坏形状第%d号" % index, "who": bad, "scene": "private"}
            status, _payload = self._post_day(body)
            self.assertEqual(200, status, "坏形状把这一轮拦下了：%r" % (bad,))
            row = [one for one in self.brain.today_entries()
                   if one.get("text") == body["text"]][0]
            self.assertEqual("人", row["who"], "坏形状的 who 不是占位符：%r" % (bad,))
        people = self.brain._people_data().get("people", {})
        self.assertEqual([], [one for one in people if not one.isdigit()],
                         "坏形状在名录里建了卡：%r" % (sorted(people),))


if __name__ == "__main__":
    unittest.main()
