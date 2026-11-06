"""说话端点：POST /chat —— 核上唯一一条写，把一轮真对话从入口说出去。

**只做 HTTP 映射，不装第二遍。** 装配（魂 → 脑 → 心 → 嘴 → 模型 → 过嘴 → 落脑）全在
organs/mind/conversation.py 的 answer()：那一处是全仓唯一的装配点，QQ 通道将来也直接调它。
本文件只做三件 HTTP 的事：认 body、把模型失败翻成状态码、把结果原样交出去。

失败一律 **非 200 + ok:false**，body 固定三键 {ok:false, kind, message}：
kind 给机器分岔，message 是模型那边的人话原文（str(LLMError) == message），一个字节都不改。
模型回了空内容也不算成功——那是 empty，不是「她轻轻没说话」；把空串当 200 发出去，页面就会
画出一个「她说了一句空话」的假成功。

**投递点只有这里，干跑端点不写。** 一轮说过之后（conversation.answer() 返回之后）判一次拒绝：
她的答话把话转开了（refusal.soft_scan）或就是那三句标准拒绝之一（refusal.enforce），就在本文件
投一次——heart.refuse.ingest 写心的 refusals.json，refusal.record 写我们自己的 refuse.jsonl。
/mouth/gate 那个干跑端点只判不投（它的 delivered 恒为 false、would_deliver 只算不写）：一个拒绝
要么落在这一层（真说过话），要么谁都不写（只是干跑），没有第三种。

**冷却里主动拒（这一轮加的）：出口先问一次心。** answer() 之前——也就是装配与那一问之前——先拿
对方这句认话题（refusal.topic_in + 心自己那份词表，与投递那一层同一个认法）：认得出就问心一次
（heart.refuse.check，**只读**）。那个话题还在冷却里，这一轮就**短路**：不调模型（省下的是一次真
调用——心已经说不接这个话题，再问一遍模型只会让她换个说法把同一句话说一遍）、不投事件（心那边的
账上已经记着这一次拒绝了，这一层再投一次就是同一件事记两遍）、直接产出 data/refusal.json 里
refused-topic 那一句（说法归拒绝那边，嘴这边不造新文案）。几泡、每泡停多久仍走现有的 split +
pace.fill，不另造一套；这一句照样过八股闸（她的话都过闸，冷却不是免检章），但**不做复述审计**：
这一轮没有装配、也没有任何注入原文，她没有可抄的东西，拿入站那句话去比她只会被冤枉——闸收到
inject=None，如实记 audit-unavailable（不是通过）。重写那一步这一轮也不发请求（_frozen_rewriter）：
冷却那一句是数据里的原话，改一个字就是另一句，而这一轮的硬约束正是「一次模型都不调」。
认不出话题（或冷却判据读不动）就不判、照常说话：没有话题就没有「不接的那一句」，替她猜一个话题
去拒比不拒更糟。

**闸在 /chat 生效（这一轮加的）**：answer() 返回之后、组装响应之前，逐泡再过一遍闸
（条款 → 动作配额 → 动作括注 → 内容重复 → 冒充做过 → 复述审计），命中就改一次、改不了就弃那一泡；弃掉的泡从 chunks 里
删掉，剩下的重算 send_at，全被弃掉就是 chunks=[]、reply=""（每一泡弃言各记一行弃言账）。所以
gate 节除了既有的 decision / hit / drops，还逐泡交代 parts / in_parts / out_parts / blocked / note。
判据一个都不在本文件：条款、复述审计、动作括注、内容重复全部经 warehouse.load 读；
「她最近几条回话」只读 mind 的 recent_turns.jsonl，取最近几轮的条数同样从 repeat.json 里读。
**动作配额也从装配传进闸**：answer() 回包的 action.quota.blocked 为真就传 forbid、为假传 allow；
装配里没有这一项就是 None（闸记 quota_status=not-checked）——没标不是 allow，不许猜。
**动作账（这一轮加的）也从回包传进闸**：claims 那一关（冒充做过，魂的红线 bounds.no-fake-doing
的出口）要的是「这一轮真发生过的动作」，见 _actions()：回包里没有这一格就是空清单 []（现在还没有
手与家的动作账，这一轮确实什么都没做，该判冒充），给了清单就原样交下去，形状读不动才是 None
（闸记 claim_status=not-checked——**拿不到不是没做过**，猜一个空清单顶上就是替她编账）。
这一关的处置与别的判据不同：改不动就删掉那一句、剩下的话继续发，删完只剩空白才弃这一泡。

**出站闸在 /chat 生效（这一轮加的）**：这是「她这轮到底说不说」的那道门（organs/heart/gate.py），
排在最后一步：拒绝判完、扣留读数收好之后才问它。输入不在这里造——answer() 用私有键 "_gate_input"
把这一轮判决用的那一份 EngageInput 交出来（同一份输入，不给门就得在这儿另造一套），这一层只负责
pop 掉它、取群号（body 的场合是 group 才取，私聊一律 None 不占群额度）、调门、把答案写回回包：
send / reason / level / limits 四格，不发就 chunks=[]、reply=""。**这一层一个判据都不写**：
门只回答发不发，心怎么想、嘴说什么都不归它。输入没交出来就记一行 WARNING，并在 gate.outbound 里
写明这一关没判（不静默）——门缺席不该让她开不了口。

**重写真的接到模型上（这一轮加的老账）**：以前命中即弃，一句口癖让她整泡消失。现在命中先改一次——
「她的原话 + 命中的那一条 + 一句人话原因」交给模型（走 answer() 用的**同一条出口** llm.complete，
不另开 HTTP 客户端），带 data/rewrite.json 的 budget_ms 硬时限；拿回来先过 rewrite.accept
（空 / 逐字相同 / 长度出区间 / 丢了数字、英文串、引号里的原话都算不合规），过了再用新稿跑一遍闸，
**二次仍命中才弃**。改不动（超时 / 空 / 不合规 / 出口抛错）就**保留原话发出**、账本记一行
rewrite-failed，绝不输出空——宁可发一句略有瑕疵的，也不许静默变哑。每一泡的重写读数挂在
parts[*].rewrite（{status, why, from, to}，from / to 只放一小截）。

**对方要她再说一遍时内容去重不判废（这一轮加的）**：asked（入站那句）原样传给闸，撞上重复而
对方明说要她重复时，那一泡只记一行账（reason 带 skipped-by-request）、照原样发出，
repeat_status 记成跳过那个状态——**她重复是对的，别吞**。这条豁免只在真撞上重复、且 asked 命中
请求标记时成立，asked 为空一律不豁免。

**同一句请求也豁免复述审计（这一轮加的）**：他要她重复／引用时，把她刚才那段设定原样再说一遍
是她照办，审计这一关也只记账、不判废。豁免开关由闸算一次（repeat.requested(asked, 表)），同时交给
内容重复与复述审计——请求标记只有 repeat.json 那一份，本文件不另存一份。跳过时那一泡照原样发出、
账本各记一行（内容重复那行带 skipped-by-request、复述审计那行带 audit-skipped-by-request），
parts[*].audit 里的 hit **照旧带着**（面板看得见她引了设定）、另加 skipped=true：豁免的是判废，
不是「撞上」这件事。一句话同时撞两关就记两行账。

**红线与「二次命中留原话」（这一轮加的）**：闸多收一张红线表（data/fatal.json，经 warehouse 读：
隐私 / 辱骂 / 放弃式），踩上的那一泡照旧 dropped，parts[*].fatal 里看得见是哪一类、撞上了什么，
fatal_status 看得出这一关判没判。其余情形改了一次还是命中时，闸不再丢整句：decision 仍是 pass、
text 是进门那句原话、parts[*].kept_after_rewrite=true，账本记一行带「<通道>-after-rewrite」的账——
**绝不静默变哑**：在最该回答的那一句上沉默，比「说话带点毛病」糟得多。

**称呼也在这一层接上（这一轮加的）**：answer() 返回之后、过闸之前先看一眼**对方这句话**有没有
「要求改称呼」（refusal 管她拒没拒，这里管他叫啥，两件事各走各路）。抽到就叫法当场换
（source=本轮改名）、把 rename_memory 那条**亲历记忆**投给脑一次（brain.ingest(**draft)），
并记进本进程那本 AddressBook（模块级单例，只活这个进程、不落盘）；下一轮他再开口，
resolve(..., session=ADDRESS_BOOK) 就用会话覆盖（source=会话覆盖）。**为什么挑这个点**：称呼只读
他那句话与脑的人名录，而 answer() 刚刚把他的原话落进脑——人名录里因此才查得到这个人；放在过闸
之前，是因为闸可能把整轮弃掉，而他改的叫法不该跟着她那句被弃的话一起丢。违禁自称照样过不去：
resolve 退到中性叫法，那一轮既不投记忆、也不记会话账。返回体因此多一节
addressed: {who, address, source}。（提示词那一段称呼由 organs/mind/conversation.py 自己按人名录
算，不在本次边界内——会话覆盖目前落在出口读数与脑那条记忆上。）

**出口认 audience（这一轮加的）：私密记忆在群里守边界。** `/chat` 的 body 多一个可选
`scope`（只认数据里那两个值、缺省值也在数据里；**非法值 400 并点名**）。装配之后、过闸之前，
`recalled[*]` 里标成私密的那几条（栏名与取值取自 organs/mouth/data/refusal.json 的 audience 节）
在 `scope=group` 时**从出口剔除**：不再进 `recalled`（于是页面不再引用它、复述审计的记忆那一档
也不再有它），剔了谁记在回包的 `audience_held: [{id, text_min, source}]`（id 与一小截摘要，不贴
正文）。**被直接问到就必须出声**：用户这句与该条 text 的连续重合 ≥ 阈值、或该条 text 的 N 元组
覆盖率 ≥ 阈值（两条判据与四个阈值全在数据里，可离线复算）时，她这一轮说的不是模型那句，而是数据
里那句边界话 `say`，并走拒绝那条路（事件形状取 `event_shape`、我们自己的 refuse.jsonl 记一行、
心那边记一次冷却，事件 why 与账本那一行里带 `mark` 记号）。**没被问到就不出声**：不为没发生的事
拒绝，但照样剔除（`audience_held` 里看得见）。`scope=private` 一律照常——私聊里内容可以说，既
不剔除也不出声。

**场合往核里传（这一轮加的）：body 多一个可选「scene」**（认 group / private / unknown，
缺省与非法一律 unknown；旧字段名「scope」只在 scene 缺席时兜底）。它与上面那一格 scope
是两件事：scope 管出口「私密记忆这一轮能不能被引用」（非法值 400 并点名），scene 管她
「在哪儿说话」（**非法值不 400**，落成 unknown）。同一个 body 里两个字段各读各的，互不顶替。
读出来的值原样交给 conversation.answer(scene=...)：装配里多一段「场合」（三句话在
assembly.json 的 scene.lines）、落脑时写给那一轮的两条记忆、回想时交给脑（group 时脑才把
audience=私密 的条目打上 retellable=False，recalled[] 里原样透出）。成功体因此多一节
scene{value}。**不猜**：缺省也是 unknown——猜成私聊，就是把私事在群里说出去了。

**段往核里传（这一轮加的）：body 多一个可选「segment」**——刚刚这一小段谁说了哪几句，
两种形状都收（list of {speaker, text}，或 list of [speaker, text]），归一化成 [(谁, 说了什么)]
后原样交给 conversation.answer(segment=...)，装配里多一段「这一小段」（标题 / 上限在
assembly.json 的 sections.segment）。它**不写进 recent**：段是刚刚这一小段、recent 是更早的
12 轮，两份输入不重叠，同一条消息不会既当段又当 recent。没给这个字段就是 None——私聊与老调用
一字不改。形状坏了（不是 list / 元素既不是对象也不是二元组 / speaker 空）当场 400 并点名，
不兜底：悄悄当成空段，等于让她以为这一段没人说话。只给段、没给 text 时，这一问取段里最后
那一条（他刚说的那句）。

**mode 往核里传（这一轮加的）：body 多一个可选「mode」**——认 passive（缺省，人发消息触发）
与 proactive（她自己开口，日子线给她一个由头）。**非法值不 400**，回落 passive：与 scene 同
一个口径（这一格只决定她这一轮的提示词与心判决，判不出就按常态走，不拦人）。proactive 的三个
入参：`seed`（由头，**必填**，缺了或全空白 400 并点名）、`reason`（理由码，可选，进提示词与
回包 seed.reason）、`who`（可选）。它与常态的差别**只有输入**：没有「对方原话」，这一轮的一问
就是 seed（回想也用它），`segment` 给了也忽略（这一轮没有别人说话），recent 照常只当背景。
出站闸、拒绝、边界三关一个字都不改地走同一条路。回包因此多两格：`mode`（passive / proactive）
与 `seed{text, reason}`。**产出为空是另一件事**：send=true 而装配出来的 reply 是空（模型那句
被嘴弃掉、或改完仍是空）时，不认识成「门说不发」，而是 gate.outbound 写 {status: core_empty}
并清空 chunks——两者都会让 chunks 空，混成一条，人事后就分不清该查门还是查嘴。

**顺序钉死：冷却 → audience 边界 → 闸 → 长度。** 冷却排在所有人前面，是因为它早于装配——那一轮
压根没有 `recalled`，本就没有边界话可说；也因为「这个话题已经拒过」比「这条记忆是私密」更靠前：
让边界话抢在冷却之前，就是把一次已经记过的拒绝再说一遍、再投一遍。audience 边界排在闸之前，是
因为边界话也要过八股闸（她的话都过闸，边界不是免检章），而闸是逐泡改/弃的那一关，替换必须发生在
它之前。判据读不动时 (a) 场景判不出就不判场景、(b) 私密判不出就**一条记忆都不引用**：没有判据，
就没有哪一条敢说它能往外讲。

成功体里多一节 refusal（**不破坏既有键**）。**冷却那一格恒在**：{cooling, topic, note}——
以前「什么也没判出来」时这一节整节不出现，于是人分不清「判过了、没在冷却」与「压根没看」；现在
cooling 永远答得出来（true / false，抽不到话题时 false + topic:null），note 说清这一轮冷却这一关
判成了什么、为什么。**真判出拒绝时才再追四样**：{kind, shape, verdict_reason, delivered{delivered,
topic, until, remaining_min, reason}}——那四样是拒绝的读数，不是冷却的读数，没判出来就不加空壳。
冷却那一轮（短路那一轮）另外多几格：{cooling: true, topic, remaining_min, reason, line, say,
event: null, note}，line 是面板那一句人话（模板与单位在 data/cooldown.json），say 是她这一轮真说的
那一句，reason 是心给的理由（refused-topic）。**event 恒为 null 是有意的**：这一轮不投事件，
所以它没有事件可给——写成「投了、只是没落盘」就是编。

**拒绝两态：「说出去过」与「没出去」——冷却只认前者。** 判拒绝发生在闸之前（enforce 要拿她进门
那句原话判），但**投递与起话题冷却必须在闸之后**：先知道这句话到底出了没有。判据只有一处
（_went_out）：`payload["send"]`（出站闸的答案）为真是前提，八股闸还得没把这一轮整句吞掉
（gate.decision 不是 dropped、chunks 还在）。出门 → 账本记 sent=True 并 heart.refuse.ingest
起话题冷却；没出门 → 账本记 sent=False、**绝不调 ingest**，另记是哪一种来由（门说不发 / 门说发
而闸吞了——两种说法都在 data/refusal.json 的 states 节）。`payload["send"]` 缺失或不是布尔当场
抛，不猜。**她拒了但没说出来，对方根本不知道；算冷却已用等于白白烧掉一次拒绝。**

**这一轮借了家里哪几份词条、各用掉几条也随成功体出去**（同样是加一节、不动既有键）：多一节
borrow {status, files:[{source, borrow_id, learned}], note}。名单是 api/chat.py 的 _books() 真读过
的那几份（clauses.json / policy.json——称呼表是装配那一步读的，这一层不替它记账），条数是这一轮
自己的账（条款按过闸真比过的那些算：有泡可过闸就是启用条款全表，一泡都没有就是 learned=0，
**0 是一次真实读数、不是缺失**）。家里还没有这份借阅条就先 register_borrow 登记一条（一 source 一条，第二轮不再登记），
再逐份 record_readout 写读数。**投递失败不许影响这一轮说话**：整段包在 try 里，失败只在 borrow
节里如实写 status=failed 与原因，她的 reply / chunks 一个字节都不动。

不加鉴权、不改绑地址：这一层的入口由 devserver / 宿主决定，端点只认 POST /chat。
每次失败写一行 WARNING 日志（logger 名 qqagent.chat，含 kind 与 message）——失败要留痕，
但日志不是给页面的东西，页面只读返回体。
"""
from __future__ import annotations

