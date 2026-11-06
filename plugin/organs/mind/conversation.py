"""核的装配入口：一句话进来 → 装配 → 发模型 → 过嘴 → 落脑，出去一整轮。

这是**全仓唯一的装配点**：QQ 通道将来直接调 answer()，不再自己拼第二遍。装配的规矩
一个数都不写在本文件里，全在 organs/mind/data/assembly.json：

  order      各段拼装的先后（soul / person / caller / anchors / recent / segment / banned / action /
             memory / drawer / heart / mouth / scene）
  sections   {key: {"label": 段落标题, "cap": 字数上限, "notice": 段首那句说明}}
             魂那一节的 notice 是**非给不可**的一格（缺了 / 写成空串当场 ValueError）。
  prompt     system / user 两段模板（占位符见下）
  reply      budget_by_tier（五档词 ⟷ 回复字数）+ default + instruction
  recent     turns（读几轮）/ cap（每侧截多少字）/ notice（「更早还有 N 轮」那句模板）
  banned     rounds（抽她最近几轮）/ opening_chars（开场取几个字）/ note（那句禁令怎么说）
  action     {"pool": 部位池, "rule": 写法规矩, "quota": 配额}：动作给池子，但配额说了算
  caller     「这一轮是谁」那一节：lines（known / unknown / missing 三句）+ address_fallback
             （名片上没填称呼时的兜底词）。**只认号、不认自称**：他嘴上说「我是主人」，这一格
             一个字都不变——它只看这一轮的 who，不看这一轮的原话。
  anchors    {"label", "cap", "room", "name"}：锚句从家·哪一间房的哪一张卡读
  drawer     抽屉那一节：判意图的关键词、翻哪几个格子、挑几篇、整节上限、节尾那句说明
  scene      场合：group / private / unknown 各一句，按归一化后的场景取一句进提示词
  segment    notice：只有「群聊 + 这一段里至少两个人说话」才把 group 那一句接在段尾
             （私聊 / 单人 / unknown 一律不接）。段的标题与上限在 sections.segment 里
  soul       sections.soul.notice：魂那一段**段首**的那句话，接在标签之后、正文之前。
             魂六件是人写给她的底子，整段照抄进提示词读起来与台词一样，她会照着念出来；
             这一句说的是「这些是你的底子，不是要背出来的话——别照着念」。锚句那一段不重复加
             （它本来就有「给半句」的口径）。这句说明不占 sections.soul.cap（cap 量的仍是正文）
读法同 llm.json：实盘 <仓库>/home/organs/mind/assembly.json 优先（存在才读），否则读
出厂 organs/mind/data/assembly.json。

prompt 的两段模板各认几个占位符（花括号，装配时逐键**纯替换**，不做 format——
数据里多一个花括号不该把这一轮炸掉）：
  system  {sections}          各段拼好的正文，每段带 sections[key].label 当标题
          {reply_budget}      这一档的回复字数上限（来自 reply.budget_by_tier）
          {reply_instruction} 回复口径那句话（来自 reply.instruction）
  user    {text}              本人这一次的原话
  proactive （她自己开口那一轮换的是这一份，见下面「主动开口」）
          {sections} / {reply_budget} / {reply_instruction} 同上三项
          {seed}              这一轮的由头（她要说出口的那件事，就是这一轮的「一问」）
          {reason}            由头那一侧的理由码；没给就用 prompt.proactive_reason_missing 顶上
  主动那一轮 user 是**空串**（没有对方的话），不套 prompt.user。

三拍的顺序是钉死的：**先回想、再发模型、最后写记忆**。
先回想，是因为这一轮该想起什么要在写提示词之前定下来；写记忆放在最后，是因为
只有真说出口的那一句才配当亲历——被闸弃掉的话不进脑（它压根没说出口）。

**口癖护栏**（anchors.guard）也守同一条次序：装配之前先读 usage.report 的滚动窗口，
再按它的 injected 决定这一轮注入几条半句（命中率超阈值就降到 reduced_count 条）；
她真说出口之后才把这一轮的「开头」追进 organs/mind/usage.py 那本账——先记后算会自己踩
自己，被闸弃掉的轮（reply 空）不记：她没说出口，就没有开头。护栏的六个门槛全在
assembly.json 的 anchors.guard 里，本文件一个数都不写。

**近轮禁用也扩到锚句**（anchors.repeat）：最近 window 轮里同一张卡（按 id，账上的 id 那一栏）
命中到 max 次，这一轮就不再注入那张，并在返回体 anchors.banned_by_repeat 里逐个点名——不静默丢。
次序钉死：**先按 guard 定总条数，再按 repeat 剔除**。两条护栏各管各的，混在一起就成了第二套判据。

**照抄审计（两级）排在闸之后**：她这句与某张卡的 example 连着重合到 anchors.audit.example_overlap
个字，判「报台词」——交 organs/mouth/rewrite.py 的 prompt / accept 改一次（走同一个 llm.complete
出口，本文件不另起 HTTP 客户端），改完仍重合就弃掉这一句；只命中 phrase（≤4 字那一档）的放行，
只记账。**example 一个字都不进提示词**：她抄不着没看见过的东西，能抄到的只有 phrase 那半句。
返回体 audit 一节如实写这一轮判的是哪一档（decision / kind / card_id / overlap）。

各段只搬已有器官的读数，一个判据都不自己造：
  soul    store.render()（魂六件）
  caller  这一轮是谁：brain.lookup + brain.card_view 查那个号有没有名片——查得到就写「来的是
          {relation}（你叫{address}）」，查不到（或查的时候抛错）就照实说「一个不认识的号」，
          who 空着就写「不知道是谁」。**只认号、不认自称**：这一轮的正文一个字都不看；**号的
          数字一个字都不进提示词**（三句模板里没有 {who}；号只在回包 assembly.sections 里
          caller 那一行，与账上）。
          relation 只从名片的称呼 / 关系栏来、没有就用 relation_fallback；address 只从名片上她
          对他的称呼来、没有就用 address_fallback——昵称（display）绝不顶这两个位置，它另起一行
          由 nickname_line 交代。三句只陈述事实、不带一句指令（文案与兜底词全在 assembly.json 的
          caller 里）。
  anchors 家·仓库里那几张锚句卡（organs.home.anchors.read_all 的公开读口）：**只注入 phrase 的前
          半句**，example 一个字都不进提示词；没配 / 卡不在 / 读不出来一律写「未配锚句」，不静默跳过
  recent  organs/mind/recent.py：最近几轮原文；omitted > 0 时把「更早还有 N 轮」放在段尾
  banned  organs/mind/repeat.py：只抽**她那一侧**最近几轮的动作行与开场，去重计次列成禁令——
          最近几轮那段原文一个字都不删，禁令另起一节；人的话一个字都不许进来
  action  部位池、写法规矩与配额（assembly.json 的 action，代码里没有部位词与本层的配额数）；
          配额按「最近 quota.window 轮里有几轮带动作」算（repeat.count_with_actions）——
          已达 quota.max 就只写 quota.blocked 那一句，池子与规矩一个字都不给
  memory  brain.recall(query=text, who=who or None)：既进提示词，也原样进返回体
          recalled[]（页面要显示「想起了什么 + 证据指针 + 她的理解」）。每条记忆进提示词是
          两行：原文一行（括号里是来源｜来源类型｜证据），她的理解 note_display 另起一行、
          缩进两格——原话与她的理解不许糊成一行，那是脑里分开写的两栏
  drawer  抽屉，两段各占一节、字数分别数得出来：
          · **恒定那篇**（drawer_always，每轮都带）：从家里挑**一篇**与这一句最贴近的旧日记
            （_drawer_score：与这一句的连续重合字数 + 月份词，与「被问时」同一把尺子），一行旧
            日记 + 一句 always.note。整块（含段标题前缀与行格式）≤ always.max_chars。
            **同一篇 name 在 dedupe_days 天内不重复带**：账本
            <器官目录>/mind/drawer_usage.jsonl 一轮一行 {at,name,scene,score,why}，选的时候
            读窗口内的 name，命中就换下一篇（全落在窗口里就这一轮不带，绝不把刚带过的塞回来）。
            **这一篇与关键词无关，每轮都算**：排在最前的那一篇压不进上限（第一句就比余地长）
            就换下一篇，绝不因为「排第一的那篇截不出一整句」把整段拖成空。
            她那几百篇旧账因此每轮都在，而不是只有被问到时才翻。
          · **被问时那 2–3 篇**（drawer）：问句里出现 drawer.keywords 任一个词才翻——**翻不翻
            只看意图，不看场合**；场合只决定能取哪几格（drawer.shelves：private 普通 + 恋爱，
            group 与 unknown 只取普通；unknown 那档在多篇那一节的节首插 drawer.unknown_note
            「来源未定」，绝不把 unknown 猜成 group / private）。cards.list_room 一次
            （room / kind / shelves 全在数据里），按相关性 + 日期挑到 max_entries 篇、逐篇
            cards.read_item 取正文，每篇一行压进 sections.drawer.cap。**恒定那篇用过的那一篇
            从这一段的候选里剔掉**（同一轮两节不重复同一篇；剔完没了就这一节空着，不为凑数
            把同一篇放回来）。节尾那句 testimony_note
            是这一节的要害：那些字是她自己写下的（testimony=false），只能说「我写过」，不许说成
            对方说过。
          两段都**只进提示词，绝不进脑**：这一节不交给 brain.ingest 一个字；哪一段没有就
          **整节不加**（不留一个只有标题的空节），关着的那一段一次 IO 都不做。
  heart   effective.effective_state（全仓唯一合成点）+ engage.decide（只 decide，
          **不 decide_and_log**）；提示词与返回体都**只给档位词**，一个数都不出门
  mouth   address.resolve + tone.from_store（口径行进提示词），过闸用 gate.enforce
          （闸那一层的重写器传 None：命中即弃），节奏用 split + pace.fill；照抄审计那一档才发
          第二次模型请求（anchors.audit，见上）
  scene   场合：按 answer(scene=...) 归一化后的值（只认 organs/brain/model.py 的 SCENE_*，
          缺省 / 空白 / 非法一律 unknown——**不猜成私聊**，猜错就是把私事说出去了）从
          assembly.json 的 scene.lines 取一句，本文件一个字的文案都不写。order 里排在最后：
          说给谁、说什么都定了，最后交代在哪儿说。
  segment 这一段刚刚的发言（answer(segment=...) 归一化后的 [(谁, 说了什么)]），每行
          「谁：说了什么」；段空着就写一句实话（NO_SEGMENT），不许留白。**段与 recent 是两份
          输入，谁也不许顶替谁**：段是刚刚这一小段，recent 是更早的那 12 轮，同一条消息不会既
          当段又当 recent——recent 的写入在说完之后，段从不回写进 recent。段尾那句「几个人在
          说话」只在这一轮是群聊、且段里至少两个人说话时才接（assembly.json 的 segment.notice）。

**主动开口（seed 不是 None 的那一轮）**：日子线给她一个「由头」，她自己开口。它与被动的差别
**只有输入**，别的一处都不改：
  · 没有对方原话——text 让位给 seed（回想也用 seed，回想的是「由头这件事」）；
  · 「这一小段」写 NO_SEGMENT（段是别人刚刚说的话，这一轮没有别人说话）；段给了也忽略；
  · 提示词换整份 prompt.proactive（由头与理由进提示词，占位符见上）；user 那一问是空串；
  · 心判决的 intent 用 INTENT_PROACTIVE（夜里那一道硬门因此对她生效：被动只判「少说」，
    主动判「不说」）；被动仍 INTENT_PASSIVE；被骂恒 False（没人说话，就没有人骂她）；
  · 落脑只有她那句（side=SIDE_SELF）：没有对方那句可落，也**不许**为凑形状写一条假的人话；
    她这句话没出口（被闸弃掉 / 空）就一条都不落；
  · recent 照旧记一行（她这一轮同样是一轮），但 said 记空串——由头是她的念头，不是人的话。
回包因此多两格：mode（这一轮是谁起的头）与 seed{text, reason}（由头原样与理由码，被动为空）。
注意 payload.reason 一直是**出站闸**的理由码（night / today-volume…），与由头的理由码不是一回事，
所以后者只在这一格，两处不共用一个字段。

**「要我记事」那一轮（remind，装配前一次轻量识别）**：这一步排在装配之前、**一个字都不进
提示词**——它判的不是她怎么说，而是这一轮要不要落一个动作。判据与文案全在
organs/mind/data/remind.json（意图词、噪声词、数字表、相对时间的单位、三句回执），本文件
一个词都没有；实现在 organs/mind/remind.py：
  · 认出「十分钟后叫我」→ 手·add_task 往家·工作台落一张卡（who=这一轮的号、title=去掉时间词
    的那半句、due=当前+10 分钟），落了卡才说 remind.json 的 say.ok；没落成就说 say.fail；
  · 有意图没时间（「回头提醒我」）→ 反问 say.ask，**一张卡都不建**——解析不出就不猜；
  · 普通闲聊 → remind.handle 回 None，这一轮一个字都不改（回包里 remind 那一格也不出现）。
主动那一轮（seed 不是 None）不认：由头是她自己的念头，不是谁让她记的事。
回包因此多一格 remind{parsed, due, card_id, ok, why}（**只在认作记事的那一轮出现**）：
parsed 认没认出时刻、due 落在哪一刻（ISO 带偏移）、card_id 哪张卡、ok 成没成、
why 为什么没成（no_time 或手那边的码）。**卡先落、话后说**：认出来就先落卡，再走模型那一趟，
所以模型出错也不会丢掉「这件事」；代价是那一轮白花一次模型调用（她的台词这句已定）。
remind_root：手的实盘根（None → <器官目录>/hand，家按兄弟目录换算）——测试靠它把卡落到临时盘。

**四个旋钮落到能验收的东西上**（阈值、字数上限、玩笑词表全在 assembly.json 的 tone_rules 里，
本文件一个数都不写）：
  长度  心情凉（mood < low_mood_below，0–1 刻度）或精力低（energy < low_energy_below，0–100 刻度）
        的那一轮，上限取 min(档位预算, max_chars_low)，装配完在**句末**硬截（绝不截半句），
        截了记 truncated。中性档取 max_chars_neutral；为 null 就照旧用 reply.budget_by_tier，
        也从不硬截——档位预算历来只是提示词里的一句话，不是锯子。
  软硬  softness 由嘴按心的读数现算，核**只记录**它，一个字都不改；心情凉那一轮不强制改称呼，
        只把 address 与 softness 一起落账，供人事后核对。
  主动  engage 判成「少说」→ no_question=true，装配后把带问句标点的整句删掉（删空则留原句，
        记 kept_because_empty）；「不说」由出站闸整轮拦下，不归这一层。
  玩笑  mood < low_mood_below → ban_joke=true：提示词加一句（文案在 tone_rules.ban_joke_note，
        本文件没有这句中文），装配后把含玩笑词的整句删掉（同一套删空保护）。
四个读数**都是这一轮已经算好的**（eff / mood_read / energy_read / offsets / judgement）：
多读一遍盘或多 decide 一次，同一个人同一刻就会得出两种结论。每轮落一行账：
<器官目录>/mind/tone_log.jsonl（只追加，坏行跳过），字段见 TONE_FIELDS。

坏卡不是崩溃：mood 照读，effective_state 抛 CardError 就降级成只要 mood 那一块——
与 api/heart.py 的既有口径同一条（面板能显示「拒载」，这一轮照旧说得出话）。

温度分档也在这里定：档位（默认 / 事实）与触发条件全在 data/llm.json 的 temperature_tiers，
本轮 recalled 非空且回复预算不超过阈值就降成事实档，实际用的那个温度传进 llm.complete，
并原样进返回体的 llm.temperature / llm.temperature_tier。

LLMError 不在这里翻译：原样上抛，由 api/chat.py 按 kind 翻成状态码与人话。
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import paths
from ..brain import model as brain_model
from ..home import access as home_access
from ..home import anchors as home_anchors
from ..home import cards
from ..home import residents as home_residents
from ..home import store as home_store
from ..heart import card as card_module
from ..heart import effective as effective_module
from ..heart import energy as energy_module
from ..heart import engage as engage_module
from ..heart import intent as intent_module
from ..heart import model as heart_model
from ..heart import mood as mood_module
from ..heart import offset as offset_module
from ..heart import ports as ports_module
from ..heart import sleep as sleep_module
from ..mouth import address as address_module
from ..mouth import drops as drops_module
from ..mouth import gate as gate_module
from ..mouth import pace as pace_module
from ..mouth import rewrite as rewrite_rules     # 参数名就叫 rewrite：模块用别名进来免得被挡住
from ..mouth import style_policy
from ..mouth import tone as tone_module
from ..mouth import warehouse
from ..mouth.split import split as split_text
from . import events
from . import llm
from . import recent
from . import remind
from . import repeat
from . import themes
from . import usage

LOGGER = logging.getLogger("qqagent.mind")   # 对账对不上要留痕：一行 warning，不刷屏
ASSEMBLY_NAME = "assembly.json"          # 装配说明书的文件名（实盘 / 出厂各一份）
HUMAN_WHO = "人"                          # 没给 who 时，人的原话记在谁名下
DEFAULT_WHO = "主人"                      # 门的读数要一个名字：没给就按主人算（与心·api 同一个兜底）
PENDING_WORD = "待定"                     # 待定轴（耐心 / 记性）没有档位词，照 effective.render_text 的说法
REPLY_KEY = "reply"                       # reply_budget.key：这笔预算出自 assembly.json 的哪一节
NO_MEMORY = "这次没想起什么。"
NO_QUOTE = "（无原话，只有她的理解）"        # 原话空、只有她的理解那一行：第一行不能空着（空行会被读成「没想起」）
NOTE_INDENT = "  "                        # 她的理解另起一行、缩进两格：与原文那行一眼分开，又不另起一节
NO_TURNS = "还没有更早的对话"                # 盘上一条历史都没有时，recent 段不写空白
ANCHORS_MISSING = "未配锚句"                  # 没配 / 卡不在 / 读不出来，三态同一句话
ANCHORS_HINT = "锚句由人写，程序不替她造。"
ANCHORS_ALL_BANNED = "这一轮没有可给的半句：都被近轮禁用挡住了（见 anchors.banned_by_repeat）。"
COPY_CLAUSE = "报台词"                  # 照抄审计命中的那一处：重写指令里说的「哪一条」
COPY_KIND = "例句重合"                  # 同上：这一处是哪一类（与返回体 kind 的 example 不是一回事）
COPY_REASON = "这句与台词卡「%s」的例句连着重合 %d 字（阈值 %d 字）：那是在报台词，不是说话。"
COPY_AGAIN = "改了一次仍与台词卡「%s」的例句连着重合 %d 字（阈值 %d 字）：这一句不发了。"
SEED_SEP = "|"                            # 节奏的种子：who 与时刻拼起来，同一轮两次调用得到同一组间隔
POOL_SEP = "／"                            # 动作池进提示词时部位之间的分隔（全角斜杠，与标点同一套）
BANNED_EMPTY = "最近几轮没有可禁用的动作行与开场。"   # 一条都抽不出来时的实话（空白会被她读成「随便用」）
NO_SEGMENT = "这一段没有别的发言"          # 段空着 / 没给时这一节写这句实话（不许留白）
SEGMENT_LINE = "%s：%s"                    # 段里一行的样子：谁 + 说了什么（没有项目符号，与 recent 一眼分开）
SEGMENT_KEYS = ("speaker", "text")         # 段里一条记录的两个键（两种形状：对象，或二元组）
SCENE_CAP_MAX = 200                       # 场合那节的上限：数据里可以更小，不许更大（这节只有一句「在哪儿说」）
SOUL_KEY = "soul"                         # 魂那一节在 order / sections 里的键：只有它非带 notice 不可
SOUL_NOTICE_HINT = ("魂六件是她写下的底子，不是台词：原文照抄进提示词，她就会当台词念出来，"
                    "所以段首那句说的是「别照着念」")   # 数据缺这句话时点名用的一句解释
CALLER_KEY = "caller"                     # 「这一轮是谁」在 order / sections 里的键
CALLER_SHAPES = ("known", "unknown", "missing")   # 「这一轮是谁」三种形状：数据里一句都不许缺
# 名片上「关系 / 称呼 / 昵称」三类各看哪几栏：键名是名片的结构，不是文案，所以不进 assembly.json。
# display 是通道写进来的群昵称（_register_aliases 与 brain.interact 都往这一栏写），它只能当昵称用。
CALLER_RELATION_KEYS = ("relation", "关系")        # 「主人」这类关系词只许从这几栏来
CALLER_ADDRESS_KEYS = ("称呼",)                    # 她对他的那个称呼：名片上人写的那一栏
CALLER_NICKNAME_KEYS = ("display", "昵称", "nickname")   # 昵称：只给 nickname_line 用，不许顶上面两处
CALLER_WHO_TOKEN = "{who}"                        # 号不进提示词：模板里带这个占位就是数据坏了
DRAWER_KEY = "drawer"                     # 抽屉那一节在 order / sections 里的键（空着就整节不加）
# 恒定那篇单独一节：它每轮都在、有自己的字数上限，与「被问时那 2–3 篇」分开数、分开锯。
DRAWER_ALWAYS_KEY = "drawer_always"
LINE_JOIN = "\n"                          # 抽屉每篇一行、行与行之间的分隔（也用来隔开节尾那句说明）
# 抽屉那一块认得的键（装配时多余的一律报错）：翻不翻只看 enabled + keywords，**没有「场合: 翻不翻」
# 那种开关**——场合只经 shelves 决定能取哪几格，猜错场合就等于在群里说私话。
DRAWER_KEYS = ("enabled", "keywords", "shelves", "unknown_note", "max_entries", "cap", "label",
               "kind", "room", "testimony_note", "note", "line", "more", "date_pattern",
               "list_limit", "date_chars", "line_chars", "topic_min_chars", "month_words",
               "always")
# 恒定那一段自己的键：开关、整块上限、去重窗口、行模板、省略号、那句 always.note。
DRAWER_ALWAYS_KEYS = ("enabled", "max_chars", "dedupe_days", "line", "more", "note")
DRAWER_LOG = "drawer_usage.jsonl"         # 恒定那篇的账本（落在 <器官目录>/mind 下，一轮一行）
# 那本账的 why 两档：话题对得上（分够 topic_min_chars）／没对上，只是按日期从新到旧补的。
DRAWER_WHY_TOPIC = "topic"
DRAWER_WHY_DATE = "date"
WORD_JOIN = " "                           # 正文里的换行压成空格：一篇只占一行，行首才认得出日期
TONE_LOG = "tone_log.jsonl"               # 语气账的文件名（落在 <器官目录>/mind 下，只追加）
# 每轮那一行的字段（冻结，人事后照这个核对）：四个旋钮落到哪儿、心与嘴各在哪一档、软度与称呼。
# address 也在里面：心情凉的那一轮不强制改称呼，所以事后要看得见她当时叫的是什么。
TONE_FIELDS = ("at", "mood_tier", "energy_tier", "tone_tier", "softness", "max_chars",
               "no_question", "ban_joke", "truncated", "dropped_questions", "dropped_jokes",
               "kept_because_empty", "heart_source", "address")


# —— 装配说明书 ———————————————————————————————————


def load_assembly(path: Optional[Any] = None) -> Dict[str, Any]:
    """读装配说明书：实盘覆盖（存在才读）→ 出厂数据；path 给定时只读那一份。

    两份都没有是错，不是「用默认值悄悄跑起来」——装配顺序与字数上限没有默认值。
    """
    if path is not None:
        return _read_assembly(Path(path))
    live = paths.organ_dir("mind") / ASSEMBLY_NAME
    if live.is_file():
        return _read_assembly(live)
    factory = paths.factory_dir("mind") / ASSEMBLY_NAME
    if factory.is_file():
        return _read_assembly(factory)
    raise ValueError("找不到装配说明：%s 与 %s 都不存在" % (live, factory))


def _read_assembly(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("读不了装配说明 %s：%s" % (path, exc)) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError("装配说明不是 JSON：%s：%s" % (path, exc)) from exc
    return _check_assembly(data, str(path))


def _check_assembly(data: Any, where: str) -> Dict[str, Any]:
    """校验并归一化：缺节、缺键、类型不对一律当场报——数据里没有的规矩不许在代码里补。"""
    if not isinstance(data, dict):
        raise ValueError("装配说明不是一份对象：%s" % where)
    order = data.get("order")
    if not isinstance(order, list) or not order:
        raise ValueError("装配说明缺 order（四段的先后）：%s" % where)
    sections = data.get("sections")
    if not isinstance(sections, dict) or not sections:
        raise ValueError("装配说明缺 sections（每段的名字与字数上限）：%s" % where)
    checked: Dict[str, Dict[str, Any]] = {}
    for key in order:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("order 里有一项不是段落名：%r（%s）" % (key, where))
        spec = sections.get(key)
        if not isinstance(spec, dict):
            raise ValueError("sections 里缺 %r 这一节：%s" % (key, where))
        label = spec.get("label")
        cap = spec.get("cap")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("sections.%s 缺 label（段落标题）：%s" % (key, where))
        if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
            raise ValueError("sections.%s 的 cap 要正整数：%s" % (key, where))
        checked[key] = {"label": label.strip(), "cap": cap}
        # 段首那句说明（现在只有魂那一节有）：写了就必须是一句能读的话，空串等于没写。
        notice = spec.get("notice")
        if notice is not None:
            if not isinstance(notice, str) or not notice.strip():
                raise ValueError("sections.%s.notice 要一句非空的话（空串等于没这句说明）：%s"
                                 % (key, where))
            checked[key]["notice"] = notice.strip()
    if SOUL_KEY in checked and "notice" not in checked[SOUL_KEY]:
        raise ValueError("sections.%s.notice 缺这一格（段首那句「别照着念」）：%s（%s）"
                         % (SOUL_KEY, SOUL_NOTICE_HINT, where))
    if "scene" in checked and checked["scene"]["cap"] > SCENE_CAP_MAX:
        raise ValueError("sections.scene.cap 不许超过 %d（场合只是一句话）：现在是 %d：%s"
                         % (SCENE_CAP_MAX, checked["scene"]["cap"], where))
    prompt = data.get("prompt")
    if not isinstance(prompt, dict):
        raise ValueError("装配说明缺 prompt（system / user 两段模板）：%s" % where)
    # proactive 两格也必须在数据里：缺了就是「她自己开口那一轮没有提示词模板」，
    # 不许在代码里补一份默认话（补了人改数据还以为生效了）。
    for name in ("system", "user", "proactive", "proactive_reason_missing"):
        if not isinstance(prompt.get(name), str) or not prompt.get(name).strip():
            raise ValueError("prompt.%s 不能为空：%s" % (name, where))
    reply = data.get("reply")
    if not isinstance(reply, dict):
        raise ValueError("装配说明缺 reply（回复预算）：%s" % where)
    budgets = reply.get("budget_by_tier")
    if not isinstance(budgets, dict) or not budgets:
        raise ValueError("reply.budget_by_tier 要一份非空的档位表：%s" % where)
    clean_budgets: Dict[str, int] = {}
    for tier, chars in budgets.items():
        if isinstance(chars, bool) or not isinstance(chars, int) or chars <= 0:
            raise ValueError("reply.budget_by_tier.%s 要正整数：%s" % (tier, where))
        clean_budgets[str(tier)] = chars
    default = reply.get("default")
    if isinstance(default, bool) or not isinstance(default, int) or default <= 0:
        raise ValueError("reply.default 要正整数：%s" % where)
    instruction = reply.get("instruction")
    if not isinstance(instruction, str):
        raise ValueError("reply.instruction 要一段字：%s" % where)
    banned_conf = _check_banned(data.get("banned"), where)
    action_conf = _check_action(data.get("action"), where)
    drawer_conf = _check_drawer(data.get("drawer"), where)
    caller_conf = _check_caller(data.get("caller"), where, "caller" in checked)
    # 同一格不许有两份数：drawer.cap / drawer.label 与 sections.drawer 说的是同一节。
    # 两处不等就当场报——人改了其中一处，另一处悄悄把这一节截掉，查起来比报错难十倍。
    if drawer_conf is not None and DRAWER_KEY in checked:
        section = checked[DRAWER_KEY]
        if section["cap"] != drawer_conf["cap"] or section["label"] != drawer_conf["label"]:
            raise ValueError(
                "drawer.cap / drawer.label 要与 sections.drawer 一致：现在 sections 是 %r/%d，"
                "drawer 是 %r/%d（%s）"
                % (section["label"], section["cap"], drawer_conf["label"],
                   drawer_conf["cap"], where))
    # 恒定那篇自成一节：这一节的上限要装得下 always.max_chars（它量的是提示词里那一整块，
    # 段标题前缀也在内）。装不下就等于把这一段唯一的要害（那句 always.note）锯掉。
    if drawer_conf is not None and DRAWER_ALWAYS_KEY in checked:
        section = checked[DRAWER_ALWAYS_KEY]
        if section["cap"] < drawer_conf["always"]["max_chars"]:
            raise ValueError(
                "sections.%s.cap 要装得下 drawer.always.max_chars：现在是 %d / %d（%s）"
                % (DRAWER_ALWAYS_KEY, section["cap"],
                   drawer_conf["always"]["max_chars"], where))
    # 禁令窗口必须比配额窗口长一轮：配额放行的那一轮，禁令里得还看得见它——
    # 否则同一条动作会在「配额刚好重置」的那一轮重生（两个窗口同时把它挤出去）。
    want = action_conf["quota"]["window"] + 1
    if banned_conf["rounds"] != want:
        raise ValueError(
            "banned.rounds 要等于 action.quota.window + 1（%d），现在是 %d："
            "短一轮会让被放行的动作从禁令窗口滑出去，长一轮白读旧动作（%s）"
            % (want, banned_conf["rounds"], where))
    return {
        "order": [key.strip() for key in order],
        "sections": checked,
        "prompt": {"system": prompt["system"], "user": prompt["user"],
                   "proactive": prompt["proactive"],
                   "proactive_reason_missing": prompt["proactive_reason_missing"]},
        "reply": {"budget_by_tier": clean_budgets, "default": default,
                  "instruction": instruction,
                  "format": _check_rules(reply.get("format"), "format", where),
                  "format_blocked": _check_rules(reply.get("format_blocked"),
                                                 "format_blocked", where)},
        "recent": _check_recent(data.get("recent"), where),
        "banned": banned_conf,
        "action": action_conf,
        "anchors": _check_anchors(data.get("anchors"), where),
        "scene": _check_scene(data.get("scene"), where),
        # 「这一轮是谁」那一节：order 里有它就要给三句，缺一句等于在数据之外补一份默认话。
        "caller": caller_conf,
        "segment": _check_segment(data.get("segment"), where, "segment" in checked),
        # 抽屉可以没有（缺这一块 = 不翻，不是每轮翻）：None 往下传，装配那一层照它判。
        "drawer": drawer_conf,
        "tone_rules": _check_tone_rules(data.get("tone_rules"), where),
    }


def _check_drawer(spec: Any, where: str) -> Optional[Dict[str, Any]]:
    """抽屉那一格：**可以没有**（没有就是「不翻」），写了就要写全。

    这里一个中文词都没有：关键词、格子名、行模板、月份词、节尾那句说明、unknown 那档的
    标注全在数据里。「有没有写全」当场报——少了哪个键，这一轮要么白翻一次抽屉，要么在提示词里
    拼出一节半截的旧事，两种都比报错难查。cap 与 label 还要与 sections.drawer 对得上：一处写
    300、一处写 200，人改的到底是哪一处就说不清了。

    **拆掉的开关不许悄悄留着**：旧数据里那份「场合: 翻不翻」（scene）已经删了，翻不翻只看
    enabled 与 keywords；再出现它（或任何不认得的键）就当场报，不静默忽略。
    """
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise ValueError("drawer 要一份对象（keywords / shelves / unknown_note / cap / label …）：%s"
                         % where)
    # 多余的键当场报：旧的那份「场合: 翻不翻」的开关已经删了，留着不许静默忽略——
    # 人改了它却什么也没变，比报错难查。
    extra = sorted(set(spec) - set(DRAWER_KEYS))
    if extra:
        hint = ("（「场合: 翻不翻」那份开关已经删了：翻不翻只看 enabled + keywords，"
                "场合只经 shelves 决定能取哪几格）" if "scene" in extra else "")
        raise ValueError("drawer 里有多余的键 %s%s（认得的只有 %s）：%s"
                         % (extra, hint, sorted(DRAWER_KEYS), where))
    clean: Dict[str, Any] = {}
    enabled = spec.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("drawer.enabled 要么 true 要么 false（缺省不是「翻」）：%s" % where)
    clean["enabled"] = enabled
    keywords = spec.get("keywords")
    if not isinstance(keywords, list) or not keywords:
        raise ValueError("drawer.keywords 要一份非空的关键词表（问句里出现任一个才翻）：%s" % where)
    clean_keywords: List[str] = []
    for word in keywords:
        if not isinstance(word, str) or not word.strip():
            raise ValueError("drawer.keywords 里有空词：%s" % where)
        clean_keywords.append(word.strip())
    clean["keywords"] = clean_keywords
    for name in ("max_entries", "cap", "list_limit", "date_chars", "line_chars",
                 "topic_min_chars"):
        value = spec.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("drawer.%s 要正整数：%s" % (name, where))
        clean[name] = value
    for name in ("label", "kind", "room", "testimony_note", "note", "unknown_note"):
        text = spec.get(name)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("drawer.%s 要一段非空的字：%s" % (name, where))
        clean[name] = text.strip()
    line = spec.get("line")
    if not isinstance(line, str) or line.count("%s") != 2:
        raise ValueError("drawer.line 要一行模板、且正好两个 %%s（日期 + 正文）：%s" % where)
    clean["line"] = line
    more = spec.get("more")
    if not isinstance(more, str):
        raise ValueError("drawer.more 要一段字（省略号，可以空）：%s" % where)
    clean["more"] = more
    pattern = spec.get("date_pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("drawer.date_pattern 要一条正则（name 前几个字像不像日期）：%s" % where)
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError("drawer.date_pattern 不是正则：%s：%s" % (pattern, exc)) from exc
    clean["date_pattern"] = pattern
    shelves = spec.get("shelves")
    if not isinstance(shelves, dict):
        raise ValueError("drawer.shelves 要一份 {场景: [允许翻的格子]} 的表：%s" % where)
    clean_shelves: Dict[str, List[str]] = {}
    for name in brain_model.SCENE_TYPES:
        value = shelves.get(name)
        if not isinstance(value, list) or not value:
            raise ValueError("drawer.shelves.%s 要一份非空的格子名列表：%s" % (name, where))
        rows: List[str] = []
        for shelf in value:
            if not isinstance(shelf, str) or not shelf.strip():
                raise ValueError("drawer.shelves.%s 里有空格子名：%s" % (name, where))
            rows.append(shelf.strip())
        clean_shelves[name] = rows
    clean["shelves"] = clean_shelves
    months = spec.get("month_words")
    if not isinstance(months, dict) or not months:
        raise ValueError("drawer.month_words 要一份 {月份: 那个月的说法} 的表：%s" % where)
    clean_months: Dict[str, str] = {}
    for month, word in months.items():
        if not isinstance(month, str) or not month.strip().isdigit():
            raise ValueError("drawer.month_words 的键要是月份数字：%r（%s）" % (month, where))
        if not isinstance(word, str) or not word.strip():
            raise ValueError("drawer.month_words.%s 要一段非空的字：%s" % (month, where))
        clean_months[str(int(month.strip()))] = word.strip()
    clean["month_words"] = clean_months
    always = _check_drawer_always(spec.get("always"), where)
    clean["always"] = always
    return clean


def _check_drawer_always(spec: Any, where: str) -> Dict[str, Any]:
    """恒定那篇那一格（drawer.always）：开关、整块上限、去重窗口、行模板、省略号、那句说明。

    **缺了不补默认值**：这几格一个默认值都没有——补一个 100 字、补一句 always.note，人改了
    数据还以为生效了。上限与窗口都要正整数：0 字的上限等于每轮都不带，0 天的窗口等于不去重，
    两种都是把这一段悄悄关掉，报错比它好查。
    """
    if not isinstance(spec, dict):
        raise ValueError("drawer.always 要一份对象（enabled / max_chars / dedupe_days / line / "
                         "more / note）：%s" % where)
    extra = sorted(set(spec) - set(DRAWER_ALWAYS_KEYS))
    if extra:
        raise ValueError("drawer.always 里有多余的键 %s（认得的只有 %s）：%s"
                         % (extra, sorted(DRAWER_ALWAYS_KEYS), where))
    clean: Dict[str, Any] = {}
    enabled = spec.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("drawer.always.enabled 要么 true 要么 false：%s" % where)
    clean["enabled"] = enabled
    for name in ("max_chars", "dedupe_days"):
        value = spec.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("drawer.always.%s 要正整数：%s" % (name, where))
        clean[name] = value
    line = spec.get("line")
    if not isinstance(line, str) or line.count("%s") != 2:
        raise ValueError("drawer.always.line 要一行模板、且正好两个 %%s（日期 + 正文）：%s" % where)
    clean["line"] = line
    more = spec.get("more")
    if not isinstance(more, str):
        raise ValueError("drawer.always.more 要一段字（省略号，可以空）：%s" % where)
    clean["more"] = more
    note = spec.get("note")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("drawer.always.note 要一段非空的字（那一篇的说明）：%s"
                         % where)
    clean["note"] = note.strip()
    return clean


def _check_tone_rules(spec: Any, where: str) -> Dict[str, Any]:
    """四个旋钮那一条：阈值、字数上限、玩笑词表、问句标点——全在数据里，代码里一个都不写。

    两条上限都可以空着（null）：max_chars_low 是「心情凉/精力低」那一档压到几句；
    max_chars_neutral 为 null 就是「中性档照旧用 reply.budget_by_tier 那一档」，不许在这里
    替它补一个默认数——那样人改了数据还以为生效了。判不了就当场报：缺一条阈值，这一轮要么
    白收紧、要么白放宽，两种都比报错难查。
    """
    if not isinstance(spec, dict):
        raise ValueError("装配说明缺 tone_rules（四个旋钮的阈值与词表）：%s" % where)
    clean: Dict[str, Any] = {}
    for name in ("low_energy_below", "low_mood_below", "neutral_mood_below"):
        value = spec.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("tone_rules.%s 要一个数：%s" % (name, where))
        clean[name] = float(value)
    low = spec.get("max_chars_low")
    if isinstance(low, bool) or not isinstance(low, int) or low <= 0:
        raise ValueError("tone_rules.max_chars_low 要正整数：%s" % where)
    clean["max_chars_low"] = low
    neutral = spec.get("max_chars_neutral")
    if neutral is not None and (isinstance(neutral, bool) or not isinstance(neutral, int)
                                or neutral <= 0):
        raise ValueError("tone_rules.max_chars_neutral 要么 null（照旧用档位预算），"
                         "要么正整数：%s" % where)
    clean["max_chars_neutral"] = neutral
    spare = spec.get("no_question_in_proactive", False)
    if not isinstance(spare, bool):
        raise ValueError("tone_rules.no_question_in_proactive 要么 true 要么 false"
                         "（缺省 false = 主动开口那一轮不删问句）：%s" % where)
    clean["no_question_in_proactive"] = spare
    words = spec.get("joke_words")
    if not isinstance(words, list) or not words:
        raise ValueError("tone_rules.joke_words 要一份非空的玩笑词表：%s" % where)
    clean_words: List[str] = []
    for word in words:
        if not isinstance(word, str) or not word.strip():
            raise ValueError("tone_rules.joke_words 里有空词：%s" % where)
        clean_words.append(word.strip())
    clean["joke_words"] = clean_words
    for name in ("question_chars", "sentence_end_chars", "ban_joke_note"):
        text = spec.get(name)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("tone_rules.%s 要一段非空的字：%s" % (name, where))
        clean[name] = text
    note = spec.get("rules_note")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("tone_rules.rules_note 要一句人话（这张表是干什么的）：%s" % where)
    clean["rules_note"] = note
    return clean


def _check_rules(spec: Any, name: str, where: str) -> List[str]:
    """reply 那两份说话规矩（format / format_blocked）同形：字符串列表，条目不许空。

    两份都要在数据里：blocked 那一态换的是**整份规矩**（动作那条从「怎么写」改成「不许写」），
    不在这里兜底补一条——数据缺了就当场报，缺的那一句会自动补成默认的话，人改不动它。
    """
    if not isinstance(spec, list):
        raise ValueError("reply.%s 要是字符串列表（空列表 = 不加规矩）：%s" % (name, where))
    clean: List[str] = []
    for rule in spec:
        if not isinstance(rule, str) or not rule.strip():
            raise ValueError("reply.%s 里有空的规矩：%s" % (name, where))
        clean.append(rule.strip())
    return clean


def _check_recent(spec: Any, where: str) -> Dict[str, Any]:
    """最近几轮那一段的三条规矩：读几轮、每侧截多少字、段尾那句模板。"""
    if not isinstance(spec, dict):
        raise ValueError("装配说明缺 recent（读几轮 / 截多少字 / notice 模板）：%s" % where)
    clean: Dict[str, Any] = {}
    for name in ("turns", "cap"):
        value = spec.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("recent.%s 要正整数：%s" % (name, where))
        clean[name] = value
    template = spec.get("notice")
    if not isinstance(template, str) or not template.strip():
        raise ValueError("recent.notice 要一句非空的模板：%s" % where)
    if recent.NOTE_SLOT not in template:
        raise ValueError("recent.notice 里缺 %s（不写占位符，那句话就永远是同一句假话）：%s"
                         % (recent.NOTE_SLOT, where))
    clean["notice"] = template
    return clean


def _check_banned(spec: Any, where: str) -> Dict[str, Any]:
    """近轮禁用那一格：抽她最近几轮、开场取几个字、那句禁令怎么说——三个都在数据里。"""
    if not isinstance(spec, dict):
        raise ValueError("装配说明缺 banned（rounds / opening_chars / note）：%s" % where)
    clean: Dict[str, Any] = {}
    for name in ("rounds", "opening_chars"):
        value = spec.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("banned.%s 要正整数：%s" % (name, where))
        clean[name] = value
    note = spec.get("note")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("banned.note 要一句非空的禁令：%s" % where)
    clean["note"] = note.strip()
    return clean


def _check_action(spec: Any, where: str) -> Dict[str, Any]:
    """动作池那一格：池子、规矩、配额都在数据里，代码里一个部位词、一个配额数都不许写死。"""
    if not isinstance(spec, dict):
        raise ValueError("装配说明缺 action（pool / rule / quota）：%s" % where)
    pool = spec.get("pool")
    if not isinstance(pool, list) or not pool:
        raise ValueError("action.pool 要一份非空的部位列表：%s" % where)
    clean_pool: List[str] = []
    for item in pool:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("action.pool 里有一项不是部位名：%r（%s）" % (item, where))
        clean_pool.append(item.strip())
    rule = spec.get("rule")
    if not isinstance(rule, str) or not rule.strip():
        raise ValueError("action.rule 要一句话（写法规矩，人能改）：%s" % where)
    return {"pool": clean_pool, "rule": rule.strip(),
            "quota": _check_quota(spec.get("quota"), where)}


def _check_quota(spec: Any, where: str) -> Dict[str, Any]:
    """动作配额的四个键：window / max 两个数，blocked / sparing 两句话。

    max 不大于 window 是硬规矩：窗口里一共只有 window 轮，配额比它还大就等于永远不禁，
    那四个键也就是摆设了。blocked / sparing 不许空——空话进提示词等于这一轮没给规矩。
    """
    if not isinstance(spec, dict):
        raise ValueError("action.quota 要给 {window, max, blocked, sparing}（动作配额的四个键）：%s"
                         % where)
    clean: Dict[str, Any] = {}
    for name in ("window", "max"):
        value = spec.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("action.quota.%s 要正整数：%s" % (name, where))
        clean[name] = value
    if clean["max"] > clean["window"]:
        raise ValueError("action.quota.max 不能大于 window（%d > %d）：%s"
                         % (clean["max"], clean["window"], where))
    for name in ("blocked", "sparing"):
        value = spec.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("action.quota.%s 要一句非空的话：%s" % (name, where))
        clean[name] = value.strip()
    return clean


def _check_anchors(spec: Any, where: str) -> Optional[Dict[str, Any]]:
    """锚句那一格：**可以没有**（没有就是「未配锚句」），写了就要写全。

    来源固定是家·仓库那五张台词卡（organs.home.anchors 的公开读口）：这一格不再认
    room / name —— 认了就成第二处「台词从哪来」的真源。
    """
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise ValueError("anchors 要一份对象（label / cap / inject / instruction）：%s" % where)
    clean: Dict[str, Any] = {}
    label = spec.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("anchors.label 要一段非空的字：%s" % where)
    clean["label"] = label.strip()
    cap = spec.get("cap")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise ValueError("anchors.cap 要正整数：%s" % where)
    clean["cap"] = cap
    inject = spec.get("inject")
    if not isinstance(inject, dict):
        raise ValueError("anchors.inject 要给 {fraction, suffix}（「只给半句」的规矩）：%s" % where)
    fraction = inject.get("fraction")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float))             or not 0 < float(fraction) <= 1:
        raise ValueError("anchors.inject.fraction 要是 (0, 1] 之间的数：%s" % where)
    suffix = inject.get("suffix")
    if not isinstance(suffix, str) or not suffix.strip():
        raise ValueError("anchors.inject.suffix 要一段非空的字（半句后面接什么）：%s" % where)
    clean["inject"] = {"fraction": float(fraction), "suffix": suffix}
    instruction = spec.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("anchors.instruction 要一句话（注入给她的行为规矩，人能改）：%s" % where)
    clean["instruction"] = instruction.strip()
    clean["guard"] = _check_guard(spec.get("guard"), where)
    clean["repeat"] = _check_repeat(spec.get("repeat"), where)
    clean["audit"] = _check_audit(spec.get("audit"), where)
    return clean


def _check_repeat(spec: Any, where: str) -> Dict[str, Any]:
    """近轮禁用扩到锚句那一格：window / max 两个数都在数据里，代码里一个都不许有。

    max 不能大于 window：窗口里一共只有 window 轮，同一张卡的次数到不了 max，那一格就是摆设。
    """
    if not isinstance(spec, dict):
        raise ValueError("anchors.repeat 要给 {window, max}（同一张卡最近几轮最多出现几次）：%s"
                         % where)
    clean: Dict[str, Any] = {}
    for name in ("window", "max"):
        value = spec.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("anchors.repeat.%s 要正整数：%s" % (name, where))
        clean[name] = value
    if clean["max"] > clean["window"]:
        raise ValueError("anchors.repeat.max 不能大于 window（%d > %d）：%s"
                         % (clean["max"], clean["window"], where))
    return clean


def _check_audit(spec: Any, where: str) -> Dict[str, Any]:
    """照抄审计的阈值：example_overlap 是「连着重合到几个字就判报台词」，正整数，缺了当场报。"""
    if not isinstance(spec, dict):
        raise ValueError("anchors.audit 要给 {example_overlap}（连着重合几个字就判报台词）：%s"
                         % where)
    value = spec.get("example_overlap")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("anchors.audit.example_overlap 要正整数：%s" % where)
    return {"example_overlap": value}


def _check_guard(spec: Any, where: str) -> Dict[str, Any]:
    """口癖护栏的六个门槛：缺一个、类型不对一律当场报——代码里不许有这些数的默认值。

    min_rounds 允许 0（等于不设小样本门槛），其余五个都必须是正整数；(0, 1] 只给阈值。
    """
    if not isinstance(spec, dict):
        raise ValueError("anchors.guard 要给 {window, threshold, opening_chars, "
                         "prefix_chars, reduced_count, min_rounds}（口癖护栏的门槛）：%s" % where)
    clean: Dict[str, Any] = {}
    for name in ("window", "opening_chars", "prefix_chars", "reduced_count"):
        value = spec.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("anchors.guard.%s 要正整数：%s" % (name, where))
        clean[name] = value
    min_rounds = spec.get("min_rounds")
    if isinstance(min_rounds, bool) or not isinstance(min_rounds, int) or min_rounds < 0:
        raise ValueError("anchors.guard.min_rounds 要非负整数（防小样本误触，0 等于不设门槛）：%s"
                         % where)
    clean["min_rounds"] = min_rounds
    threshold = spec.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
            or not 0 < float(threshold) <= 1:
        raise ValueError("anchors.guard.threshold 要是 (0, 1] 之间的数：%s" % where)
    clean["threshold"] = float(threshold)
    return clean


def _check_caller(spec: Any, where: str, wanted: bool) -> Optional[Dict[str, Any]]:
    """「这一轮是谁」那一格：**可以没有**（没有就不加这一节），写了就要三种形状各一句。

    三句的名字是这一层的判据（known / unknown / missing），不是场景名，所以不跟着
    brain_model.SCENE_TYPES 走；缺一句就当场报——补一句默认话，等于在数据之外又造了一处
    「来的是谁」的真源。relation_fallback / address_fallback / nickname_line 同理，一个都不许
    在代码里补默认：那三处都是文案，也正是上一版实盘 bug 的位置（昵称顶了 relation）。

    两条硬约束在这里就拦住，不等渲染：
      · 三句里不许有 {who}——号任何方向都不进提示词（她嘴里也不许出数字）；
      · known 必须有 {relation} 与 {address}——少了哪个,那个词就白算了，昵称就会顺手顶上去。
    """
    if spec is None:
        if wanted:
            raise ValueError("order 里有 caller，装配说明却缺 caller（三句话）：%s" % where)
        return None
    if not isinstance(spec, dict):
        raise ValueError("caller 要一份对象（lines 三句 + nickname_line + 两个兜底词）：%s"
                         % where)
    lines = spec.get("lines")
    if not isinstance(lines, dict):
        raise ValueError("caller.lines 要一份 {形状: 一句话} 的表：%s" % where)
    clean: Dict[str, str] = {}
    for name in CALLER_SHAPES:
        text = lines.get(name)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("caller.lines.%s 要一句非空的话：%s" % (name, where))
        if CALLER_WHO_TOKEN in text:
            raise ValueError("caller.lines.%s 里不许出现 {who}（号不进提示词，也不许念出来）：%s"
                             % (name, where))
        clean[name] = text.strip()
    for token in ("{relation}", "{address}"):
        if token not in clean["known"]:
            raise ValueError("caller.lines.known 里缺 %s（关系词与称呼各有各的来处，"
                             "不许拿昵称顶）：%s" % (token, where))
    nickname_line = _nonempty_text(spec.get("nickname_line"), "caller.nickname_line", where)
    if "{nickname}" not in nickname_line:
        raise ValueError("caller.nickname_line 里缺 {nickname}：%s" % where)
    relation_fallback = _nonempty_text(spec.get("relation_fallback"),
                                       "caller.relation_fallback（名片上没有关系词时用它）", where)
    address_fallback = _nonempty_text(spec.get("address_fallback"),
                                      "caller.address_fallback（名片上没填称呼时用它）", where)
    return {"lines": clean, "nickname_line": nickname_line,
            "relation_fallback": relation_fallback, "address_fallback": address_fallback}


def _nonempty_text(value: Any, name: str, where: str) -> str:
    """数据里的一句非空文案：缺了 / 空串 / 不是字符串当场报，并把这个键的名字点出来。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s 要一句非空的文案：%s" % (name, where))
    return value.strip()