import concurrent.futures
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from organs.brain import model as brain_model
from organs.heart import gate as heart_gate
from organs.heart import refuse as heart_refuse
from organs.home import residents as home_residents
from organs.home import store as home_store
from organs.mind import conversation
from organs.mind import events
from organs.mind import llm
from organs.mind import recent as mind_recent
from organs.mind.llm import LLMError
from organs.mouth import action_note, address, audit, borrow, cooldown, drops
from organs.mouth import gate, refusal, repeat, warehouse
from organs.mouth.pace import fill as pace_fill          # 节奏：与装配同一个函数，不另造一套
from organs.mouth.split import split as split_text       # 切分：包里的 split 是函数名，这里直接点模块
from organs.mouth import kin as kin_rules               # 认亲：认不认这个号由人名录定（判据在 data/kin.json）
from organs.mouth import quota as quota_rules          # 参数名就叫 quota，模块用别名进来免得被挡住
from organs.mouth import rewrite as rewrite_rules      # 参数名就叫 rewrite，模块用别名进来免得被挡住
from organs.mouth import style as style_rules          # 目标长度与句末标点：判据在 data/style_target.json
from organs.mouth.gate import ClauseBroken

from .core import Result

CHAT_ROUTES = {"/chat": "POST"}

LOGGER_NAME = "qqagent.chat"
MISSING_TEXT = "POST /chat 要给 text（一句话）"
MISSING_SEED = "POST /chat 的 proactive 要给 seed（这一轮她自己开口的由头）"
SEGMENT_BROKEN = "POST /chat 的 segment 收不了：%s"   # 段形状坏：400，并把她认不出的那一条点名
STORE_MISSING = "魂未载入：说话前先起核"
BRAIN_MISSING = "脑未载入：没有记忆就不开口"

# 模型失败 → HTTP：拿不到 key 是配置问题（503，去补），鉴权/服务端是上游的脸，
# 超时与限流是「等会儿再来」，空内容是上游没给话（502）而不是这一轮的错。
STATUS_BY_KIND = {
    "no-key": 503,
    "auth": 502,
    "timeout": 504,
    "rate_limit": 429,
    "empty": 502,
    "http": 502,
    "config": 500,
}
DEFAULT_STATUS = 502

LOG = logging.getLogger(LOGGER_NAME)

#: 出站闸没判成时 gate.outbound.status 的取值（不是门的一个判决，是「这一关没跑」的记号）。
OUTBOUND_UNJUDGED = "unjudged"
#: 产出为空时 gate.outbound.status 的取值（send=true 却一个字都没装出来）：与 send=false 两件事。
CORE_EMPTY = "core_empty"

# 重写那一次模型调用的注入点：与装配（answer() → llm.complete）走的是同一条出口，本文件不另开
# HTTP 客户端。生产三样都是 None（配置从 data/llm.json 读、key 从环境读、transport 走 urllib）；
# 测试把假 transport / 假配置塞进这一格，一个字节都不发真网络。
TRANSPORT: Optional[Any] = None
CONFIG: Optional[Mapping[str, Any]] = None
ENV: Optional[Mapping[str, str]] = None

# 这一轮叫他什么，本进程内的一本薄账（address.SessionAddressBook）：**只活这个进程、不落盘**。
# 退出即丢是它的全部语义——真要长久的叫法靠投给脑的那条亲历记忆（rename_memory），下回合由人名录
# 带回来；这本账只负责「他刚说完、还没轮到下一轮」那一小段。换进程＝新的一本，回到名录档位。
ADDRESS_BOOK = address.SessionAddressBook()

GATE_NOTE = ("闸在 /chat 生效：answer() 返回之后逐泡过一遍闸（条款 → 动作括注 → 内容重复 → 复述审计），"
             "命中就交给模型改一次（同一个 transport、带 budget_ms 硬时限）；改完不命中就用新稿，"
             "二次仍命中才弃那一泡，改不动（超时 / 空 / 不合规 / 出口抛错）则保留原话发出并记账；"
             "弃掉的泡从 chunks 里删掉，send_at 按剩下的重算。"
             "in_parts 是进闸的泡数、out_parts 是出闸的泡数，blocked 是这次拦下过她的 clause_id"
             "（去重、按出现顺序），parts 逐泡交代判词与这一泡过闸前后的原文，"
             "另带这一泡的几样「判没判」读数：audit 是复述审计（status 不是 ok 就是没比过；"
             "skipped 是「对方要她重复／引用、这一关只记账没判废」，此时 hit 照旧带着）、"
             "action_status 是动作括注、repeat_status 是内容重复（skipped-by-request 是「对方要她"
             "再说一遍、这一关只记账没判废」）、quota_status 是动作配额、claim_status 是冒充做过"
             "（not-checked 是这一轮没拿到动作账、这一关没跑）、kin_status 是认亲（not-checked 是"
             "这一轮没拿到人名录读数、这一关没跑，**不是「判过不是认亲」**）、"
             "fabricate_status 是编记忆（not-checked 同理，这一轮没拿到想起来的记忆）、"
             "fatal_status 是红线这一关"
             "（not-checked 都是这一关没跑，不是通过）、fatal 是踩上的那一类红线（没踩就是 null），"
             "以及 kept_after_rewrite 是「改过一次还是命中、按原话发了」（true 时这一泡没被丢掉），"
             "加上 rewrite 是这一泡的重写读数"
             "（{status: ok|failed|not-tried, why, from, to}，from / to 只是一小截）；"
             "顶层的 quota 是这一轮交给闸的配额写法"
             "（forbid / allow / null），取自装配 action.quota.blocked，装配没标就是 null。"
             "每一泡弃言都记一行账（organs/mouth/drops.py）。")


def dispatch(route: str, body: Optional[Dict[str, Any]],
             store: Any, brain: Any) -> Result:
    """POST /chat：一句话进来，一整轮出去；判得出拒绝就在这儿投一次心。

    body 非 dict、text 缺失或全空白 → 400；store / brain 未载入 → 503（说不出话的两种原因
    分开报：没有魂是没底色，没有脑是没记忆）。LLMError 之后一律非 200 + ok:false。

    **在冷却里的话题先短路**：认得出话题、心又说它还在冷却 → 不调模型、不投事件，直接交回那一句
    拒绝（_cooled_payload）。这一步排在 answer() 前面，所以那一轮一次模型调用都没有；认不出话题
    或判据读不动就往下走，照常说话。

    body 里还可以给一个 `scope`（group / private，缺省取数据里那个）：它只决定「私密记忆这一轮
    能不能被引用」。非法值当场 400 并点名，连魂与脑都不必在场——body 就是错的。

    body 里还可以给一个 `scene`（group / private / unknown，缺省 unknown，旧字段名 scope 兜底）：
    它只决定「这是哪儿说话」——装配里写哪一句、落脑写哪一栏、回想时脑要不要把私密条目打上
    retellable=False。**非法值不 400**（落成 unknown，不猜成 private），与上面那一格各读各的。

    body 里还可以给一个 segment（刚刚这一小段，两种形状都收）：归一化后交给 answer(segment=...)，
    进「这一小段」那一节；形状坏了当场 400。没给就是 None（私聊与老调用一字不改）。
    **proactive 那一轮段一律忽略**（给了也不读、不判形状）：段是别人刚刚说的话，这一轮没人说话。

    body 里还可以给一个 mode（passive / proactive，缺省 passive）：**非法值不 400**，回落 passive
    （与 scene 同一个口径）。proactive 是她自己开口那一轮，入参三样：seed（由头，**必填**，
    缺了或全空白 400 并点名——没有由头就没有这一轮）、reason（理由码，可选，进提示词与回包
    seed.reason）、who（可选，与常态同一格）。它与常态的差别**只有输入**：没有对方原话，这一轮
    的一问就是 seed（回想也用它），段为空，recent 照常只当背景；出站闸、拒绝、边界三关走同一条
    路（`_outbound_round` 一个字都没改）。回包因此多两格：mode 与 seed{text, reason}
    ——注意顶层 reason 一直是**出站闸**的理由码，与由头的理由码不是一回事，两处不共用一个字段。

    **产出为空**也在最后一步看：门说发（send=true）而装配出来的 reply 是空（模型那句被嘴弃掉、
    或改完仍是空）时，`gate.outbound` 写 {status: core_empty} 并清空 chunks——这一笔与
    send=false 是两件事（那一件是门的结论，这一件是装配没装出话来），别混成一条。

    装配回包里的 event（核·识别这一轮的读数：kind / who / applied / reason / mode）**照旧原样
    透出去**：本层不重组、不改写、不拿它做任何判断——识别与投心是装配那一步的事。

    成功之后先过闸：拿 answer() 装配进去的那几段原文（魂 / 锚句 / 记忆原文 / 心档位词 / 规矩句 /
    对方原话）与装配体逐泡过一遍闸，结果就地落回 payload（chunks / reply / gate）。闸坏了不 fail-open：
    判据读不动就整轮弃掉，并在 gate 里如实说为什么。

    再过一步：拿对方那句（text）与她那句（payload["reply"]）判一次拒绝，判出来就投——
    **全仓唯一的投递点就在这一处**。判据坏了（拒绝数据读不动）不改这一轮的成功：她那句话已经
    说出口了，写一行 WARNING、并在 refusal 那一节里如实说这次没判成、没投心。

    最后一步是**出站闸**（organs/heart/gate.py，_outbound_round）：装配用私有键 "_gate_input"
    交回这一轮判决用的那一份 EngageInput，门据此只回答一件事——这一轮发不发。send=false 就
    chunks=[]、reply=""，send / reason / level 三格与 limits（交给嘴）一并回包；群号只看 body
    里的场合（私聊一律 None，不占群额度）。取不到输入就记一行 WARNING 并在 gate.outbound 里
    说这一关没判——不静默，也不替她拿主意。
    """
    # 谁起的头先归一化：proactive 只认那一个字面值，其余（含缺省 / 非法值）一律 passive。
    # 它排在最前面，因为下面「这一轮的输入是什么」两种 mode 各读各的。
    mode = _mode_of(body)
    seed = ""
    segment: Optional[List[Tuple[str, str]]] = None
    if mode == events.MODE_PROACTIVE:
        # 她自己开口：**必填的是由头**，不是 text；段给了也不读（这一轮没有别人说话）。
        seed = _seed_of(body)
        if not seed:
            return 400, {"ok": False, "message": MISSING_SEED}
        text = seed
    else:
        segment, broken = _segment_state(body)
        if broken:
            return 400, {"ok": False, "message": broken}
        text = _text(body)
        # 只给了段、没给 text：这一问就是段里最后那一条（他刚说的那句），不另外编一句。
        if text is None and segment:
            text = segment[-1][1]
        if text is None:
            return 400, {"ok": False, "message": MISSING_TEXT}
    # 场景（可选，缺省值是数据里那个）：只决定「私密记忆这一轮能不能被引用」，非法值当场 400 并点名。
    scene = _scope_state(body)
    if scene["error"]:
        return 400, {"ok": False, "message": scene["error"]}
    # 场合（可选，缺省 unknown）：只决定「装配里写哪一句 / 落脑写哪一栏」，非法值不 400。
    place = _scene_of(body)
    if store is None:
        return 503, {"ok": False, "message": STORE_MISSING}
    if brain is None:
        return 503, {"ok": False, "message": BRAIN_MISSING}
    # 闸要的「她最近几条回话」在答题之前读：answer() 最后一步会把这一轮追加进同一份账，
    # 之后再读就把本轮当成了历史——她这句会跟自己比一次，于是每一句都成复读。
    history = _her_recent()
    # 认亲那一格同样要在答题之前读：answer() 最后一步会把他这句话投进脑，而 ingest 会顺手给他
    # 建一张卡——读晚了这一关永远看到「他在册」，于是对谁都放行，那不是判据，是自己把自己的读数
    # 改了。所以「这一轮他在不在人名录里」问的是**这一轮开口之前**的名录。
    known = _known(brain, _who(body))
    # 冷却先问一次心：在冷却里就整轮短路，模型一次都不调（见 _cooled_payload 的说明）。
    cooled = _cooling_state(text)
    if cooled["cooling"]:
        return 200, _cooled_payload(cooled, text, store, history, _who(body),
                                    scope=scene["scope"], known=known)
    try:
        # mode 原样交给装配：passive 是人发消息触发的，proactive 是她自己开口（由头在 seed）。
        # 装配按 seed 给不给决定走哪条路（prompt.proactive / INTENT_PROACTIVE / 只有她那句），
        # 这一层只负责认字段与翻状态码。
        payload = conversation.answer(
            text, who=_who(body), scene=place, segment=segment, mode=mode,
            aliases=_aliases_of(body),
            seed=seed if mode == events.MODE_PROACTIVE else None,
            reason=_reason_of(body) if mode == events.MODE_PROACTIVE else None,
            store=store, brain=brain)
    except LLMError as exc:
        status = STATUS_BY_KIND.get(exc.kind, DEFAULT_STATUS)
        LOG.warning("POST /chat 失败：kind=%s message=%s", exc.kind, exc.message)
        return status, {"ok": False, "kind": exc.kind, "message": exc.message}
    # 出站闸先判、先问、**先不落手**：拒绝要不要算数取决于她的话到底有没有出去，
    # 所以 sent 必须在判拒绝之前拿到。清正文（send=false 的双保险）留到最后一步。
    _outbound_round(payload, body)
    sent = _send_flag(payload)                 # 只认 payload["send"]（门说发不发）；缺了或不是布尔当场抛
    _address_round(payload, text, brain, _who(body))
    boundary = _audience_round(payload, text, _who(body), scene["scope"], sent=sent)
    _gate_round(payload, text, store, history, scope=scene["scope"], who=_who(body),
                known=known)
    # 八股闸排在出站闸之后，它也可能把这一轮的话整句吞掉：门说发、闸吞了，对方还是没听到。
    # 所以「出没出门」到这一步才定（_went_out 是唯一那处判据），边界话那条的投递也搬到这里。
    judged = boundary["judged"]
    if judged is not None:
        _deliver_judged(judged, payload)
    else:
        outcome = _went_out(sent, payload)
        judged = _judge(text, payload.get("reply"), sent=outcome["sent"],
                        reason=outcome["reason"], level=outcome["level"], why=outcome["why"])
    payload["refusal"] = _refusal_section(cooled, judged)
    _borrow_round(payload)
    _outbound_apply(payload)
    _core_empty(payload)
    return 200, payload


# —— 出站闸：这一轮到底发不发 ———————————————————