def _check_scene(spec: Any, where: str) -> Dict[str, Any]:
    """场合那一格：三个场景各一句话，一句都不许缺。

    场景名取自 organs/brain/model.py 的 SCENE_TYPES（核里不另写一份 group / private / unknown）。
    缺一句就当场报——补一句默认话，就等于在数据之外又造了一处「什么时候说什么」的真源。
    """
    if not isinstance(spec, dict):
        raise ValueError("装配说明缺 scene（group / private / unknown 各说一句）：%s" % where)
    lines = spec.get("lines")
    if not isinstance(lines, dict):
        raise ValueError("scene.lines 要一份 {场景: 一句话} 的表：%s" % where)
    clean: Dict[str, str] = {}
    for name in brain_model.SCENE_TYPES:
        text = lines.get(name)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("scene.lines.%s 要一句非空的话：%s" % (name, where))
        clean[name] = text.strip()
    return {"lines": clean}


def _check_segment(spec: Any, where: str, wanted: bool) -> Optional[Dict[str, Any]]:
    """「这一小段」那一格：order 里有 segment 就要给 notice（三个场景各一句，人能改）。

    私聊 / unknown 两句允许是空串（那两态本来就不加这句话），group 那一句不许空——空了等于
    群聊多人时也不给那句话，而数据上看起来「配过了」。没配 segment 却把它留在 order 里同样当场报：
    缺一句就补一份默认话，那是数据之外的第二处真源。
    """
    if spec is None:
        if wanted:
            raise ValueError("order 里有 segment，装配说明却缺 segment（notice 三句话）：%s" % where)
        return None
    if not isinstance(spec, dict):
        raise ValueError("segment 要一份对象（notice：group / private / unknown 各一句）：%s" % where)
    notice = spec.get("notice")
    if not isinstance(notice, dict):
        raise ValueError("segment.notice 要一份 {场景: 一句话} 的表：%s" % where)
    clean: Dict[str, str] = {}
    for name in brain_model.SCENE_TYPES:
        text = notice.get(name)
        if not isinstance(text, str):
            raise ValueError("segment.notice.%s 要一句话（私聊 / unknown 可以是空串）：%s"
                             % (name, where))
        clean[name] = text.strip()
    if wanted and not clean[brain_model.SCENE_GROUP]:
        raise ValueError("segment.notice.group 要一句非空的话（群里多人那一条）：%s" % where)
    return {"notice": clean}