def _outbound_round(payload: Dict[str, Any], body: Optional[Dict[str, Any]]) -> None:
    """拿装配交回的那一份 EngageInput 问心一句「这一轮发不发」，答案落进回包。

    **这一层一个判据都不写**：门只回答发不发（send）与拦下时的 level / reason，心怎么想
    （organs/heart/engage.py 那三道硬门、五道软门）、嘴说什么都不归它。这里做四件事：
    取出输入 → 取群号 → 调门 → 把门的答案原样写进回包。

    - **输入是装配那一份**：answer() 用 "_gate_input" 交回来（私有键，pop 掉就不会进 JSON）。
      取不到不当作「发」也不当作「不发」，而是如实说这一关没判：记一行 WARNING，
      gate.outbound 写 status=unjudged——门缺席不该让她开不了口，但也不许假装判过了。
    - **群号只看 body**（_group_of）：私聊一律 None，不占群额度（门那边 group=None 就是私聊）。
    - **root 不传**：门自己走器官默认根（HEART_ROOT 是测试的切换口），这一层不替它挑根。
    - **不发就把话说回去**：chunks=[]、reply=""——她要说的那句不发了；
      send / reason / level 三格照门说的写，limits 交给嘴（有限制没限制都在这一格，形状不随状态变）。
    """
    inp = payload.pop("_gate_input", None)
    if inp is None:
        LOG.warning("POST /chat 出站闸未判：装配没交出 EngageInput（这一轮照常发）")
        _unjudged(payload, "装配没交出 EngageInput（_gate_input 缺席）")
        return
    verdict = heart_gate.gate(inp, group=_group_of(body))
    payload["send"] = bool(verdict["send"])
    payload["reason"] = verdict["reason"]
    payload["level"] = verdict["level"]
    payload["limits"] = dict(verdict["limits"] or {})


def _outbound_apply(payload: Dict[str, Any]) -> None:
    """闸的最后一步：send=false 就把正文清干净（双保险）。

    放在判拒绝之后：那一判要用她装配出来的原话，先清正文会把输入抹掉。这里只落手，不重判。
    """
    if payload.get("send") is not True:
        payload["chunks"] = []
        payload["reply"] = ""


def _core_empty(payload: Dict[str, Any]) -> None:
    """产出为空：门说发，装配却一个字都没拿出来——chunks 清空，gate.outbound 记 `core_empty`。

    **与 send=false 是两件事**：那一件是「门说这一轮不发了」（正文其实装出来了，被门拦下），
    这一件是「装出来的就是空的」（模型那句被嘴弃掉、或改完仍是空）。两者都会让 chunks 空，
    混成一条，人事后就分不清该查门还是查嘴。所以这里只在**门说发**而 reply 空时才记这一笔；
    门已经判过不发的那一轮不动它——那一轮的沉默是门的结论，不是空产出。

    已经记过的那一格不覆盖（未判 unjudged 是另一份读数：那一关根本没跑，别把它改写成一笔
    「空产出」，那是替一次没发生的判决作证）。
    """
    if payload.get("send") is not True:
        return
    if str(payload.get("reply") or "").strip():
        return
    section = dict(payload.get("gate") or {})
    if "outbound" in section:
        return
    section["outbound"] = {"status": CORE_EMPTY}
    payload["gate"] = section
    payload["chunks"] = []


def _unjudged(payload: Dict[str, Any], why: str) -> None:
    """门这一轮没判：四格照常给（send=true，她照常开口），并在 gate.outbound 里写明为什么。

    没判不是通过、也不是拦下——两样都不许冒充，所以 reason / level 是 None（门没给答案），
    limits 是空（没有限制可谈）。gate 节其余键一个不动（decision / hit / drops 是嘴那一道的）。
    """
    payload["gate"] = dict(payload.get("gate") or {},
                           outbound={"status": OUTBOUND_UNJUDGED, "why": why})
    payload["send"] = True
    payload["reason"] = None
    payload["level"] = None
    payload["limits"] = {}


def _group_of(body: Any) -> Optional[str]:
    """这一轮在哪个群：只认 body 里的场合，**私聊一律 None**（不占群额度、不写群窗）。

    两个来源按约定次序：body["where"]["groupId"] 先，取不到再退到老字段 body["group"]。
    场合不是 group 就是私聊，哪怕 body 里塞了群号也不占——场合是这一轮的判据，群号只是账。
    """
    if not isinstance(body, Mapping) or _scene_of(body) != brain_model.SCENE_GROUP:
        return None
    where = body.get("where")
    for raw in ((where.get("groupId") if isinstance(where, Mapping) else None),
                body.get("group")):
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


# —— 出门没有：门说发不发 + 八股闸吞没吞（起不起冷却的唯一判据） ————

#: 没出门却说不清是哪一种来由时的那句人话（正常路径到不了：_went_out 一定给得出那两种之一）。
UNKNOWN_SUPPRESSED = "她的话没发出去，也没说清是哪一种来由（门说不发 / 门说发而闸吞了）"


def _send_flag(payload: Any) -> bool:
    """这一轮发不发：只认 payload["send"]（出站闸的答案），缺了或不是布尔就抛，**绝不猜**。

    不许默认成发了（那会把一次没出门的拒绝记成发出去了，还顺手烧掉一次冷却）；也不许拿闸的
    别的读数顶替（chunks 非空不等于话出去了：send=false 时正文要到 _outbound_apply 才清）。
    判不了就说判不了，抛出来点名缺的是哪一格。
    """
    value = payload.get("send") if isinstance(payload, Mapping) else None
    if not isinstance(value, bool):
        raise refusal.RefusalBroken("的 payload.send 没给或不是布尔（读到的是 %r）：这一轮的话"
                                   "到底有没有出去判不了，拒绝生效与否要等这一格（门说发不发）"
                                    % (value,))
    return value


def _swallowed(payload: Mapping[str, Any]) -> bool:
    """八股闸把这一轮的话吞了没有：判词记 dropped，或者一泡都不剩。

    这不是第二处判据，是同一个问题的另一半：门说发、闸却把话吞了，对方还是一个字没听到。
    """
    section = payload.get("gate")
    section = section if isinstance(section, Mapping) else {}
    return section.get("decision") == "dropped" or not (payload.get("chunks") or [])


def _went_out(send: bool, payload: Mapping[str, Any]) -> Dict[str, Any]:
    """这一轮的话到底出门没有：门说的（send）+ 八股闸吞没吞。**这一层只有这一处判据。**

    返回 {"sent", "why", "reason", "level"}：
      sent    话真出去了没有（出去才算冷却用过）
      why     没出去时是哪种来由的名字（refusal.SUPPRESSED_OUTBOUND＝门说不发 /
              SUPPRESSED_SWALLOWED＝门说发而闸吞了）；出去了就是 None。整句说法在数据里，
              由 refusal.record 取——这里只报是哪一种，好让复查时分得清「她不想说」与
              「她自己把话拦了」。
      reason  门给的那句理由（落账用）
      level   门给的那一层（落账用）
    """
    if not send:
        why: Optional[str] = refusal.SUPPRESSED_OUTBOUND
    elif _swallowed(payload):
        why = refusal.SUPPRESSED_SWALLOWED
    else:
        why = None
    return {"sent": why is None, "why": why,
            "reason": payload.get("reason"), "level": payload.get("level")}