def normalize_segment(segment: Any) -> Optional[List[Tuple[str, str]]]:
    """把 segment 归一成 [(谁, 说了什么)]；没给（None）就是 None。

    收两种形状（通道与页面各写各的，服务端认这两种，别的一律不猜）：
      [{"speaker": "阿黄", "text": "我妈住院了"}, ...]   对象
      [["阿黄", "我妈住院了"], ...]                      二元组
    坏形状当场 ValueError，不兜底：不是 list、元素既不是对象也不是二元组、speaker 空、
    text 不是字或全空白——这几种都是**调用方写错了**，悄悄当空段就等于让她以为这一段没人说话。
    归一化是幂等的（交回来的 [(谁, 什么)] 再进来还是它自己），api 层归一过一遍、这里再判一遍。
    """
    if segment is None:
        return None
    if isinstance(segment, (str, bytes, Mapping)) or not isinstance(segment, (list, tuple)):
        raise ValueError("segment 要一份列表（每一条是 {speaker, text} 或 [speaker, text]）：%r"
                         % (segment,))
    rows: List[Tuple[str, str]] = []
    for index, one in enumerate(segment, 1):
        if isinstance(one, Mapping):
            speaker, said = one.get(SEGMENT_KEYS[0]), one.get(SEGMENT_KEYS[1])
        elif isinstance(one, (list, tuple)) and len(one) == 2:
            speaker, said = one[0], one[1]
        else:
            raise ValueError("segment 第 %d 条既不是对象也不是二元组：%r" % (index, one))
        if not isinstance(speaker, str) or not speaker.strip():
            raise ValueError("segment 第 %d 条没有 speaker（谁说的）：%r" % (index, one))
        if not isinstance(said, str) or not said.strip():
            raise ValueError("segment 第 %d 条没有 text（说了什么）：%r" % (index, one))
        rows.append((speaker.strip(), said.strip()))
    return rows


# —— 事件（核·识别）：这一句话对她意味着什么 ————————————

def _reply_window(cfg: Mapping[str, Any]) -> int:
    """冷落窗口要几轮她的回话：轮数**从识别数据里读**，核里不写死。

    判据是"最近 N 轮她的回话都是极短应答"，N 与词表同一份东西，改数据就改行为。
    取 rule == "window" 的那一类（窗口类判据不止 cold 一个时也按各自的数据走路）。
    """
    for kind in cfg["priority"]:
        rule = cfg["kinds"][kind]
        if rule.get("rule") == "window":
            return int(rule.get("window") or 0)
    return 0


def _her_replies(turns: Sequence[Mapping[str, Any]],
                 window: int) -> Tuple[str, List[str]]:
    """最近几轮**她那侧**的回话：(最后一句, 最后 window 句)——老的在前、新的在后。

    取的是 recent 那本账的 reply 栏（她那侧），不是人的原话：冷落窗口数的是她回了什么。
    窗口凑不满就凑不满（不拿一轮当 N 轮），短了照样原样交出去，由识别那一侧自己判。
    """
    rows = list(turns)
    if window > 0:
        rows = rows[-window:]
    replies = [str(row.get(recent.REPLY) or "") for row in rows]
    return (replies[-1] if replies else ""), replies


# —— 唯一入口 ————————————————————————————————————


def answer(text: str, who: str = "", *, scene: Optional[str] = None,
           segment: Optional[Any] = None,
           mode: str = events.MODE_PASSIVE,
           seed: Optional[str] = None, reason: Optional[str] = None,
           store: Any = None, brain: Any = None,
           heart_root: Any = None, recent_root: Any = None, usage_root: Any = None,
           drawer_root: Any = None,
           remind_root: Any = None,
           now: Optional[str] = None,
           transport: Optional[Any] = None, config: Optional[Mapping[str, Any]] = None,
           env: Optional[Mapping[str, str]] = None,
           assembly: Optional[Mapping[str, Any]] = None,
           aliases: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """装配一轮并说出口：返回页面要的那份成功体；模型这边的失败原样上抛（由 api 翻译）。

    store / brain 是底线：没有魂就没有底色，没有记忆就不开口——这里当场报，不假装说了一轮。
    now 给了就按它算（测试把时间往前推靠它），没给就用器官自己的钟（brain.model.now_iso）。
    transport / config / env 三个注入点原样转给 llm.complete：测试一律注入假 transport，
    绝不发真网络请求；本文件不写死任何模型名、地址与 key。
    recent_root：最近几轮那份账的落点（None → <器官目录>/mind/recent_turns.jsonl）。
    usage_root：口癖账的落点（None → <器官目录>/mind/anchor_usage.jsonl）。
    remind_root：记事那张卡的落点（手的实盘根：None → <器官目录>/hand，家按兄弟目录换算，
    与手那边同一条口径）——只有认作「要我记事」的那一轮才会落卡。
    drawer_root：恒定那篇的账本落点（None → <器官目录>/mind/drawer_usage.jsonl）——去重看的就是
    这一本账，它记的是「哪一篇哪一轮带过」，与提示词无关。
    assembly：给一份内存里的装配说明就用它（不读盘）——评测脚本靠它关掉某几段做空基线。
    aliases：通道顺手发来的**别名**（QQ 昵称、群名片那一类），只登记进人名录的别名表：
    id 才是身份（who 一个字节都不动），别名不改身份、不写记忆条、不进提示词。没给就是空（老调用
    一字不改）。它只做人名册的登记——**这一层不拿它认人**：回想、落脑、称呼仍只按 who 走。
    scene：这是什么场合（group / private / unknown，取值以 organs/brain/model.py 的 SCENE_* 为准）。
    缺省 / 空白 / 非法一律 unknown，**不猜成私聊**——猜错就是把私事说出去了。它一路往下走三处：
    装配加一段「场合」（文本来自 assembly.json 的 scene.lines）、落脑时写进那一条的 scene 栏、
    回想时交给脑（group 才把 audience=私密 的条目打上 retellable=False）。位置写在 order 里
    （出厂数据排在最后：说给谁、说什么都定了，最后交代在哪儿说）。
    segment：刚刚这一小段的发言（list of {"speaker","text"} 或 list of [speaker, text]；
    没给就是 None——私聊与老调用一字不改）。它**只**进「这一小段」那一节，不写进 recent：
    recent 是更早的那几轮，段是眼下这一小段，两份输入谁都不顶替谁（同一条消息不许既当段又当
    recent）。形状不对当场 ValueError，不兜底也不猜。

    mode：这一轮是谁起的头——events.MODE_PASSIVE（"passive"，人发消息触发的，/chat 走这条）
    或 events.MODE_PROACTIVE（"proactive"，她自己开口那条路），其它值一律回落 passive（口径在
    events.record 里，本层不再判第二遍）。它只进事件账的那一栏与回包 event.mode / mode：
    不进提示词、不动任何判据。

    seed / reason：**她自己开口**那一轮的两个入参。seed 是 None 就是被动（老调用一字不改）；
    不是 None 就是主动（回包 mode 与事件账那一栏因此都是 proactive，调用方传 passive 也不改口），
    此时：
      · text 让位给 seed（这一轮的「一问」就是由头，回想也用 seed），seed 空着当场 ValueError
        ——空由头不是「她想开口」，是调用方写错了；
      · segment 一律忽略（段是别人刚刚说的话，这一轮没有别人说话，见上面「主动开口」）；
      · 提示词用 assembly.json 的 prompt.proactive（占位符 {sections} / {reply_budget} /
        {reply_instruction} / {seed} / {reason}），user 那一问是空串；
      · reason 是理由码（参数里怎么给就怎么进提示词；没给就用 prompt.proactive_reason_missing）；
      · 心判决 intent=INTENT_PROACTIVE、被骂恒 False；
      · 落脑只有她那句（side=SIDE_SELF），recent 照记但 said 是空串。
    回包多两格：mode（这一轮谁起的头）与 seed{text, reason}（由头与理由码；被动两格都是空串）。

    **事件排在所有器官之前**（这一条必须留在这里：位置一挪，整轮读到的就是旧心情）：
    装配先调一次 events.record，它落一行事件账、并把事件投给心（praise 上行 / insult、cold
    下行），之后才读心、才回想、才写提示词——这一轮的口径档与限额因此吃得到刚发生的这件事。
    反过来它**一个字都不进提示词**：note / evidence 只落在账上，装配不因为多识别出一件事
    而多一节（order 与 sections 一位不动）。
    被骂那一门也与它同口径：engage 的 insulted_today 取 events.is_insult(text)，与 record
    用的是同一个函数——**全仓唯一被骂判据**，这里不许另写一张脏话表。

    返回（键名对外冻结，pages/chat.html 照它渲染）：
      ok / reply / chunks[{text,gap_before,send_at}] / scene{value} /
      event{kind,who,applied,reason,mode}（核·识别这一轮的读数：kind 是五类之一或空串
      ——空串＝没识别出，不是"被心拒了"，那两件事在 reason 上分得开；applied 是心接没接；
      mode 是这一轮落账用的那个值） /
      recalled[{id,text,source,source_type,evidence_key,score,who,note_display,
      retellable?,retell_note?}]（后两栏只在脑给了的时候带）/ heart{mood{tier,direction},
      voice{tone,direction,bans}, axes{轴: 档位词}, engage{state,reason}}
      / gate{decision, hit{clause_id,reason}|null, drops{today}} /
      assembly{order, sections[{key,chars,cap}]（抽屉那一节另带 count：这一轮真放了几篇）,
      reply_budget{key,tier,chars,tier_chars}} /
      tone{tier,label,softness,max_chars,no_question,ban_joke,truncated,heart_source}
      （四个旋钮落到这一轮的实话：max_chars 是硬截用的那个数，truncated 说截了没有） /
      continuity{turns_loaded, omitted, notice, reconciled, rounds[{ts,fidelity,msg_id}]} /
      banned{rounds, action_lines[{text,count}], openings[开场前 N 字]} /
      action{pool, rule, quota{window,max,used,blocked}} /
      llm{model, temperature, temperature_tier} /
      anchors{configured, count, available, injected, instruction, items[{id, phrase, injected,
      when}] 实际注入的, skipped[没注入的 id], banned_by_repeat[被近轮禁用挡住的 id]|note} /
      audit{decision, kind, card_id, overlap}（照抄审计：kind 是 example / phrase / none，
      decision 是 pass / rewritten / dropped，与上面 gate 那个 decision 各说各的） /
      guard{window, threshold, min_rounds, rounds, hits, rate, over, injected, available} /
      memory{wrote[{id,who}]}
      另带一个**私有键** "_gate_input"：这一轮判决用的那一份 EngageInput（对象本身，不是 dict）。
      它只给 api 层的出站闸（organs/heart/gate.py）用——门判「发不发」必须用同一份输入，
      不给它就得在 api 层另造一套。**回包前必须 pop 掉，绝不进 JSON**：它不是给页面看的。

    guard 的读数一律是**这一轮之前**的滚动窗口（口癖账还没记这一轮），八个键对外冻结。
    落账放在最后一步：只有过了闸、reply 非空的那一轮才有「开头」可算——
    被闸弃掉的轮（reply 空）**不记口癖账**，她没说出口，就没有开头。
    """
    if store is None:
        raise ValueError("魂未载入（store=None）：没有魂就没有装配的底色")
    if brain is None:
        raise ValueError("脑未载入（brain=None）：没有记忆就不开口")
    # 主动那一轮没有「对方原话」：由头（seed）就是这一轮的一问，text 让位给它。
    proactive = seed is not None
    # 给了 seed 就是主动，所以这一轮落账的 mode 也是 proactive——调用方传了 passive 也不改口：
    # 回包与事件账上的那一栏说「被动」，这一轮却是她自己起的头，那就是记错账。
    if proactive:
        mode_value = events.MODE_PROACTIVE
    else:
        mode_value = mode if mode in events.MODES else events.MODE_PASSIVE
    said = str(text or "").strip()
    if proactive:
        said = str(seed or "").strip()
        if not said:
            raise ValueError("seed 不能为空：她自己开口那一轮，由头就是那一问")
    elif not said:
        raise ValueError("text 不能为空：没有一句话就没有这一轮")
    reason_text = str(reason or "").strip() if proactive else ""
    # 段先归一化：形状不对就在动任何器官之前当场报（不兜底，也不把它悄悄当空段）。
    # 主动那一轮**段一律不认**（给了也忽略，连形状都不判）：这一轮没有别人说话。
    rows = [] if proactive else (normalize_segment(segment) or [])
    conf = _check_assembly(assembly, "传入的 assembly") if assembly is not None else load_assembly()
    at = now if isinstance(now, str) and now else brain_model.now_iso()
    # 场合先归一化：后面三处（装配 / 落脑 / 回想）都只认这一个值，不各自再判一遍。
    where = _scene_value(scene)
    # 别名先登记（排在读人名录之前）：这一轮的称呼与「关于他」因此当场看得到昵称 / 群名片，
    # 不是下一轮才生效。**只登记别名**——身份还是 who，记忆条一条都不写（见 _register_aliases）。
    _register_aliases(brain, who, aliases)
    # 0.1) 「要我记事」那一轮：**装配之前**先认一次（不进提示词，一个字都不进这一轮的装配）。
    #      认出来就先落卡（手·add_task），没认出时刻只反问、不落卡；普通闲聊这里是 None，
    #      这一轮因此一个字节都不变（口径与文案见文档「要我记事」那一节）。
    #      主动那一轮不认：由头是她自己的念头，没有人在这句话里让她记什么。
    remind_read = (None if proactive
                   else remind.handle(said, who=who, now=at, root=remind_root))

    # 0) 最近几轮先读、先对账：脑里记了、这份账上却没落的轮，**这一轮就得看见**。
    #    这一读必须排在 recall 之前——recall 会按它自己的钟顺手换日（换日就把「今日在役」
    #    那几条归档了），排在它后面读到的是换过之后的那一天，补记于是永远补不上。
    loaded = recent.load(root=recent_root, turns=conf["recent"]["turns"])
    # 她自己的自称表现读一次：对账要靠它把「她自己的回话」认出来（见 _reconcile 第三条剔除）
    her_names = _her_names(store)
    loaded = _reconcile(brain, loaded, recent_root, conf, at, her_names)

    # 0.5) 事件：这一句话对她意味着什么——**排在任何一个器官读数之前**（心读数之前、回想之前）。
    #     record 只做两件事：落一行事件账、把事件投给心（praise 上行 / insult、cold 下行），
    #     所以它必须在读心之前：这一轮的口径档与限额才吃得到刚发生的这件事。反过来，它
    #     **一个字都不进提示词**（note / evidence 只在账上），order 与 sections 一位不动。
    #     她那几句回话从 recent 那本账取：窗口 N 从识别数据来（_reply_window），不在这里写死。
    #     一份规则快照同时给识别与窗口用：两份读数各读一次数据，迟早一处按旧窗口、一处按新窗口。
    event_conf = events.load_config()
    last_reply, recent_replies = _her_replies(loaded.get("turns") or [],
                                              _reply_window(event_conf))
    event = events.record(said, last_reply=last_reply, scene=where, who=who, at=at,
                          mode=mode, recent_replies=recent_replies, data=event_conf)
    # 被骂那一门与上面这条事件同一个函数（events.is_insult）：两处各写一份判据，迟早就分叉。
    # 主动那一轮恒 False：没有人说话，就没有人骂她。拿她自己的由头去判，等于让她自己把这一轮
    # 判成「被骂」（那是心的一道硬门，判错了整轮都变了口径）。
    insulted = False if proactive else events.is_insult(said, data=event_conf)

    # 1) 魂与脑：先回想（这一轮该想起什么，要在写提示词之前定下来）
    soul_text = store.render()
    recalled, memory_text = _recall(brain, said, who, where)

    # 1.5) 锚句与连续性：几个读数都在写提示词之前定下来
    omitted = int(loaded.get("omitted") or 0)
    note = recent.notice(omitted, conf["recent"]["notice"])
    recent_text = _recent_text(loaded.get("turns") or [], note, conf)
    # 近轮禁用：原文段一个字都不删，禁令另起一节，只抽她那一侧（store 里她的自称就是她）
    her_name = store.snapshot().identity.display
    history = loaded.get("turns") or []
    banned_text, banned_read = _banned_text(history, conf["banned"], her_name)
    # 动作配额：数最近 quota.window 轮里有几轮带动作（轮数不是行数，口径与禁令同一份）
    action_text, action_read = _action_text(
        conf, repeat.count_with_actions(history, conf["action"]["quota"]["window"], her_name))
    # 口癖护栏：先读**这一轮之前**的滚动窗口，再定这一轮注入几条（先记后算会自己踩自己）。
    # 家现行台词读一次，注入按 guard 裁，口癖统计仍看全量——两件事各用各的那一份。
    # 近轮禁用（anchors.repeat）也是「这一轮之前」的读数：同一本账，按 id 数次数。
    anchors_rows = _anchor_rows()
    guard_conf = _guard(conf)
    guard_read = (usage.report(guard_conf, injected_available=len(anchors_rows),
                               root=usage_root)
                  if guard_conf is not None else _guard_missing())
    anchor_text, anchor_read = _anchor(conf, anchors_rows, guard_read["injected"],
                                       _anchor_repeats(conf, usage_root))

    # 2) 心：唯一合成点 + 一次判决；坏卡降级成只要 mood 那一块
    mood_read = mood_module.read(heart_root, at)
    offsets = offset_module.overview(root=heart_root, now=at)
    eff = _effective(heart_root, at, mood_read, offsets)
    energy_read = energy_module.read(heart_root, at, **_sleep_terms(heart_root, at))
    # 主动那一轮把 intent 交给心：夜里那一道硬门只对主动判「不说」（被动判「少说」）——
    # 同样是夜里，一样的话，两种起头就该两个结论。这是「夜里主动 → SILENT」生效的前提。
    gate_input, judgement = _engage(
        heart_root, at, eff, energy_read, brain, who, insulted,
        intent=(heart_model.INTENT_PROACTIVE if proactive else heart_model.INTENT_PASSIVE))
    heart_words = _heart_words(eff, judgement)
    heart_text = _heart_text(eff, heart_words)

    # 3) 嘴：称呼 → 语气档（口径行进提示词）。心的四样读数**真传进去**：心情凉得能压住话长、
    #    精力低得能收紧口径，softness 才不是一句空话（口径行的 heart_source 也从「未接入」变实话）。
    heart_read = _heart_readings(mood_read, energy_read, offsets, judgement)
    # 2.5) 抽屉：恒定那篇每轮都带；命中关键词再取 2–3 篇（两段各占一节、各一个上限）。
    #      放在 memory 之后、heart 之前（order 里也是这个位置）：旧事比当下记忆远一档。
    #      旧事正文**只进提示词**：这一节不交给 brain.ingest 一个字。
    drawer_read, drawer_always_text, drawer_text = _drawer(conf, said, where, at=at,
                                                           root=drawer_root)
    voice = _voice(store, brain, who, heart_read)
    # 四个旋钮：判据全在 assembly.json 的 tone_rules 里，这一层只照着判，一个阈值都不带。
    knobs = _knobs(conf["tone_rules"], heart_read, judgement, mode)
    mouth_text = _mouth_text(voice, knobs, conf["tone_rules"])
    person_text = _person_text(brain, who)
    # 「这一轮是谁」：恒在（三种之一），只看号、不看这一轮的原话——她认下「我是主人」时，
    # 手里得先有一句反证材料：号是号，自称是自称。正文里一个数字都没有（号不进提示词），
    # 号留在 caller_read.who 里给回包 / 面板 / 账用。
    caller_text, caller_read = _caller_read(conf, brain, who)

    # 4) 提示词与回复预算
    tier = heart_words["mood"]["tier"]
    # 配额满了就换另一份说话规矩：动作那条从「怎么写」改成「不许写」——格式要求本身也在
    # 催她每轮写一行动作，只留一句禁令压不住它。
    rules = (conf["reply"]["format_blocked"] if action_read["quota"]["blocked"]
             else conf["reply"]["format"])
    reply_rule = "\n".join([conf["reply"]["instruction"]] + list(rules))
    budget = conf["reply"]["budget_by_tier"].get(tier, conf["reply"]["default"])
    # 这一轮的硬上限：低档压到 max_chars_low，中性档看 max_chars_neutral（null 就是照旧）。
    # {reply_budget} 与后面那一步的硬截用的是同一个数——提示词说 60、手里截 220 就成了两套口径。
    max_chars, capped = _max_chars(budget, knobs, conf["tone_rules"])
    contents = {"soul": soul_text, "anchors": anchor_text, "recent": recent_text,
                "segment": _segment_text(rows, conf, where),
                "banned": banned_text, "action": action_text,
                "memory": memory_text, "drawer": drawer_text,
                "drawer_always": drawer_always_text,
                "heart": heart_text, "mouth": mouth_text,
                "person": person_text, "caller": caller_text,
                "scene": _scene_text(conf, where)}
    blocks: List[str] = []
    sections: List[Dict[str, Any]] = []
    for key in conf["order"]:
        spec = conf["sections"][key]
        body = contents.get(key, "")
        # 抽屉这两段谁没翻谁就**一节都不加**：不留一个只有标题的空节（她会以为旧事是空的）。
        if key in (DRAWER_KEY, DRAWER_ALWAYS_KEY) and not body:
            continue
        clipped = body if len(body) <= spec["cap"] else body[:spec["cap"]]
        blocks.append("[%s]\n%s%s" % (spec["label"], _notice_head(spec), clipped))
        row = {"key": key, "chars": len(clipped), "cap": spec["cap"]}
        if key == DRAWER_KEY:
            row["count"] = drawer_read["count"]      # 这一轮真放进几篇（页面照它显示）
        if key == DRAWER_ALWAYS_KEY:
            row["count"] = 1 if drawer_always_text else 0   # 恒定那篇：一篇或没有
        if key == CALLER_KEY:
            # 这一轮是谁的读数：号**只在这一行**（回包 / 面板 / 账看得到），提示词里一个数字都没有。
            # 不另开顶层键：成功体的键是冻结契约，这一格挂在已有的 assembly.sections 里。
            row.update(caller_read)
        sections.append(row)
    # 主动那一轮换的是**整份模板**（prompt.proactive：由头与理由都进提示词），user 那一问是空串
    # ——没有对方的话，就没有可替换的 {text}。两态只差模板与那一问，装配出来的几段一模一样。
    values = {"sections": "\n\n".join(blocks), "reply_budget": str(max_chars),
              "reply_instruction": reply_rule}
    if proactive:
        values["seed"] = said
        values["reason"] = reason_text or str(conf["prompt"]["proactive_reason_missing"])
        template = conf["prompt"]["proactive"]
        user = ""
    else:
        template = conf["prompt"]["system"]
        user = _fill(conf["prompt"]["user"], {"text": said})
    system = _fill(template, values)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    # 4.5) 温度分档：两个温度与触发条件都从 data/llm.json 读，代码里没有字面量
    call_conf = llm.resolve_config(config)
    policy = llm.temperature_policy(config)
    temperature_tier, temperature = llm.choose_temperature(
        policy, recalled_nonempty=bool(recalled), reply_budget=max_chars)

    # 5) 发模型（LLMError 原样上抛）
    said_back = llm.complete(messages, config=call_conf, env=env, transport=transport,
                             temperature=temperature)

    # 5.1) 认作「要我记事」的那一轮，嘴里那句就是回执（文案来自 remind.json）：模型那句不用——
    #      这一轮要说的话在落卡那一刻就已经定了，再让她自由发挥就有了两种说法。
    #      排在闸与照抄审计之前：这句话一样要过闸、一样要分包（她一样是在说话）。
    if remind_read is not None:
        said_back = str(remind_read.get("say") or "")

    # 5.5) 硬兜底：配额满了就替她把动作行删掉（不靠模型自觉）——删后的文本往下走全程
    said_back = _strip_blocked_actions(said_back, action_read)

    # 6) 过嘴：八股闸（闸那一层的重写器传 None：命中即弃——这一层只判「怎么说」）
    trace = drops_module.path_of()
    verdict, ledger = _through_mouth(said_back, trace)
    dropped = verdict.decision == "dropped"
    reply = "" if dropped else verdict.text
    hit = verdict.hits[0] if verdict.hits else None

    # 6.5) 照抄审计（两级）：与某张卡的 example 连着重合到阈值 → 判「报台词」，改一次；仍重合就弃。
    #      放在闸之后：被闸弃掉的那一句已经不发了，没有抄没抄可判。只命中 phrase 的放行、只记账，
    #      那一份读数（phrase_hit）原样交给第 8 步落账——两处不许各算一遍判据。
    audit_read, phrase_hit = _audit_clear(), None
    if not dropped and reply and isinstance(conf.get("anchors"), Mapping):
        audit_read, phrase_hit, reply, dropped = _copy_audit(
            reply, anchors_rows, conf, _copy_rewriter(transport, call_conf, env), _sink(trace))
    # 6.9) 四个旋钮落到这一句上：删玩笑句、删问句、按上限在句末截断。
    #      排在闸与照抄审计之后（被弃掉的句子没人听得见）、分包之前（拿改过的句子算分包）。
    # 记事那一轮的两个删除旋钮**不锯回执**：回执是固定的那一句（「记着了…」/「几点？…」），
    # 被「少说」删掉前半句就成了看不懂的半句——这一轮她说的是「记了没有」，不是闲聊。
    receipt_knobs = (dict(knobs, no_question=False, ban_joke=False)
                     if remind_read is not None else knobs)
    shaped = _shape_reply(reply, max_chars, capped, receipt_knobs, conf["tone_rules"])
    reply = shaped["reply"]
    chunks = [] if dropped else _bubbles(reply, who, at)

    # 7) 落脑：人的原话先记，她真说出口的那句再记（被弃的那句没说出口，不进脑）
    wrote, msg_id = _remember(brain, store, who, said, reply, dropped, at, where,
                              proactive=proactive)
    # 7.5) 落账：这一轮的原文进最近几轮（reply 不管有没有说出口，人那句一定留着）。
    # msg_id 取人那句进脑回来的 id：下一次装配对账就靠它把「脑里有、账上也有」认出来。
    # 主动那一轮也照记一行（她这一轮同样是一轮），但 said 记空串：这一轮**没有对方原话**，
    # 不拿由头去冒充「他说了什么」——那是她的念头，不是人的话。
    recent.append({"ts": at, "who": who or HUMAN_WHO,
                   "said": "" if proactive else said, "reply": reply,
                   "msg_id": msg_id}, root=recent_root, cap=conf["recent"]["cap"])
    # 8) 落口癖账：放在最后一步。被闸弃掉的轮（reply 空）不记——她没说出口就没有开头；
    #    命中一律对**家现行全部台词**，不是只注入的那几条（口癖统计看的是全部台词）。
    if not dropped and reply and isinstance(guard_conf, Mapping):
        # 命中哪一张（id）用的是照抄审计那一步算过的同一份读数：两处各算一遍，迟早一处按 phrase、
        # 一处按老 say。名字也不与上面的闸命中（hit）撞车——两件事互不相干，共用一个变量就会互相踩。
        spoken = phrase_hit or {}
        usage.append(bool(phrase_hit),
                     usage.opening(reply, int(guard_conf["opening_chars"])),
                     str(spoken.get(usage.SCENE) or ""), root=usage_root,
                     card_id=str(spoken.get(usage.CARD_ID) or ""),
                     phrase=str(spoken.get(usage.PHRASE) or ""))
    # 8.5) 落语气账：每轮一行（被闸弃掉的那轮也有一行——那一轮同样判过四个旋钮）。
    _log_tone(_tone_row(at, heart_words, energy_read, voice, shaped, max_chars, knobs))

    return {
        # 私有键（带下划线）：这一轮判决用的那一份 EngageInput，只给 api 层的出站闸用——
        # 回包前必须 pop 掉，绝不进 JSON（它不是给页面看的东西，也没法序列化）。
        "_gate_input": gate_input,
        "ok": True,
        "reply": reply,
        "chunks": chunks,
        "scene": {"value": where},
        # 这一轮是谁起的头（passive / proactive）。页面与验收照这一格分两条路。
        "mode": mode_value,
        # 由头原样与理由码（被动两格都是空串，形状不随状态变）。理由码只在这一格：
        # 下面 payload.reason 一直是**出站闸**的理由码，两处不共用一个字段。
        "seed": {"text": said if proactive else "",
                 "reason": reason_text},
        # 核·识别这一轮的读数（五键冻结）：kind 是五类之一或空串——空串＝"没识别出"，
        # 与"心拒了"是两件事，reason 上分得开。它只落账与投心，一个字都没进提示词。
        "event": {"kind": str(event.get("kind") or ""),
                  "who": str(event.get("who") or ""),
                  "applied": bool(event.get("applied")),
                  "reason": str(event.get("reason") or ""),
                  "mode": str(event.get("mode") or "")},
        "recalled": recalled,
        "heart": heart_words,
        "gate": {
            "decision": verdict.decision,
            "hit": {"clause_id": hit.clause_id, "reason": hit.reason} if hit else None,
            "drops": {"today": int(ledger.get("today") or 0)},
        },
        # 照抄审计的读数（四键冻结）：decision 是这一档自己的判决，与上面闸那个 decision 各说各的
        # ——闸判「怎么说」，这一档判「说的是不是背来的台词」。
        "audit": audit_read,
        "assembly": {
            "order": list(conf["order"]),
            "sections": sections,
            # chars 是这一轮**真正**的上限（低档已收紧）：提示词与硬截用的是同一个数。
            "reply_budget": {"key": REPLY_KEY, "tier": tier, "chars": max_chars,
                             "tier_chars": budget},
        },
        # 嘴这一格给页面看的四样：哪一档、多软、上限几位、两个开关、截没截、心到底接上没有。
        "tone": {"tier": voice.tier, "label": voice.label, "softness": voice.softness,
                 "max_chars": max_chars, "no_question": bool(knobs["no_question"]),
                 "ban_joke": bool(knobs["ban_joke"]), "truncated": bool(shaped["truncated"]),
                 "heart_source": voice.heart_source},
        "continuity": {"turns_loaded": len(loaded.get("turns") or []),
                       "omitted": omitted, "notice": note,
                       "reconciled": recent.reconciled(history),
                       "rounds": [{"ts": str(row.get(recent.TS) or ""),
                                   "fidelity": str(row.get(recent.FIDELITY) or recent.LIVE),
                                   "msg_id": str(row.get(recent.MSG_ID) or "")}
                                  for row in history]},
        "banned": banned_read,
        "action": action_read,
        "llm": {"model": call_conf["model"], "temperature": temperature,
                "temperature_tier": temperature_tier},
        "anchors": anchor_read,
        "guard": guard_read,
        "memory": {"wrote": wrote},
        # 「要我记事」那一轮才有的那一格（普通闲聊一个字节都不多）：见文档那一段。
        **({"remind": remind.packet(remind_read)} if remind_read is not None else {}),
    }


# —— 各段 ————————————————————————————————————


def _recent_text(turns: List[Mapping[str, Any]], note: str,
                 conf: Mapping[str, Any]) -> str:
    """最近几轮这段进提示词的样子：一条一轮的原文（谁 + 原话），段尾接「更早还有 N 轮」。

    notice 先占位再截正文：段尾那句是这一轮最要紧的一句，被 cap 锯掉就等于没装进去。
    """
    body = recent.render(turns) or NO_TURNS
    spec = conf["sections"].get("recent")
    if not note:
        return body
    if not isinstance(spec, Mapping):
        return body + "\n" + note
    cap = spec["cap"]
    room = cap - len(note) - 1
    if room <= 0:
        return note[:cap]
    head = body if len(body) <= room else body[:room]
    return head + "\n" + note


def _segment_text(rows: List[Tuple[str, str]], conf: Mapping[str, Any],
                  scene: str) -> str:
    """「这一小段」这一段进提示词的样子：一行一个人说的一句，段空着就写一句实话。

    每行「谁：说了什么」（没有项目符号：与 recent 那一节的「- 谁：什么」一眼分开）。
    段空着 / 没给就写 NO_SEGMENT——这一节不许留白：空白会被读成「这一段没人说话」，
    而「没给段」是另一回事，得让她看得见。

    段尾那句「几个人在说话」（segment.notice）只在这一轮是群聊、且段里至少两个人说话时接，
    先给它留位置再截正文（真话被 cap 锯掉就等于没装进去）。
    """
    body = "\n".join(SEGMENT_LINE % (who, said) for who, said in rows) or NO_SEGMENT
    notice = _segment_notice(rows, conf, scene)
    if not notice:
        return body
    spec = conf["sections"].get("segment")
    if not isinstance(spec, Mapping):
        return body + "\n" + notice
    room = spec["cap"] - len(notice) - 1
    if room <= 0:
        return notice[:spec["cap"]]
    head = body if len(body) <= room else body[:room]
    return head + "\n" + notice


def _notice_head(spec: Mapping[str, Any]) -> str:
    """段首那句说明（接在标签之后、正文之前）：有就带一个换行，没有就是空串。

    魂那一段现在带着一句「这些是你的底子，不是要背出来的话——别照着念。」：魂六件是人手写给
    她的底子，整段照抄进提示词时读起来与台词一样，她会照着念出来。这句话是**说明**，不是内容，
    所以不占 sections.soul.cap 的字数（cap 量的仍是正文），也不进任何账。
    """
    notice = spec.get("notice")
    return "%s%s" % (notice, LINE_JOIN) if notice else ""


def _segment_notice(rows: List[Tuple[str, str]], conf: Mapping[str, Any],
                    scene: str) -> str:
    """段尾那句话：只有「群聊 + 这一段里至少两个人说话」才给，其余一律空串。

    两个条件缺一不可：私聊里没有「他们互相说」，单人段也没有——那两态加这一句，等于告诉她
    有人在跟她对话，或有人在互相说话，都是编出来的场面。说话者按**不同的 speaker** 算。
    """
    spec = conf.get("segment")
    if not isinstance(spec, Mapping) or scene != brain_model.SCENE_GROUP:
        return ""
    if len({who for who, _said in rows}) < 2:
        return ""
    return str(spec["notice"].get(brain_model.SCENE_GROUP) or "")


def _her_names(store: Any) -> set:
    """她的自称表（identity.display + synonyms），从魂的公开只读口（snapshot）现读。

    对账要用它把「她自己的回话」认出来——脑里那几条早年 side 还没接上，被记成了
    他说的；补回来就是拿她说的话当对方说的，比漏补更糟。读不到魂就返回空集：
    对账宁可少挡（下一条 ts 规则多半还挡得住），也不许因此让她闭口。
    """
    try:
        identity = store.snapshot().identity
        return {str(identity.display)} | {str(name) for name in identity.synonyms}
    except Exception:      # noqa: BLE001
        return set()


def _reconcile(brain: Any, loaded: Mapping[str, Any], root: Any,
               conf: Mapping[str, Any], at: str,
               her_names: Any = ()) -> Dict[str, Any]:
    """对账：脑里今日「他说」的条，最近几轮这份账上没有的（按 msg_id 对）回填进来。

    为什么要对：脑和这份账是两次写。进程重启、写盘失败时，脑里已经记了这一轮，
    这份账上却没有——模型于是以为那一段没说发生过。补回来是**补数据**，
    所以补的每一条都标 fidelity="reconciled"，由 recent.render 在提示词里带「（补记）」；
    她那句一律记空串，渲染成「（没有记到她那句）」——不替她编一句她没说过的话。

    **先认领旧行，再对账**：升级前写下的行没有 msg_id，只按 msg_id 对会把它们全当成
    丢了的轮、全量标成「（补记）」——误标与不标一样坏，而且更糟（她与人都会以为那些是补的）。
    所以先拿 (ts, said) 给旧行补上凭据（fidelity 不动，仍是 live，认领不出声），
    认领完还剩下的才是真缺的，才回填、才标补记、才打那一行 warning。

    三条剔除（都是「这一轮其实记过 / 根本不是刚才」）：
    · msg_id 已经在了：上一次回填的、或这一轮亲历落的——幂等靠它，不靠重新算；
    · ts 与账上任何一行完全相同：同一轮。早些年 her 的回话被错标成 side=他说，
      那几条的 ts 就是账上人那一轮的时刻——补进来等于把一轮写成两轮（第三种误标）；
    · ts 晚于本轮装配时刻 at：明天才发生的事不是「刚才那一轮」，补进来她就会把
      还没发生的事当成刚说过；
    · who 是她自己（魂的 identity.display / synonyms 现读）：那本来是**她的回话**，
      早年 side 还没接上才被记成「他说」。补进来等于拿她说的话当对方说的——
      比漏补更糟，而且发言者会写成她的名字。

    时段：只看 ts >= 最近 N 轮里最早那一轮（recent 为空就取今日全部）。
    对不上要留痕：有回填就打一行 warning，带「缺 N 轮」；N=0 一个字都不打（不刷屏）。
    """
    names = set(her_names or ())
    said_rows = [row for row in (_said_row(entry) for entry in _said_today(brain, at))
                 if row["who"] not in names]
    # 旧行认领：这一步不出声（它不是「缺」，是当年没有凭据）；认领过就重读，后面按新凭据对。
    if recent.claim(said_rows, root=root, cap=conf["recent"]["cap"]):
        loaded = recent.load(root=root, turns=conf["recent"]["turns"])
    turns = list(loaded.get("turns") or [])
    known = {str(row.get(recent.MSG_ID) or "") for row in turns} - {""}
    taken = set(recent.stamps(root=root))      # 整份账的时刻，不只是读进提示词那几轮
    since = str(turns[0].get(recent.TS) or "") if turns else ""
    missing = [row for row in said_rows
               if row["msg_id"] and row["msg_id"] not in known
               and (not since or row["ts"] >= since)
               and row["ts"] not in taken          # 同 ts 即同一轮：不重复补
               and not _after(row["ts"], at)]      # 未来时刻不是「刚才」
    if not missing:
        return dict(loaded)
    missing.sort(key=lambda row: row["ts"])
    written = recent.insert_reconciled(
        [{"ts": row["ts"], "who": row["who"], "said": row["said"], "reply": "",
          "msg_id": row["msg_id"]} for row in missing],
        root=root, cap=conf["recent"]["cap"])
    if not written:
        return dict(loaded)
    # 一行就够：带「缺 N 轮」，N 是这一次真补进去的条数（不是猜的条数）
    LOGGER.warning("最近几轮与脑对不上：缺 %d 轮（已从脑回填，标为补记，她那句记空）",
                   len(written))
    return recent.load(root=root, turns=conf["recent"]["turns"])


def _after(stamp: str, at: str) -> bool:
    """stamp 是不是晚于本轮装配时刻；能解析就按真时刻比，解析不了退回字符串比。"""
    try:
        return datetime.fromisoformat(stamp) > datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return stamp > at


def _said_today(brain: Any, at: str) -> List[Mapping[str, Any]]:
    """脑里今日在役的「他说」那几条；脑没这条通道（假脑 / 只读一半）就当他没说过。"""
    reader = getattr(brain, "today_entries", None)
    if not callable(reader):
        return []
    try:
        try:
            entries = reader(at)
        except TypeError:      # 只认一个可选时刻的假脑：退回不带参数那一次
            entries = reader()
    except Exception:      # noqa: BLE001  对账读不动不该让她闭口：这一轮照旧装配
        return []
    return [entry for entry in (entries or [])
            if isinstance(entry, Mapping)
            and str(entry.get("side") or brain_model.SIDE_OTHER) == brain_model.SIDE_OTHER]


def _said_row(entry: Mapping[str, Any]) -> Dict[str, str]:
    """脑里一条「他说」变成这一层认的形状：ts / who / said / msg_id 四个键。

    认领与回填共用这一份口径：两处各自拼一遍，迟早一处用 text 一处用 quote。
    """
    return {"ts": str(entry.get(recent.TS) or ""),
            "who": str(entry.get("who") or ""),
            "said": str(entry.get("text") or entry.get("quote") or ""),
            "msg_id": _entry_id(entry)}


def _entry_id(entry: Mapping[str, Any]) -> str:
    """脑里那一条的 id：亲历条的 evidence_key 就是它自己，缺了退到 id（与 _wrote_row 同口径）。"""
    return str(entry.get("evidence_key") or entry.get("id") or "")


def _banned_text(turns: List[Mapping[str, Any]], conf: Mapping[str, Any],
                 her_name: str) -> Tuple[str, Dict[str, Any]]:
    """近轮禁用这一段进提示词的样子：那句禁令 + 逐条列出她刚用过的动作行与开场。

    只抽她那一侧（repeat.her_texts），去重计次，重复几遍照实标出来——「用了一次」与
    「用了三次」是她这一轮该躲的两件事。开场是 usage.opening 的口径（去标点后前 N 字），
    本层不另写一套去标点。

    读出来的那一份原样交回调用方进返回体（页面与验收要看它到底禁了什么）；
    一条都抽不出来时写一句实话，不留白——空白她会读成「随便用」。
    """
    got = repeat.banned(turns, conf, her_name)
    lines = [str(conf["note"])]
    for row in got["action_lines"]:
        mark = repeat.REPEAT % row["count"] if row["count"] > 1 else ""
        lines.append("- %s%s%s" % (repeat.ACTION_PREFIX, row["text"], mark))
    for head in got["openings"]:
        lines.append("- %s%s" % (repeat.OPENING_PREFIX, head))
    if len(lines) == 1:
        lines.append(BANNED_EMPTY)
    return "\n".join(lines), got


def _action_text(conf: Mapping[str, Any], used: int) -> Tuple[str, Dict[str, Any]]:
    """动作池这一段进提示词的样子（两态）与返回体那一份 action。

    未达 quota.max：规矩 + quota.sparing + 部位池，三样都照抄 assembly.json——只说「写她自己的
    身体」，她就一直用尾巴；给了池子她才得挑。（旧口径「这一轮换一个部位」被她读成「每轮都要
    写一个」，六轮六次带动作，所以现在的规矩是「偶尔才写」，配额这一层替她数。）
    已达 quota.max：**整段只写 quota.blocked 那一句**，池子与规矩一个字都不给——给了她就会挑
    一个写，而这一轮该写的正是零个。

    返回体那一份照样三键齐全（pool / rule / quota）：blocked 时 pool 给空列表而不是 None，
    页面照同一套键渲染，不必为这一态另写一支。
    """
    spec = conf["action"]
    quota = spec["quota"]
    blocked = used >= quota["max"]
    read = {"pool": [] if blocked else list(spec["pool"]), "rule": spec["rule"],
            "quota": {"window": quota["window"], "max": quota["max"],
                      "used": used, "blocked": blocked,
                      "stripped": 0, "kept_because_empty": False}}
    if blocked:
        return quota["blocked"], read
    return ("%s\n%s\n可用部位：%s"
            % (spec["rule"], quota["sparing"], POOL_SEP.join(spec["pool"]))), read


def _strip_blocked_actions(reply: str, action: Dict[str, Any]) -> str:
    """blocked 时把成品里的整行动作行删掉，并把删了几行记进 action.quota.stripped。

    这里是「不许靠模型自觉」的落点：提示词那句禁令压不住另外几条同向指令（格式要求还在教她
    怎么写动作行、锚句的规矩预设了有动作、最近几轮她自己的动作行一直在示范），所以这一轮
    真写出来的动作行在这里删掉。删空就留原文（kept_because_empty）——不把一轮回复删没。
    未 blocked 时一个字都不动，stripped 恒 0（这一态她本来就该偶尔写一个）。
    """
    quota = action["quota"]
    if not quota["blocked"]:
        return reply
    clean, count, kept = repeat.strip_action_lines(reply)
    quota["stripped"] = count
    quota["kept_because_empty"] = kept
    return clean


def _anchor_rows() -> List[Mapping[str, Any]]:
    """家·仓库的现行台词（每 scene 一条，按现行顺序）：这一轮唯一的「她现在说什么」真源。

    没配 / 卡不在 / 读不出来一律回空表（那一格写「未配锚句」）——不在这里造第二份，
    也不替她补一句。
    """
    try:
        got = home_anchors.read_all()
    except (home_anchors.AnchorError, home_access.AccessDenied,
            home_store.StoreError, OSError):
        return []
    if not got.get("file_exists"):
        return []
    return list(got.get("anchors") or [])


def _guard(conf: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """口癖护栏的门槛：没配锚句就没有护栏（没有台词，也就无所谓「老用哪一句」）。"""
    spec = conf.get("anchors")
    if not isinstance(spec, Mapping):
        return None
    guard = spec.get("guard")
    return guard if isinstance(guard, Mapping) else None


def _guard_missing() -> Dict[str, Any]:
    """没配护栏那一态：八个键照样交出去，值一律 null，over 是 False（不降档不记账）。

    键名冻结的另一半：页面照同一套键渲染，缺字段就写「没给」，不留白也不报错。
    """
    return {"window": None, "threshold": None, "min_rounds": None, "rounds": None,
            "hits": None, "rate": None, "over": False, "injected": None,
            "available": None}


def _anchor_repeats(conf: Mapping[str, Any], root: Any = None) -> Dict[str, int]:
    """最近 anchors.repeat.window 轮里每张卡各命中过几次（按 id，账在 organs/mind/usage.py）。

    没配锚句就没有这一层（没有台词，也就无所谓「最近老念哪一张」）；那一格的数据形状由
    _check_repeat 在装载时判过，这里不再兜底。
    """
    spec = conf.get("anchors")
    if not isinstance(spec, Mapping):
        return {}
    return usage.repeats(int(spec["repeat"]["window"]), root=root)


def _anchor(conf: Mapping[str, Any], rows: List[Mapping[str, Any]],
            limit: Optional[Any] = None,
            repeats: Optional[Mapping[str, int]] = None) -> Tuple[str, Dict[str, Any]]:
    """锚句这一段：只走家的公开读口，只给半句，按两条护栏的读数决定注入哪几条。

    没配 / 卡不在 / 读不出来三种情况都写「未配锚句」——这一格不许空着，也不许悄悄跳过：
    空着她就以为自己没有过一句锚。

    **只给 phrase 的半句**（两档是判据，docs/plan.md §3.14）：整句给她她就照着念；给半句她才得
    自己补完。进提示词的是「phrase 前 fraction 段 + suffix」（两个数都在 assembly.json 的
    anchors.inject 里）。**example 一个字都不进提示词**，返回体也不带它——她要抄只能抄到那半句，
    审计要用 example 时现读家（organs.home.anchors）。没有 phrase 的卡（升级前写下的 say 那几行）
    没有半句可给：跳过它，不拿整句顶上去。

    limit 是这一轮**该注入几条**（来自 usage.report 的 injected：先按 guard 定总条数）：
    降档时只取现行顺序的前 limit 条，其余进返回体的 skipped 里逐个点名。
    repeats 是最近几轮每张卡的命中次数（_anchor_repeats）：达到 anchors.repeat.max 的那张这一轮
    不再注入（**再按 repeat 剔除**），并在返回体的 banned_by_repeat 里逐个点名——不静默丢。
    两条护栏各管各的，次序是「先 guard 定总数、再 repeat 剔」，剔除后不拿 skipped 里的卡回补：
    回补等于把刚被禁的那张从后门放回来。
    """
    spec = conf.get("anchors")
    if not isinstance(spec, Mapping):
        return _anchors_missing()
    fraction = float(spec["inject"]["fraction"])
    suffix = str(spec["inject"]["suffix"])
    items: List[Dict[str, str]] = []
    for row in rows:
        phrase = str(row.get("phrase") or "").strip()
        if not phrase:
            continue
        cut = min(max(int(math.ceil(len(phrase) * fraction)), 1), len(phrase))
        items.append({
            "id": str(row.get("id") or ""),
            "phrase": phrase,
            "injected": phrase if cut >= len(phrase) else phrase[:cut] + suffix,
            # when 只进返回体当语境提示（给人看），**不是触发条件**：这里不按它挑卡，也不拼进提示词
            "when": str(row.get("when") or ""),
            # example / body 进返回体**只为面板核对**（人得看得见整句长什么样）；
            # 提示词那一侧一个字节都不取——test_example_never_enters_the_prompt 钉着。
            "example": str(row.get("example") or row.get("say") or ""),
            "body": str(row.get("body") or ""),
        })
    if not items:
        return _anchors_missing()
    keep = len(items) if limit is None else max(int(limit), 0)
    guarded = items[:keep]
    counted = repeats or {}
    most = int(spec["repeat"]["max"])
    banned = [item for item in guarded if int(counted.get(item["id"], 0)) >= most]
    blocked = {item["id"] for item in banned}
    shown = [item for item in guarded if item["id"] not in blocked]
    instruction = str(spec["instruction"])
    lines: List[str] = [instruction] + ["- " + item["injected"] for item in shown]
    if not shown:
        lines.append(ANCHORS_ALL_BANNED)
    return "\n".join(lines), {"configured": True, "count": len(items),
                               "available": len(items), "injected": len(shown),
                               "instruction": instruction, "items": shown,
                               "skipped": [item["id"] for item in items[keep:]],
                               "banned_by_repeat": [item["id"] for item in banned]}


def _anchors_missing() -> Tuple[str, Dict[str, Any]]:
    """未配锚句那一态：提示词里写明那句话，返回体给机器一个 false（不是空串）。"""
    return ("%s。%s" % (ANCHORS_MISSING, ANCHORS_HINT),
            {"configured": False, "note": ANCHORS_MISSING})


# —— 照抄审计（两级）———————————————————————————


def _audit_clear() -> Dict[str, Any]:
    """没命中任何一档时的读数：四个键齐全、decision 是 pass——页面照同一套键渲染，缺了就是留白。"""
    return {"decision": "pass", "kind": "none", "card_id": "", "overlap": 0}


def _longest_common(left: str, right: str) -> str:
    """两串里最长的那一段**连续**重合（DP；一张卡的 example 只有几十字，不必上后缀自动机）。

    只认连续：她把这句拆开重排不算照抄，连着念出六个字才算。空串返回空串。
    """
    best_len, best_end = 0, 0            # best_end 是在 left 上的结束位置（1 起）
    previous = [0] * (len(right) + 1)
    for i in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        head = left[i - 1]
        for j in range(1, len(right) + 1):
            if head == right[j - 1]:
                current[j] = previous[j - 1] + 1
                if current[j] > best_len:
                    best_len, best_end = current[j], i
        previous = current
    return left[best_end - best_len:best_end]


def _example_overlap(reply: str, rows: List[Mapping[str, Any]]) -> Tuple[str, int, str]:
    """她这句与哪张卡的 example 连着重合得最长：返回 (card_id, 字数, 重合的那段字)。

    两边都先过 usage.normalize（去标点 / 空白 / 控制字符）：标点不该把一句照抄切碎，
    而这个去标点的口径全仓只有 usage 那一份，本文件不另写一套。
    """
    body = usage.normalize(reply)
    best: Tuple[str, int, str] = ("", 0, "")
    if not body:
        return best
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        example = usage.normalize(row.get("example") or row.get("say"))
        if not example:
            continue
        found = _longest_common(body, example)
        if len(found) > best[1]:
            best = (str(row.get("id") or ""), len(found), found)
    return best


def _copy_audit(reply: str, rows: List[Mapping[str, Any]], conf: Mapping[str, Any],
                rewrite_one: Any, sink: Any = None
                ) -> Tuple[Dict[str, Any], Any, str, bool]:
    """照抄审计（两级）：返回 (audit 读数, phrase 命中, 这一轮出去的文本, 是不是整句弃掉)。

    第一级看 **example**：她这句与某张卡的整句例句连着重合到 anchors.audit.example_overlap 个字，
    判「报台词」——那不是在说话，是在背台词。判到就交 rewrite_one 改一次（走 organs/mouth/rewrite.py
    的 prompt / accept，与 api/chat.py 那套重写同一条路）；改完仍重合、或改不动（超时 / 空 / 不合规 /
    出口抛错）就**弃掉这一句**，与闸「改写一次再判」同一条路：宁可这一轮不说，也不许照原样报台词。
    第二级看 **phrase**（≤4 字那一档）：命中就放行，只记账（kind="phrase"），不判废——
    她看见的就是那半句，念出来是这一档设计好的用法，不是抄。

    两档同时撞上时按第一级判：能对上六个字的例句，已经不是「念了半个口癖」。

    phrase 那一份读数（usage.hit 的结果）连同文本一起交回调用方：落账那一栏用的是同一个判据，
    不在这里算一遍、那里再算一遍。
    """
    spec = conf.get("anchors")
    guard = spec["guard"]
    size = int(guard["prefix_chars"])
    phrase_hit = usage.hit(reply, rows, size)
    threshold = int(spec["audit"]["example_overlap"])
    card_id, overlap, matched = _example_overlap(reply, rows)
    if overlap >= threshold:
        why = COPY_REASON % (card_id, overlap, threshold)
        said = {"clause_id": COPY_CLAUSE, "kind": COPY_KIND, "reason": why, "matched": matched}
        try:
            new = rewrite_one(reply, said)
        except (rewrite_rules.Unrewritable, llm.LLMError) as exc:
            _note_drop(sink, "%s 改不动（%s），这一句不发了。" % (why, exc))
            return ({"decision": "dropped", "kind": "example", "card_id": card_id,
                     "overlap": overlap}, phrase_hit, "", True)
        again_id, again_overlap, _again = _example_overlap(new, rows)
        if again_overlap >= threshold:
            _note_drop(sink, COPY_AGAIN % (again_id, again_overlap, threshold))
            return ({"decision": "dropped", "kind": "example", "card_id": card_id,
                     "overlap": overlap}, phrase_hit, "", True)
        # 换过稿之后，落账要数的是**她真说出口的那一句**：拿改稿前那句去记 hit 与开头，
        # 账本上就会出现「开头是这句、命中的却是另一句」的错行。
        return ({"decision": "rewritten", "kind": "example", "card_id": card_id,
                 "overlap": overlap}, usage.hit(new, rows, size), new, False)
    if phrase_hit is not None and phrase_hit[usage.PHRASE]:
        # kind=phrase 时 overlap 是**命中的前缀字数**（不是例句重合）：两档同用一个键，
        # 页面与验收读得出「这一次对上几个字」，而不必猜这一格对 phrase 是什么意思。
        return ({"decision": "pass", "kind": "phrase", "card_id": phrase_hit[usage.CARD_ID],
                 "overlap": min(len(usage.normalize(phrase_hit[usage.PHRASE])), size)},
                phrase_hit, reply, False)
    return _audit_clear(), phrase_hit, reply, False


def _note_drop(sink: Any, reason: str) -> None:
    """照抄审计弃掉的那一句也要进弃言账本：不记，账本上就与「没判到」长得一样。"""
    if sink is not None:
        sink(COPY_CLAUSE, reason)


def _copy_rewriter(transport: Any, config: Any, env: Any) -> Any:
    """造这一档的 rewrite(text, hit -> text)：把「报台词」的那一句交给模型改一次。

    走的是 api/chat.py 同一条路、同一份判据：**发**（organs/mind/llm.complete，同一个注入的
    transport / config / env；时限用 data 里的 budget_ms，次数用 attempts）→ **验**
    （rewrite.accept：长度区间、数字 / 英文串 / 引号原话一条都不许少）→ 不成抛 Unrewritable，
    由 _copy_audit 按「改不动」处置。organs/mouth/rewrite.py 与它那份数据一个字都不改。
    判据表在这里才现读（warehouse.load 是缓存的），不判这一档的轮一次都不读。
    """
    def rewrite(body: str, hit: Any) -> str:
        table = warehouse.load("rewrite")
        budget = int(rewrite_rules.budget_ms(table))
        conf = dict(config) if isinstance(config, Mapping) else {}
        conf["timeout_ms"] = min(int(conf.get("timeout_ms") or budget), budget)
        conf["retries"] = max(0, int(rewrite_rules.attempts(table)) - 1)
        messages = [{"role": "user",
                     "content": rewrite_rules.prompt(body, hit, table)}]
        said = llm.complete(messages, config=llm.resolve_config(conf), env=env,
                            transport=transport)
        ok, why = rewrite_rules.accept(body, said, table)
        if not ok:
            raise rewrite_rules.Unrewritable(why)
        return said
    return rewrite


# —— 场合 ————————————————————————————————————


def _scene_value(value: Any) -> str:
    """归一化场景：只认脑那三个值（SCENE_TYPES），别的说法一律 unknown。

    缺省 / 空白 / 非法都不**猜**成私聊：猜错的代价是把一件私事在群里说出来，
    而 unknown 那句话说得很清楚——按平常说，不假设是群，也不假设是私聊。
    """
    if isinstance(value, str) and value.strip() in brain_model.SCENE_TYPES:
        return value.strip()
    return brain_model.SCENE_UNKNOWN


def _scene_text(conf: Mapping[str, Any], scene: str) -> str:
    """场合这一段进提示词的样子：按归一化后的场景取数据里那一句，本层一个字的文案都不写。

    三句话都在 assembly.json 的 scene.lines（_check_scene 已经保证三个场景各有一句），
    缺哪一句在装载那一步就报了，这里不做第二遍判。
    """
    return str(conf["scene"]["lines"].get(scene) or "")


def _drawer_due(spec: Mapping[str, Any], said: str) -> bool:
    """装配前判一次意图：**翻不翻只看意图**——enabled 为 true，且问句里出现关键词表里任一个词。

    场合在这儿一个字都不看（场合只经 _drawer_rows 决定能取哪几格）：群里问到旧事同样会翻，
    只是取不到私密那几格。判在这两处之外一个字节都不读：没命中就一次 IO 都不做（不 list、
    不 read），这一节也就压根不进提示词。被动与主动（她自己开口那一轮，由头就是这一问）用的
    是同一把尺子。
    """
    if not spec.get("enabled"):
        return False
    return any(word in said for word in spec["keywords"])


def _drawer_rows(spec: Mapping[str, Any], scene: str) -> List[Mapping[str, Any]]:
    """家·抽屉的清单：一格一次 list_room，只读，一个字都不写回去。

    **场合在这一层才起作用**：只取 spec["shelves"][scene] 里列的那几格——private 是普通 + 恋爱，
    group 与 unknown 只有普通那格（私密内容不进退群上下文）。scene 是归一化之后的值（只认脑那
    三个），所以这里不做第二遍判；不认的场合在归一化那一步已经是 unknown，不会被猜成 group 或
    private。

    读不出来（没这间房 / 没这一格 / 格子坏行）就跳过那一格、回空表：这一节不加，
    整轮照旧说得出口——旧事读不到，不该把她这一轮也拖哑。
    """
    rows: List[Mapping[str, Any]] = []
    seen = set()
    for shelf in spec["shelves"].get(scene) or []:
        try:
            got = cards.list_room(room=spec["room"], shelf=shelf, limit=spec["list_limit"])
        except (cards.CardError, home_access.AccessDenied, home_store.StoreError, OSError):
            LOGGER.warning("抽屉那一格没读出来（%s/%s）：这一轮不加那一节", spec["room"], shelf)
            continue
        for item in got.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            if item.get("kind") != spec["kind"] or (item.get("shelf") or "") != shelf:
                continue
            key = str(item.get("card_id") or item.get("name") or "")
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
    return rows


def _drawer_date(item: Mapping[str, Any], spec: Mapping[str, Any]) -> str:
    """这一篇的行首日期：name 前 date_chars 字像日期就用它，不像就退回 at 前那几个字。"""
    size = spec["date_chars"]
    head = str(item.get("name") or "")[:size]
    if re.match(spec["date_pattern"], head):
        return head
    return str(item.get("at") or "")[:size]


def _drawer_month(date: str, spec: Mapping[str, Any]) -> str:
    """日期落在哪个月、那个月的说法是什么（说法表在 drawer.month_words，缺这个月就空串）。"""
    piece = date[5:7] if len(date) >= 7 else ""
    if not piece.isdigit():
        return ""
    return str(spec["month_words"].get(str(int(piece))) or "")


def _drawer_overlap(said: str, text: str) -> int:
    """这段字与问句最长的连续重合字数（一个字都不重合就是 0）。

    「连续重合」是这一节唯一的话题尺子：分词要另一份词表，词表必漏；而两句话说的是同一件事时，
    总会有一段字一模一样。取最长的那个窗口——不是命中/没命中两档，是为了给每一篇一个分数：
    恒定那篇按分数从高到低取，被问时那几篇也是按分数先排。
    """
    best = 0
    size = len(text)
    for start in range(size):
        # 只找比手上更长的：end 从尾巴往回缩，缩到 start + best 就停。
        for end in range(size, start + best, -1):
            if text[start:end] in said:
                best = end - start
                break
    return best


def _drawer_share(said: str, text: str, size: int) -> bool:
    """这段字与问句有没有连续 size 字重合（逐段滑一遍；分词要另一份词表，这里不造）。"""
    return _drawer_overlap(said, text) >= size


def _drawer_score(item: Mapping[str, Any], date: str, spec: Mapping[str, Any], said: str) -> int:
    """这一篇有多贴近这一句：月份词（那个月的说法）整词命中 + name / at / note 的连续重合字数。

    「若干」是 drawer.topic_min_chars，本文件一个数都不写。两条判据**相加**，所以
    `score >= topic_min_chars` 就是原来那个「相关 / 不相关」的判据（月份词单独命中就够）——
    恒定那篇要的不是命中与否，是高低：用同一份分，两段才不会把「最相关」判成两篇。
    """
    size = spec["topic_min_chars"]
    month = _drawer_month(date, spec)
    score = size if (month and month in said) else 0
    for piece in (str(item.get("name") or ""), str(item.get("at") or ""),
                  str(item.get("note") or "")):
        best = _drawer_overlap(said, piece)
        if best > score:
            score = best
    return score


def _drawer_ranked(items: Sequence[Mapping[str, Any]], spec: Mapping[str, Any],
                   said: str) -> List[Tuple[int, str, Mapping[str, Any]]]:
    """整份清单排一遍：(分, 日期, 篇)，分高的先、同分按日期从新到旧。

    两段（恒定那篇与「被问时」）都从这一份里取——排一次，两处不各排一份，否则同一轮里
    两段可能把「最相关」判成两篇。日期一栏用 name / at 的行首若干字（YYYY-MM-DD 按字面比就是
    按日先后比）；写不出日期的那一篇整篇不进（行首没有日期，行就对不上号）。
    """
    rows: List[Tuple[int, str, Mapping[str, Any]]] = []
    for item in items:
        date = _drawer_date(item, spec)
        if not date:
            continue
        rows.append((_drawer_score(item, date, spec, said), date, item))
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return rows


def _drawer_pick(ranked: Sequence[Tuple[int, str, Mapping[str, Any]]],
                 spec: Mapping[str, Any]) -> List[Tuple[Mapping[str, Any], str]]:
    """被问时挑哪几篇：从排好的那一份里取前 max_entries（分高的先、同分新的先）。"""
    return [(item, date) for _score, date, item in ranked[:spec["max_entries"]]]


def _drawer_name(item: Mapping[str, Any]) -> str:
    """这一篇叫什么（去重按 name，不按 card_id：人认的是名字，账上也记名字）。"""
    return str(item.get("name") or item.get("card_id") or "")


def _drawer_body(item: Mapping[str, Any], spec: Mapping[str, Any]) -> str:
    """这一篇的正文：走家的公开读口 read_item（一篇一次）。读不出来就空串，这一篇不放。

    正文里的换行压成空格：一篇只占一行，行首才认得出日期；也免得把整节劈成好几段。
    """
    name = str(item.get("name") or "") or str(item.get("card_id") or "")
    try:
        got = cards.read_item(room=spec["room"], name=name)
    except (cards.CardError, home_access.AccessDenied, home_store.StoreError, OSError):
        LOGGER.warning("抽屉里这一篇没读出来：%s", name)
        return ""
    return WORD_JOIN.join(str(got.get("body") or "").split())


def _drawer_whole(text: str, room: int, ends: str) -> str:
    """掐到 room 字以内、且在句末收尾：整段优先，一句都凑不齐就一个字都不给（半句不如不给）。

    句末字符表来自 tone_rules.sentence_end_chars（与硬截回话那一层同一份），代码里没有标点。
    """
    if room <= 0:
        return ""
    cut = -1
    for end in ends:
        at = text.rfind(end, 0, room)
        if at > cut:
            cut = at
    return text[:cut + 1] if cut >= 0 else ""


def _drawer_excerpt(body: str, allow: int, spec: Mapping[str, Any],
                    ends: str) -> Tuple[str, bool]:
    """一篇正文压进 allow 字：整段放得下就整段，放不下就截到整句再接 more。

    返回 (字, 截没截)；截不出整句就是空串——那一种情况这一篇不放，不硬截半个词。
    """
    if len(body) <= allow:
        return body, False
    piece = _drawer_whole(body, allow - len(spec["more"]), ends)
    if not piece:
        return "", True
    return piece + spec["more"], True


# —— 恒定那篇的账本 ————————————————————————————————


def drawer_log_path(root: Optional[Any] = None) -> Path:
    """恒定那篇的账本落点：root 给定时按它（测试），否则 <器官目录>/mind/drawer_usage.jsonl。

    与口癖账、语气账落在同一处：它是她天天变的行为账，不进 git，也不是配置。
    """
    base = Path(root) if root is not None else paths.organ_dir("mind")
    return base / DRAWER_LOG


def read_drawer_log(root: Optional[Any] = None) -> List[Dict[str, Any]]:
    """读账本：坏行（不是 JSON、不是对象）跳过不炸——一份底稿读不下去，不该让她这一轮闭口。"""
    path = drawer_log_path(root)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _log_drawer(row: Mapping[str, Any], root: Optional[Any] = None) -> None:
    """追一行：只追加，不重写全文件。写不进去只 WARN——记账不许把这一轮的话挡回去。"""
    path = drawer_log_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        LOGGER.warning("抽屉·恒定那篇的账写不进去（%s）：%s", path, exc)


def _drawer_when(text: Any) -> Optional[datetime]:
    """账上那一行的时刻：解析不了就 None（坏行不参与窗口，也不炸）。"""
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def _drawer_used(at: str, spec: Mapping[str, Any], root: Optional[Any] = None) -> set:
    """最近 dedupe_days 天带过的 name（窗口的边看这一轮的 at，不看本机此刻的钟）。

    8 天前那一行已经出了窗口、3 天前那一行还在里面——去重的全部就是这一句。
    账上带时区与不带时区凑到一块（老行）比不了大小，那一行不参与判：不猜，也不炸。
    """
    now = _drawer_when(at)
    if now is None:
        return set()
    days = int(spec["dedupe_days"])
    used = set()
    for row in read_drawer_log(root):
        name = str(row.get("name") or "")
        when = _drawer_when(row.get("at") or "")
        if not name or when is None:
            continue
        try:
            inside = (now - when) <= timedelta(days=days)
        except TypeError:
            continue
        if inside:
            used.add(name)
    return used


def _drawer_always_candidates(ranked: Sequence[Tuple[int, str, Mapping[str, Any]]],
                              used: Any) -> List[Tuple[int, str, Mapping[str, Any]]]:
    """恒定那一篇的候选：从分最高的往下走，窗口内带过的那几个名字剔掉。

    回的是**一整串**不是一篇：排最前的那一篇未必压得进 always.max_chars（第一句就比余地长
    就截不出整句），调用方要一路往下试到有一篇撑得起这一节为止。候选全空（每个名字都在
    dedupe_days 窗口里）才是「这一轮不带」——宁可空一轮，也不把刚带过的那篇塞回来。
    """
    rows: List[Tuple[int, str, Mapping[str, Any]]] = []
    for score, date, item in ranked:
        name = _drawer_name(item)
        if name and name in used:
            continue
        rows.append((score, date, item))
    return rows


def _drawer_always_text(conf: Mapping[str, Any], spec: Mapping[str, Any],
                        pick: Tuple[int, str, Mapping[str, Any]], ends: str
                        ) -> Tuple[bool, str]:
    """恒定那一段的字（不含段标题）：一行旧日记 + 一句数据给的说明。

    上限是 always.max_chars，量的是**提示词里那一整块**——段标题（sections.drawer_always.label）
    与行格式（日期 + 正文）都在里面，所以正文的余地要先扣掉标题前缀、行首与那句说明。
    放不下就按话题截到整句再接 always.more；一句都截不出来就这一轮不带（不硬塞半句）。
    """
    always = spec["always"]
    section = (conf.get("sections") or {}).get(DRAWER_ALWAYS_KEY)
    if not isinstance(section, Mapping):
        LOGGER.warning("抽屉·恒定那篇没有自己的那一节（%s）：这一轮不带", DRAWER_ALWAYS_KEY)
        return False, ""
    prefix = "[%s]%s" % (section["label"], LINE_JOIN)
    _score, date, item = pick
    body = _drawer_body(item, spec)
    if not body:
        return False, ""
    note = always["note"]
    head = always["line"] % (date, "")
    allow = int(always["max_chars"]) - len(prefix) - len(head) - len(LINE_JOIN) - len(note)
    if allow <= 0:
        LOGGER.warning("抽屉·恒定那篇的余地不够（上限 %d 字）：这一轮不带",
                       always["max_chars"])
        return False, ""
    piece, _cut = _drawer_excerpt(body, allow, always, ends)
    if not piece:
        return False, ""
    text = (always["line"] % (date, piece)) + LINE_JOIN + note
    if len(prefix) + len(text) > int(always["max_chars"]):
        return False, ""
    return True, text


def _drawer(conf: Mapping[str, Any], said: str, scene: str, at: str = "",
            root: Optional[Any] = None) -> Tuple[Dict[str, Any], str, str]:
    """抽屉两段：恒定那一篇（每轮都带）+ 被问时那 2–3 篇（命中关键词才翻）。

    次序（挑法是这一节的判据，常数全在 assembly.json 的 drawer 里）：
      0. 抽屉关着，或恒定那一段关着又没有关键词命中 → 都回空，**一次 IO 都不做**；
      1. _drawer_rows 按 room / kind / shelves[场合] 读一次清单（场合只决定能取哪几格）；
      2. _drawer_ranked 排一次（话题相似度 + 日期），两段都从这一份里取；
      3. 恒定那篇：从 _drawer_always_candidates 往下试，**第一篇压得进上限的就是它**
         （截不出整句就换下一篇，绝不因为排第一的那篇放不下把整段拖成空）；挑到就落一行账
         （at / name / scene / score / why），渲染成一行旧日记 + always.note；
      4. 命中关键词时，「被问时」那一节照旧取 max_entries 篇、逐篇 read_item、压进 cap：
         恒定那篇用过的那一篇先从候选里剔掉（同一轮两节不重复同一篇；剔完没了就这一节空着），
         unknown 那档在节内第一行插 unknown_note，节尾接 testimony_note。
    两段各占一节、各一个上限：字数分别数得出来，一段被锯不会把另一段也吃掉。
    回包那一格只带 key 与 count——正文进提示词，**不进长期记忆**。
    """
    blank: Dict[str, Any] = {"key": DRAWER_KEY, "count": 0}
    spec = conf.get("drawer")
    if not isinstance(spec, Mapping) or not spec.get("enabled"):
        return blank, "", ""
    always = spec.get("always")
    always_on = isinstance(always, Mapping) and bool(always.get("enabled"))
    due = _drawer_due(spec, said)
    if not due and not always_on:
        return blank, "", ""
    ends = str(conf["tone_rules"]["sentence_end_chars"])
    ranked = _drawer_ranked(_drawer_rows(spec, scene), spec, said)
    if not ranked:
        return blank, "", ""
    always_text = ""
    carried_name = ""
    if always_on:
        # 恒定那篇**不看关键词**（due 只拦被问那一段）：一路往下试，第一篇撑得起这一节的就是它。
        for pick in _drawer_always_candidates(ranked, _drawer_used(at, always, root)):
            carried, drawn = _drawer_always_text(conf, spec, pick, ends)
            if not carried:
                continue        # 这一篇压不进上限（第一句就比余地长）：换下一篇
            score, _date, item = pick
            carried_name = _drawer_name(item)
            always_text = drawn
            _log_drawer({"at": at, "name": carried_name, "scene": scene,
                         "score": int(score),
                         "why": (DRAWER_WHY_TOPIC
                                 if score >= spec["topic_min_chars"] else DRAWER_WHY_DATE)},
                        root)
            break
    if not due:
        return blank, always_text, ""
    # 同一轮两节不许是同一篇：恒定那篇用完就从这一段剔掉；剔完没了这一段就空着，不为凑数放回来。
    rest = ([row for row in ranked if _drawer_name(row[2]) != carried_name]
            if carried_name else list(ranked))
    picked = _drawer_pick(rest, spec)
    if not picked:
        return blank, always_text, ""
    note = spec["testimony_note"]
    # 来源未定那一档：节内第一行（节标题之后、条目之前）明说这一轮不是私聊，**不猜场合**。
    # 只认 unknown 这一个值：group / private 一个字都不加（群里那句「来源未定」本身就是噪声）。
    head_lines: List[str] = ([spec["unknown_note"]]
                             if scene == brain_model.SCENE_UNKNOWN else [])
    # 这两行先占位：它们是这一档的要害，被 cap 锯掉就等于没写。
    budget = (spec["cap"] - len(note) - len(LINE_JOIN)
              - sum(len(row) + len(LINE_JOIN) for row in head_lines))
    lines: List[str] = []
    used = 0
    for item, date in picked:
        body = _drawer_body(item, spec)
        if not body:
            continue
        head = spec["line"] % (date, "")
        sep = len(LINE_JOIN) if lines else 0
        room = budget - used - sep - len(head)
        if room <= 0:
            break
        piece, _cut = _drawer_excerpt(body, min(spec["line_chars"], room), spec, ends)
        if not piece:
            continue
        line = spec["line"] % (date, piece)
        lines.append(line)
        used += sep + len(line)
    if not lines:
        return blank, always_text, ""
    body_text = LINE_JOIN.join(head_lines + lines) + LINE_JOIN + note
    return {"key": DRAWER_KEY, "count": len(lines)}, always_text, body_text


def _recall(brain: Any, said: str, who: str, scene: Optional[str] = None
            ) -> Tuple[List[Dict[str, Any]], str]:
    """脑装包：既进提示词，也原样进返回体（页面要显示「想起了什么 + 证据指针」）。

    记忆行在脑里是两栏：quote（对方原话，逐字）与 note（她的理解，可为空）。note_display
    由脑的 recall 装配好（「她理解：…」/「（无理解）」/「（unclear，未理解）」），本层只搬运、
    一个前缀都不自己加——「召回时标明这是她的理解」这条判据的真源在脑，不在这里。

    交给脑的查询串是 themes.expand(said)（原句 + 同一主题的其它说法）：偏的只是**这一句
    怎么问**，记忆、召回引擎、装包一处都不动——「换说法认不出」的病只能在查询这一侧治。

    scene 原样交给脑的 recall：**判据不在这一层**——group 时脑才把 audience=私密 的条目打上
    retellable=False / retell_note（它只打标记、不过滤召回）。这两个键脑给了就原样透出，
    没给就不带（没给不是「能讲」，是这一栏压根没判）。
    """
    # 只扩查询、不动记忆：这一句「闹别扭」要能想起记忆里那句「又生气了」，靠的是主题词表
    # （data/themes.json）在这**一句**上补同一主题的其它说法；记忆一个字都不许为召回去改。
    # 一个主题都没命中就原样查；词表本身坏了只该少召回一点，不该把这一轮打哑。
    try:
        query = themes.expand(said)
    except ValueError:
        query = said
    try:
        result = brain.recall(query=query, who=who or None, scene=scene)
    except TypeError:      # 只认两个参数的假脑 / 老脑：这一次就不带 scene（不猜，也不让它闭口）
        result = brain.recall(query=query, who=who or None)
    items = result.get("items") or []
    recalled: List[Dict[str, Any]] = []
    for item in items:
        row = {"id": item.get("id", ""),
               "text": item.get("text", ""),
               "display_text": item.get("display_text", item.get("text", "")),
               "source": item.get("source", ""),
               "source_type": item.get("source_type", ""),
               "evidence_key": item.get("evidence_key", ""),
               "score": item.get("score", 0.0),
               "who": item.get("who", ""),
               "note_display": item.get("note_display", "")}
        for name in ("retellable", "retell_note"):
            if name in item:
                row[name] = item[name]
        recalled.append(row)
    if not recalled:
        return recalled, NO_MEMORY
    lines: List[str] = []
    for row in recalled:
        # 第一行：原话 + 来源括号。display_text 由脑的 recall 造好——
        # 迁移来的「（无原话，来自旧家记录）」也在这里，装配侧不自造第二份。
        lines.append("- %s（来源：%s｜来源类型：%s｜证据：%s）"
                     % (row["display_text"] or row["text"] or NO_QUOTE,
                        row["source"], row["source_type"],
                        row["evidence_key"] or "无"))
        # 第二行：她的理解另起一行、缩进两格。没有理解就不写这一行（空串不进提示词，
        # 多出来的空行会把一条记忆劈成两条）。note_display 自带前缀，这里不再加一次。
        if row["note_display"]:
            lines.append(NOTE_INDENT + row["note_display"])
    return recalled, "\n".join(lines)


def _effective(heart_root: Any, at: str, mood_read: Mapping[str, Any],
               offsets: Any) -> Dict[str, Any]:
    """心·唯一合成点；坏卡降级成只要 mood 一块（与 api/heart.py 的既有口径一致）。"""
    try:
        return effective_module.effective_state(
            mood_read["baseline"], mood_read["now"],
            offset=offsets, root=heart_root, now=at)
    except card_module.CardError:
        return {"mood": effective_module.mood_block(mood_read["baseline"],
                                                    mood_read["now"])}


def _sleep_terms(heart_root: Any, at: str) -> Dict[str, Any]:
    """睡眠账给 energy 的两个派生量：生效窗口与睡眠回血封顶。

    照 api/heart.py 的 _sleep_terms：两个数都由 sleep 读时现算，本层只搬运、不重算。
    少传这两个，面板的精力读数与说话这边的读数在睡眠账非默认时就会分叉。
    """
    return {"sleep_window": sleep_module.effective_window(heart_root, at),
            "wake_cap": sleep_module.wake_cap(heart_root, at)}


def _intent_context(readout: Any, name: str, at: str,
                    energy_read: Mapping[str, Any]) -> Dict[str, Any]:
    """意图排序的上下文：今日条数、熟稔、精力那两口数（照 api/heart.py 的 _intent_context）。

    精力也注入：这一层已经按睡眠账的窗口结算过一次，排序层拿同一个数算清醒度，
    否则它会自己再读一次 energy——两次结算一条先后，档位与系数就可能与面板分叉。
    """
    context: Dict[str, Any] = {
        "who": name,
        "today_count": readout.today_count(name, at),
        "familiarity": readout.familiarity(name, at),
    }
    if isinstance(energy_read, Mapping):
        context["energy_value"] = energy_read.get("value")
        context["energy_tier"] = energy_read.get("tier")
    return context


def _engage(heart_root: Any, at: str, eff: Mapping[str, Any],
            energy_read: Mapping[str, Any], brain: Any, who: str,
            insulted: bool = False, intent: Optional[str] = None
            ) -> Tuple[Any, Dict[str, Any]]:
    """一次判决：只 decide，不 decide_and_log（核不写门的日志，那是心的账）。

    返回 (判决输入本身, 判决)。**输入也要交回去**：出站闸（organs/heart/gate.py）判「这一轮
    到底发不发」，用的必须是同一份 EngageInput——api 层另造一套就是第二处真源，同一个人同一刻
    两处会读出两种结论。这里只多交一个引用：不重算、也不第二次 decide。

    入参与 api/heart.py 的 _engage 一毫不差：窗口、意图排序（第 5 道软门要它）、
    按睡眠账结算过的精力、熟稔与今日条数。少喂一项，门判出的理由就跟面板不一样——
    同一个人同一刻，面板说「不是时候」，她这边却说「夜里」。

    insulted 是这一轮的「被骂」：**由调用方用 events.is_insult(text) 算好传进来**，本函数
    不判、也不许另写一张脏话表——engage 的硬门（第 2 道）与投 insult 事件用的必须是同一个
    布尔、同一个函数（全仓唯一被骂判据在 organs/mind/events.is_insult），否则"她今天被骂了
    没有"会有两份结论。不给就是 False（老调用与面板那条路一字不改）。

    intent 是这一轮谁起的头：heart.model 的 INTENT_PASSIVE / INTENT_PROACTIVE，别的值一律回落
    passive（不猜成主动——猜错了，夜里那一道硬门就会替她整轮闭口）。它是心判「夜里说不说」的
    前提：夜里 + 主动 → SILENT，夜里 + 被动 → LESS。调用方按 seed 给不给传，本层不另判一遍。
    """
    readout = ports_module.from_brain(brain)
    name = who or DEFAULT_WHO
    intents = intent_module.rank(heart_root, at,
                                 context=_intent_context(readout, name, at, energy_read))
    inp = engage_module.EngageInput.from_effective(
        dict(eff),
        window=engage_module.window_signal(heart_root, at),
        intents=intents,
        energy=energy_read["value"],
        familiarity=readout.familiarity(name, at),
        today_count=readout.today_count(name, at),
        intent=(intent if intent in heart_model.INTENTS else heart_model.INTENT_PASSIVE),
        who=name,
        now=at,
        insulted_today=bool(insulted),
    )
    return inp, engage_module.decide(inp)


def _register_aliases(brain: Any, who: str, aliases: Any) -> None:
    """把通道给的昵称 / 群名片登记进人名录当**别名**：id 是身份，昵称只是别名。

    只调脑的公开口：lookup（有没有这张卡）/ interact（建档，第一条别名落 display）/
    add_alias（已有的卡再补一条）。本层**不新开存储、不改身份、不写记忆条**——这里一个字都不进
    ingest，别名不是记忆。

    坏形状（who 不是号、aliases 不是串、一串空白）、脑拒了、脑自己坏了，一律记一行 WARNING 就过：
    别名是锦上添花，**不许因为它让这一轮说不出话**。
    """
    if brain is None or not isinstance(who, str) or not who.strip():
        return
    if isinstance(aliases, str) or not isinstance(aliases, (list, tuple)):
        return
    names = []
    for one in aliases:
        if isinstance(one, str) and one.strip() and one.strip() not in names:
            names.append(one.strip())
    if not names or not brain_model.valid_who(who):
        return
    try:
        person = brain.lookup(who)
        if not person:
            # 名录里还没有这个人：先用第一条（昵称在前）建档——身份就是 who，昵称落 display。
            brain.interact(who, display=names[0])
        for name in names:
            brain.add_alias(who, name)
    except Exception as exc:  # noqa: BLE001 - 别名坏了不许拖垮这一轮
        LOGGER.warning("装配·记别名没成（who=%s）：%s", who, exc)


def _caller_card(brain: Any, who: str) -> Optional[Mapping[str, Any]]:
    """这个号在脑里查不查得到名片：查得到给那一份，查不到 / 读不出来就给 None。

    读不出来（抛错）按「查不到」处置并在日志里留一句，**不抛**：这一格是给她一份反证材料，
    不该把整轮拖哑。名片为空对象也算查不到——那不是一张名片，只是一个空壳。
    """
    if brain is None or not who:
        return None
    try:
        person = brain.lookup(who)
        if not person:
            return None
        card = brain.card_view(person.get("id") or who) or {}
    except Exception as exc:      # noqa: BLE001 - 名片坏了不许把这一轮的话扣下
        LOGGER.warning("这一轮是谁：查名片没成（who=%s）：%s", who, exc)
        return None
    return card if isinstance(card, Mapping) and card else None


def _caller_text(conf: Mapping[str, Any], brain: Any, who: str) -> str:
    """这一节的正文（不含号）：_caller_read 的正文那一半，判据与来处全在那里。"""
    return _caller_read(conf, brain, who)[0]


def _caller_read(conf: Mapping[str, Any], brain: Any, who: str) -> Tuple[str, Dict[str, Any]]:
    """「这一轮是谁」这一节：**只认号，不认自称**——他嘴上说「我是主人」，这一个字都不变。

    三种形状各一句，文案全在 assembly.json 的 caller.lines（本文件没有这句中文）：
      known   who 有号、脑里也查得到名片 → {relation}（你叫{address}）+
              拿得到昵称时另起一行由 nickname_line 交代；
      unknown who 有号、脑里没名片（或查名片时抛了错）→ 一个不认识的号，照实说；
      missing who 是空串 → 不知道是谁在说话。
    判据只有 who 这一个输入：这一轮的正文一个字都不看——她顺着一句「我是主人」认下来，
    缺的正是这一格反证，不是又一条指令。

    **号的数字一个字都不进正文**（三句模板里根本没有 {who}）：号只在第二格那个读数里
    （assembly 把它挂在 assembly.sections 的 caller 那一行），供回包 / 面板 / 账用。她该知道
    「这是谁」，不该把号念出来。

    relation 只从名片的称呼 / 关系那类栏来，没有就用 caller.relation_fallback；address 只从
    名片上她对他的那个称呼来，没有就用 caller.address_fallback。昵称（display）**只喂
    nickname_line**：上一版实盘里群昵称被塞进 {who} 那个位置，成了「你号是能毕业就是胜利」，
    病根就是让 display 去顶一个它不该顶的位置。昵称那一行放不进 sections.caller.cap 就整句
    不放：锯一半正好砍掉「昵称不是身份」，剩下的正好是「把昵称当称呼用」。
    """
    spec = conf.get("caller")
    if not isinstance(spec, Mapping):
        return "", {"who": str(who or "").strip(), "shape": ""}
    lines = spec["lines"]
    key = str(who or "").strip()
    if not key:
        return str(lines["missing"]), {"who": "", "shape": "missing"}
    card = _caller_card(brain, key)
    if card is None:
        return _fill(str(lines["unknown"]), {}), {"who": key, "shape": "unknown"}
    relation = _card_word(card, CALLER_RELATION_KEYS) or str(spec["relation_fallback"])
    address = _card_word(card, CALLER_ADDRESS_KEYS) or str(spec["address_fallback"])
    text = _fill(str(lines["known"]), {"relation": relation, "address": address})
    nickname = _card_word(card, CALLER_NICKNAME_KEYS)
    line = _fill(str(spec["nickname_line"]), {"nickname": nickname}) if nickname else ""
    cap = _caller_cap(conf)
    if line and (not cap or len(text) + 1 + len(line) <= cap):
        text = text + "\n" + line
    return text, {"who": key, "shape": "known", "nickname": bool(nickname)}


def _card_word(card: Mapping[str, Any], keys: Tuple[str, ...]) -> str:
    """名片上这几栏里第一个有字的：一栏都没有就给空串，由调用方决定用哪个兜底词。

    只认列出来的那几栏：display 是昵称，所以它只出现在 CALLER_NICKNAME_KEYS 里，
    绝不参与 relation / address 的挑选——「谁顶上去谁就是上次那个 bug」。
    """
    for key in keys:
        value = card.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _caller_cap(conf: Mapping[str, Any]) -> int:
    """这一节的上限（sections.caller.cap）：数据没给就当 0，昵称那一行于是不放。"""
    sections = conf.get("sections")
    spec = sections.get("caller") if isinstance(sections, Mapping) else None
    cap = spec.get("cap") if isinstance(spec, Mapping) else None
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        return 0
    return cap


def _person_text(brain: Any, who: str) -> str:
    """关于他这一段：从名片取，每轮都进提示词，不占记忆名额、不被遗忘曲线淡掉。"""
    if brain is None or not who:
        return ""
    try:
        person = brain.lookup(who)
    except Exception:
        return ""
    if not person:
        return ""
    try:
        got = brain.card_view(person.get("id") or who) or {}
        card = got if isinstance(got, dict) else {}
    except Exception:
        card = {}

    def join(value: Any) -> str:
        if isinstance(value, (list, tuple)):
            return "、".join(str(x) for x in value if str(x).strip())
        return str(value or "").strip()

    bits = []
    for label, key in (("称呼", "display"), ("生日", "birthday"),
                       ("备注", "notes"), ("雷点", "prefs")):
        value = join(card.get(key))
        if value:
            bits.append("%s：%s" % (label, value))
    # 别名（通道发来的昵称 / 群名片）也交代一句：他换过叫法，她知道那是同一个人。
    # 名片上没有这一栏，所以取的是人名录那条（lookup 的 aliases），不另造一份存储。
    alias_text = join(person.get("aliases"))
    if alias_text:
        bits.append("别名：%s" % alias_text)
    return "｜".join(bits)

def _heart_words(eff: Mapping[str, Any], judgement: Mapping[str, Any]) -> Dict[str, Any]:
    """心这一块**只给档位词**：两个数（now / baseline）一律不出门。

    坏卡降级时没有 voice 那一块：tone 就取 mood 的档位词——effective.py 里 voice.tone
    本来就是 mood 的档，两者同源，这一格不是第二套换算。
    """
    mood_read = eff.get("mood") or {}
    voice = eff.get("voice") or {}
    mood_tier = mood_read.get("tier")
    mood_direction = mood_read.get("direction")
    return {
        "mood": {"tier": mood_tier, "direction": mood_direction},
        "voice": {"tone": voice.get("tone", mood_tier),
                  "direction": voice.get("direction", mood_direction),
                  "bans": [str(ban) for ban in (voice.get("bans") or [])]},
        "axes": _axis_words(eff.get("axes") or {}),
        "engage": {"state": judgement.get("state"), "reason": judgement.get("reason")},
    }


def _axis_words(rows: Mapping[str, Any]) -> Dict[str, str]:
    """六轴各一个档位词：成对轴取面上那一档，待定轴照实写「待定」（不写数、不写 0）。"""
    words: Dict[str, str] = {}
    for axis, row in rows.items():
        if not isinstance(row, Mapping) or row.get("greyed"):
            words[str(axis)] = PENDING_WORD
            continue
        words[str(axis)] = str(row.get("shown_word") or PENDING_WORD)
    return words


def _heart_text(eff: Mapping[str, Any], heart_words: Mapping[str, Any]) -> str:
    """心这一段进提示词的样子：还是只有词——心里/面上两个档位词也是词，不是数。"""
    lines = ["心境：%s（%s）" % (heart_words["mood"]["tier"], heart_words["mood"]["direction"]),
             "语气：%s（%s）" % (heart_words["voice"]["tone"], heart_words["voice"]["direction"])]
    bans = heart_words["voice"]["bans"]
    lines.append("禁令：%s" % ("；".join(bans) if bans else "没有"))
    axes = eff.get("axes") or {}
    words: List[str] = []
    for axis, row in axes.items():
        if not isinstance(row, Mapping) or row.get("greyed"):
            words.append("%s：%s" % (axis, PENDING_WORD))
            continue
        words.append("%s：心里%s，面上%s"
                     % (axis, row.get("inner_word") or PENDING_WORD,
                        row.get("shown_word") or PENDING_WORD))
    lines.append("轴：%s" % ("；".join(words) if words else "没有"))
    engage = heart_words["engage"]
    lines.append("参与：%s（%s）" % (engage["state"] or "没说参与态",
                                    engage["reason"] or "没有拦下的理由"))
    return "\n".join(lines)


def _voice(store: Any, brain: Any, who: str, heart: Mapping[str, Any]) -> Any:
    """嘴的语气档：称呼走人名录（与 api/mouth.py 同一个用法），心的四样读数原样交过去。

    heart 是这一轮**已经算好的**那一份（见 _heart_readings）：本函数既不重读盘，
    也不再 decide 一次——同一个人同一刻判两遍，迟早得出两种语气。
    """
    table = address_module.load_table()
    person = brain.lookup(who) if who else None
    called = address_module.resolve(person, table)
    return tone_module.from_store(store, called, heart=heart, table=table)


def _mouth_text(voice: Any, knobs: Mapping[str, Any], rules: Mapping[str, Any]) -> str:
    """嘴这一段进提示词的样子：称呼、语气档、口径行、禁不禁开玩笑（都是词）。

    心情凉那一轮再加一句「这一轮不逗人」：文案在 tone_rules.ban_joke_note 里，本文件没有
    这句中文。禁玩笑在这一层落到两处——提示词这一句，与装配后的整句删（见 _shape_reply）。
    """
    lines = ["称呼：%s（关系档：%s）" % (voice.address, voice.relation),
             "语气档：%s" % voice.label,
             "口径行：%s" % voice.line,
             "禁玩笑：%s" % ("禁" if voice.ban_joke else "不禁")]
    if knobs["ban_joke"]:
        lines.append(str(rules["ban_joke_note"]))
    return "\n".join(lines)


def _heart_readings(mood_read: Mapping[str, Any], energy_read: Mapping[str, Any],
                    offsets: Any, judgement: Mapping[str, Any]) -> Dict[str, Any]:
    """心的四样读数交给嘴的形状：mood 0–1、energy 原值 0–100、offset 六轴、engage 三态之一。

    两档刻度都是嘴的数据定的（address_table 的 high_mood_at_least = 0.2 按 0–1 判，
    low_energy_below = 30 按 0–100 判），所以 mood 先除 100 再进嘴，energy 原样进。
    这四样全是这一轮**已经算好的**：少一样，嘴那边就少一条折算；重算一遍，就多一处真源。
    """
    mood_now = _float(mood_read.get("now"))
    engage = str(judgement.get("state") or "").strip()
    return {"mood": None if mood_now is None else round(mood_now / 100.0, 3),
            "energy": _float(energy_read.get("value")),
            "offset": _offset_readings(offsets),
            "engage": engage or None}


def _offset_readings(offsets: Any) -> Optional[Dict[str, float]]:
    """六轴折算压成一层「轴 → 值」再交给嘴。

    不把整份 overview 原样交过去：它带着 week / at 与四个派生量，str() 出来五百多字，
    嘴那一节（sections.mouth.cap）会把「禁玩笑」那一行悄悄锯掉——读数要如实，
    但摊在提示词里的那本字典不是读数。
    """
    if not isinstance(offsets, Mapping):
        return None
    axes = offsets.get("axes")
    if not isinstance(axes, Mapping):
        return None
    rows: Dict[str, float] = {}
    for axis, row in axes.items():
        value = _float(row.get("value")) if isinstance(row, Mapping) else None
        if value is not None:
            rows[str(axis)] = round(value, 3)
    return rows or None


def _knobs(rules: Mapping[str, Any], heart: Mapping[str, Any],
           judgement: Mapping[str, Any], mode: str = events.MODE_PASSIVE
           ) -> Dict[str, Any]:
    """四个旋钮这一轮各按哪个档：判据全部来自 tone_rules，本函数一个阈值都不带。

    low_mood / low_energy 是「长度收紧 + 玩笑全不产出」那两条；neutral 只管字数那一格
    （max_chars_neutral 为 null 时它什么也不做）。no_question 只认「少说」这一态：
    「不说」由出站闸整轮拦下（不归这一层），「说」就照常问。
    """
    mood = _float(heart.get("mood"))
    energy = _float(heart.get("energy"))
    low_mood = mood is not None and mood < float(rules["low_mood_below"])
    low_energy = energy is not None and energy < float(rules["low_energy_below"])
    state = str(judgement.get("state") or "").strip()
    return {"low_mood": low_mood, "low_energy": low_energy,
            "low": bool(low_mood or low_energy),
            "neutral": mood is not None and mood < float(rules["neutral_mood_below"]),
            # 主动开口那一轮默认不删问句：LESS 的本意是「别追问」，而她自己开口时那句
            # 由头本身可能就是问题（「想问问他手术结果」）。开关进数据，改数据即改行为。
            "no_question": bool(state == heart_model.STATE_LESS
                                and (mode != events.MODE_PROACTIVE
                                     or bool(rules.get("no_question_in_proactive", False)))),
            "ban_joke": bool(low_mood)}


def _max_chars(budget: int, knobs: Mapping[str, Any], rules: Mapping[str, Any]
               ) -> Tuple[int, bool]:
    """这一轮的回复字数上限，以及要不要按它在句末硬截。

    低档取 min(档位预算, max_chars_low)；中档取 max_chars_neutral（null = 照旧用档位预算）；
    其余原样。第二个值是「这一轮有没有一条硬线」：档位预算历来只是提示词里的一句话，
    从来没硬截过——只有压到数据给的那条线时才动锯子（中性那一轮因此与改动前逐字相同）。
    """
    limit: Optional[int] = None
    if knobs["low"]:
        limit = int(rules["max_chars_low"])
    elif knobs["neutral"] and rules["max_chars_neutral"] is not None:
        limit = int(rules["max_chars_neutral"])
    if limit is None:
        return int(budget), False
    return min(int(budget), limit), True


def _shape_reply(reply: str, max_chars: int, capped: bool, knobs: Mapping[str, Any],
                 rules: Mapping[str, Any]) -> Dict[str, Any]:
    """装配后按四个旋钮收拾这一句：删玩笑句 → 删问句 → 按上限在句末截断。

    三件事都排在闸与照抄审计之后（被弃掉的句子再说也没人听得见）、分包之前。
    **删空不是沉默**：整句都该删就保留原句，并记 kept_because_empty——删光了不是「她没说」，
    是她把该说的说完了，剩下的都是不该说的。
    中性那一轮一个字都不动：两个开关都关、也没有硬线时原样返回，与改动前逐字相同。
    """
    shaped: Dict[str, Any] = {"reply": reply, "truncated": False, "dropped_questions": 0,
                              "dropped_jokes": 0, "kept_because_empty": False}
    if not reply:
        return shaped
    ends = str(rules["sentence_end_chars"])
    if knobs["ban_joke"] or knobs["no_question"]:
        jokes = [str(word) for word in rules["joke_words"]]
        questions = str(rules["question_chars"])
        # 行是她的排版单位（嘴要求分行说）：先按行切，行内再按句切。
        # 一行里整句删光了就整行不留——删句不许在原文里留一串空行，那不是她的排版。
        kept: List[str] = []
        for line in reply.split("\n"):
            if not line.strip():
                kept.append(line)          # 空行是排版，不是句子
                continue
            left: List[str] = []
            for row in _sentences(line, ends):
                # 先玩笑后问句：一句里两样都占，记在玩笑那一栏（一处只记一次）。
                if knobs["ban_joke"] and any(word in row for word in jokes):
                    shaped["dropped_jokes"] += 1
                    continue
                if knobs["no_question"] and any(char in row for char in questions):
                    shaped["dropped_questions"] += 1
                    continue
                left.append(row)
            if left:
                kept.append("".join(left))
        while kept and not kept[0].strip():
            kept.pop(0)                # 删掉的头几行不许在原文最前面留一串空行
        while kept and not kept[-1].strip():
            kept.pop()                 # 尾巴同理
        if "".join(kept).strip():
            shaped["reply"] = "\n".join(kept)
        else:
            shaped["kept_because_empty"] = True
    if capped and len(shaped["reply"]) > max_chars:
        shaped["reply"], shaped["truncated"] = _clip_at_sentence(
            shaped["reply"], max_chars, ends)
    return shaped


def _sentences(text: str, ends: str) -> List[str]:
    """按句末标点切成整句（标点跟着前一句走）；没有标点的尾巴也算一句。

    碎片一片都不许丢：一块空白也是原文的一块（她分行的空行就是分行的那个空行），
    丢一片再拼回去，等于替她改行——那正是「一个字都不许动」不许做的事。
    """
    rows: List[str] = []
    start = 0
    for index, char in enumerate(text):
        if char in ends:
            rows.append(text[start:index + 1])
            start = index + 1
    if start < len(text):
        rows.append(text[start:])
    return rows


def _clip_at_sentence(text: str, max_chars: int, ends: str) -> Tuple[str, bool]:
    """按上限在句末截：取不超过上限的最后一个句末；一句都放不下就留第一句整句。

    绝不截半句：宁可比上限长一句，也不把一句话锯成两截——那半句她没说过。
    """
    first = 0
    cut = 0
    for index, char in enumerate(text):
        if char not in ends:
            continue
        if not first:
            first = index + 1
        if index + 1 <= max_chars:
            cut = index + 1
        else:
            break
    if cut and cut < len(text):
        return text[:cut], True
    if first and first < len(text):
        return text[:first], True
    # 整段就是一句、又比上限长：留整句，并如实说「一个字节都没截」（截了才算 truncated）
    return text, False


def _float(value: Any) -> Optional[float]:
    """只管数：布尔与别的类型一律当「没有」（不把 None 悄悄当 0）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _tone_row(at: str, heart_words: Mapping[str, Any], energy_read: Mapping[str, Any],
              voice: Any, shaped: Mapping[str, Any], max_chars: int,
              knobs: Mapping[str, Any]) -> Dict[str, Any]:
    """这一轮那一行账（字段见 TONE_FIELDS）：档位词、软度、上限、两个开关、删了几处、心接上没有。

    行形状只由 TONE_FIELDS 一份说了算：少一栏当场 KeyError，不许悄悄多一栏、也不许少一栏。
    """
    row = {"at": at,
            "mood_tier": heart_words["mood"]["tier"],
            "energy_tier": energy_read.get("tier"),
            "tone_tier": voice.label,
            "softness": voice.softness,
            "max_chars": int(max_chars),
            "no_question": bool(knobs["no_question"]),
            "ban_joke": bool(knobs["ban_joke"]),
            "truncated": bool(shaped["truncated"]),
            "dropped_questions": int(shaped["dropped_questions"]),
            "dropped_jokes": int(shaped["dropped_jokes"]),
            "kept_because_empty": bool(shaped["kept_because_empty"]),
            "heart_source": voice.heart_source,
            "address": voice.address}
    return {name: row[name] for name in TONE_FIELDS}


def tone_log_path(root: Optional[Any] = None) -> Path:
    """这一份账的落点：root 给定时按它（测试），否则 <器官目录>/mind/tone_log.jsonl。"""
    base = Path(root) if root is not None else paths.organ_dir("mind")
    return base / TONE_LOG


def read_tone_log(root: Optional[Any] = None) -> List[Dict[str, Any]]:
    """读账本：坏行（不是 JSON、不是对象）跳过不炸——记账的一份底稿读不下去，不该让她闭口。"""
    path = tone_log_path(root)
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _log_tone(row: Mapping[str, Any], root: Optional[Any] = None) -> None:
    """追一行账：只追加，不重写全文件。写不进去只 WARN——记账不许把这一轮的话挡回去。"""
    path = tone_log_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        LOGGER.warning("核·语气账写不进去（%s）：%s", path, exc)


def _through_mouth(reply: str, trace: Any) -> Tuple[Any, Dict[str, Any]]:
    """过八股闸：条款来自 warehouse.load("clauses")，重写器传 None（这一层命中即弃）。

    弃言记进 organs/mouth/drops.py 的账本：闸只报「哪条条款、为什么」，时刻与落盘都在
    这一层给（照 api/mouth.py 的 _sink / _day 写法）。这一层不发第二次模型请求——
    「改一次」那一档在照抄审计（_copy_audit），只对报台词的那一句才发生。
    """
    clauses = warehouse.load("clauses").get("clauses")
    verdict = gate_module.enforce(reply, clauses, None, _sink(trace))
    return verdict, drops_module.report(trace, _day())


def _pref_card(who: str) -> Dict[str, Any]:
    """这一轮是谁的偏好卡：「这个人希望她怎么说话」的两个旋钮；没有卡就是 {}。

    读的是家的名片盒（organs/home/residents.py 的 preference_of，与 api/chat.py 同一个读口）。
    **读不到偏好不算错**：偏好是「怎么说」的调节，不是这一轮能不能说话的前提——空 who、卡片坏件、
    家根读不动，一律只记一行、照默认往下走。范围判定（0 / 负数 / bool 算没设）不在这里：
    那是 organs/mouth/style_policy.py 那一份判据的事。
    """
    key = str(who or "").strip()
    if not key:
        return {}
    try:
        return dict(home_residents.preference_of(root=None, who=key) or {})
    except Exception as exc:                       # CardError / OSError 全算「没卡」
        LOGGER.warning("嘴·偏好卡读不动（%s）：这一轮按默认口径说话", exc)
        return {}


def _bubbles(reply: str, who: str, at: str) -> List[Dict[str, Any]]:
    """她说出口的那一句怎么分包：切分 + 节奏（seed 稳定，同一轮两次调用得到同一组间隔）。

    必须排在照抄审计之后：审计那一档会把整句换掉或弃掉，拿旧的那句算分包，
    发出去的就是她没说的那句。

    **这一轮的 who 就是「触发者」**：按他的偏好卡盖掉 split 的旋钮再切（覆盖规则在
    organs/mouth/style_policy.py，与 api/chat.py 同一份判据，不在这里另抄一套）。节奏仍用
    没盖过的 policy——卡管的是「切成几泡、每泡几个字」，不是「停多久」。
    「两个旋钮都给了、字限赢」的留痕不回包（这一层没有回包），只记一行日志。
    """
    if not reply:
        return []
    policy = warehouse.load("policy")
    split_policy, note = style_policy.overlay(policy, _pref_card(who))
    if note is not None:
        LOGGER.info("嘴·本次忽略 chunks_per_reply=%s：字限优先（max_chars=%s 那一档赢）",
                    note["chunks_per_reply"], note["max_chars"])
    batch = pace_module.fill(split_text(reply, split_policy), policy,
                             "%s%s%s" % (who or "", SEED_SEP, at))
    return [{"text": part.text, "gap_before": part.gap_before, "send_at": part.send_at}
            for part in batch.parts]


def _sink(trace: Any) -> Any:
    """把闸的弃言接到账本上：闸只报「哪条条款、哪句原因」，时刻在这里给。"""
    def note(clause_id: str, reason: str) -> None:
        drops_module.record(trace, clause_id, reason, datetime.now())
    return note


def _day() -> str:
    """今天的日期（ISO，十位）：账本按这个前缀认「今天」。"""
    return datetime.now().date().isoformat()


def _remember(brain: Any, store: Any, who: str, said: str, reply: str,
              dropped: bool, at: str, scene: Optional[str] = None, *,
              proactive: bool = False
              ) -> Tuple[List[Dict[str, str]], str]:
    """落脑：人的原话先记；她真说出口的那句再记（被弃的那句没说出口，不进脑）。

    两条都带同一个 scene（人那句与她那句）：场合是这一轮的事实，不是她那一侧才有的。
    unknown 也照传 unknown——脑自己认不出这个值就按 unknown 存，本层不替它抹成别的。

    时刻与 schema 全走脑自己的 ingest：这里不新开表、不另造一行形状。
    id 用的是 ingest 回来的 evidence_key——亲历这一条的 id 就是它自己的证据指针。
    第二半是**人那一句**的 id，交给最近几轮那一份账当 msg_id（对账靠它对齐）；
    人那句没落成（脑拒了）就是空串：没有凭据，下一轮对账认不出来也不要假装认出来了。

    proactive 是她自己开口那一轮：**只有她那句**。没有对方那句可落，也不为凑形状写一条假的
    人话（写了就是她自己给自己编了一句对方说的话）；msg_id 因此恒空串——这一轮没有人的那条
    凭据，下一轮对账也就没有它对得上，那是实话。
    """
    wrote: List[Dict[str, str]] = []
    msg_id = ""
    if not proactive:
        human = brain.ingest(who=who or HUMAN_WHO, text=said, ts=at, scene=scene)
        if human.get("ok"):
            row = _wrote_row(human)
            wrote.append(row)
            msg_id = row["id"]
    if dropped or not reply:
        return wrote, msg_id
    hers = brain.ingest(who=store.snapshot().identity.display, text=reply, ts=at,
                          side=brain_model.SIDE_SELF, scene=scene)
    if hers.get("ok"):
        wrote.append(_wrote_row(hers))
    return wrote, msg_id


def _wrote_row(result: Mapping[str, Any]) -> Dict[str, str]:
    """一行「落了谁的哪条」：id 取证据指针，带上这条记在谁名下。"""
    entry = result.get("entry") or {}
    return {"id": str(entry.get("evidence_key") or entry.get("id") or ""),
            "who": str(entry.get("who") or "")}


def _fill(template: str, values: Mapping[str, str]) -> str:
    """模板里逐键纯替换：只换这次给了的那几个花括号，别的字符一个都不动。"""
    out = str(template)
    for key, value in values.items():
        out = out.replace("{%s}" % key, str(value))
    return out