def _deliver_judged(judged: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    """边界话那条拒绝的投递：**搬到闸之后**——先知道话出没出去，再决定冷却烧不烧。

    它那一份 sent 是闸之前拿到的（门说发不发），这里再叠一次「八股闸吞没吞」（_went_out），
    与平常拒绝那条路是同一个判据：两条路只有一个投递口，也只有一处「算不算出门」。
    """
    if not isinstance(judged, dict) or "event" not in judged:
        return
    outcome = _went_out(bool(judged.get("sent")), payload)
    judged["delivered"] = _deliver(judged.get("asked"), judged["event"], judged.get("kind"),
                                   sent=outcome["sent"], reason=judged.get("reason"),
                                   level=judged.get("level"), why=outcome["why"])


# —— 称呼：这一轮叫他什么 ——————————————————————

def _address_round(payload: Dict[str, Any], asked: str, brain: Any, who: str) -> None:
    """认「他要求改称呼」→ 当场换 → 投给脑一条亲历记忆 → 记进本进程那本薄账，并把读数落进回包。

    四步，顺序钉死（别调换）：

    1. **认**：address.requested_name(asked, 表) 从**对方这句话**里抽他要的叫法。判据全在
       data/address_table.json 的 rename_markers（named 带捕获组＝新叫法，denied 不带＝只否掉
       旧叫法）；抽不到就是 None——不编、也不从人名录里挑一个顶上，照旧按档位叫。
    2. **换**：把这一轮的要求塞进 person 的 rename_request 再 resolve（优先级：会话覆盖 > 本轮改名 >
       人名录档位），于是这一轮 source=本轮改名。**先 resolve 再 remember**：反过来的话这一轮就会
       被自己的覆盖盖住，面板上看到的会是「会话覆盖」，分不出「这一轮他刚说的」与「上一轮记下的」。
    3. **投**：没被违禁自称挡下（renamed 且 banned_self 为空）才 rename_memory → brain.ingest(**draft)
       投一次，并 ADDRESS_BOOK.remember —— 记忆是长久的（下回合由人名录带回来），会话账只活这个进程。
       被挡下的那一轮**两样都不做**：退到中性叫法，不投那条记忆（改名不是绕过自称红线的后门）。
    4. **透出**：payload["addressed"] = {who, address, source}，面板上看得见这轮叫他什么。

    who 是人名录里的 id（脑查人只认 id 与别名，不按昵称猜人）：记会话账也用这个 id，下一轮
    resolve 的 _session_override 按同一个 id 找，才找得回来。

    坏了不 fail-open 也不 fail-close：称呼判据读不动就记一行 WARNING，**这一节不出现**（不拿空壳
    冒充「这轮没改名」）——她那句话已经说出口了，称呼这一层不许因为它自己坏掉就把整轮吞了。
    """
    try:
        table = address.load_table()
        person = brain.lookup(who) if who else None
        wanted = address.requested_name(asked, table)
        view = dict(person) if isinstance(person, Mapping) else None
        if wanted:
            view = dict(view or {})
            view["rename_request"] = wanted
        called = address.resolve(view, table, session=ADDRESS_BOOK, who=who)
        key = called.who or who
        if wanted and key and called.renamed and not called.banned_self:
            _remember_rename(brain, key, wanted)
            ADDRESS_BOOK.remember(key, wanted)
    except (address.PolicyMiss, warehouse.WarehouseMiss, warehouse.WarehouseBroken,
            OSError, ValueError, TypeError, KeyError) as exc:
        LOG.warning("POST /chat 记称呼没成：%s", exc)
        return
    payload["addressed"] = {"who": key, "address": called.address, "source": called.source}


def _remember_rename(brain: Any, who: str, called: str) -> None:
    """把「他要求过叫他某个叫法」投给脑一次：草稿的形状就是 ingest 的入参（address.rename_memory 给）。

    投不进去只记一行 WARNING：这一轮的话已经说出去了，记忆没记上不该让整轮失败；会话账照记——
    一条记忆（持久）与一本薄账（只活这个进程）是两件事，一件没成不连累另一件。
    """
    try:
        brain.ingest(**address.rename_memory(who, called, ts=_stamp()))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        LOG.warning("POST /chat 记改称呼没成：%s", exc)


# —— 出口认 audience：私密记忆在群里守边界 ————————————————

#: 判据那一节的名字（organs/mouth/data/refusal.json）：栏名、取值、阈值、说法全在那一节里。
AUDIENCE_KEY = "audience"

#: 这一节必须有的字符串键与表键：缺一个就抛（缺键不补默认值——补一个就是第二处真源）。
AUDIENCE_WORDS = ("field", "private_value", "scope_message", "say", "blur",
                  "reason", "why", "mark", "event_shape")
AUDIENCE_TABLES = ("scopes", "criteria")
#: scopes 与 criteria 两张表里各自必须有的键。
SCOPE_VALUES = ("group", "private", "default")
CRITERIA_VALUES = ("overlap_min", "gram_n", "gram_cover", "excerpt_chars")


def _scene_of(body: Any) -> str:
    """读 body 里的场合：认 scene，旧字段名 scope 兜底；缺省 / 空白 / 非法一律 unknown。

    与 _scope_state 是两件事，别合并：那一格管「私密记忆这一轮能不能被引用」（非法值当场 400
    并点名），这一格管「她在哪儿说话」（非法值**不 400**——body 没错到说不出话，只是我们不知道
    这是哪儿）。三个取值不在这里另写一份字符串，从 organs/brain/model.py 的 SCENE_* 来：
    核里多写一份，迟早跟脑分叉。**不猜成 private**：猜错就是把私事在群里说出来。
    """
    raw: Any = None
    if isinstance(body, Mapping):
        raw = body.get("scene")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raw = body.get("scope")      # 旧字段名：只给了 scope 的老调用照旧读得出来
    if isinstance(raw, str) and raw.strip() in brain_model.SCENE_TYPES:
        return raw.strip()
    return brain_model.SCENE_UNKNOWN


def _scope_state(body: Any) -> Dict[str, Any]:
    """读 body 里的场景：合法就交回那一个值，非法就交回一句点名的人话（由 dispatch 翻成 400）。

    只认数据里那两个值（`scopes` 节），缺省也是数据里那个；没给、给了空白都按缺省算。
    **判据读不动时不 400**：那是我们的数据坏了，不是他的 body 坏了，所以只记一行 WARNING 并交回
    scope=None——下游看见 None 就一条记忆都不引用（没有判据，就没有哪一条敢说它能往外讲）。

    返回 {"scope": str|None, "error": str|None}；error 非空时 dispatch 直接 400。
    """
    try:
        cfg = _audience_cfg(warehouse.load("refusal"))
        scopes = cfg["scopes"]
        allowed = [str(scopes[name]) for name in SCOPE_VALUES[:2]]
        default = str(scopes["default"])
        message = str(cfg["scope_message"])
    except (warehouse.WarehouseMiss, warehouse.WarehouseBroken, refusal.RefusalBroken,
            OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("POST /chat 读场景判据没成：%s", exc)
        return {"scope": None, "error": None}
    raw = body.get("scope") if isinstance(body, Mapping) else None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {"scope": default, "error": None}
    value = raw.strip() if isinstance(raw, str) else raw
    if value in allowed:
        return {"scope": value, "error": None}
    return {"scope": None,
            "error": _word(message, "audience.scope_message",
                           allowed=" / ".join(allowed), value="%r" % (raw,))}


def _audience_round(payload: Dict[str, Any], asked: str, who: str,
                    scope: Optional[str], *, sent: bool = True) -> Dict[str, Any]:
    """出口认 audience：私密记忆在群里守边界（剔除注入 / 被问到就说一句边界话）。

    三步，顺序钉死：

    1. **剔**：scope=group 时把 `recalled[*]` 里那一栏等于私密值的条目从出口剔掉（栏名与取值
       都取自数据）。剔掉之后这一轮就没有它了：页面不再引用它（recalled 里没有）、复述审计的
       「记忆原文」那一档也不再拿它当注入原文。剔了谁记在 payload["audience_held"] 里，每行
       {id, text_min, source}——id 与一小截摘要，**不贴正文**。scope=private 一律不动。
    2. **问**：被剔掉的那几条里，有哪一条是这一轮**被直接问到**的（判据见 _asked_private：
       连续重合与 N 元组覆盖率两条，阈值全在数据里的 criteria 节）。一条都没被问到就到此为止
       ——**不为没发生的事拒绝**：不出声、不投事件，召回里少了它这件事由 audience_held 交代。
    3. **说**：被问到了就换说法——reply 与 chunks 换成数据里那句边界话（切分与节奏仍走
       split.split + pace.fill，与平常说话同一份判据），并走拒绝那条路（_audience_judged）。
       **沉默不算数**：沉默等于「她刚好没想到」，说了才等于「她在守边界」。这一句仍要过八股闸
       （调用方在下一步做，她的话都过闸）。

    **判据读不动就一条记忆都不引用**（不 fail-open）：这时候连「哪条是私密」「算不算被问到」都
    判不出，于是把 recalled 整份剔掉、也不出声，只记一行 WARNING——没有判据，就没有哪一条敢说
    它能往外讲。payload["audience_held"] 里照样看得见被剔掉的 id（摘要那一格留空，因为截多少字
    也在那份读不动的数据里）。

    为什么排在这里：它必须在装配之后（recalled 到这时才有），又必须在过闸之前（边界话要过闸，
    而闸逐泡改/弃，替换得在它前面）。冷却更早：那一轮压根没有装配，也就没有边界话可言。

    返回 {"held": [...], "spoke": bool, "judged": {...}|None}；judged 非空时由 dispatch 拿去当
    这一轮 refusal 节里那四样（它已经投过一次，不许再让 _judge 判第二遍）。
    """
    rows = [one for one in (payload.get("recalled") or []) if isinstance(one, Mapping)]
    try:
        book = warehouse.load("refusal")
        cfg = _audience_cfg(book)
    except (warehouse.WarehouseMiss, warehouse.WarehouseBroken, refusal.RefusalBroken,
            OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("POST /chat 判 audience 没成：%s", exc)
        payload["recalled"] = []
        payload["audience_held"] = [{"id": str(one.get("id") or ""), "text_min": "",
                                     "source": str(one.get("source") or "")} for one in rows]
        return {"held": payload["audience_held"], "spoke": False, "judged": None}
    payload["audience_held"] = []
    if str(scope or "") != str(cfg["scopes"]["group"]):
        return {"held": [], "spoke": False, "judged": None}
    field, private = str(cfg["field"]), str(cfg["private_value"])
    held = [one for one in rows if _audience_of(one, field) == private]
    if not held:
        return {"held": [], "spoke": False, "judged": None}
    payload["recalled"] = [one for one in rows if _audience_of(one, field) != private]
    payload["audience_held"] = _held_rows(held, int(cfg["criteria"]["excerpt_chars"]))
    criteria = cfg["criteria"]
    if not any(_asked_private(one, asked, criteria) for one in held):
        return {"held": payload["audience_held"], "spoke": False, "judged": None}
    _say_boundary(payload, str(cfg["say"]), who)
    return {"held": payload["audience_held"], "spoke": True,
            "judged": _audience_judged(cfg, book, asked, sent=sent,
                                       reason=payload.get("reason"),
                                       level=payload.get("level"))}


def _say_boundary(payload: Dict[str, Any], said: str, who: str) -> None:
    """把这一轮的 reply / chunks 换成那句边界话：切分与节奏仍走平常那两处（不另造一套）。

    与冷却那一轮同一个拼法（split.split + pace.fill，seed 是 who 与时刻）：同一句话切几泡、
    每泡停多久，跟她平常说话是同一份判据，预览与实发才不会对不上。
    """
    policy = warehouse.load("policy")
    batch = pace_fill(split_text(said, policy, warehouse.load("action_note")), policy,
                      "%s%s%s" % (who or "", conversation.SEED_SEP, _stamp()))
    payload["reply"] = said
    payload["chunks"] = [{"text": part.text, "gap_before": part.gap_before,
                          "send_at": part.send_at} for part in batch.parts]


def _audience_judged(cfg: Mapping[str, Any], book: Mapping[str, Any],
                     asked: str, *, sent: bool = True,
                     reason: Any = None, level: Any = None) -> Optional[Dict[str, Any]]:
    """边界话走拒绝那条路：装事件 → 投一次（心那本账 + 我们自己的 refuse.jsonl）。

    与平常的拒绝**同一个投递点**（_deliver：认话题 → heart.refuse.ingest → 记一行账本），
    但**投递落在闸之后**（dispatch 调 _deliver_judged）：先知道这句话到底出了没有，再定它算不算
    冷却用过。why 里带数据里的记号（mark），所以账本那一行的 why 一眼看得出这次是 audience 挡下的。
    事件形状取自数据的 event_shape（refused-topic），本文件不写死理由名：边界话不是新理由，
    是「这个话题先不接」的另一种来由。

    事件装不出来（数据里 event_shape 指向一个不存在的形状）就记一行 WARNING、返回 None：
    那句边界话已经说出去了，事件这一笔缺了不该把整轮吞掉——但也不假装投过。
    """
    try:
        why = _word(str(cfg["why"]), "audience.why", mark=str(cfg["mark"]))
        found = refusal.event(str(cfg["event_shape"]), why, now=_stamp(), table=book)
    except refusal.RefusalBroken as exc:
        LOG.warning("POST /chat 记边界事件没成：%s", exc)
        return None
    # 投递**不在这里**：这里只把事件与「门说发不发」这一份交给 dispatch，等八股闸判完再投
    # （_deliver_judged）——门说发、闸把那句话吞了，对方还是没听到，那就不该起冷却。
    return {"kind": refusal.KIND_HARD, "shape": found["shape"],
            "verdict_reason": _word(str(cfg["reason"]), "audience.reason",
                                    mark=str(cfg["mark"])),
            "asked": asked, "event": dict(found),
            "sent": sent, "reason": reason, "level": level}


def _audience_of(one: Mapping[str, Any], field: str) -> str:
    """这一条记忆那一栏的值（去掉空白）；没有那一栏就是空串——**没标不是私密**。"""
    value = one.get(field)
    return value.strip() if isinstance(value, str) else ""


def _held_rows(held: List[Mapping[str, Any]], chars: int) -> List[Dict[str, str]]:
    """扣下的那几条在回包里的样子：{id, text_min, source}——摘要只给一小截，不贴正文。"""
    return [{"id": str(one.get("id") or ""),
             "text_min": _excerpt(str(one.get("text") or ""), chars),
             "source": str(one.get("source") or "")} for one in held]


def _excerpt(text: str, chars: int) -> str:
    """一小截摘要：截 chars 个字（截多少在数据里）；chars 不是正数就一个字都不给。"""
    body = "".join((text or "").split())
    return body[:chars] if chars > 0 else ""


def _asked_private(one: Mapping[str, Any], asked: str, criteria: Mapping[str, Any]) -> bool:
    """这一条私密记忆是不是被这一轮**直接问到**了：两条判据，过一条就算（都能离线复算）。

    - 连续重合：用户这句与这条 text 去掉空白后的最长连续重合 ≥ overlap_min 字；
    - 覆盖率：这条 text 的 gram_n 元组里有多少出现在用户这句里，占比 ≥ gram_cover。

    两条都不过就是没被问到：那一轮不主动引用、也不出声。判据与阈值全在数据的 criteria 节；
    读不动（缺键、不是数）照实抛，由调用方翻成「一条记忆都不引用」。
    """
    body = str(one.get("text") or "")
    if not body.strip():
        return False
    overlap_min = int(criteria["overlap_min"])
    gram_n = int(criteria["gram_n"])
    cover = float(criteria["gram_cover"])
    return (_longest_run(asked, body) >= overlap_min
            or _gram_cover(asked, body, gram_n) >= cover)


def _longest_run(asked: Any, body: Any) -> int:
    """两段字的最长连续重合有几个字（去空白后逐字比）：0 就是一处都没撞上。"""
    left = "".join(str(asked or "").split())
    right = "".join(str(body or "").split())
    best = 0
    previous = [0] * (len(right) + 1)
    for one in left:
        current = [0] * (len(right) + 1)
        for index in range(1, len(right) + 1):
            if one == right[index - 1]:
                current[index] = previous[index - 1] + 1
                best = max(best, current[index])
        previous = current
    return best


def _gram_cover(asked: Any, body: Any, size: int) -> float:
    """这条 text 的 N 元组覆盖率：它的元组里有几分之几出现在用户这句里（0.0–1.0）。"""
    left = "".join(str(asked or "").split())
    right = "".join(str(body or "").split())
    if size < 1 or len(right) < size:
        return 0.0
    grams = {right[index:index + size] for index in range(len(right) - size + 1)}
    if not grams:
        return 0.0
    return len([one for one in grams if one in left]) / float(len(grams))


def _audience_cfg(book: Any) -> Mapping[str, Any]:
    """取 audience 那一节：字符串键、两张表、表里那几个数，缺一个就抛并指名。

    读不动一律抛（缺键不补默认值）：补一个「私密」或补一个阈值，代码里就又多一处真源。
    """
    part = book.get(AUDIENCE_KEY) if isinstance(book, Mapping) else None
    if not isinstance(part, Mapping):
        raise refusal.RefusalBroken("的数据里没有一节可用的 %r（私密记忆的出口判据）" % AUDIENCE_KEY)
    for name in AUDIENCE_WORDS:
        value = part.get(name)
        if not isinstance(value, str) or not value.strip():
            raise refusal.RefusalBroken("的 %s 节缺 %r，或它不是一句非空字符串（缺键不补默认值）"
                                        % (AUDIENCE_KEY, name))
    for name in AUDIENCE_TABLES:
        if not isinstance(part.get(name), Mapping):
            raise refusal.RefusalBroken("的 %s 节缺 %r，或它不是一份键值表（缺键不补默认值）"
                                        % (AUDIENCE_KEY, name))
    for name in SCOPE_VALUES:
        value = part["scopes"].get(name)
        if not isinstance(value, str) or not value.strip():
            raise refusal.RefusalBroken("的 %s.scopes 缺 %r（场景认的那几个值缺一个，"
                                        "缺键不补默认值）" % (AUDIENCE_KEY, name))
    for name in CRITERIA_VALUES:
        value = part["criteria"].get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise refusal.RefusalBroken("的 %s.criteria 缺 %r，或它不是个数（判据不许是字面量，"
                                        "缺键不补默认值）" % (AUDIENCE_KEY, name))
    return part


def _word(template: str, where: str, **fields: Any) -> str:
    """把数据里那句话填成人话；占位符在数据里有、代码这里没给就抛，别把 {mark} 发出去。"""
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise refusal.RefusalBroken("的 %s 这句填不出来：%s" % (where, exc)) from None


# —— 长度与句末标点（style）：过闸之前先把「形」修一遍 ————————————————

# 两层都在 organs/mouth/style.py 判，判据全在 organs/mouth/data/style_target.json：逐泡的目标长度
# （按场景 × tone 档）、整段的句末「。」比例。这里只负责接线：哪一段交给谁改、改完的整段怎么
# 重新切回泡。整段的三件套形状**不在这一层判**（见下面 REPLY_SCOPE 那段说明）。

#: 数据里标了 scope=reply 的条款是「按整条回复判」的（三件套那种跨得上泡边界的形状）。
#: **这一层不判它**：装配那一步（organs/mind/conversation.py 的 _through_mouth）已经拿整句过了
#: 一遍闸，命中就弃；这里再判一遍是同一句话判两次，而那一层连重写器都没有——多一层不会更好。
REPLY_SCOPE = "reply"

#: 回包 gate.style.status 的取值：ok=两层都真判了；not-checked=读不动/没跑，不许当成通过
STYLE_OK = "ok"
STYLE_NOT_CHECKED = "not-checked"

STYLE_NO_TIER_NOTE = ("这一轮没拿到语气档（回包里 tone.tier 缺了，或它不是整数）：长度与句末标点"
                      "这两层都没跑——**档位不许猜**：猜一个顶上，账本上就再也分不清这一句是拿"
                      "哪一套区间判的。")
STYLE_NOTE = ("过闸之前先修形：逐泡对目标长度（超了改一次、改不动放行并记账）、整段对句末「。」"
              "比例（少于数据里的最少句数不判）。两层判据都在 "
              "organs/mouth/data/style_target.json，本层不写死一个数。")


def _tone_tier(payload: Mapping[str, Any]) -> Optional[int]:
    """这一轮的语气档（tone.tier，1..4）：拿不到就是 None——长度这一层宁可没跑，也不猜一个档。"""
    one = payload.get("tone")
    value = one.get("tier") if isinstance(one, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _reply_cap(payload: Mapping[str, Any]) -> Optional[int]:
    """核给这一轮的字数上限（reply_budget.chars）；没给就 None（那一泡拿自己的字数当上限）。"""
    one = payload.get("reply_budget")
    value = one.get("chars") if isinstance(one, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _style_sink(lo: Any = None, hi: Any = None) -> Callable[[str, str], None]:
    """长度/标点这一层的记账：走 style.record（与 /mouth/gate 同一本 style.jsonl，吃 MOUTH_TRACE_DIR）。

    给了目标区间就多带一列 target：账本上一眼看得见这一句是拿 12-32 判的，还是拿旧那套判的。
    """
    trace = style_rules.path_of()

    def sink(decision: str, reason: str) -> None:
        target = (lo, hi) if lo is not None and hi is not None else None
        style_rules.record(trace, decision, reason, datetime.now(), target=target)

    return sink


def _style_rewriter(kind: str, *, table: Mapping[str, Any], books: Mapping[str, Any],
                    facts: Mapping[str, Any]) -> Callable[[str], str]:
    """造这一层的 rewrite（text -> text）：指令按数据里的模板拼、走同一份 transport，改不动就抛。

    两句指令（把这一句说短 / 把那一段的句号收掉）都在 style_target.json 的 prompt 节，这里一个词
    都不写。拿回来的新稿先过 style.accept（非空、与原文不同、原文里的数字一个都不许丢），
    过了才交给上一层；长度与句号率是不是真合格，由 apply / stops 拿数据自己重算。

    「改短」那一档还多一条（accept 的 must_shorten）：改回来比原文还长的不算改过——不拦这一条，
    一次没改好的重写会把一泡撑成三泡，后面的闸跟着一连串误判。
    """
    def rewrite(body: str) -> str:
        said = _ask_model(style_rules.prompt(kind, body, table=table,
                                             **dict(facts, chars=len(body))),
                          books["rewrite"])
        ok, why = style_rules.accept(body, said, table=table, must_shorten=(kind == "length"))
        if not ok:
            raise rewrite_rules.Unrewritable(why)
        return said

    return rewrite


def _length_row(counted: Any) -> Dict[str, Any]:
    """一泡的长度读数（进 gate.parts[*].style）。"""
    return {"decision": counted.decision, "scene": counted.scene,
            "target_lo": counted.target_lo, "target_hi": counted.target_hi,
            "chars": counted.chars, "rewrites": counted.rewrites,
            "over_hard_cap": counted.over_hard_cap, "reason": counted.reason}


def _stop_row(counted: Any) -> Dict[str, Any]:
    """整段的句末标点读数（进 gate.style.stopped）：judged=false 是「句数不够、没判」，不是通过。"""
    return {"decision": counted.decision, "judged": counted.judged,
            "sentences": counted.sentences, "stopped": counted.stopped,
            "ratio": counted.ratio, "ceiling": counted.ceiling,
            "min_sentences": counted.min_sentences, "mark": counted.mark,
            "rewrites": counted.rewrites, "reason": counted.reason}


def _resplit(said: str, who: str) -> List[Dict[str, Any]]:
    """整段被改过之后重新切一次泡：与装配 / 边界话 / 冷却那一轮同一个拼法（split + pace）。"""
    policy = warehouse.load("policy")
    batch = pace_fill(split_text(said, policy, warehouse.load("action_note")), policy,
                      "%s%s%s" % (who or "", conversation.SEED_SEP, _stamp()))
    return [{"text": part.text, "gap_before": part.gap_before, "send_at": part.send_at}
            for part in batch.parts]


def _style_round(chunks: List[Dict[str, Any]], *, payload: Mapping[str, Any],
                 books: Mapping[str, Any], assembled: bool, scope: Optional[str],
                 who: str) -> Dict[str, Any]:
    """这一轮先把「形」修一遍：逐泡长度 → 整段句末句号率。

    返回 {"chunks": 最终那几泡, "rows": 与之一一对应的长度读数, "changed": 有没有动过字,
    "section": 进回包 gate.style 那一节}。整段被改过就重新切泡（与装配同一个拼法），切完按泡
    重算一遍长度读数——**同一次回复里不再往外发第二次请求**：改一次就是一次。

    场景（私聊 / 群聊）从 /chat 的 scope 来，档位从回包的 tone.tier 来，上限从 reply_budget.chars
    来：三个都是这一轮已经有的读数，不在这里另算一遍。拿不到档位就整层不跑并如实记 no-tier。
    """
    table = warehouse.load("style_target")
    tier = _tone_tier(payload)
    cap = _reply_cap(payload)
    original = "".join(str(one.get("text") or "") for one in chunks)
    if tier is None:
        return {"chunks": chunks, "rows": [None for _ in chunks], "changed": False,
                "section": {"status": STYLE_NOT_CHECKED, "note": STYLE_NO_TIER_NOTE}}
    info = style_rules.describe(tier, table=table, scene=scope)
    lo, hi = int(info["lo"]), int(info["hi"])
    facts = {"lo": lo, "hi": hi, "scene": info["label"], "tier": info["tier"]}
    len_sink = _style_sink(lo, hi)
    rows: List[Any] = []
    texts: List[str] = []
    for one in chunks:
        body = str(one.get("text") or "")
        rewrite = (_style_rewriter("length", table=table, books=books, facts=facts)
                   if assembled else None)
        counted = style_rules.apply(body, tier, len(body) if cap is None else cap,
                                    table=table, rewrite=rewrite, sink=len_sink, scene=scope)
        texts.append(counted.text)
        rows.append(_length_row(counted))
    reply = "".join(texts)
    probed = style_rules.stops(reply, table=table)
    stop_facts = {"mark": probed.mark, "stopped": probed.stopped,
                  "sentences": probed.sentences, "ratio": round(probed.ratio * 100, 1)}
    stop_rewrite = (_style_rewriter("stops", table=table, books=books, facts=stop_facts)
                    if assembled else None)
    marks = style_rules.stops(reply, table=table, rewrite=stop_rewrite, sink=_style_sink())
    after = marks.text
    if after != reply:
        chunks = _resplit(after, who)
        rows = []
        for one in chunks:
            body = str(one.get("text") or "")
            rows.append(_length_row(style_rules.apply(
                body, tier, len(body) if cap is None else cap, table=table, scene=scope)))
    else:
        chunks = [dict(one, text=body) for one, body in zip(chunks, texts)]
    return {"chunks": chunks, "rows": rows, "changed": after != original,
            "section": {"status": STYLE_OK, "note": STYLE_NOTE, "scene": info["scene"],
                        "scene_label": info["label"], "tier": info["tier"], "cap": cap,
                        "target": "%d-%d" % (lo, hi), "p90": info["p90"],
                        "stopped": _stop_row(marks)}}


# —— 闸：逐泡过一遍，结果落回返回体 ————————————————

def _gate_round(payload: Dict[str, Any], asked: str, store: Any,
                history: Optional[List[str]], *, assembled: bool = True,
                scope: Optional[str] = None, who: str = "",
                known: Optional[bool] = None) -> None:
    """answer() 之后、交出去之前：把 chunks 里的每一泡单独过一遍闸，结论就地写回 payload。

    assembled 标的是「这一轮有没有装配」：冷却那一轮（_cooled_payload）是 False，于是
    **凑不出注入原文（inject=None），复述审计如实记 audit-unavailable**——她没有可抄的东西，
    这一关根本没比过，写成通过就是替她背一次没人做过的检查；重写也不发请求（0 次模型调用），
    命中就保留原话发出。条款 / 动作括注 / 内容重复 / 红线这几关照旧判，一格都不少。


    六件事：

    - **逐泡过闸**：每一泡各调一次 gate.enforce（条款 → 动作配额 → 动作括注 → 内容重复 →
      冒充做过 → 认亲 → 编记忆 → 复述审计）。注入的
      原文、最近几轮、判据表都是同一份——同一轮里的几泡是同一句话切出来的，凭什么两样看待。
    - **命中先改一次，不再命中即弃**（这一轮接上的）：命中之后把「她的原话 + 命中的那一条 + 一句
      人话原因」交给模型改一次（走 answer() 同一个 transport，带 data/rewrite.json 的 budget_ms
      硬时限），拿回来先过 rewrite.accept，过了再用新稿跑第二遍闸；二次仍命中才弃那一泡。
      **改不动（超时 / 空 / 不合规 / 出口抛错）就保留原话发出**并记一笔账，绝不输出空——
      一句略有瑕疵的话也比她突然变哑强。每一泡的重写读数挂在 parts[*].rewrite 上。
    - **对方要她重复时不判废**：asked（入站那句）原样转给闸，撞上重复而对方明说要她再说一遍时，
      这一泡照发、账本记一行 skipped-by-request，repeat_status 记成跳过那个状态。**同一句请求也
      豁免复述审计**：那一关撞上了同样只记一行账（audit-skipped-by-request）、照原样发出，而
      parts[*].audit 里的 hit 与 status 一字节不改，只多一个 skipped=true——标记不丢，面板看得见
      她引了设定。两关同时撞上就各记一行，不合并。
    - **改了一次还是命中（不是红线）就留原话**：这一泡的 decision 仍是 pass、text 是进门那句
      （闸里 Verdict.kept_after_rewrite=True），账本多一行「<通道>-after-rewrite」——在最该回答的
      那一句上沉默，比说话带点毛病糟得多。**红线是唯一例外**：踩上隐私 / 辱骂 / 放弃式，这一泡
      照旧 dropped（parts[*].fatal 里看得见是哪一类）。
    - **真的落到返回体**：pass 原样留着；rewritten 换 Verdict.text（gap 沿用那一泡的）；dropped 从
      chunks 里删掉。**一处都没被改时不碰 chunks**——不接闸时逐字相同，这条是被测过的。
    - **改形排在闸之后**：留下来的那几泡再按数据修「形」（逐泡目标长度、整段句末「。」比例，
      见 _style_round）。顺序不能反：闸要判的是**她说的那句话**，先改形再判，照抄审计比的注入
      原文、内容重复比的最近几轮就都对不上。改形只动长短与句末标点，不动她说了什么。
    - **重算 send_at**：删掉几泡之后，剩下的按原来的 gap_before 累加重排，不留洞（小数位与
      pace.fill 同一个数，取自 policy 的 pace 段）。
    - **全被弃掉**：chunks=[]、reply=""，gate.decision 记 dropped；每一泡弃言由 sink 记一行账。
    - **判据读不动**：整轮弃掉（_not_gated），并在 gate.note 里说清是哪一处坏——出口不许 fail-open：
      改不了就不发这句，也不拿她的沉默去兜一次没人做过的检查之外的东西。

    动作配额（这一轮补的）也在这条路上：装配标了这一轮不许写动作就给闸 forbid、标了放行给 allow、
    没标给 None（闸记 not-checked，**没标不是 allow**）。配额命中是闸里两条不丢整句的路之一——
    它把动作行剥掉、正文接着过其余判据，于是这一泡是 rewritten 而不是 dropped，text 换成剥完的正文。

    动作账（这一轮接的）同一条路：_actions(payload) 取这一轮真发生过的动作交给「冒充做过」那一关，
    没给这一格就是 []（这一轮什么都没做，该判冒充）、给了清单就交下去、形状读不动才是 None
    （闸记 claim_status=not-checked）。它原来那句「我这就去画」改不掉时，闸**删掉那一句**、剩下的
    正文接着过其余判据，于是这一泡是 rewritten（salvaged=true、text 是删剩下的正文），删完只剩
    空白才是 dropped——谎只长在那一句里，别的话照发。

    认亲（这一轮加的）同一条路：known 是**一份人名录读数**（不在册 / 在册 + relation 档位 / 判不了就
    None），原样交给闸，判没判看 parts[*].kin_status。**「在册」不是「熟」**：够不够熟由 data/kin.json
    的 known_by 判（现在 relation ≥ 3，档位与档界全在 address_table.json 的 tiers 里），所以刚被 ingest
    建档的陌生号第二轮也照样判。**仍然在 answer() 之前读**：读的是「这一轮开口之前」的名录，不依赖
    ingest 的副作用，两轮之间口径一致（答题那一步会把他这句话投进脑，顺手就把他建了档）。
    命中之后走的是「救人」那一族——改一次还不行就把那一处称呼 / 那一句记忆换成数据里的中性说法，
    不是整条丢。

    gate 节既不新增顶层键，也不丢既有键：decision / hit / drops 是 answer() 给的那一份（页面与既有
    断言都在读它），这里追加 parts / in_parts / out_parts / blocked / quota / note；被闸改过时
    decision 与 hit 按最终结果改写——她一句都没出去，就不能还挂着 pass。
    """
    chunks = [dict(one) for one in (payload.get("chunks") or [])]
    quota = _quota(payload)
    actions = _actions(payload)
    recalled = _recalled(payload)
    section = dict(payload.get("gate") or {})
    section.update({"parts": [], "in_parts": len(chunks), "out_parts": 0,
                    "blocked": [], "quota": quota,
                    "note": GATE_NOTE if assembled else GATE_NOTE + COOLING_GATE_NOTE})
    payload["gate"] = section
    if not chunks:
        section["note"] = (GATE_NOTE + "这一轮没有泡可过闸：reply 是空串，"
                                    "装配那一步的闸已经把它整句弃掉了。")
        return
    try:
        books = _books()
        inject = _inject(asked, payload, store, books["audit"]) if assembled else None
    except (warehouse.WarehouseMiss, warehouse.WarehouseBroken, gate.ClauseBroken,
            audit.AuditBroken, action_note.ActionBroken, repeat.RepeatBroken,
            kin_rules.KinBroken,
            OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("POST /chat 过闸没成：%s", exc)
        _not_gated(payload, exc)
        return

    trace = drops.path_of()
    today = _day()
    recorded: List[str] = []

    def sink(clause_id: str, reason: str) -> None:
        """闸叫的那一声（只在弃掉那一泡时叫）：时刻在这一层给，落盘只在 drops 里。"""
        drops.record(trace, clause_id, reason, datetime.now())
        recorded.append(clause_id)

    parts: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    blocked: List[str] = []
    changed = False
    for index, chunk in enumerate(chunks):
        rewritten = _outcome()
        rewrite_one = (_rewriter(chunk["text"], asked=asked, books=books, inject=inject,
                                 recent=history, quota=quota, actions=actions,
                                 recalled=recalled, outcome=rewritten, known=known)
                       if assembled else _frozen_rewriter(books["rewrite"], rewritten))
        verdict = gate.enforce(
            chunk["text"], books["clauses"], rewrite_one,
            sink, inject=inject, policy=books["audit"],
            action=books["action"], recent=history,
            repeat_table=books["repeat"], quota=quota,
            asked=asked, keep_unrewritten=True, fatal=books["fatal"],
            actions=actions, claim=books["claim"],
            recalled=recalled, fabricate=books["fabricate"],
            kin=books["kin"], known=known)
        parts.append({"i": index, "decision": verdict.decision,
                      "hits": [_hit_row(one) for one in verdict.hits],
                      "reason": verdict.reason,
                      "before": chunk["text"], "after": verdict.text,
                      "audit": _audit_row(verdict.audit),
                      "action_status": verdict.action_status,
                      "repeat_status": verdict.repeat_status,
                      "quota_status": verdict.quota_status,
                      "claim_status": verdict.claim_status,
                      "kin_status": verdict.kin_status,
                      "fabricate_status": verdict.fabricate_status,
                      "fatal": _fatal_row(verdict.fatal),
                      "fatal_status": verdict.fatal_status,
                      "kept_after_rewrite": bool(verdict.kept_after_rewrite),
                      "rewrite": rewritten,
                      "style": None})
        if verdict.decision == "pass":
            kept.append({"text": chunk["text"], "gap_before": chunk["gap_before"]})
            continue
        changed = True
        if verdict.decision == "dropped":
            blocked.append(verdict.hits[-1].clause_id if verdict.hits else "")
            continue
        kept.append({"text": verdict.text, "gap_before": chunk["gap_before"]})

    # 长度与句末标点排在闸**之后**：闸判的必须是她说的那句话——先改形再判，闸看到的就是另一句
    # 了（照抄审计比的是注入原文、内容重复比的是最近几轮，字形一改这两关都对不上；词组命中被
    # 改短之后连命中都没有了）。反过来，改形只动长短与句末标点，落在闸之后才不会被自己改过的
    # 字骗过。这一层读不动（数据坏 / 缺件）不吞掉这一轮：话照发，回包如实记 not-checked。
    if kept:
        try:
            shaped = _style_round(kept, payload=payload, books=books, assembled=assembled,
                                  scope=scope, who=who)
        except (style_rules.StyleBroken, warehouse.WarehouseMiss,
                warehouse.WarehouseBroken, OSError) as exc:
            LOG.warning("POST /chat 长度/标点这一层没跑成：%s", exc)
            shaped = {"chunks": kept, "rows": [None for _ in kept], "changed": False,
                      "section": {"status": STYLE_NOT_CHECKED,
                                  "note": "长度与句末标点这一层没跑（%s：%s）：话照发，"
                                          "但这是「改不了」，不是「合格」。"
                                          % (type(exc).__name__, exc)}}
    else:
        shaped = {"chunks": [], "rows": [], "changed": False,
                  "section": {"status": STYLE_NOT_CHECKED,
                              "note": "这一轮一句都没留下来（闸全弃了）：没有字可判长短与句末标点。"}}
    survived = [one for one in parts if one["decision"] != "dropped"]
    rows = list(shaped["rows"] or [])
    for row, part in zip(rows, survived):
        part["style"] = row
    if len(rows) != len(survived):
        section["style"] = dict(shaped["section"], resplit=True)
    else:
        section["style"] = shaped["section"]
    if shaped["changed"]:
        changed = True
    kept = [{"text": one["text"], "gap_before": one["gap_before"]} for one in shaped["chunks"]]

    section.update({"parts": parts, "out_parts": len(kept), "blocked": _unique(blocked)})
    if changed:
        _resend(payload, kept, books["digits"])
        section["decision"] = "dropped" if not kept else "rewritten"
        first = next((row for row in parts if row["hits"]), None)
        if first is not None:
            section["hit"] = {"clause_id": first["hits"][-1]["clause_id"],
                              "reason": first["hits"][-1]["reason"]}
    if recorded:
        section["drops"] = {"today": int(drops.report(trace, today).get("today") or 0)}


def _not_gated(payload: Dict[str, Any], exc: Exception) -> None:
    """判据读不动：这一轮整句弃掉，并在 gate 里说清为什么（不 fail-open，也不假装过了闸）。"""
    payload["chunks"] = []
    payload["reply"] = ""
    section = dict(payload.get("gate") or {})
    section.update({"decision": "dropped", "out_parts": 0, "blocked": [],
                    "note": GATE_NOTE + "这一次判据读不动（%s：%s），按「改不了就不许发」整轮弃掉，"
                                        "一句都没出去。" % (type(exc).__name__, exc)})
    payload["gate"] = section


# —— 冷却：先问一次心，在冷却里就短路 ————————————————

#: 回包里 refusal.note 取数据里哪一条（三种情形各一条，全在 data/cooldown.json 的 notes 节里）
NOTE_COOLING = "cooling"
NOTE_CLEAR = "clear"
NOTE_NO_TOPIC = "no_topic"

COOLING_UNREAD = "这一轮没判成冷却（%s）：判据读不动时照常说话——「没读到」不是「在冷却里」。"

# 冷却那一轮交给闸的说明：没有装配就没有注入原文，复述审计这一关没跑（不是通过）；重写也不发请求。
COOLING_GATE_NOTE = ("这一轮在冷却里：没有装配、也没有任何注入原文，所以复述审计这一关没跑"
                     "（audit-unavailable，不是通过）；重写那一步也不发请求——命中就保留原话发出。")


def _cooling_state(asked: str) -> Dict[str, Any]:
    """对方这句话是不是在说一个正在冷却的话题：是就交回这一轮的读数，不是就交回「没在冷却」。

    认法只有一条，与投递那一层逐字相同：refusal.topic_in(asked, heart_refuse.vocabulary()) 从
    心自己那份词表里抽出话题名 → heart_refuse.check([topic]) 问心一次（**只读**，一个字节都不写）
    → items 里那一格 cooling 为真就是冷却中。**抽不到话题名就不判**：没有话题就没有「不接的那一句」，
    替她猜一个话题去拒，比不拒更糟——认不出就说认不出。

    读不动（冷却数据缺件坏件、心的账读不出来、词表读不到）只记一行 WARNING，并当成「没在冷却」：
    这一层判不出来的时候说话要紧——「没读到」不是「在冷却里」，所以不拿沉默去兜一次没做成的检查。
    回包的 refusal.note 会如实写这一轮没判成，人看得出来。

    返回 {"cooling", "topic", "item", "note"}；cooling 为真时另带 say 与 line 两句现成的人话。
    """
    try:
        table = warehouse.load("cooldown")
        topic = refusal.topic_in(asked, heart_refuse.vocabulary())
        if topic is None:
            return {"cooling": False, "topic": None, "item": None,
                    "note": cooldown.note(table, NOTE_NO_TOPIC)}
        state = heart_refuse.check([topic])
        item = next((one for one in (state.get("items") or [])
                     if isinstance(one, Mapping) and one.get("topic") == topic), None)
        if not item or not item.get("cooling"):
            return {"cooling": False, "topic": topic, "item": item,
                    "note": cooldown.note(table, NOTE_CLEAR)}
        remaining = item.get("remaining_min")
        return {"cooling": True, "topic": topic, "item": item,
                "say": cooldown.say(topic, item, table),
                "line": cooldown.line(topic, remaining, item.get("reason"), table),
                "note": cooldown.note(table, NOTE_COOLING)}
    except (warehouse.WarehouseMiss, warehouse.WarehouseBroken, cooldown.CooldownBroken,
            OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("POST /chat 判冷却没成：%s", exc)
        return {"cooling": False, "topic": None, "item": None,
                "note": COOLING_UNREAD % exc}


def _cooled_payload(state: Mapping[str, Any], asked: str, store: Any,
                    history: Optional[List[str]], who: str,
                    scope: Optional[str] = None,
                    known: Optional[bool] = None) -> Dict[str, Any]:
    """冷却里那一轮的回包：**不调模型、不投事件**，把那一句拒绝切好、过闸、交出去。

    为什么这么短路：
    - **不调模型**：心已经说不接这个话题了，再问一遍模型只会让她换个说法把同一句话重说一遍
      ——省下的是一次真调用（这也是验收里「假 transport 的调用计数为 0」那一格）。
    - **不投事件**：心那边的账上已经记着这一次拒绝了，这一层再投一次就是同一件事记两遍。所以
      这一轮的 refuse.jsonl 与她 refusals.json 都不会多一行（这也是被测过的一条）。
    - **切分与节奏仍走现成的两处**（split.split + pace.fill，seed 与装配同一套拼法）：几泡、
      每泡停多久，跟她平常说话是同一份判据，不另造一套预览与实发会对不上的算法。
    - **这一句仍要过八股闸**（_gate_round，assembled=False）：她的话都过闸，冷却不是免检章。
    - **不做复述审计**：这一轮没有装配、没有任何注入原文，她没有可抄的东西；拿入站那句话去比
      只会冤枉她，所以交给闸的 inject 是 None，闸如实记 audit-unavailable（不是通过）。

    refusal 节在这一轮多几格：line 是面板那一句人话，say 是她真说的那一句，event 恒为 null——
    这一轮不投事件，没有事件可给。判据读不动（policy / action_note 缺件）照实往上抛：连切分都
    做不了的时候假装说了一句，比报错更糟。
    """
    policy = warehouse.load("policy")
    said = str(state["say"])
    batch = pace_fill(split_text(said, policy, warehouse.load("action_note")), policy,
                      "%s%s%s" % (who or "", conversation.SEED_SEP, _stamp()))
    payload: Dict[str, Any] = {
        "ok": True,
        "reply": said,
        "chunks": [{"text": part.text, "gap_before": part.gap_before,
                    "send_at": part.send_at} for part in batch.parts],
        "gate": {"decision": "pass", "hit": None},
        "refusal": _cooled_section(state),
        "audience_held": [],               # 冷却那一轮没有装配、没有 recalled：没人可剔，这一格在
    }
    _gate_round(payload, asked, store, history, assembled=False, scope=scope, who=who,
                known=known)
    # 冷却这一轮没有装配、没有 EngageInput：出站闸无从判，四格照给并如实说「没判」。
    _unjudged(payload, "冷却那一轮没有装配、没有 EngageInput")
    return payload


def _cooled_section(state: Mapping[str, Any]) -> Dict[str, Any]:
    """冷却那一轮的 refusal 节：{cooling: true, topic, remaining_min, reason, line, say, event, note}。

    event 恒为 null：这一轮没有投事件（心那边已经在算这次拒绝了），写成「投了、只是没落盘」就是编。
    """
    item = state.get("item") or {}
    return {"cooling": True, "topic": state.get("topic"),
            "remaining_min": item.get("remaining_min"), "reason": item.get("reason"),
            "line": state.get("line"), "say": state.get("say"), "event": None,
            "note": state.get("note")}


def _refusal_section(cooled: Mapping[str, Any],
                     judged: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """拼照常说话那一轮的 refusal 节：冷却那一格恒在，拒绝那四样只有真判出来时才有。

    **cooling 恒在（true / false）是改过的口径**：以前什么也没判出来时这一节整节不出现，于是
    调用方分不清「判过了、没在冷却」与「压根没看」。现在 cooling 永远答得出来，note 说清这一轮
    冷却这一关判成了什么；kind / shape / verdict_reason / delivered 仍是「判出拒绝才有」——
    那四样是拒绝的读数，不是冷却的读数，没判出来就不加空壳。
    """
    section: Dict[str, Any] = {"cooling": False, "topic": cooled.get("topic"),
                               "note": cooled.get("note")}
    if judged:
        # 只透出这四样：事件与投递用那几格中间量已经落进两本账，回包里不重复贴。
        section.update({name: judged[name] for name in ("kind", "shape", "verdict_reason",
                                                       "delivered") if name in judged})
    return section


def _frozen_rewriter(table: Mapping[str, Any],
                     outcome: Dict[str, Any]) -> Callable[[str], str]:
    """冷却那一轮的重写器：一次请求都不发，命中就照实说「这一轮没有可用的重写器」。

    为什么不干脆给闸 None：闸拿 None 时按它自己的规矩把命中那一泡弃掉（改不了就不发），而冷却
    那一句正是她该说的那句话——整句消失比说话带点毛病糟得多。接一支只会抛的笔，闸就走
    keep_unrewritten 那条路：decision 仍是 pass、text 是进门那句、kept_after_rewrite=true，
    账本照记一行（reason 取自 data/rewrite.json 的 why.no_rewriter：这一轮压根没有可用的重写器）。
    而这一轮的硬约束是**一次模型都不调**，所以这里必须抛，不许问模型。
    """
    def frozen(body: str) -> str:
        why = rewrite_rules.no_rewriter_why(table)
        outcome.update({"status": rewrite_rules.STATUS_FAILED, "why": why,
                        "from": rewrite_rules.excerpt(body, table), "to": ""})
        raise rewrite_rules.Unrewritable(why)

    return frozen


# —— 重写：命中之后改一次，不改就留原话 ————————————————————

def _outcome() -> Dict[str, Any]:
    """这一泡的重写读数：{status, why, from, to}，进 gate.parts[*].rewrite。

    默认 not-tried：这一泡没命中，一次请求都没发。why 用数据里那句「没命中，不用改」——
    空白字段会让人以为「没看见」，而这里其实是「不需要」。
    """
    return {"status": rewrite_rules.STATUS_NOT_TRIED, "why": rewrite_rules.clear_reason(),
            "from": "", "to": ""}


def _rewriter(original: str, *, asked: str, books: Mapping[str, Any], inject: Any,
              recent: Optional[List[str]], quota: Optional[str], actions: Any,
              outcome: Dict[str, Any], recalled: Any = None,
              known: Optional[bool] = None) -> Callable[[str], str]:
    """造这一泡的 rewrite（text -> text）：把命中的那一条交给模型改一次，改不动就抛 Unrewritable。

    闸只在命中之后才调它，所以没命中的那泡一次模型请求都不发。要改的是哪一条由 gate.check 现问：
    入参与紧接着的 enforce 一模一样（同一份 clauses / inject / recent / 判据表 / asked），
    而两道判据都是纯函数、顺序也钉死了，所以两处指的必然是同一条——问一次不会分岔。

    三件事按顺序做，缺一件都不算改成：**发**（_ask_model，带硬时限）→ **验**（rewrite.accept，
    六条判据全在数据里）→ **交**（把新稿还给闸，由闸拿它再判一遍）。任何一步不过就抛
    Unrewritable，把「为什么改不动」原样带出去——闸据此保留原话、记一行账，绝不输出空。
    from / to 只放必要的一小截（截多少取自数据）：面板要看的是「改的是哪一句」，不是整段再抄一遍。
    """
    table = books["rewrite"]

    def rewrite(body: str) -> str:
        hit = gate.check(body, books["clauses"], inject=inject, policy=books["audit"],
                         action=books["action"], recent=recent,
                         repeat_table=books["repeat"], quota=quota, asked=asked,
                         fatal=books["fatal"], actions=actions, claim=books["claim"],
                         recalled=recalled, fabricate=books.get("fabricate"),
                         kin=books.get("kin"), known=known)
        before = rewrite_rules.excerpt(body, table)
        try:
            said = _ask_model(rewrite_rules.prompt(body, hit, table), table)
            ok, why = rewrite_rules.accept(body, said, table)
        except rewrite_rules.Unrewritable as exc:
            outcome.update({"status": rewrite_rules.STATUS_FAILED, "why": str(exc),
                            "from": before, "to": ""})
            raise
        if not ok:
            outcome.update({"status": rewrite_rules.STATUS_FAILED, "why": why,
                            "from": before, "to": rewrite_rules.excerpt(said, table)})
            raise rewrite_rules.Unrewritable(why)
        outcome.update({"status": rewrite_rules.STATUS_OK, "why": why, "from": before,
                        "to": rewrite_rules.excerpt(said, table)})
        return said

    return rewrite


def _ask_model(prompt_text: str, table: Mapping[str, Any]) -> str:
    """把重写那一段指令发出去、把新稿拿回来：走 answer() 用的同一条出口，带 budget_ms 硬时限。

    **同一个 transport、同一份配置。** 出口就是 organs/mind/llm.complete（模型名 / 地址 / key /
    重试都在 data/llm.json 与那份配置里，与装配那一问完全一样），本文件不另开 HTTP 客户端、
    不自己拼地址；测试把假 transport 塞进模块顶上那三个注入点，一个字节都不发真网络。

    **两个时限一起收：** transport 那一侧把 config 的 timeout_ms 换成 budget_ms（一次读写别拖过
    整件事的预算），attempts 按数据算（1 就是不重试，免得「一次重写」变成两三次）；外面再用
    线程等一个硬时限，超了就抛——**等不到就当改不动**，原话留着发。

    失败一律翻成 Unrewritable（为什么取自数据）：超时、出口抛错、配置读不动都算「改不动」，
    绝不往上抛裸异常——闸那边只认这一种「改不了」的说法。
    """
    budget = rewrite_rules.budget_ms(table)
    try:
        conf = dict(llm.resolve_config(CONFIG), timeout_ms=budget,
                    retries=max(0, rewrite_rules.attempts(table) - 1))
        messages = [{"role": "user", "content": prompt_text}]
        return _bounded(lambda: llm.complete(messages, config=conf, env=ENV,
                                             transport=TRANSPORT),
                        budget / 1000.0)
    except concurrent.futures.TimeoutError:
        raise rewrite_rules.Unrewritable(rewrite_rules.timeout_why(table)) from None
    except LLMError as exc:
        raise rewrite_rules.Unrewritable(rewrite_rules.error_why(exc, table)) from None
    except Exception as exc:          # 配置读不动、出口抛别的：一样算「改不动」，不往上抛裸异常
        raise rewrite_rules.Unrewritable(rewrite_rules.error_why(exc, table)) from None


def _bounded(call: Callable[[], str], seconds: float) -> str:
    """在 seconds 秒内等这一次调用的结果；到点就抛 concurrent.futures.TimeoutError。

    为什么用线程、而不是只靠 transport 自己的超时：那一层管的是「一次 HTTP 读写等多久」，重试、
    连接、读响应各算各的，凑起来能把一次重写拖到三四倍。这里要的是一个「整件事最多等多久」的
    硬时限，所以拿线程等：submitted 之后 result(timeout) 到点就抛。

    **不 join**：超时之后那次请求可能还在路上，等它就把这一轮 HTTP 请求挂死了。线程自己不清理
    也没关系——它不持有本文件的任何状态，回来也只会被丢掉。
    """
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(call).result(timeout=seconds)
    finally:
        pool.shutdown(wait=False)


def _books() -> Dict[str, Any]:
    """这一轮过闸用的八份判据，全部经 warehouse 读——api 里不写死一条条款、一个数。

    条数（rounds）与小数位（round_digits）也在数据里：前者是她最近几条回话的窗口，后者与
    pace.fill 算 send_at 用的是同一个数，重排时才不会跟切分那一处对不上。缺件坏件照实抛，
    由 _gate_round 翻成「整轮弃掉」——判据读不动时不猜一个默认值顶上。

    另带一份 sources（数据文件名 → 表本体），只放**这一层真读过**的那两份：借阅读数拿它算
    「这一轮借了哪几份、各用掉几条」。称呼表是装配那一步读的，这里不替它记账——报一份没读过的，
    读数就是编的。
    """
    policy = warehouse.load("policy")
    clauses_book = warehouse.load("clauses")
    repeat_book = warehouse.load("repeat")
    return {
        "clauses": clauses_book.get("clauses"),
        "audit": warehouse.load("audit"),
        "fatal": warehouse.load("fatal"),
        "action": warehouse.load("action_note"),
        "claim": warehouse.load("claim"),
        "kin": warehouse.load("kin"),
        "fabricate": warehouse.load("fabricate"),
        "repeat": repeat_book,
        "rewrite": warehouse.load("rewrite"),
        "digits": int(policy["pace"]["round_digits"]),
        # 这一层真读过的数据文件名 → 表本体：借阅读数拿它算「这一轮借了哪几份、各用掉几条」。
        # 只报这儿读过的：称呼表是装配那一步读的，这一层不替它记账（报一份没读过的，读数就是编的）。
        "sources": {"clauses.json": clauses_book, "policy.json": policy},
    }


def _actions(payload: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """这一轮真发生过的动作账：交给闸里「冒充做过」那一关，**拿不到与给了空是两件事**。

    取值只有一条路：answer() 回包顶层的 actions（手与家那一侧真做过的事，一项一份带 verb 的
    键值表）。三种情形三种结论：

    - **没有这一格 → []**：现在还没有手与家的动作账，这一轮确实一件事都没做——那就该判冒充。
      「账本还不存在」不是「判不了」：把它当成 None，这一关就永远不跑，红线等于没接。
    - **给了清单 → 原样交下去**（空清单也是清单：账上空着，照样判冒充）——闸只认字，不在这里
      改形状；形状坏了由闸那边的 claim 当场抛 ClaimBroken，照实变成「整轮弃掉」。
    - **给了别的形状（读不动）→ None**：**拿不到**，闸记 claim_status=not-checked，不判冒充、
      也不声称判过。猜一个空清单顶上，等于替她记一笔「什么都没做」——那是编出来的账。
    """
    if "actions" not in payload:
        return []
    book = payload.get("actions")
    if isinstance(book, (list, tuple)):
        return [dict(one) if isinstance(one, Mapping) else one for one in book]
    return None


def _recalled(payload: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """这一轮真的想起来的记忆：交给闸里「编记忆」那一关，**拿不到与一条都没想起来是两件事**。

    取值只有一条路：answer() 回包顶层的 recalled（脑召回的那几条，一项一份带 text / source /
    source_type / retellable 的键值表）。三种情形三种结论：

    - **没有这一格 → None**：这一轮没有装配（冷却那一轮，装配那一步压根没跑），**拿不到**这一轮
      想起来的是什么——闸记 fabricate_status=not-checked，不判、也不声称判过。猜成空清单顶上，等于
      替她作证「一条都没想起来」：她引用的细节明明是召回来的，也会被当成编的。
    - **给了清单 → 原样交下去**（空清单也是清单：这一轮确实一条都没想起来，出现「引用标记 + 具体
      细节」就该判编记忆）——闸只认字，不在这里改形状；形状坏了由闸那边的 fabricate 当场抛
      FabricateBroken，照实变成「整轮弃掉」。
    - **给了别的形状（读不动）→ None**：同上，拿不到就不判，也不猜。

    与 _actions 的差别只在一处：动作账那一条把「没有这一格」当成空账（那一轮确实什么都没做），
    而记忆这一条把「没有这一格」当成拿不到——没装配不等于她没想起什么，这两件事不能混。
    """
    if "recalled" not in payload:
        return None
    book = payload.get("recalled")
    if isinstance(book, (list, tuple)):
        return [dict(one) if isinstance(one, Mapping) else one for one in book]
    return None


def _quota(payload: Mapping[str, Any]) -> Optional[str]:
    """装配这一轮给不给写动作的资格，翻成闸认的那两种写法；没标就是 None。

    取值路径只有一条，用的是 answer() 回包里现成的那一份（装配那边按
    organs/mind/data/assembly.json 的 action.quota 数最近几轮算出来的 blocked）：

    - blocked 为真 → "forbid"（本轮一句动作行都不许写）；
    - blocked 为假 → "allow"（装配给了配额配置、这一轮放行）；
    - action / action.quota / blocked 缺了、或 blocked 不是布尔 → None：**装配没标这一轮**，
      闸记 quota_status="not-checked"。没标不是 allow——猜成放行，这一关就等于白设。

    两个写法取自 organs/mouth/quota.py 的常量（数据里 quota_values 的别名也认，但这里不必自己拼词）。
    """
    action = payload.get("action")
    if not isinstance(action, Mapping):
        return None
    book = action.get("quota")
    if not isinstance(book, Mapping):
        return None
    blocked = book.get("blocked")
    if not isinstance(blocked, bool):
        return None
    return quota_rules.FORBID if blocked else quota_rules.ALLOW


def _inject(asked: str, payload: Mapping[str, Any], store: Any,
            table: Mapping[str, Any]) -> Optional[List[Dict[str, str]]]:
    """这一轮装配进提示词的原文，按 type 分段交给复述审计；一段都拿不到就是 None。

    六段各按本来的来源取，取不到就**如实少一段**，不拿别的东西凑：

    - 魂：store.raw 那几张卡的正文（identity / manner）；
    - 锚句：answer() 真的注进去的半句（回包 anchors.items[*].injected），未配就是空；
    - 记忆原文：回包 recalled[*].text；
    - 心档位词：回包 heart 里那几档词（轴上的档位词、心境档、语气档）；
    - 规矩句：装配说明里的 reply.instruction 与那几条格式要求；
    - 对方原话：入站那句 text。

    None（一段都没取到）交给 audit 报 audit-unavailable——**不许伪造通过**：没料可比就是没比过。
    """
    kinds = _kinds(table)
    rows: List[Dict[str, str]] = []
    for kind, text in ((kinds["soul"], _soul_text(store)),
                       (kinds["anchor"], _anchor_text(payload)),
                       (kinds["memory"], _memory_text(payload)),
                       (kinds["heart"], _heart_text(payload)),
                       (kinds["rule"], _rule_text()),
                       (kinds["quote"], asked)):
        body = (text or "").strip()
        if body:
            rows.append({"kind": kind, "text": body})
    return rows or None


def _kinds(table: Mapping[str, Any]) -> Dict[str, str]:
    """六个 kind 的原词全部取自 data/audit.json：五类设定段按数据里的顺序，对方原话取 quote_kind。

    五类设定段在审计那边是同一档（照搬一律拦），所以这里的顺序只决定报出来的来源段叫什么；
    数量不对就当场抛（改数据改坏了要响，不许在这儿补一个默认名——那就是第二处真源）。
    """
    setting = table.get("setting_kinds")
    quote = table.get("quote_kind")
    if not isinstance(setting, (list, tuple)) or len(setting) != 5:
        raise ValueError("audit.json 的 setting_kinds 要是五类设定段，读到 %r" % (setting,))
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("audit.json 的 quote_kind 要是一个非空字符串，读到 %r" % (quote,))
    soul, anchor, memory, heart, rule = setting
    return {"soul": str(soul), "anchor": str(anchor), "memory": str(memory),
            "heart": str(heart), "rule": str(rule), "quote": quote.strip()}


def _soul_text(store: Any) -> str:
    """魂那几张卡的正文（identity / manner），摊平拼成一段；读不动的那张就当少一段。"""
    out: List[str] = []
    for block in ("identity", "manner"):
        try:
            raw = store.raw(block)
        except (ValueError, OSError) as exc:
            LOG.warning("POST /chat 读魂的 %s 卡没成：%s", block, exc)
            continue
        out.extend(_strings(raw))
    return "\n".join(out)


def _strings(value: Any) -> List[str]:
    """把一张卡里所有非空字符串（含嵌套的清单与键值表）摊平；空着的一律不取。"""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Mapping):
        out: List[str] = []
        for one in value.values():
            out.extend(_strings(one))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for one in value:
            out.extend(_strings(one))
        return out
    return []


def _anchor_text(payload: Mapping[str, Any]) -> str:
    """这一轮真的注进去的锚句（半句）：回包 anchors.items[*].injected，未配锚句就是空。

    不拿 say 全文替一份她没看见的原文——她抄不着没进提示词的东西，报成抄了就是冤枉她。
    """
    items = (payload.get("anchors") or {}).get("items") or []
    return "\n".join(str(one.get("injected") or "") for one in items
                      if isinstance(one, Mapping))


def _memory_text(payload: Mapping[str, Any]) -> str:
    """这一轮想起来的记忆原文：回包 recalled[*].text，一条都没有就是空。"""
    return "\n".join(str(one.get("text") or "") for one in (payload.get("recalled") or [])
                      if isinstance(one, Mapping))


def _heart_text(payload: Mapping[str, Any]) -> str:
    """这次心给的档位 / 摘要词：轴上的档位词加心境档、语气档（全是词，没有数）。"""
    heart = payload.get("heart") or {}
    out = _strings(heart.get("axes") or {})
    for part in (heart.get("mood") or {}, heart.get("voice") or {}):
        for key in ("tier", "tone"):
            value = part.get(key)
            if isinstance(value, str) and value.strip():
                out.append(value.strip())
    return "\n".join(out)


def _rule_text() -> str:
    """装配说明里的规矩句（reply.instruction 与那几条格式要求）；读不到就少这一段。"""
    try:
        conf = conversation.load_assembly()["reply"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("POST /chat 读装配说明没成：%s", exc)
        return ""
    rows = [str(conf.get("instruction") or "")]
    rows.extend(str(one) for one in (conf.get("format") or []))
    return "\n".join(one for one in rows if one.strip())


def _her_recent() -> Optional[List[str]]:
    """她最近几条自己的回话（旧的在前），只读 mind 的 recent_turns.jsonl；读不动返回 None。

    条数（最近几轮）不写死：从 repeat.json 的 rounds 读，与去重那一关看的是同一个窗口。
    三种情形必须分得开：账还没建 = 空清单（判过了，只是还没有历史）；账在、读出来空 = 空清单；
    **读不动 = None**——这一关没判，闸会把它记成 not-checked，不许当成「没重复」。
    """
    try:
        rounds = int(warehouse.load("repeat")["rounds"])
        path = mind_recent.path_of()
        if not path.is_file():
            return []
        rows = mind_recent.load(turns=rounds).get("turns") or []
    except (warehouse.WarehouseMiss, warehouse.WarehouseBroken, OSError,
            ValueError, KeyError, TypeError) as exc:
        LOG.warning("POST /chat 读不到她最近的回话：%s", exc)
        return None
    return [str(row.get("reply") or "") for row in rows]


def _resend(payload: Dict[str, Any], kept: List[Dict[str, Any]], digits: int) -> None:
    """把剩下的泡重排 send_at 后写回返回体：按原来的 gap_before 累加，删掉几泡也不留洞。

    reply 跟着一改写的是「她这一轮实际说出口的话」：被弃的那几泡没发出去，就不该还留在 reply 里。
    """
    clock = 0.0
    rows: List[Dict[str, Any]] = []
    for one in kept:
        clock = round(clock + float(one["gap_before"]), digits)
        rows.append({"text": one["text"], "gap_before": one["gap_before"], "send_at": clock})
    payload["chunks"] = rows
    payload["reply"] = "".join(str(one["text"]) for one in rows)


def _audit_row(seen: Any) -> Dict[str, Any]:
    """这一泡的复述审计读数：status / 有没有按他说的放过 / 撞上的那一条 / 最长撞了几个字 / 阈值。

    **status 原样透出**：不是 "ok" 就是没真的比过（audit-unavailable），这时既不是「通过」也不是
    「拦住」——把它写成通过，等于替她背了一次没人做过的检查。没拿到注入原文时就是这一态。
    **skipped 与 hit 同时为真**是合法的一态：她确实照搬了设定段、对方也明说要她重复，于是这一句
    只记账不判废——hit 照旧交出去，面板上因此看得见「她引了设定」，而这一泡没被丢掉。
    """
    one = getattr(seen, "hit", None)
    return {
        "status": getattr(seen, "status", None),
        "skipped": bool(getattr(seen, "skipped", False)),
        "hit": None if one is None else {"kind": one.kind, "seg_kind": one.seg_kind,
                                         "matched": one.matched},
        "lcs_len": getattr(seen, "lcs_len", None),
        "threshold": getattr(seen, "threshold", None),
    }


def _fatal_row(one: Any) -> Optional[Dict[str, str]]:
    """这一泡的红线读数：哪一类、撞上了什么、一句人话；没踩红线（或者这一轮没给表）就是 None。

    **None 不是「不是红线」**：这一关判没判挂在同一泡的 fatal_status 上（"ok" 真的判过，
    "not-checked" 这一轮没给红线表）。踩上红线时这一泡必然 dropped——那是唯一不许留原话的一类。
    """
    if one is None:
        return None
    return {"category": getattr(one, "category", ""), "tag": getattr(one, "tag", ""),
            "clause_id": getattr(one, "clause_id", ""), "kind": getattr(one, "kind", ""),
            "matched": getattr(one, "matched", ""), "reason": getattr(one, "reason", "")}


def _hit_row(one: Any) -> Dict[str, str]:
    """一条命中：哪条（条款 id 或违规类别）、什么标签、一句人话、命中的那段原文。"""
    return {"clause_id": one.clause_id, "kind": one.kind,
            "reason": one.reason, "matched": one.matched}


def _unique(items: List[str]) -> List[str]:
    """去重、按出现顺序（空串是「闸说弃了却没报条款」的缺口，不往 blocked 里塞）。"""
    out: List[str] = []
    for one in items:
        if one and one not in out:
            out.append(one)
    return out


def _day() -> str:
    """今天的日期（ISO，十位）：弃言账按这个前缀认「今天」。"""
    return datetime.now().date().isoformat()


# —— 拒绝：判一次、投一次（唯一的投递点） ————————————————————

def _judge(asked: str, answer: Any, *, sent: bool = True,
           reason: Any = None, level: Any = None,
           why: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """一轮说完之后判一次拒绝：判出来就投给心；没判出来返回 None（成功体里不出现这一节）。

    两条路，顺序钉死：先问「她这句是不是把话转开了」（refusal.soft_scan），再看它是不是那三句
    标准拒绝之一（refusal.enforce：形状取自 data/refusal.json，代码里不留那三句话）。
    **软转向在前**是因为它更常见，而且硬拒绝那三句本来也不带垫话/转移——顺序换过来不会更准，
    只会把「转开了」这句话当成普通回答放过。

    asked 是对方那句原话，answer 是她那句（被闸弃掉的轮 reply 是空串，这里自然判不出什么）。

    这里也把红线表（data/fatal.json）透传给闸：拒绝那三句"改了一次还是命中"时，闸按新规矩留
    原话发出（不再整句吞掉），只有踩上红线才丢——"她该拒绝的时候整句消失"是最糟的一种哑。
    """
    body = answer if isinstance(answer, str) else ""
    try:
        book = warehouse.load("refusal")
        soft = refusal.soft_scan(body, asked, table=book)
        shape = refusal.soft_shape(book) if soft.is_soft else _standard_shape(body, book)
        if shape is None:
            return None
        kind = refusal.KIND_SOFT if soft.is_soft else refusal.KIND_HARD
        delivered: Dict[str, Any] = {}

        def sink(found_event: Dict[str, Any]) -> None:
            """库约定的投递口：事件到手就投一次心，投没投成如实写进 delivered 那一节。"""
            delivered.update(_deliver(asked, found_event, kind, sent=sent,
                                      reason=reason, level=level, why=why))

        # 软转向那一遍不过闸（她的原话一个字都不改），所以只有硬拒绝才去取条款与红线表。
        # 红线表透传给闸：拒绝的那三句"改了还不成"时，闸照样留原话，但踩上红线才丢。
        # **sink 只在「话真出门了」时挂上**：没出门就没有投递，那句 reason 于是如实说没投
        # （sink 是 None，账本里记 sent=false），不会在面板上冒充「投给心」——两个格子各说各的，
        # 谁也不许替另一格作证。没挂 sink 时事件仍在（enforce 照旧返回它），投递那一步自己走。
        found = refusal.enforce(shape, table=book, refuse_sink=sink if sent else None,
                                now=_stamp(),
                                clauses=None if soft.is_soft else _clauses(),
                                asked=asked if soft.is_soft else None, answer=body,
                                fatal=warehouse.load("fatal"))
        if not sent:
            delivered.update(_deliver(asked, found.event, kind, sent=sent, reason=reason,
                                      level=level, why=why))
    except (refusal.RefusalBroken, ClauseBroken, warehouse.WarehouseMiss,
            warehouse.WarehouseBroken) as exc:
        LOG.warning("POST /chat 判拒绝没成：%s", exc)
        return {"kind": None, "shape": None,
                "verdict_reason": "拒绝判据读不动，这一轮没判成：%s" % exc,
                "delivered": {"delivered": False, "topic": None, "until": None,
                              "remaining_min": None, "reason": "判据读不动，没投心"}}
    return {"kind": found.kind, "shape": found.shape, "verdict_reason": found.reason,
            "delivered": delivered}


def _deliver(asked: str, found_event: Mapping[str, Any], kind: str, *,
             sent: bool = True, reason: Any = None, level: Any = None,
             why: Optional[str] = None) -> Dict[str, Any]:
    """把这一次拒绝投给心：认话题 → heart.refuse.ingest → 把心的账翻成五样，顺手记我们自己的账。

    事件由库（refusal.enforce 的 refuse_sink）交过来，这里只认 {ts, shape, full_name, why} 那四键：
    嘴与心只认识一条 dict，不认识彼此的存储；kind（hard|soft）只用来写我们账本那一列。

    话题名沿用现有认法：只在心自己那份词表里找 asked 里出现的那个词（refusal.topic_in，与
    /mouth/gate 的干跑同一个认法）；找不到就投不成（delivered=false）并说清为什么——心不收
    词表以外的话题，这一层也不替它造分类。投不进去（今日额度用完）也照实 false，不写成投过了。

    落盘两处：心的 refusals.json（ingest 那一笔，**全仓唯一写它的地方**）与我们自己的
    refuse.jsonl（refusal.record，一行一次拒绝）。写不进去也是失败：记一行 WARNING 并在 reason
    里说清楚，不把「账没记上」当成「没拒过」。

    **两态：话出没出门，后果不一样。** sent 是「这一轮的话到底出门没有」，由调用方按 _went_out
    判，**这一层不重判一遍**；why 只在没出门时有意义，报的是哪一种来由
    （refusal.SUPPRESSED_OUTBOUND＝门说不发 / SUPPRESSED_SWALLOWED＝门说发而闸吞了），整句说法由
    refusal.record 从数据里取。

    - sent=True  → 投心（**起话题冷却**）+ 账本 sent=True。投不进去（认不出话题 / 额度用完）照实
      delivered=false，账本照样是 sent=True：话确实出去了，「有没有落到心上」是另一件事。
    - sent=False → **一个字都不写心（绝不 ingest，不起冷却）**，账本记 sent=False + 那一态 +
      why + 那一轮的 reason / level。
    """
    if not sent:
        # 她的话没出去：这次拒绝不生效——**不投心（不起冷却）**，账上记 sent=false、是哪一态、
        # 为什么没发（门说不发 / 门说发而闸吞了）与那一轮的 reason / level。两态的名字与整句说法
        # 都不在这份代码里：refusal.record 从 data/refusal.json 的 states 节取。
        state, tail = refusal.state_of(False), ""
        try:
            refusal.record(refusal.path_of(), found_event, False, shape=kind,
                           why=why, reason=reason, level=level)
        except (OSError, ValueError) as exc:
            LOG.warning("POST /chat 记拒绝账没成：%s", exc)
            tail = "；但拒绝账没记上：%s" % exc
        word = refusal.suppressed_why(why) if why else UNKNOWN_SUPPRESSED
        return {"delivered": False, "suppressed": True, "state": state, "topic": None,
                "until": None, "remaining_min": None, "reason": word + tail}

    topic = refusal.topic_in(asked, heart_refuse.vocabulary())
    if topic is None:
        answer = {"ok": False,
                  "message": "认不出拒的是哪个话题：对方那句问话里没有心认可的话题名"
                             "（词表见 organs/brain/topics.json），这次没投心"}
    else:
        try:
            answer = heart_refuse.ingest(topic, str(found_event.get("full_name") or ""),
                                         now=_stamp())
        except (OSError, ValueError) as exc:
            LOG.warning("POST /chat 投拒绝没成：%s", exc)
            answer = {"ok": False, "message": "投心失败：%s" % exc}
    delivered = bool(answer.get("ok"))
    state = refusal.state_of(True)
    try:
        refusal.record(refusal.path_of(), found_event, True, shape=kind)
    except (OSError, ValueError) as exc:
        LOG.warning("POST /chat 记拒绝账没成：%s", exc)
        tail = "；但拒绝账没记上：%s" % exc
    else:
        tail = ""
    return {"delivered": delivered, "state": state, "topic": topic,
            "until": answer.get("until"), "remaining_min": answer.get("remaining_min"),
            "reason": _delivered_word(topic, found_event, answer) + tail}


def _delivered_word(topic: str, found_event: Mapping[str, Any], out: Mapping[str, Any]) -> str:
    """投完之后那句人话：投进去了就说冷却到什么时候，没投进去就用心自己的原话（不代它编）。"""
    if out.get("ok"):
        return "投给心：%r 记一次「%s」，冷却到 %s。" % (
            topic, found_event.get("full_name"), out.get("until"))
    return str(out.get("message") or out.get("code") or "心没收这次拒绝")


def _standard_shape(body: str, book: Mapping[str, Any]) -> Optional[str]:
    """她说的是不是那三句标准拒绝之一：是就回那个形状，不是就回 None。

    比的是数据里那句 say 的原文（一字不差），代码里不写死任何一句台词；形状名同样从数据里取，
    认不出就一个都不算——不猜她「大概是在拒绝」。
    """
    said = body.strip()
    if not said:
        return None
    shapes = book.get("shapes") if isinstance(book, Mapping) else None
    for key in shapes if isinstance(shapes, Mapping) else ():
        if refusal.say(key, table=book).strip() == said:
            return key
    return None


def _clauses() -> Any:
    """闸的条款：refusal.enforce 过闸要用它——没有条款不是「没命中」，是「不许放行」（它会抛）。"""
    return warehouse.load("clauses").get("clauses")


def _stamp() -> str:
    """投心时给的那一刻（ISO，秒级）：api 层是时刻的供应方，库层一个都不取时钟。"""
    return datetime.now().replace(microsecond=0).isoformat()


def _text(body: Any) -> Optional[str]:
    """body 里的那句话：不是对象、缺 text、全空白都算没给（返回 None，调用方回 400）。"""
    if not isinstance(body, dict):
        return None
    raw = body.get("text")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def _segment_state(body: Any) -> Tuple[Optional[List[Tuple[str, str]]], Optional[str]]:
    """读 body 里的段：归一化过就交回去，形状坏了交回一句点名的人话（由 dispatch 翻成 400）。

    认的两种形状与判据全在 organs/mind/conversation.py 的 normalize_segment（api 层不另写一份）：
    这一层只把 ValueError 翻成一句话。没给这个字段就是 (None, None)——私聊与老调用一字不改。
    """
    raw = body.get("segment") if isinstance(body, Mapping) else None
    if raw is None:
        return None, None
    try:
        return conversation.normalize_segment(raw), None
    except ValueError as exc:
        return None, SEGMENT_BROKEN % exc


WHO_ID_KEY = "id"                       # 对象形状里只有这一格是身份（QQ 号），别的都是别名
WHO_ALIAS_KEYS = ("nickname", "card")   # 别名：QQ 昵称 + 群名片（顺序即登记顺序，昵称在前）


def _who(body: Any) -> str:
    """这一轮是谁在说话：可选，认不出就是空串（装配那边按生人档算称呼）。

    认两种形状，**两种都过脑的口径**（organs/brain/model.py 的 valid_who）：
      · 字符串（老调用）：原样，但昵称不是身份——不合法就交回空串；
      · 对象（通道真发的形状 {"id": 3425635473, "nickname": "…", "card": "…"}）：只认 id 那一格，
        int 转成裸数字串（不加前缀，脑的 WHO_RE 本来就收裸号），再同一个口径过一遍。
    其余形状（数字 / 数组 / 对象里没 id / id 不是号）一律空串：**不猜也不抛**——
    认不出「是谁」就按生人档说话，比把她的话记到一个猜出来的人名下要好。
    """
    raw = body.get("who") if isinstance(body, Mapping) else None
    if isinstance(raw, Mapping):
        raw = raw.get(WHO_ID_KEY)
        if isinstance(raw, bool):          # bool 是 int 的子类，True 不是「1 号」
            return ""
        if isinstance(raw, int):
            raw = str(raw)                 # 裸数字：#3425635473 / 3425635473 都不加，只去掉类型
    if not isinstance(raw, str):
        return ""
    who = raw.strip()
    return who if brain_model.valid_who(who) else ""


def _known(brain: Any, who: str) -> Optional[Dict[str, Any]]:
    """这一轮的人名录读数（在不在册 + 够不够熟）：交给闸的认亲那一关，**判不了就说判不了**。

    三种结论，一个都不许含糊：
    - {"in_book": False}：名录读得动、里面没有这个号——陌生号，回复里出现只有熟人才能触发的称呼
      或记忆复述就要判；
    - {"in_book": True, "relation": n, "tier": …}：在册。**在册不等于熟**：answer() 会把他这句话
      ingest 建档，所以「查得到他」说一句话就成立；够不够熟由闸按 data/kin.json 的 known_by 判
      （现在要 relation ≥ 3，那一档叫什么名字、档界在哪，全在 address_table.json 的 tiers 里）。
      这里的 relation / tier 就是称呼那一关现算出来的那份读数（address.resolve），**不另造一套判定**；
    - None：**判不了**。who 为空（认不出是谁）、脑没接、称呼表读不动、或脑自己坏了都算这一种：
      这一关不跑，闸记 kin_status="not-checked"，不判、也不声称判过。猜成「在册」这一关就永远放行，
      猜成「不在册」就凭一个读错的格子把熟人当生人拦下来——两种猜法都比「如实说没查」难查。
    """
    if brain is None or not who:
        return None
    try:
        person = brain.lookup(who)
        if not person:
            return {"in_book": False}
        called = address.resolve(person, address.load_table(), who=who)
        return {"in_book": True, "relation": called.relation, "tier": called.tier}
    except (address.PolicyMiss, warehouse.WarehouseMiss, warehouse.WarehouseBroken,
            OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        LOG.warning("POST /chat 查这一轮认不认得这个号没成（who=%s）：%s", who, exc)
        return None


def _aliases_of(body: Any) -> List[str]:
    """who 那个对象里带的昵称 / 群名片：**只是别名，不是身份**，所以单独一格交给装配。

    只认非空字符串，去重保序；字符串形状的 who 没有别名（那是老调用，只有身份没有名片），
    认不出身份（_who 空）或形状坏了也一律空表——不猜也不抛。这一格只进人名录的别名表，
    不改身份、不写记忆条。
    """
    raw = body.get("who") if isinstance(body, Mapping) else None
    if not isinstance(raw, Mapping) or not _who(body):
        return []                          # 认不出是谁就没有别名：别名跟着身份走
    names: List[str] = []
    for key in WHO_ALIAS_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in names:
            names.append(value.strip())
    return names


def _mode_of(body: Any) -> str:
    """这一轮是谁起的头：proactive 只认那一个字面值，其余一律 passive。

    **非法值回落、不 400**（与 scene 同一个口径）：这一格只决定她用哪份提示词与心判决的
    意图；认不出就按常态走，不拿一个错字段把人拦在门外。取值不在这里另写一份字符串，
    从 organs/mind/events.py 的 MODE_* 来。
    """
    raw = body.get("mode") if isinstance(body, Mapping) else None
    return events.MODE_PROACTIVE if raw == events.MODE_PROACTIVE else events.MODE_PASSIVE


def _seed_of(body: Any) -> str:
    """她自己开口那一轮的由头：不是字 / 全空白都算没给（返回空串，由调用方回 400 点名）。"""
    raw = body.get("seed") if isinstance(body, Mapping) else None
    return raw.strip() if isinstance(raw, str) else ""


def _reason_of(body: Any) -> Optional[str]:
    """由头那一侧的理由码（可选）：不是字就当作没给——它是给人看的码，不是判据。"""
    raw = body.get("reason") if isinstance(body, Mapping) else None
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


# —— 借阅读数：这一轮借了家里哪几份词条、各用掉几条 ——————————

#: 这一层真读用到的 policy 段：pace.round_digits（重排 send_at 与切分用的是同一个数）。
POLICY_SECTIONS_USED: Tuple[str, ...] = ("pace",)

BORROW_FALLBACK = "读数没投进家（%s：%s），这一轮的话照发——失败的只是读数，不是她的话"


def _borrow_round(payload: Dict[str, Any]) -> None:
    """把这一轮「借了家里哪几份词条、各用掉几条」投回家；投不进去也只写一节 failed。

    **投递失败不许影响这一轮说话**：登记借阅条、写读数、追审计全在 try 里，任何一步抛了都只把
    结论写进回包的 borrow 节——reply / chunks 一个字节都不动，也不降级成空回话。读数是账，
    话是说给他听的那句：账写不进去，不许连话一起吞掉。

    数据在这一层重新读一次（不借闸那一份）：闸可能半路就整轮弃掉了，那时候这一层根本没读成，
    读数就得如实说没投；借闸的那一份，等于替一次没发生的读取作证。
    """
    try:
        payload["borrow"] = _deliver_readout(payload)
    except Exception as exc:                    # 读数这一关坏成什么样都不许动她的话
        LOG.warning("POST /chat 投读数没成：%s", exc)
        payload["borrow"] = {"status": borrow.STATUS_FAILED, "files": [],
                             "note": BORROW_FALLBACK % (type(exc).__name__, exc)}


def _deliver_readout(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """算这一轮真用过的词条 → 家里缺借阅条的先登记 → 每份写一条读数 → 交回一节 borrow。

    三件事各归各家：算读数是嘴（organs/mouth/borrow.py 的纯函数），登记借阅条与写读数在家
    （residents.register_borrow / record_readout，写权与上限只在家的 access 里判一次）。
    这一层只做顺序与往返：名单来自 _books()（它最清楚读了哪几份），条数来自这一轮自己的账。
    """
    books = _books()
    words = warehouse.load("borrow")
    event = borrow.usage(books["sources"], counts=_counts_used(payload, books))
    ids = borrow.ids_of(home_residents.borrow_ledger().get("borrows"))
    for item in event["files"]:
        if item["source"] in ids:
            continue                            # 一 source 一条：第二轮不再登记
        ids[item["source"]] = _register_borrow(books["sources"][item["source"]], item, words)
    files: List[Dict[str, Any]] = []
    for row in borrow.readout(event, ids)["rows"]:
        home_residents.record_readout(
            row["borrow_id"], row["learned"], role="organ", by="mouth",
            note=borrow.note_of(words, "record", label=borrow.label(words, row["source"]),
                                used=row["learned"]))
        files.append({"source": row["source"], "borrow_id": row["borrow_id"],
                      "learned": row["learned"]})
    if not files:
        return {"status": borrow.STATUS_OK, "files": [], "note": borrow.note_of(words, "empty")}
    joined = "、".join("%s（%s）" % (borrow.label(words, one["source"]), one["source"])
                      for one in files)
    return {"status": borrow.STATUS_OK, "files": files,
            "note": borrow.note_of(words, "ok", count=len(files), sources=joined)}


def _register_borrow(table: Mapping[str, Any], item: Mapping[str, Any],
                     words: Mapping[str, Any]) -> str:
    """家里还没有这份词的借阅条：按数据现场算指纹登记一条，回来它的 borrow_id。

    version 取 sha256 的前 12 位（给人看的那一截），total 是这份**整份几条**（事件里的 entries，
    不是这一轮用掉几条），sha256 是全量——短形只是个招牌，认身份靠全量那一份。
    """
    digest = home_store.sha256_file(warehouse.path_of(item["source"]))
    outcome = home_residents.register_borrow(
        role="organ", by="mouth", source=item["source"], version=digest[:12],
        total=item["entries"], sha256=digest,
        note=borrow.note_of(words, "register", source=item["source"]))
    return outcome["borrow"]["borrow_id"]


def _counts_used(payload: Mapping[str, Any], books: Mapping[str, Any]) -> Dict[str, int]:
    """这一轮每份词条真用掉几条：条款数过闸时真比过的那些，policy 数这一层真读过的那几段。

    条款按「真比过」算，不按「命中过」算：一条都没命中不等于没用上——闸本来就逐条比一遍，
    比过的都算用上。所以有泡可过闸时是**启用的条款全表**，一泡都没有（装配那一步就整句弃掉了）
    就是 0。**0 是一次真实读数**：这一轮一条条款都没比过，不是「没量过」，也不是缺失。
    policy 那边同理：只数这一层真读了的那几段（pace.round_digits 这一段）。
    """
    sources = books.get("sources") or {}
    counts: Dict[str, int] = {}
    if "clauses.json" in sources:
        counts["clauses.json"] = _compared(books.get("clauses"), payload)
    if "policy.json" in sources:
        policy = sources["policy.json"]
        counts["policy.json"] = len([name for name in POLICY_SECTIONS_USED
                                     if isinstance(policy, Mapping) and name in policy])
    return counts


def _compared(clauses: Any, payload: Mapping[str, Any]) -> int:
    """这一轮过闸真比过的条款条数：有泡可过闸就是启用条款全表（停用的不算用上），否则 0。

    为什么按「比过」不按「命中」：命中在装配那一步就已经弃掉了，能走到这一层的泡里几乎没有
    命中；按命中算，读数就永远是 0，「她这一轮用掉几条」也就永远答不出。
    """
    parts = ((payload.get("gate") or {}).get("parts") or [])
    if not parts:
        return 0
    return len([one for one in (clauses or [])
                if isinstance(one, Mapping) and one.get("enabled", True)])
