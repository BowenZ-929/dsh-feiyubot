"""嘴的闸：开口之前先过一遍条款；命中就拦，改一次还不行就弃掉这句。

**条款不在代码里，全部来自「家·仓库」。** 调用方用 warehouse.load("clauses")["clauses"] 读出一张
条款清单，原样交给 check / enforce；本文件一条条款都不写——加一条、改一条、删一条都是改数据
（organs/mouth/data/clauses.json），代码不动。理由在 docs/reference/mouth.md：凡是要她「不说某种话」，
一律做成出口的结构闸门；判据表一旦写进代码，就成了第二处真源，改口吻要改代码。

条款形状（一条一条校验，坏一条就当场抛 ClauseBroken，并指名是哪一条）：

    {"id": 稳定字符串, "kind": 标签, "pattern": 正则, "reason": 一句人话, "note": 可选}

顶层是一份键值表 {"clauses": [...]}。kind 只是给理由用的标签（数据里现有 反例 / 可判废 / 流程泄露
三类），匹配一律只看 pattern——给一条条款换 kind 不会改变拦不拦，换 pattern 才会。

**条款顺序即优先级**：按 clauses 里的先后顺序逐条 re.search，返回第一条命中的，后面的不再看。
要调优先级就调数据里的顺序，不要在代码里加「先看哪类」的分支。

两道出口，缺一道都不算拦住：

- check(text, clauses, inject=None, policy=None, action=None, recent=None, repeat_table=None,
  quota=None) -> Hit | None：命中返回一条 Hit，
  里面带一句人话原因（含命中的那段原文）；没命中返回 None。**不返回布尔**——布尔说不出「为什么拦」，
  日志与面板也就没法复盘，只能再翻译一次。
- enforce(text, clauses, rewrite, sink=None, inject=None, policy=None, action=None, recent=None,
  repeat_table=None, quota=None, asked="", keep_unrewritten=False) -> Verdict：命中一次 → 交给注入的
  rewrite 改一次 → 再 check；还命中就 decision="dropped"、text 留空串，两次命中的原因都按发生顺序
  留在 hits 里。Verdict.audit 是这一轮复述审计的读数（status / hit / lcs_len / threshold，
  按他说的放过时另带 skipped / skip_clause_id / skip_reason——hit 照旧在里面），
  Verdict.action_status 是动作括注那一关判没判过（"ok" / "not-checked"），Verdict.repeat_status 是
  内容重复那一关的状态（"ok" / "not-checked" / "skipped-by-request"），Verdict.quota_status 是
  动作配额那一关判没判过（同样两个取值）。

rewrite 是调用方注入的 callable（text -> text），本文件没有模型客户端：闸只管判，不改写。
传 None 视为「改不了」——**出口不许 fail-open**，改不了就不许把命中句原样当成「判过了」发出去。
rewrite 自己抛错、或者改回来是空的，也只算「改不了」，错误照实写进 reason，出口本身不抛错。

**keep_unrewritten=True 时「改不了」不弃句，留原话发。** 这是调用方开的第二条出口：她那句话是
她说的，改不动也要发出去——**宁可发一句略有瑕疵的，也不许静默变哑**。这时 decide 是 "pass"、
text 一个字不改、hits 里留着那条命中，reason 用 organs/mouth/rewrite.py 里那句带 rewrite-failed
的说法，并且往 sink 记一笔账（好让人看见闸本想过改它）。默认 False，旧行为一个字节都不变；
配额命中仍然优先走剥行兜底（把动作行拿掉比留着原话强）。

sink 也是调用方注入的 callable（clause_id, reason -> None），**只在「弃掉」那一次**被叫一遍
（例外只有配额兜底救回来的那一句：它也记一笔，好让人看见闸动过手），好让弃言留下痕迹：谁被弃、为什么。sink 为 None 就不记，闸仍是纯函数——本文件不取时钟、不写盘，
时间与落盘都在调用方（api 层与 organs/mouth/drops.py）。sink 自己抛错就照实往上抛：记账失败
也是失败，不许咽掉（否则「今天弃了几次」会悄悄变少）。

**停用的条款跳过匹配，但停用这件事必须看得见。** 数据里 enabled 为 false 的条款不参与 re.search，
它的 id 收在 Verdict.disabled 里，由 api 层原样带进返回的 disabled_ids——停用不是删除，更不是
「没命中」。空清单照旧抛 ClauseBroken；**一条生效条款都没有（全部被停用）也一样抛**：
没有闸就是没有闸，不许当成「这句没踩条款」放行。

**复述审计是另一半闸（第二轮 A）**：条款管「怎么说」，审计管「说的是不是抄来的」。注入的原文
由调用方给（inject），判据全在 organs/mouth/audit.py 与 data/audit.json——设定类（魂、锚句、
心档位词、规矩句）被照搬一律拦；对方原话和记忆原文的字面重合只记读数，不据此拦截。
闸把审计拦下的那一条当成一次命中，走同一条「改一次还不行就弃」的路：审计拦下的 Hit 里，
clause_id 记违规类别、kind 记来源段那一类（两个都取自数据里的说法），所以弃言账本里看到的仍是
一个能指认的名字。注入段形状坏掉（未知 kind 之类）由 audit 当场抛 AuditBroken，照实往上抛。

**audit-unavailable 只挂在 Verdict.audit 上，check 的返回值不承载它。** 这么选有两条理由：
check 的返回类型是 Optional[Hit]，塞一条假 Hit 进去等于把「没命中」与「判不了」挤进同一条通道，
调用方再也分不开；而既有调用与断言（api 层、脑那一侧）都在用 check(...) is None 认「没踩条款」，
加一条「永远是 Hit」的旁路会把它们全部推翻。要区分 ok 与 audit-unavailable 就看 enforce 的
Verdict.audit（status 不是 "ok" 就是没真的比过），或者直接调 organs/mouth/audit.py 的 audit()。
**这条不影响「缺料不弃这句」**：审计判不了时 enforce 照常按条款走，decision 与既有五种结局一字
不变，只是 audit.status 如实写着 audit-unavailable——缺料是调用侧的问题，不拿她的沉默来兜。

**动作括注是第三道判据**：检查完整括注中的环境词，不通过身体词与地点词共现拦截普通正文。
判据与词表全在 organs/mouth/action_note.py 与 data/action_note.json，闸只负责把它当成一次普通命中
接进来：action 是调用方给的判据表，**None 就整段不判**——不判绝不是通过，所以判没判挂在
Verdict.action_status 上（"ok" 真的判过，"not-checked" 这一轮没给表），由 api 层照实透出。
命中走同一条路（改一次还不行就弃），Hit 里的 clause_id 与 kind 也取自数据，弃言账本里看到的因此
是一个能指认的名字。判据表形状坏掉由 action_note 当场抛 ActionBroken，照实往上抛。

**字面重合只作观察**：repeat 比较最近输出的开头和文字行；不能据此判断语义复读。
进门原稿有重合时 repeat_status=observed，原文保留、不触发改写；明确请求复述时仍记原有审计。
无重合为 ok，未提供 recent 为 not-checked。其他内容判据照常执行。

**冒充做过是第五道判据（这一轮加的）**：她说了这一轮没做过的动作，就是演一个做完的样子。判据与
词表全在 organs/mouth/claim.py 与 data/claim.json——魂那条红线（bounds.no-fake-doing，
enforced_by=mouth·语义复核）说的就是这件事，闸这边接的是它：回复里出现「人称 +（前缀）+
（填充）+ 动作类动词 +（后缀）」这种「说成做过的」写法，而这一轮的动作账里对不上这个动词，就算
命中（词表与前后缀全在 data/claim.json，闸里一个词都不写）。顺序排在
内容重复之后、复述审计之前：前两关问「这句话是不是又是我说的、说的事是不是抄来的」，这一关问
「这件事我到底做没做过」，最后才是审计。actions 是调用方给的动作账，**None 就是拿不到账**——
判不了就不判、也不声称判过，判没判挂在 Verdict.claim_status 上；**空清单 [] 是给了账、账上空着**，
那就该判冒充。这条差别是这一关的命门：把「没给账」当成「没做过」，会把她真做过的事当成谎话拦下
来；把「给了空账」当成「判不了」，这一关就白设了。**命中之后改不动就删那一句，不整条丢**（见下）。

**对方明说要她重复时，这一关只记账、不判废（这一轮加的）。** asked 是对方这一轮说的那句话，
由调用方原样传下来、交给 repeat 判「是不是在要她再说一遍」；撞上重复而对方确实要她重复时，
repeat 给的不是命中而是一条 RepeatSkip：**decision 保持 pass、text 一个字不改**，但闸要替这一关
记一行账（clause_id 取自数据 hit.skip_clause_id，reason 里带 skipped-by-request），并把
Verdict.repeat_status 记成跳过那个状态。为什么记账：跳过是对的，但它必须留痕——不然「她今天
复读了三次没被拦」与「这一关没跑」在账本上长得一模一样。这条豁免只在**真的撞上重复**且
**asked 命中请求标记**时成立，asked 为空一律不豁免（不许默认豁免，那是把这一关白设）。

**同一句请求也豁免复述审计（这一轮加的）。** 他要她重复／引用时，把她说过的那段设定原样再说
一遍正是他要的，审计若照旧判废，她照做的这一下又被吞掉一次。所以闸拿 repeat.requested(asked, 表)
**算一次**那件事，把同一个布尔同时交给内容重复与复述审计两关（标记词表只有 repeat.json 那一份，
audit.json 里不许另抄一张——抄了就分岔成「这一关当他要重复、那一关不认」）。审计给出的读数是
**命中照旧**（status 仍是 "ok"、hit 带着、面板看得见她引了设定），另加 skipped 与一句取自数据的
skip_reason；闸见到 skipped 就**不再把它当命中**：decision 保持 pass、text 一个字不改，但替这一关
记一行账（clause_id 取自数据 skip.clause_id，reason 里带 audit-skipped-by-request）。为什么还要记：
我们不想让她养成引用设定的习惯，只是不在这句上拦——不拦而无声，账本上就与「这一关没跑」长得一样。
一句话同时撞上内容重复与复述审计时，**两关各记一行账、各带自己的 skip 记号**，账本上读得出
「同一句撞了两关、两关都按他说的放过」。

**红线是唯一不许留原话的一类（这一轮加的）。** 隐私（手机号、身份证、住址、账号密码）、辱骂、
放弃式这三类话，判的不是「这一句有毛病」，而是「这一句不能出去」。判据与说法全在
organs/mouth/data/fatal.json，闸只把命中摊成一条 FatalHit：clause_id 与 kind 取自数据，
category 是数据里那一类，reason 里带着数据里那个记号（fatal-<tag>）。命中挂在 Verdict.fatal 上，
由 api 层原样透出（面板看得见是哪一类）。**红线不交给重写器**：改一次也不能保证它不出去，
所以进门那一遍命中就丢掉；改完回来的新稿若自己踩上红线，也一样丢掉。

**改了一次还是命中（且不是红线）→ 保留进门那句原话，不再丢空（这一轮加的）。** 以前二次命中就
只是弃掉：她明明改了、也没改成红线，整泡却消失了——**在最该回答的那一句上沉默，比「说话带点
毛病」糟得多**。所以二次命中（非红线）改成 decision="pass"、text 是**进门那句**（不是那个没改成的
新稿：能出去的还是她自己那句），hits 里两条命中都留着，Verdict.kept_after_rewrite=True，并往 sink
记一行 reason 带「<通道>-after-rewrite」的账——留原话不是「没问题」，账本上必须看得出「这句改过、
还是不行、按原话发了」。通道名与那句人话都取自 data/rewrite.json 的 after_rewrite 节：闸只报
「是条款 / 动作括注 / 内容重复 / 复述审计哪一条没改掉」，说法一个字都不进代码。
**这条不看调用方给没给红线表**：没给表只说明判不了是不是红线（fatal_status=not-checked、账行里
补一句取自数据的说明），不说明可以吞掉这句话。红线表给了才判得出红线，才可能丢。
**红线是唯一例外**：二次命中踩上红线照旧 decision="dropped"、text=""——宁可这一句不说。
**配额剥行那条例外不变**：配额命中仍优先剥行兜底（剥完只剩空白才丢），红线这一层压在它前面：
剥掉动作行也救不回一句带手机号的话。

**动作配额排在第二道（这一轮加的），因为它比「动作写得对不对」更早。** 装配标了「本轮不许写
动作」时，她根本没资格写动作，那就不必先问「动作里有没有编场景」。判据与说法全在
organs/mouth/quota.py 与 data/quota.json；quota 是调用方给的配额写法（forbid / allow），**None 就是
装配没标**——没标不是 allow，判没判挂在 Verdict.quota_status 上。顺序因此钉成：条款 → 动作配额 →
动作括注 → 内容重复 → 冒充做过 → 复述审计。**顺序不是风格问题**：验收句「（一段尾巴动作，里面带着我们没给
过的场景词）可爱归可爱，那也不是胖。」两关都踩——动作括注先判，这句就整条丢了，走不到配额兜底；
配额先判，她至少还能把正文留下（验收句原文收在 organs/mouth/quota.py 与 data/quota.json 的说明里，
闸里一个字都不抄：抄进来就又是一处真源）。

**复述命中之后也是「删那一句」，不是整条弃（这一轮加的）。** 判据一句话：**宁可她少说一句，
不许她整句不说**。以前审计命中的处置与别的判据一样：重写改不动、或改完还照搬，就整泡弃成空串——
她沉默了，比念台词更糟。现在改成照 claim 那一关的形状做一条同族分支（_strip_recited）：改过还是照搬、
或根本改不了（没给重写器 / 抛错 / 改回来是空的），就**只删被审计命中的那一句**，剩下的正文照原样再
过一遍其余判据（条款 → 配额 → 动作括注 → 重复 → 冒充做过 → 认亲 → 编记忆 → 审计），命中哪条就照旧
处置；剩下的正文还有一句在照搬就接着删那一句，递归深度由句数封顶。删完只剩空白（这一泡只有那一句）
才弃，那句人话取自数据里的 reason.recited_only，带着 recited-only 记号——**是「只剩那一句」，不是
「整条判废」**，账本上要分得开。救回来的那一笔挂在 Verdict.salvaged 上、照既有规矩记 sink
（reason 里说清删了哪一句）。句界、记号、三句人话全在 data/audit.json，闸里一个句界字符都不写。
**红线不受这条影响**：踩上红线照旧整句丢，删句救不回一句带手机号的话。

**配额剥行与冒充做过的删句是两条「不丢整句」的路（后者这一轮加的）。** 其它判据第二次命中照旧
弃掉整句（旧行为一个字节都不变）；配额命中时第二次（以及没给重写器时）改为 quota.strip() 把动作行
剥掉、正文接着往下判（其余判据照跑），剥完只剩空白才 decision="dropped"。救回来那一句挂在
Verdict.salvaged 上，账本照样记一笔（sink 收的是配额那条 clause_id 与剥行那句话），好让人看见
「这句本来要丢、靠剥行留下」。**冒充做过与之同族、但删的是那一句**：谎只长在那一句里，别的话还是
她要说的话，删句比整条丢下强；删完只剩空白才弃整条，也照样记账。

**编记忆排在冒充做过后面、复述审计前面（这一轮加的）。** 这一关问的是「她引用的那段过去，这一轮到底
想起来没有」：回复里出现引用标记（你说过／你当时说／我记着／我照做了……）**加上具体细节**（数字、
时间词、引号里的原话、或一段说得够长的话），而这一轮的 recalled 里没有一条对得上——那就是她编的。
判据与说法全在 organs/mouth/fabricate.py 与 data/fabricate.json。recalled 是这一轮真的想起来的记忆，
**None 表示拿不到**——这一关没跑，Verdict.fabricate_status 记 not-checked，不判、也不声称判过；
**空清单 [] 是给了账、这一轮一条都没想起来**，照判。**态度不是事实**：「我没忘你」「我记得你这人」
说的是心，豁免表上的那一句整句跳过（宁可放过一句带细节的，也不冤枉一句只是在表心意的）。
这一条的处置与别的判据不同，与冒充做过的删句同族：命中先交给 rewrite 改一次，**改不了（没给重写器 /
抛错 / 改回来是空的）或改完还命中，就把这一整泡换成一句实话**（fabricate.fallback_text；那句原话的
原文在 data/fabricate.json 里，闸一个字都不抄）——**不是整条丢**。说不真话的时候认怂，比在最该回话的那
一句上沉默强：人那边至少知道该再提一句。救回来的那一泡同样挂在 Verdict.salvaged 上、照样记一笔账。
**这一条也盖过 keep_unrewritten**：留原话就是留一句编出来的过去。换成实话之后只再过一遍红线——
那句实话取自数据、不是她说的，别拿条款 / 配额 / 重复 / 冒充四关去卡一句我们自己写的话。

纯函数：入参只有一段文本与一张只读的条款清单，出参只有结构化数据；不碰时钟、不碰随机数、
不联网、不碰宿主 IO（本文件不 import time / datetime / random），所以能离线预览，也能被单独替换。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import action_note, repeat
from . import claim as claim_rules         # 参数名就叫 claim（调用方按这个名字传），模块用别名进来免得被挡住
from . import fabricate as fabricate_rules  # 参数名就叫 fabricate（调用方按这个名字传），同上
from . import kin as kin_rules               # 参数名就叫 kin（调用方按这个名字传），同上
from . import quota as quota_rules          # 参数名就叫 quota（调用方按这个名字传），模块用别名进来免得被挡住
from . import rewrite as rewrite_rules      # 参数名就叫 rewrite（调用方按这个名字传），模块用别名进来免得被挡住
from .audit import AuditBroken, AuditResult, audit as _audit
from .warehouse import load

FATAL_OK = "ok"                     # 红线这一关真的判过了：这一轮给了红线表，判完没踩
FATAL_HIT = "fatal"                 # 踩上红线了：Verdict.fatal 里带着是哪一类
FATAL_NOT_CHECKED = "not-checked"   # 这一轮没给红线表，这一关没跑；不是「不是红线」

__all__ = ["ClauseBroken", "FatalBroken", "FatalHit", "Hit", "Verdict",
           "FATAL_OK", "FATAL_HIT", "FATAL_NOT_CHECKED", "check", "enforce", "fatal_hit"]


class ClauseBroken(ValueError):
    """条款形状不合格：缺键、正则编译不过、id 重复都算，坏一条就当场响。

    报错信息里带着那条条款的 id（连 id 都没有就带它在清单里的位置），好让人一眼找到清单里的
    哪一条写错了。**不许跳过坏条款静默降级**：跳过一条坏条款，闸就少一道，而且没人知道
    （CONTRIBUTING.md §1.1 第三条：失败必须响）。
    """

    def __init__(self, where: str, detail: str) -> None:
        self.where = where
        self.detail = detail
        super().__init__("条款清单 %s %s（清单在 organs/mouth/data/clauses.json，"
                         "由 warehouse.load(\"clauses\")[\"clauses\"] 读出）" % (where, detail))


@dataclass(frozen=True)
class Hit:
    """一次命中：哪一条、什么标签、为什么（一句人话）、命中的那段原文。"""

    clause_id: str
    kind: str
    reason: str     # 一句人话，里面带着命中的那段原文；日志与面板直接显示这句
    matched: str    # 正则这次命中的那段原文


@dataclass(frozen=True)
class Verdict:
    """过闸的结果：最终发什么、怎么决定的、命中过什么、一句人话总结。"""

    text: str                                   # 最终文本；弃了就是空串
    decision: str                               # "pass" | "rewritten" | "dropped"
    hits: List[Hit] = field(default_factory=list)   # 按发生顺序，最多两条（改写前、改写后）
    reason: str = ""                            # 一句人话：为什么放行、为什么弃掉
    disabled: List[str] = field(default_factory=list)   # 这次被跳过（enabled=false）的条款 id，按清单顺序
    # 这一轮复述审计的读数（audit.AuditResult）：status 不是 "ok" 就是没真的比过，别当成通过；
    # 记的是**进门那一段**（调用方给的 text）的读数，改写后的那次只为决定 decision，不覆盖它。
    audit: Optional[AuditResult] = None
    # 动作括注那一关判没判过：action_note.STATUS_OK（真的判过）或 STATUS_NOT_CHECKED（没给判据表）。
    # 与 audit 同理——「没判」不是「判过没问题」，两件事必须分得开，所以默认就是 not-checked。
    action_status: str = action_note.STATUS_NOT_CHECKED
    # 进门原稿的字面重合读数：observed / skipped-by-request / ok / not-checked。
    repeat_status: str = repeat.STATUS_NOT_CHECKED
    # 动作配额那一关判没判过：quota.STATUS_OK（装配标了这一轮的配额，forbid 与 allow 都算）或
    # STATUS_NOT_CHECKED（装配没标，quota 是 None）。同理——「没标」不是「标了 allow」。
    quota_status: str = quota_rules.STATUS_NOT_CHECKED
    # 冒充做过那一关判没判过：claim.STATUS_OK（这一轮给了动作账，空账也算给了）或
    # STATUS_NOT_CHECKED（actions 是 None，拿不到账）。同理——「没给账」不是「没做过」。
    claim_status: str = claim_rules.STATUS_NOT_CHECKED
    # 认亲那一关判没判过：kin.STATUS_OK（这一轮拿到了人名录读数，True 与 False 都算）或
    # STATUS_NOT_CHECKED（known 是 None，没拿到读数）。同理——「没查」不是「查过、是熟人」，
    # 也不是「查过、是生人」；判不了就不诬陷。
    kin_status: str = kin_rules.STATUS_NOT_CHECKED
    # 编记忆那一关判没判过：fabricate.STATUS_OK（这一轮给了 recalled，空清单也算给了）或
    # STATUS_NOT_CHECKED（recalled 是 None，拿不到这一轮想起来的是什么）。同理——「没给账」不是
    # 「没编记忆」，两件事必须分得开。
    fabricate_status: str = fabricate_rules.STATUS_NOT_CHECKED
    # 这一句是不是靠「救人」那一族救回来的，两条路各救各的：配额命中之后剥掉动作行（quota.strip），
    # 或冒充做过命中之后删掉那一句（claim.strip_sentence），剩下的正文过了其余判据才为真。为真时
    # decision 是 "rewritten"（剥行与删句都是一种改写），text 是剥完 / 删完的正文——账本上因此看得见。
    salvaged: bool = False
    # 红线这一关的读数：这一轮踩上红线就是一条 FatalHit（category / tag / matched / reason 全在里面），
    # 面板据此看得见「这句为什么连原话都没留下」。没踩就是 None——**没给表的时候也是 None**，
    # 但那时 fatal_status 是 not-checked：判不了不等于不踩（这一关没跑，两件事必须分得开）。
    fatal: Optional[FatalHit] = None
    # 红线那一关判没判过：FATAL_OK（这一轮给了红线表，真的判过）或 FATAL_NOT_CHECKED（没给表）。
    fatal_status: str = FATAL_NOT_CHECKED
    # 二次命中（且不是红线）时是不是把进门那句原话留下来了：为真时 decision 是 "pass"、
    # text 是**进门那句**（不是那个没改成的新稿），hits 里两条命中都留着，账本上多一行
    # 「<通道>-after-rewrite」——「绝不静默变哑」，宁可发一句略有瑕疵的，也不许整泡消失。
    kept_after_rewrite: bool = False


class FatalBroken(ValueError):
    """红线判据表不合格：缺节、缺键、正则编译不过、两类撞名、人话里没带记号，都算，坏一处就当场响。

    报错信息里带着那一项的名字：**不许拿一个空表当「这一句没有红线」**——那等于把三类不该出去的
    话静默放出去，而且没人知道（CONTRIBUTING.md §1.1 第三条：失败必须响）。
    """

    def __init__(self, where: str, detail: str) -> None:
        self.where = where
        self.detail = detail
        super().__init__("红线判据 %s%s（判据数据在 organs/mouth/data/fatal.json，"
                         "由 warehouse.load(\"fatal\") 读出）" % (where, detail))


@dataclass(frozen=True)
class FatalHit:
    """一次「这句不能出去」的证据：哪一类红线、撞上的那几个字、一句人话、闸用的两个名字。

    字段（api 与闸按这个接，改字段等于改接口）：
      category   哪一类红线（隐私 / 辱骂 / 放弃式），取自数据里的 id
      tag        那一类的记号（按它填进人话里那个 mark），取自数据
      clause_id  闸记账用的名字，取自数据——弃言账本与 gate.blocked 里看到的就是它
      kind       闸那条 Hit 的标签，取自数据
      matched    撞上的那几个字（正则这次命中的那段原文）；日志与面板直接显示它
      reason     一句人话，里面带着哪一类、撞上了什么、以及那个记号（fatal-<tag>）
    """

    category: str
    tag: str
    clause_id: str
    kind: str
    matched: str
    reason: str


@dataclass(frozen=True)
class _FatalCategory:
    """红线里的一类：叫什么、记号是什么、怎么认、拦下来怎么说。"""

    category: str
    tag: str
    patterns: Tuple[Any, ...]
    reason: str


@dataclass(frozen=True)
class _FatalRules:
    """data/fatal.json 拆成一堆可以直接用的值：数据里没有的一样都不补。"""

    clause_id: str
    kind: str
    mark: str
    categories: Tuple[_FatalCategory, ...]


@dataclass(frozen=True)
class _RecitedRules:
    """data/audit.json 里「删那一句」要用的那几样：句界表、归一化那两张表、记号与三句人话。"""

    status: str
    strip: str
    blank: str
    sentence_marks: str
    strip_reason: str
    only_reason: str
    leftover_reason: str


def fatal_hit(text: str, table: Optional[Mapping[str, Any]] = None) -> Optional[FatalHit]:
    """看这段字有没有踩红线：踩了给一条证据，没踩返回 None——**不是布尔**（说不出「为什么不能出去」）。

    table 是红线判据表（warehouse.load("fatal") 读出来的那份），传 None 就现读仓库里的默认表。
    三类按数据里的顺序逐条 re.search，命中第一条就停：一句里同时踩两类时报前面那类（顺序即优先级，
    要调就调数据里的顺序）。判据表形状坏掉当场抛 FatalBroken，不补默认值、不静默跳过一类——
    跳过一类红线，就等于有一类话永远能发出去，而这种漏法没有声音。
    """
    rules = _fatal_rules(table)
    body = _text_of(text)
    for one in rules.categories:
        for pattern in one.patterns:
            found = pattern.search(body)
            if found is None:
                continue
            matched = found.group(0)
            return FatalHit(category=one.category, tag=one.tag, clause_id=rules.clause_id,
                            kind=rules.kind, matched=matched,
                            reason=_fill(one.reason, "categories.reason", category=one.category,
                                         tag=one.tag, matched=matched))
    return None


def check(text: str, clauses: Sequence[Mapping[str, Any]],
          inject: Any = None, policy: Optional[Mapping[str, Any]] = None,
          action: Optional[Mapping[str, Any]] = None,
          recent: Optional[Sequence[str]] = None,
          repeat_table: Optional[Mapping[str, Any]] = None,
          quota: Any = None,
          asked: str = "",
          fatal: Any = None,
          actions: Any = None,
          claim: Any = None,
          recalled: Any = None,
          fabricate: Any = None,
          kin: Any = None,
          known: Any = None) -> Optional[Hit]:
    """看这段字有没有踩条款、有没有超动作配额、有没有在动作里带场景、有没有在反复念、有没有说这一轮没做过的动作、有没有编一段这一轮没想起来的过去、有没有在复述注入的原文：命中给一条 Hit，否则 None。

    **返回的不是布尔。** Hit 里带一句人话原因，原因里含这次命中的那段原文，所以日志、面板、
    复盘都能直接用这句话，不必再翻译一遍。

    顺序钉死：条款 → 动作配额 → 动作括注 → 内容重复 → 冒充做过 → 编记忆 → 复述审计。条款按 clauses 的
    先后顺序逐条 re.search，返回第一条命中的（顺序即优先级）；零宽正则也会命中，那种情况下原因里
    改说位置，免得出现一对空引号。**动作配额排在动作括注前面，因为「本轮根本没资格写动作」比
    「动作写得对不对」更早**：验收句是一段行首括注的动作（里面带着没给过的场景词）加上「可爱归
    可爱，那也不是胖。」，两关都踩——动作括注先判就整条丢了，走不到配额兜底。动作括注与条款一样
    是「怎么写」的形状判据，所以紧跟其后；内容重复接着判「这句是不是又是我说的」；冒充做过问的是
    「这件事我到底做没做过」；审计管的是「说的事是不是抄来的」，仍是最后一关。

    **没给 inject 就整段不跑审计**：没有料可比的审计既不是通过、也不是拦住，而这个返回类型装不下
    第三种状态。要区分 ok 与 audit-unavailable，看 enforce 的 Verdict.audit 或直接调 audit()。
    **没给 action 同样整段不跑动作括注**，道理一样：没判不是通过，判没判看 Verdict.action_status。
    **recent 为 None 同样整段不跑内容重复**：拿不到她最近说过什么就是判不了，而判不了不是「没重复」。
    **quota 为 None 同样整段不跑动作配额**：装配没标就是这一关没跑，判没判看 Verdict.quota_status；
    标了 allow 才是一律不命中（真判过、判过没问题）——这两种情况在返回类型里都只能是 None，
    所以分不分开看状态，不看返回值。
    **asked 是对方这一轮说的那句话**：闸拿 repeat.requested(asked, 表) 算一次，把同一个结论交给
    内容重复与复述审计。他明说要她重复时，撞上重复也只是一次跳过，撞上复述也一样——两者都不是
    命中（返回 None：跳过不是命中，这个返回类型装不下它，账与状态由 enforce 记）。
    **fatal 是这一轮的红线表**：给了就跟条款一样当一次命中（而且排在最前面）——红线问的不是
    「怎么写」，是「这一句能不能出去」。没给（None）这一关不跑，判没判看 Verdict.fatal_status。
    **actions 是这一轮的动作账**：None 表示拿不到账，冒充做过这一关整段不跑（返回 None 不是放行，
    判没判看 Verdict.claim_status）；给了清单就照实判，**空清单 [] 也算给了**——账上空着就是说这一轮
    什么都没做，该判冒充。claim 是那一关的判据表，账本形状坏掉由 claim 当场抛 ClaimBroken。
    **recalled 是这一轮真的想起来的记忆**：None 表示拿不到，编记忆这一关整段不跑（返回 None 不是放行，
    判没判看 Verdict.fabricate_status）；**空清单 [] 也算给了**——这一轮一条都没想起来，出现「引用标记
    + 具体细节」就是编的，照判。fabricate 是那一关的判据表。编记忆与冒充做过同族（都问「说的是不是真
    发生过的」），排在它后面：先说做过没做过，再说记得没记得。
    **known 是一份人名录读数（两段判），不是布尔**：None 表示这一轮没拿到读数，这一关整段不跑
    （返回 None 不是放行，判没判看 Verdict.kin_status）；给了读数就在册看 in_book、够不够熟看数据里
    known_by 那一档（现在 relation ≥ 3）。**在册不等于熟**——answer() 会把他这句话 ingest 建档，
    「查得到他」说一句话就成立，所以只有在册 *且* 读数到档才放行；刚建档的新号照样照实判。
    别的形状（True / False / 1 / "true"）由 kin 当场抛 KinBroken：一个布尔说不出「够不够熟」，猜一次
    等于替调用方把这一关关掉。
    认亲排在冒充做过后面、编记忆前面：先问「这件事我到底做没做过」，再问「这个人在不在册」，最后问
    「这段过去是不是编的」；前两关都答「不是」，才轮到「记不记得」。
    """
    prepared, _disabled = _prepared(clauses)
    body = text or ""
    dead = _fatal(body, fatal)                          # 红线最先看：它不排在任何写法判据后面
    if dead is not None:
        return _from_fatal(dead)
    found = _scan(prepared, body)
    if found is not None:
        return found
    quoted = _quota(body, quota)
    if quoted is not None:
        return quoted
    noted = _noted(body, action)
    if noted is not None:
        return noted
    wanted = repeat.requested(asked, repeat_table)      # 算一次：两关共用同一个结论
    repeated, _skip = _dedup(body, recent, repeat_table, wanted)
    if repeated is not None:
        return repeated
    claimed = _claimed(body, actions, claim)
    if claimed is not None:
        return claimed
    kinned, _evidence = _kin(body, known, kin)
    if kinned is not None:
        return kinned
    invented, _fact = _fabricated(body, recalled, fabricate)
    if invented is not None:
        return invented
    return _recited(body, inject, policy, wanted)


def enforce(text: str, clauses: Sequence[Mapping[str, Any]],
            rewrite: Optional[Callable[[str], str]] = None,
            sink: Optional[Callable[[str, str], None]] = None,
            inject: Any = None, policy: Optional[Mapping[str, Any]] = None,
            action: Optional[Mapping[str, Any]] = None,
            recent: Optional[Sequence[str]] = None,
            repeat_table: Optional[Mapping[str, Any]] = None,
            quota: Any = None,
            asked: str = "",
            keep_unrewritten: bool = False,
            fatal: Any = None,
            actions: Any = None,
            claim: Any = None,
            recalled: Any = None,
            fabricate: Any = None,
            kin: Any = None,
            known: Any = None) -> Verdict:
    """过闸：命中一次 → 用注入的 rewrite 改一次 → 再 check；还命中就弃掉这句（配额那一条另有兜底）。

    rewrite 是调用方注入的 callable（text -> text），本文件没有模型客户端。六种结局：

    - 没命中：decision="pass"，text 原样发出。
    - 命中但 rewrite 是 None：decision="dropped"、text=""——改不了就不许发，不许 fail-open
      （自带兜底的几条判据除外：配额剥行、冒充做过删句、认亲中性化、编记忆换实话，以及这一轮
      加的复述删句，都不走这条「弃空」）。
    - rewrite 抛错、或改回来是空的：同样 dropped，并把「重写失败」这件事本身写进 reason
      （上面那几条自带兜底的判据照样各自走它们自己的路）。
    - 改完不再命中：decision="rewritten"，text 是改写后的那句。
    - **冒充做过命中、改不动或二次仍命中：删掉那一句**（decision="rewritten"、salvaged=True、text 是
      删剩下的正文）；删完只剩空白才 decision="dropped"。
    - **复述命中、改不动或二次仍命中：删掉被审计命中的那一句**（decision="rewritten"、
      salvaged=True、text 是删剩下的正文，hits 里两条命中都留着）；删完只剩那一句才
      decision="dropped"，reason 取自数据的 recited_only（带 recited-only 记号）——**宁可她少说一句，
      不许她整句不说**。红线不受这条影响：踩上红线照旧整句丢。
    - **改完还命中（且不是红线）：decision="pass"，text 是进门那句原话**，hits 两条都留着，
      kept_after_rewrite=True，sink 记一行 reason 带「<通道>-after-rewrite」的账。为什么不再丢：
      **在最该回答的那一句上沉默，比「说话带点毛病」糟得多**——她改了、也尽力了，这时候把整泡
      吞掉，人那边只看到「她没说话」。
    - **踩红线（不论第几次）：decision="dropped"、text=""**——红线是唯一不许留原话的一类，宁可
      这一句不说，也不许把手机号 / 骂人的话 / 放弃式的句子发出去。红线也不再交给重写器：改一次
      不能保证它不出去。

    sink 是调用方注入的 callable（clause_id, reason -> None）：dropped 那几种会被叫一遍（参数是
    **最终拦下它的那一条**与这一句人话原因），二次命中留原话那一种也会被叫一遍（hits 两条都留着，
    记的是第二次拦下它的那一条，好让人看出这一句改过、还是不行）——pass 与 rewritten 不叫，话
    发出去了，没有弃言可记。sink=None 就不记，闸照旧是纯函数（sink 自己抛错会照实往上抛）。

    fatal 是这一轮的红线表：给了就进门先判一遍（踩上就丢掉，不进重写器），改完回来的新稿也再判
    一遍（新稿踩上同样丢掉）；没给（None）这一关没跑，Verdict.fatal_status 记 not-checked——
    **没判不是「不是红线」**。配额剥行那一条例外不变（剥完只剩空白才丢），但红线压在它前面：
    剥掉动作行也救不回一句带手机号的话。
    **「二次命中留原话」不看给没给表**：没给表只说明「判不了这一句是不是红线」，不说明可以吞掉
    这句话——所以二次命中照样留原话（kept_after_rewrite=True、decision="pass"），只是 fatal_status
    记 not-checked，账行里补一句取自数据的「这一轮没拿到红线表」。把「给不给表」当成「吞不吞这句
    话」的开关，会把「她说了、但没人听懂」变成新的哑法；拒绝那三句（organs/mouth/refusal.py）就是
    这么被吞掉的——调用侧现在把同一份 fatal 表也透给它。

    复述审计接在条款后面，走同一条路：审计拦下的那一条也当成一次命中。**它的第二处置与别的判据不同**：
    改一次还照搬、或根本改不了（没给重写器 / 抛错 / 改回来是空的），就只删掉被审计命中的那一句、把
    剩下的正文接着往下判（见 _strip_recited）——整条弃掉等于她沉默了，比念台词更糟。只有删完没正文
    可发（这一泡只有那一句）才 decision="dropped"，reason 用数据里的 recited_only。
    返回值里的 audit 是这一轮审计的读数（audit.AuditResult）：status 为 "ok" 才算真的比过，
    为 "audit-unavailable" 表示没拿到注入原文、判不了——**这时决策照常按条款走，不弃这句**，
    但状态如实带出来，api 层照实透出（缺料不是清白，也不是拦）。

    动作括注（第三轮 B）同样接在这条路上，判在条款与审计之间：命中就当一次普通命中。给没给判据表
    看 action_status——"ok" 表示这一轮真的判过，"not-checked" 表示没给表、这一关没跑；**没跑不是
    通过**，两者在返回值里必须分得开（这也是它不肯塞一条假 Hit 进 check 的原因）。

    字面重合仅记录进门原稿的观察读数，不阻断输出、不触发重写。
    repeat_status 区分 observed、skipped-by-request、ok 和 not-checked。

    冒充做过（这一轮加的）接在内容重复后面、复述审计前面，判据与词表全在 organs/mouth/claim.py 与
    data/claim.json（魂那条红线 bounds.no-fake-doing 的出口）。actions 是这一轮的动作账，**None
    表示拿不到账**——这一关没跑，Verdict.claim_status 记 not-checked，不判冒充、也不声称判过；
    **空清单 [] 是给了账，账上空着就说这一轮什么都没做，该判冒充**。这一条的处置与别的判据不同：
    命中之后照样先交给 rewrite 改一次，**改不了（没给重写器 / 抛错 / 改回来是空的）或改完还命中，
    就删掉那一句**（claim.strip_sentence，只删那一句连同它自己的句末标点），剩下的正文接着过其余
    判据（条款 → 配额 → 动作括注 → 重复 → 冒充 → 审计，命中哪一条就照旧弃）；删完只剩空白才
    decision="dropped"。救回来的那一句挂在 Verdict.salvaged 上（与配额剥行同族），账本照样记一笔
    （sink 收的是冒充做过那条 clause_id 与删句那句话）——**谎只长在那一句里，别的话还是她要说的话**，
    为一句删掉整条，比删句更糟。
    **这一条盖过 keep_unrewritten**：留原话就是留一句谎，所以改不动时选的是删句，不是留原话。

    编记忆（这一轮加的）接在冒充做过后面、复述审计前面：判据与说法全在 organs/mouth/fabricate.py 与
    data/fabricate.json。recalled 是这一轮真的想起来的记忆——**None 表示拿不到**，这一关没跑，
    Verdict.fabricate_status 记 not-checked，不判也不声称判过；**空清单 [] 是给了账、这一轮一条都
    没想起来**，照判。处置与冒充做过同族、但换的不是「删一句」而是「整泡换成一句实话」：命中之后照样
    先交给 rewrite 改一次，**改不了（没给重写器 / 抛错 / 改回来是空的）或改完还命中，就把这一泡换成
    fabricate.fallback_text**（那句实话的原文在 data/fabricate.json 里，闸不抄），decision="rewritten"、
    salvaged=True、text 是那句实话，hits 两条都留着，账本记一笔——**不是整条丢**：说不真话的时候认怂，
    比在最该回话的那一句上沉默强，人那边至少知道该再提一句。**这一条同样盖过 keep_unrewritten**：
    留原话就是留一段编出来的过去。
    **换成实话之后只再过一遍红线**：那句实话取自数据、不是她说的，别拿条款 / 配额 / 动作括注 / 内容
    重复 / 冒充做过 / 审计去卡一句我们自己写的话——拿自己的话把自己的救场话拦掉，等于换了个法子变哑。

    认亲（这一轮加的）排在冒充做过后面、编记忆前面：判据与词表全在 organs/mouth/kin.py 与 data/kin.json
    （魂那条红线 bounds.no-claim-kin 的出口：认不认这个号由人名录定，不由回复里的字定）。known 是一份
    人名录读数，**两段判**：在册看 in_book、够不够熟看数据里 known_by 那一档（现在 relation ≥ 3，
    档位阶梯只有 address_table.json 那一份）。**只有在册且读数到档才放行**，他爱怎么叫就怎么叫；
    不在册、或刚被 ingest 建档还没爬到那一档（也就是「说过一句话」的陌生人，第二轮起也一样），回复里
    出现只有熟人才能触发的称呼或记忆复述就照实判。**None 表示没拿到人名录读数**，这一关没跑，
    Verdict.kin_status 记 not-checked，不判也不声称判过（判不了就不诬陷）。处置与冒充做过同族、但换的
    不是「删一句」也不是「整泡换」：命中之后照样先交给 rewrite 改一次，**改不了（没给重写器 / 抛错 /
    改回来是空的）或改完还命中，就把命中的那一处中性化**（kin.neutralize：称呼换成数据里的中性词、
    记忆那一句换成数据里的实话，再补一句稳妥话术），decision="rewritten"、salvaged=True、text 是改完
    的正文——**认错人不必把整泡丢掉**：别的句子还是她要说的话。**这一条同样盖过 keep_unrewritten**：
    留原话就是留一句替他认亲的话。改完只剩空白才 decision="dropped"。

    asked 是对方这一轮说的那句话：闸拿 repeat.requested(asked, 表) 算一次，把同一个结论交给内容
    重复与复述审计两关。他明说要她重复时，撞上的那一关只记一笔账（clause_id 与 reason 都取自
    数据，各带自己的 skip 记号：skipped-by-request / audit-skipped-by-request），decision 保持
    pass、text 一个字不改，repeat_status 记成跳过那个状态；**豁免只在真撞上、且 asked 命中请求
    标记时成立**，asked 为空一律不豁免。两关同时撞上就记两行账——同一句撞了两关，账本上要读得
    出来。审计那一路即便跳过，Verdict.audit 也照旧带着 hit：豁免的是判废，不是撞上这件事。

    keep_unrewritten 是「改不了也不许哑」的那个开关（调用方开）：rewrite 是 None、抛错、或改回来
    是空的，都不弃句——text 原样发、decision 记 "pass"、hits 留着那条命中、reason 用
    organs/mouth/rewrite.py 里那句带 rewrite-failed 的说法，并往 sink 记一笔账。**配额命中是例外**：
    它优先走剥行兜底（把动作行拿掉比留着原话强）。默认 False 时旧行为一个字节都不变。

    动作配额（这一轮加的）排在条款后面、动作括注**前面**：装配没给这一轮写动作的资格，她根本没
    资格写动作，不必先问「动作写得对不对」。给没给配额看 quota_status——"ok" 表示装配标了这一轮
    的配额（forbid 与 allow 都算判过），"not-checked" 表示没标；**没标不是 allow**。

    **配额命中是唯一一条不丢整句的路**：先把这一句交给 rewrite 改一次（与别的判据一样）；改不了
    （rewrite 是 None）或改完还超配额，就改用 quota.strip() 把动作行剥掉、正文接着过其余判据
    （条款 → 动作括注 → 内容重复 → 复述审计，配额不再复判：动作已经被拿掉了）。正文过了其余判据
    就 decision="rewritten"、salvaged=True、text 是剥完的正文，并且往 sink 记一笔（配额那条
    clause_id 与「剥掉了什么」那句话）；剥完只剩空白、或剥完的正文仍被别的判据拦下，照旧
    decision="dropped"。**别的判据第二次命中仍照旧弃整句**，旧行为一个字节都不变。
    """
    prepared, disabled = _prepared(clauses)
    body = text or ""
    dead = _fatal(body, fatal)          # 红线最先看：它不问「怎么写」，只问「这一句能不能出去」
    found = _scan(prepared, body)
    wanted = repeat.requested(asked, repeat_table)      # 算一次：两关共用同一个结论
    seen = _audit(body, inject, policy, requested=wanted)
    quoted = _quota(body, quota)
    noted = _noted(body, action)
    repeated, skipped = _dedup(body, recent, repeat_table, wanted)
    claimed = _claimed(body, actions, claim)
    kinned, kin_evidence = _kin(body, known, kin)
    invented, invented_fact = _fabricated(body, recalled, fabricate)
    judged = action_note.STATUS_OK if _has_action(action) else action_note.STATUS_NOT_CHECKED
    deduped = _dedup_status(recent, skipped)
    quotaed = quota_rules.STATUS_OK if _has_quota(quota) else quota_rules.STATUS_NOT_CHECKED
    ledgered = claim_rules.STATUS_OK if _has_actions(actions) else claim_rules.STATUS_NOT_CHECKED
    base = dict(disabled=disabled, audit=seen, action_status=judged,
                repeat_status=deduped, quota_status=quotaed, claim_status=ledgered,
                kin_status=_kin_status(known),
                fabricate_status=_fabricate_status(recalled),
                fatal_status=_fatal_reading(dead, fatal))
    if dead is not None:
        # 红线：不重写、不留原话，进门这一遍就丢掉（改一次也不能保证它不出去）
        return _dropped(Verdict(text="", decision="dropped", hits=[_from_fatal(dead)],
                                reason=dead.reason, fatal=dead, **base), sink)

    def _salvage(source: str, hits: List[Hit]) -> Verdict:
        """配额兜底：剥掉动作行，正文接着过其余判据；判过就把剥完的正文发出去。

        这是唯一一条不丢整句的路：配额是「本轮根本没资格写动作」，动作行可以整段拿掉，剩下的
        正文还是她要说的话。**剥过不等于放行**：正文要照原样再过一遍条款 → 动作括注 → 内容重复
        → 复述审计（配额不再复判：动作已经被拿掉了），命中哪一条就照旧弃掉。剥完只剩空白时
        没有正文可发，照样弃掉。救回来的那一笔也记进 sink——账本上要看得见「本来要丢、被剥行
        留下」，不然后人只看到一句没被弃的话，猜不出闸动过手。

        剥完的正文按他说的放过的那两关（内容重复 / 复述审计）照旧各记一行账：跳过必须留痕这条
        规矩不因为「这一句是被剥出来的」就打个折——留下的正文同样是她要说的话。
        """
        bodies, removed = quota_rules.strip(source)
        if not bodies.strip():
            return _dropped(Verdict(text="", decision="dropped", hits=list(hits),
                                    reason=quota_rules.blank_reason(removed), **base), sink)
        kept = quota_rules.strip_reason(removed)
        dead_bodies = _fatal(bodies, fatal)               # 剥掉动作行也救不回一句带手机号的话
        if dead_bodies is not None:
            return _dropped(Verdict(text="", decision="dropped",
                                    hits=list(hits) + [_from_fatal(dead_bodies)],
                                    reason=dead_bodies.reason, fatal=dead_bodies,
                                    **dict(base, fatal_status=FATAL_HIT)), sink)
        again_repeated, again_skipped = _dedup(bodies, recent, repeat_table, wanted)
        again_seen = _audit(bodies, inject, policy, requested=wanted)
        again_seen_hit = _from_audit(again_seen)
        again_kinned, again_evidence = _kin(bodies, known, kin)
        again_invented, again_fact = _fabricated(bodies, recalled, fabricate)
        after = _first(_scan(prepared, bodies), _noted(bodies, action), again_repeated,
                       again_kinned, again_invented, again_seen_hit)
        if again_seen_hit is not None and after is again_seen_hit:
            return _strip_recited(bodies, list(hits) + [after])   # 剥出来的正文还在照搬：删那一句
        if after is not None and after is again_invented:
            return _confess(list(hits) + [after], again_fact)   # 剥出来的正文还在编：换成实话
        if after is not None and after is again_kinned:
            return _neutralize_kin(bodies, list(hits) + [after], again_evidence)
        if after is not None:
            return _dropped(Verdict(text="", decision="dropped", hits=list(hits) + [after],
                                    reason=quota_rules.leftover_reason(after.clause_id,
                                                                      after.matched), **base), sink)
        if sink is not None:
            sink(hits[-1].clause_id, kept)
        _note(sink, _skip_rows(again_skipped, again_seen))
        return Verdict(text=bodies, decision="rewritten", hits=list(hits), reason=kept,
                       salvaged=True, **base)

    def _strip_claim(source: str, hits: List[Hit]) -> Verdict:
        """冒充做过命中之后：只删那一句，剩下的话接着往下判；删完只剩空白才弃整条。

        为什么删句而不是整条丢：这一关判的是「这一句说了没做过的事」，谎只长在那一句里——别的话
        还是她要说的话，为一句谎把整条吞掉，等于在最该回答的那一句上沉默。删句与配额剥行同族
        （两块都是「救人」的分支，被救回来的那一句都挂在 Verdict.salvaged 上、都记一笔账），
        差别只在删什么：配额剥的是动作行，这一关删的是那一整句。

        **删过不等于放行**：剩下的正文要照原样再过一遍其余判据（条款 → 配额 → 动作括注 → 重复 →
        冒充做过 → 审计），命中哪一条就照旧弃掉；剩下的正文里再有一句冒充，就接着删那一句——
        递归的深度由句数封顶，删一句少一句。删完只剩空白时没有正文可发，照样弃掉。

        那一句取不出来（strip_sentence 一个字都没删）也弃掉：拿不准删没删干净，就不许把一句谎
        当「处理过了」发出去。救回来的那一笔照样记进 sink——账本上要看得见「本来要弃、被删句
        留下」，不然后人只看到一句没被弃的话，猜不出闸动过手。
        """
        rest, removed = claim_rules.strip_sentence(source, hits[-1], claim)
        if not removed:
            return _dropped(Verdict(text="", decision="dropped", hits=list(hits),
                                    reason=claim_rules.leftover_reason(hits[-1].clause_id,
                                                                      hits[-1].matched, claim),
                                    **base), sink)
        if not rest.strip():
            return _dropped(Verdict(text="", decision="dropped", hits=list(hits),
                                    reason=claim_rules.blank_reason(removed, claim), **base), sink)
        kept = claim_rules.strip_reason(removed, claim)
        dead_rest = _fatal(rest, fatal)                # 删掉一句谎也救不回一句带手机号的话
        if dead_rest is not None:
            return _dropped(Verdict(text="", decision="dropped",
                                    hits=list(hits) + [_from_fatal(dead_rest)],
                                    reason=dead_rest.reason, fatal=dead_rest,
                                    **dict(base, fatal_status=FATAL_HIT)), sink)
        again_repeated, again_skipped = _dedup(rest, recent, repeat_table, wanted)
        again_seen = _audit(rest, inject, policy, requested=wanted)
        again_seen_hit = _from_audit(again_seen)
        again_claimed = _claimed(rest, actions, claim)
        again_kinned, again_evidence = _kin(rest, known, kin)
        again_invented, again_fact = _fabricated(rest, recalled, fabricate)
        after = _first(_scan(prepared, rest), _quota(rest, quota), _noted(rest, action),
                       again_repeated, again_claimed, again_kinned, again_invented,
                       again_seen_hit)
        if after is None:
            if sink is not None:
                sink(hits[-1].clause_id, kept)
            _note(sink, _skip_rows(again_skipped, again_seen))
            return Verdict(text=rest, decision="rewritten", hits=list(hits), reason=kept,
                           salvaged=True, **base)
        if after is not None and after is again_invented:
            return _confess(list(hits) + [after], again_fact)   # 删剩的正文还在编：换成实话
        if again_claimed is not None and after is again_claimed:
            return _strip_claim(rest, list(hits) + [after])     # 剩下的正文还有一句冒充：接着删
        if again_kinned is not None and after is again_kinned:
            return _neutralize_kin(rest, list(hits) + [after], again_evidence)
        if again_seen_hit is not None and after is again_seen_hit:
            return _strip_recited(rest, list(hits) + [after])   # 删剩的正文还在照搬：接着删那一句
        return _dropped(Verdict(text="", decision="dropped", hits=list(hits) + [after],
                                reason=claim_rules.leftover_reason(after.clause_id, after.matched,
                                                                   claim),
                                **base), sink)

    def _strip_recited(source: str, hits: List[Hit]) -> Verdict:
        """复述命中之后：只删被审计命中的那一句，剩下的话接着往下判；删完只剩空白才弃整条。

        为什么删句而不是整条丢：照搬长在那一句里——别的话还是她要说的话，为一句复述把整条吞掉，
        等于她沉默了，比念台词更糟。一句判据：**宁可少说一句，不许整句不说**。这一支与冒充做过的
        删句同族（都是「救人」的分支：救回来的那一笔挂在 Verdict.salvaged 上、都记一笔账），差别
        只在削什么：那一关削的是「说她做过」的那一句，这一支削的是审计命中的那一句——删哪一句由
        命中决定，句界取自 audit.json 的 sentence_marks（逗号不算句末）。

        **删过不等于放行**：剩下的正文要照原样再过一遍其余判据（条款 → 配额 → 动作括注 → 重复 →
        冒充做过 → 认亲 → 编记忆 → 审计），命中哪一条就照旧处置；剩下的正文里还有一句在照搬，就接着
        删那一句——递归的深度由句数封顶，删一句少一句。删完只剩空白（这一泡只有那一句）时没有正文
        可发，照样弃掉，那句人话取自数据里的 recited_only（带 recited-only 记号）：**是「只剩那一句」，
        不是「整条判废」**，账本上要分得开。

        那一句取不出来（正文里找不出命中的那几个字）也弃掉：拿不准删没删干净，就不许把一句复述当成
        「处理过了」发出去——这时那句人话用审计自己那条原因，如实说「弃掉不发」，不再另编一句。
        """
        rules = _recited_rules(policy)
        rest, removed = _cut_recited(source, hits[-1], rules)
        if not removed:
            return _dropped(Verdict(text="", decision="dropped", hits=list(hits),
                                    reason=hits[-1].reason, **base), sink)
        if not rest.strip():
            return _dropped(Verdict(text="", decision="dropped", hits=list(hits),
                                    reason=rules.only_reason.format(removed=removed), **base), sink)
        kept = rules.strip_reason.format(removed=removed, clause_id=hits[-1].clause_id,
                                         matched=hits[-1].matched)
        dead_rest = _fatal(rest, fatal)          # 删掉一句复述也救不回一句带手机号的话
        if dead_rest is not None:
            return _dropped(Verdict(text="", decision="dropped",
                                    hits=list(hits) + [_from_fatal(dead_rest)],
                                    reason=dead_rest.reason, fatal=dead_rest,
                                    **dict(base, fatal_status=FATAL_HIT)), sink)
        again_repeated, again_skipped = _dedup(rest, recent, repeat_table, wanted)
        again_seen = _audit(rest, inject, policy, requested=wanted)
        again_seen_hit = _from_audit(again_seen)
        again_claimed = _claimed(rest, actions, claim)
        again_kinned, again_evidence = _kin(rest, known, kin)
        again_invented, again_fact = _fabricated(rest, recalled, fabricate)
        after = _first(_scan(prepared, rest), _quota(rest, quota), _noted(rest, action),
                       again_repeated, again_claimed, again_kinned, again_invented,
                       again_seen_hit)
        if after is None:
            if sink is not None:
                sink(hits[-1].clause_id, kept)
            _note(sink, _skip_rows(again_skipped, again_seen))
            return Verdict(text=rest, decision="rewritten", hits=list(hits), reason=kept,
                           salvaged=True, **base)
        if after is again_invented:
            return _confess(list(hits) + [after], again_fact)   # 删剩的正文还在编：换成实话
        if again_claimed is not None and after is again_claimed:
            return _strip_claim(rest, list(hits) + [after])     # 剩下的正文还有一句冒充：接着删
        if again_kinned is not None and after is again_kinned:
            return _neutralize_kin(rest, list(hits) + [after], again_evidence)
        if again_seen_hit is not None and after is again_seen_hit:
            return _strip_recited(rest, list(hits) + [after])   # 剩下的正文还有一句照搬：接着删
        return _dropped(Verdict(text="", decision="dropped", hits=list(hits) + [after],
                                reason=rules.leftover_reason.format(clause_id=after.clause_id,
                                                                   matched=after.matched),
                                **base), sink)

    def _confess(hits: List[Hit], fact: Any) -> Verdict:
        """编记忆命中之后：把这一整泡换成那句实话，不是整条丢；换过不等于放行，红线照样先判。

        为什么换、而不是删句或丢空：这一关判的是「她引用了一段没发生过的过去」——**编造长在整泡的
        前提上**，删掉哪一句都可能剩下半截照样在替自己编（「是周三上午吧？」删了前半句就只剩一个
        反问）。所以处置与配额剥行、冒充做过删句不同：**整泡换成 data 里那句实话**（fallback_text）。
        这一格与它们同族的地方在于——都是「救人」的分支：救回来的那一泡挂在 Verdict.salvaged 上、
        都一样记一笔账（否则账本上只看到一句没被弃的话，猜不出闸动过手）。
        **换上去的那句只再过一遍红线**：那句实话取自数据、不是她说的，拿条款 / 配额 / 动作括注 /
        内容重复 / 冒充做过 / 审计去卡一句我们自己写的话，等于用自己的规矩把自己的救场话拦掉——
        那就又变哑了。红线例外：一句带手机号 / 骂人的话，换成实话也不许发出去。
        """
        honest = fabricate_rules.fallback_text(fabricate)
        why = fabricate_rules.fallback_reason(fact, fabricate)
        dead_honest = _fatal(honest, fatal)      # 换成实话也救不回一句带手机号的话
        if dead_honest is not None:
            return _dropped(Verdict(text="", decision="dropped",
                                    hits=list(hits) + [_from_fatal(dead_honest)],
                                    reason=dead_honest.reason, fatal=dead_honest,
                                    **dict(base, fatal_status=FATAL_HIT)), sink)
        if sink is not None:
            sink(hits[-1].clause_id, why)
        return Verdict(text=honest, decision="rewritten", hits=list(hits), reason=why,
                       salvaged=True, **base)

    def _neutralize_kin(source: str, hits: List[Hit], evidence: Any) -> Verdict:
        """认亲命中之后：把命中的那一处换成中性说法，剩下的话接着往下判；换完只剩空白才弃整条。

        为什么中性化、而不是删句或整泡换：认亲错长在「对谁的称呼 / 谁的记忆」上，不在整句话上——
        叫错了人，不该把整泡吞掉（那是在最该回话的那一句上沉默），也不该删掉那一句（删完她连招呼
        都没打）。所以处置是：称呼类只换那一处称呼、记忆类只换那一句，别的句子一个字不动，再补一句
        数据里的稳妥话术；救回来的这一泡挂在 Verdict.salvaged 上（与配额剥行、冒充删句同族），
        账本照样记一笔——**不然后人只看到一句没被弃的话，猜不出闸动过手**。

        **换过不等于放行**：剩下的正文要照原样再过一遍其余判据（条款 → 配额 → 动作括注 → 重复 →
        冒充做过 → 认亲 → 编记忆 → 审计），命中哪一条就照旧弃掉；剩下的正文里还有一处认亲，就接着
        换那一处——递归的深度由命中的处数封顶，换一处少一处。

        那一处换不出来（neutralize 一个字都没改）也弃掉：拿不准换没换，就不许把一句认亲的话当
        「处理过了」发出去。**这一条盖过 keep_unrewritten**：留原话就是留一句替他认亲的话。
        """
        rest, changed = kin_rules.neutralize(source, evidence, kin)
        if not changed:
            return _dropped(Verdict(text="", decision="dropped", hits=list(hits),
                                    reason=kin_rules.leftover_reason(hits[-1].clause_id,
                                                                     hits[-1].matched, kin),
                                    **base), sink)
        if not rest.strip():
            return _dropped(Verdict(text="", decision="dropped", hits=list(hits),
                                    reason=kin_rules.blank_reason(changed, kin), **base), sink)
        kept = kin_rules.neutralize_reason(changed, kin)
        dead_rest = _fatal(rest, fatal)          # 换个称呼也救不回一句带手机号的话
        if dead_rest is not None:
            return _dropped(Verdict(text="", decision="dropped",
                                    hits=list(hits) + [_from_fatal(dead_rest)],
                                    reason=dead_rest.reason, fatal=dead_rest,
                                    **dict(base, fatal_status=FATAL_HIT)), sink)
        again_repeated, again_skipped = _dedup(rest, recent, repeat_table, wanted)
        again_seen = _audit(rest, inject, policy, requested=wanted)
        again_seen_hit = _from_audit(again_seen)
        again_claimed = _claimed(rest, actions, claim)
        again_kinned, again_evidence = _kin(rest, known, kin)
        again_invented, again_fact = _fabricated(rest, recalled, fabricate)
        after = _first(_scan(prepared, rest), _quota(rest, quota), _noted(rest, action),
                       again_repeated, again_claimed, again_kinned, again_invented,
                       again_seen_hit)
        if after is None:
            if sink is not None:
                sink(hits[-1].clause_id, kept)
            _note(sink, _skip_rows(again_skipped, again_seen))
            return Verdict(text=rest, decision="rewritten", hits=list(hits), reason=kept,
                           salvaged=True, **base)
        if after is not None and after is again_invented:
            return _confess(list(hits) + [after], again_fact)   # 换过的正文还在编：换成实话
        if again_claimed is not None and after is again_claimed:
            return _strip_claim(rest, list(hits) + [after])     # 换过的正文还有一句冒充：接着删
        if again_kinned is not None and after is again_kinned:
            return _neutralize_kin(rest, list(hits) + [after], again_evidence)
        if again_seen_hit is not None and after is again_seen_hit:
            return _strip_recited(rest, list(hits) + [after])   # 换过的正文还在照搬：删那一句
        return _dropped(Verdict(text="", decision="dropped", hits=list(hits) + [after],
                                reason=kin_rules.leftover_reason(after.clause_id, after.matched,
                                                                 kin),
                                **base), sink)

    audit_hit = _from_audit(seen)          # 复述那一关的命中：删句那一支按它认（identity 比较）
    first = _first(found, quoted, noted, repeated, claimed, kinned, invented, audit_hit)
    if first is None:
        reason = "没命中任何条款，这句照发。"
        if _has_action(action):
            reason += action_note.clear_reason(action)     # 放行要有据：这一关查过了也要说出来
        if _has_recent(recent):
            reason += skipped.reason if skipped is not None else repeat.clear_reason(repeat_table)
        if _has_quota(quota):
            reason += quota_rules.clear_reason()           # 配额这一关查过了，同样要说出来
        if _has_actions(actions):
            reason += claim_rules.clear_reason(claim)      # 冒充做过这一关查过了，同样要说出来
        if _has_kin(known):
            reason += kin_rules.clear_reason(kin)          # 认亲这一关查过了，同样要说出来
        if _has_recalled(recalled):
            reason += fabricate_rules.clear_reason(fabricate)   # 编记忆这一关查过了，同样要说出来
        rows = _skip_rows(skipped, seen)
        if rows:
            return _skipped(body, rows, reason, sink, base)  # 这几关本来要判废，按他说的放
        return Verdict(text=body, decision="pass", hits=[], reason=reason, **base)
    if rewrite is None:
        if first is claimed:
            return _strip_claim(body, [first])             # 冒充做过自带兜底：没有重写器也删那一句
        if first is kinned:
            return _neutralize_kin(body, [first], kin_evidence)   # 认亲自带兜底：没有重写器也中性化
        if first is invented:
            return _confess([first], invented_fact)        # 编记忆自带兜底：没有重写器也换成实话
        if first is quoted:
            return _salvage(body, [first])                 # 配额自带兜底：没有重写器也剥
        if first is audit_hit:
            return _strip_recited(body, [first])           # 复述自带兜底：没有重写器也删那一句
        if keep_unrewritten:
            return _kept(body, first, rewrite_rules.no_rewriter_why(), sink, base)
        return _dropped(Verdict(text="", decision="dropped", hits=[first],
                                reason="%s也没给重写器，这句改不了，弃掉不发。" % _lead(first),
                                **base), sink)
    try:
        again = rewrite(body)
    except Exception as exc:          # 重写失败只算「改不了」，出口不许抛错
        if first is claimed:
            return _strip_claim(body, [first])             # 改不动一句谎：删那一句，不留原话
        if first is kinned:
            return _neutralize_kin(body, [first], kin_evidence)   # 改不动一处认亲：中性化，不留原话
        if first is invented:
            return _confess([first], invented_fact)        # 改不动一段编出来的过去：换成实话
        if first is audit_hit:
            return _strip_recited(body, [first])           # 重写失败也删那一句，不许整泡消失
        if keep_unrewritten:
            if first is quoted:
                return _salvage(body, [first])             # 配额命中：剥掉动作行比留原话强
            return _kept(body, first, _unrewritable_why(exc), sink, base)
        return _dropped(Verdict(text="", decision="dropped", hits=[first],
                                reason="%s重写时出了错（%s：%s），这句改不了，弃掉不发。"
                                       % (_lead(first), type(exc).__name__, exc),
                                **base), sink)
    rewritten = again if isinstance(again, str) else ("" if again is None else str(again))
    if not rewritten.strip():
        if first is claimed:
            return _strip_claim(body, [first])             # 改回来是空的等于改不了：删那一句
        if first is kinned:
            return _neutralize_kin(body, [first], kin_evidence)   # 改回来是空的：中性化
        if first is invented:
            return _confess([first], invented_fact)        # 改回来是空的：换成实话
        if first is audit_hit:
            return _strip_recited(body, [first])           # 改回来是空的等于改不了：删那一句
        if keep_unrewritten:
            if first is quoted:
                return _salvage(body, [first])
            return _kept(body, first, rewrite_rules.not_text_why(), sink, base)
        return _dropped(Verdict(text="", decision="dropped", hits=[first],
                                reason="%s重写回来是空的，等于改不了，弃掉不发。" % _lead(first),
                                **base), sink)
    retried = _scan(prepared, rewritten)
    requoted = _quota(rewritten, quota)
    again_noted = _noted(rewritten, action)
    again_repeated = _dedup(rewritten, recent, repeat_table, wanted)[0]
    again_claimed = _claimed(rewritten, actions, claim)
    again_kinned, again_evidence = _kin(rewritten, known, kin)
    # 改完这句自己过的那一遍同样按他说的放：他要求的是重复，不是允许她抄一段设定出去——
    # 所以这里的审计若给出 skipped，second 就是 None（不弃），但账不再记一笔：发出去的是新稿，
    # 新稿没撞上，跳过的那一句没被发出去，记了就与事实对不上。
    again_seen = _audit(rewritten, inject, policy, requested=wanted)
    again_hit = _from_audit(again_seen)
    again_invented, again_fact = _fabricated(rewritten, recalled, fabricate)
    # 新稿自己踩上红线：一个新稿都还没判它撞不撞条款，就已经不能发了——所以红线排在 second 前面。
    dead_new = _fatal(rewritten, fatal)
    if dead_new is not None:
        return _dropped(Verdict(text="", decision="dropped",
                                hits=[first, _from_fatal(dead_new)],
                                reason=dead_new.reason, fatal=dead_new,
                                **dict(base, fatal_status=FATAL_HIT)), sink)
    second = _first(retried, requoted, again_noted, again_repeated, again_claimed,
                    again_kinned, again_invented, again_hit)
    if second is None:
        return Verdict(text=rewritten, decision="rewritten", hits=[first],
                       reason="%s改写一次后不再命中，发改写后的这句。" % _lead(first), **base)
    if second is again_claimed:
        return _strip_claim(rewritten, [first, second])     # 改完还谎：删那一句，不丢整条
    if second is again_invented:
        return _confess([first, second], again_fact)        # 改完还在编：整泡换成实话，不丢整条
    if second is again_kinned:
        return _neutralize_kin(rewritten, [first, second], again_evidence)   # 改完还在认亲：中性化
    if requoted is not None and second is requoted:
        return _salvage(rewritten, [first, second])        # 配额第二次命中：剥行兜底，不丢整句
    if again_hit is not None and second is again_hit:
        return _strip_recited(rewritten, [first, second])  # 改完还照搬：删那一句，不丢整条
    # 二次命中且不是红线：留进门那句原话，记一行 <通道>-after-rewrite 的账（不再丢空）。
    # **这一格不看调用方给没给红线表**：没给表只说明「判不了是不是红线」，不说明可以吞掉这句话，
    # 所以账行里如实写上「这一轮没拿到红线表」，话照原样发出（见 rewrite.after_rewrite_reason）。
    return _after_rewrite(body, first, second,
                          channel=_rewrite_channel(second, retried, again_noted,
                                                   again_repeated, again_hit),
                          checked=_has_fatal(fatal), sink=sink, base=base)


def _dropped(verdict: Verdict, sink: Optional[Callable[[str, str], None]]) -> Verdict:
    """把弃掉的那一句交给注入的 sink 记一笔；sink 为 None 就什么也不发生。

    只传最终拦下它的那条条款与那句人话原因——账本记的是「哪条条款、为什么弃」，不存整句原文；
    reason 里会带上这次命中的那一小段（闸的判据要用它复盘），整句不会进账本。

    配额兜底救回来的那一句也由 enforce 自己叫一次 sink（不走这个函数）：那是唯一一条「记了账
    却没丢」的路，账本上因此看得见「本来要丢、被剥行留下」。
    """
    if sink is not None and verdict.hits:
        sink(verdict.hits[-1].clause_id, verdict.reason)
    return verdict


def _recited(text: str, inject: Any, policy: Optional[Mapping[str, Any]],
             requested: bool = False) -> Optional[Hit]:
    """审计这一关：判「在复述」就返回一条 Hit；没给注入原文、判不了、或按他说的放过都返回 None。

    没给 inject（None、空清单、空白字符串）就整段不跑：没有料可比的审计既不是通过、也不是拦住，
    而这个返回类型装不下第三种状态——那种情况的状态在 enforce 的 Verdict.audit 里。给了 inject
    就照实判，注入段形状坏掉（未知 kind 之类）由 audit 当场抛 AuditBroken，不许静默从严。
    requested 为真时，审计撞上也只是「按他说的放过」——返回 None 不是「没撞上」，账本与状态由
    enforce 记（这个返回类型装不下跳过，理由同上）。
    """
    if not _has_inject(inject):
        return None
    return _from_audit(_audit(text, inject, policy, requested=requested))


def _from_audit(result: Optional[AuditResult]) -> Optional[Hit]:
    """把审计拦下的那一条翻成闸自己的 Hit：clause_id 记违规类别，kind 记来源段那一类。

    两个名字都取自数据（audit.json 的 verdicts 与六类 kind），所以弃言账本里看到的仍是一个能
    指认的名字；matched 是归一化之后撞上的那几个字，reason 是审计那句人话。
    **跳过的那一条翻不成 Hit**：他的要求是把这句放过，放过的意思是「不判废」，不是「没撞上」——
    所以 hit 留在 Verdict.audit 里（面板看得见她引了设定），而不进 hits（那会让闸照旧判废）。
    """
    if result is None or result.hit is None or result.skipped:
        return None
    one = result.hit
    return Hit(clause_id=one.kind, kind=one.seg_kind, matched=one.matched, reason=one.reason)


def _recited_rules(policy: Optional[Mapping[str, Any]] = None) -> _RecitedRules:
    """把审计数据里「删那一句」那几样读出来；policy 为 None 就现读仓库里的默认表。

    为什么闸要自己读一次这份数据：删句要用的句界表与那句人话只有这里才有，而 enforce 手上的
    policy 也可能就是 None（调用方不传、用默认表的时候）。**不补默认值**：缺节缺键一律抛
    AuditBroken（与 audit.py 同一个口径），数据里没有的句界不在代码里写一个字符。
    硬要求：句界表非空；三句人话里都必须带着数据里那个记号（recited.status）——账本与面板靠它
    认出「这一笔是删句救回来的」，记号与说法不许各走各的。
    """
    book = policy if policy is not None else load("audit")
    try:
        recited = book["recited"]
        reasons = book["reason"]
        status = recited["status"]
        marks = book["sentence_marks"]
        strip = book["strip"]
        blank = book["blank"]
        strip_reason = reasons["recited_strip"]
        only_reason = reasons["recited_only"]
        leftover_reason = reasons["recited_leftover"]
    except (KeyError, TypeError):
        raise AuditBroken("数据", "缺少删那一句要用的项（recited.status、sentence_marks、strip、"
                                  "blank 与 reason.recited_* 三句人话）。") from None
    words = (("recited.status", status), ("reason.recited_strip", strip_reason),
             ("reason.recited_only", only_reason),
             ("reason.recited_leftover", leftover_reason))
    for name, value in words:
        if not isinstance(value, str) or not value.strip():
            raise AuditBroken("数据", "的 %r 必须是一句非空字符串。" % name)
    for name, value in (("sentence_marks", marks), ("strip", strip), ("blank", blank)):
        if not isinstance(value, str) or not value:
            raise AuditBroken("数据", "的 %r 必须是一串非空字符（表里的字符逐个用）。" % name)
    for name, line in (("reason.recited_strip", strip_reason),
                       ("reason.recited_only", only_reason),
                       ("reason.recited_leftover", leftover_reason)):
        if status not in line:
            raise AuditBroken("数据", "里 %s 没有带上 recited.status（%r）：账本与面板靠这个记号"
                                      "认出「这一笔是删句救回来的」，说法与记号不许各走各的。"
                              % (name, status))
    return _RecitedRules(status=status, strip=strip, blank=blank, sentence_marks=marks,
                         strip_reason=strip_reason, only_reason=only_reason,
                         leftover_reason=leftover_reason)


def _normalized_positions(body: str, strip: str, blank: str) -> Tuple[str, List[int]]:
    """按审计那张标点表与空白表归一化，同时留下位置表：归一化后第 i 个字原来是原文的哪一个字。

    审计的 matched 是**归一化之后**的字，删句却要落在原文坐标上——没有这张表，标点一多就会
    删错句（把前一句的尾巴连同后一句的头一起切掉）。
    """
    drop = set(strip) | set(blank)
    kept: List[str] = []
    spots: List[int] = []
    for index, char in enumerate(body):
        if char in drop:
            continue
        kept.append(char)
        spots.append(index)
    return "".join(kept), spots


def _sentence_spans(body: str, marks: str) -> List[Tuple[int, int, int]]:
    """按数据里的句界表分句：每句给（起, 句末, 标点之后）三个位置，位置连续才删得准。

    与 claim.py 的分句同一个形状（那份句界表在它自己的数据里）：标点本身不算进句子，删句时要连
    它一起删，否则删完会剩一个孤零零的句号。末句后面没有句末标点时，三个位置都落在文末。
    """
    chars = "".join(dict.fromkeys(marks))
    pattern = re.compile("[%s]" % "".join(re.escape(ch) for ch in chars))
    out: List[Tuple[int, int, int]] = []
    start = 0
    for found in pattern.finditer(body):
        out.append((start, found.start(), found.end()))
        start = found.end()
    out.append((start, len(body), len(body)))
    return out


def _cut_recited(text: str, hit: Hit, rules: _RecitedRules) -> Tuple[str, str]:
    """只删被审计命中的那一句，返回（剩下的正文, 被删掉的那一句）；定位不到就原样返回、removed 是空串。

    先在归一化后的正文里找 hit.matched（审计给的是归一化之后的字），再用位置表映射回原文坐标，
    然后按数据里的句界表分句、只删含这一起点的那一句连同它自己的句末标点：**逗号不算句末**，
    一句话里夹着逗号时后半截跟着一起留下。命中若跨了两句（归一化把标点去掉，它们粘成了一句），
    这里只删起点那一句，剩下的半截还会再撞上，由调用方接着删下一句——一次只删一句。
    """
    body = _text_of(text)
    if not hit.matched:
        return body, ""
    norm, spots = _normalized_positions(body, rules.strip, rules.blank)
    at = norm.find(hit.matched)
    if at < 0:
        return body, ""
    start = spots[at]
    for head, tail, mark_end in _sentence_spans(body, rules.sentence_marks):
        if head <= start <= tail:
            return (body[:head] + body[mark_end:]).strip(), body[head:tail].strip()
    return body, ""


def _noted(text: str, action: Any) -> Optional[Hit]:
    """动作括注这一关：判「动作里带了没给的场景」就返回一条 Hit；没给判据表就整段不跑。

    clause_id 与 kind 都取自判据表（action_note 带出来的那两个名字），闸不自己造名字——账本里
    看到的因此是一个数据里能指认的名字。**没给表就返回 None 不是放行**：这一关判没判挂在
    Verdict.action_status 上（None 与「判过没问题」挤不进同一条通道，所以不塞假 Hit）。
    表本身形状坏掉由 action_note 当场抛 ActionBroken，照实往上抛。
    """
    if not _has_action(action):
        return None
    found = action_note.check(text, action)
    if found is None:
        return None
    return Hit(clause_id=found.clause_id, kind=found.kind, matched=found.matched,
               reason=found.reason)


def _has_action(action: Any) -> bool:
    """给没给判据表：None 就是没给。别的形状一律当成给了，交给 action_note 去报错并指名缺哪一项。"""
    return action is not None


def _claimed(text: str, actions: Any, claim: Any) -> Optional[Hit]:
    """冒充做过这一关：说了这一轮没做过的动作就返回一条 Hit；没给动作账（None）就整段不跑。

    clause_id 与 kind 都取自判据表（claim 带出来的那两个名字），闸不自己造名字——账本里看到的
    因此是一个数据里能指认的名字。**没给账（None）就返回 None 不是放行**：这一关判没判挂在
    Verdict.claim_status 上（「没给账」与「给了账、判过没问题」挤不进同一条通道，所以不塞假 Hit）；
    给了空账（[]）时 claim 自己按「账上空着」判，那才是真的判过。**这两种形状差一个字，差的却是
    「判没判」这件事**，所以这里只认 None，别的一律交给 claim 去判。账本形状坏掉由 claim 当场抛
    ClaimBroken，照实往上抛——不许猜成「账上没有这个动作」。
    """
    if not _has_actions(actions):
        return None
    found = claim_rules.check(text, actions, claim)
    if found is None:
        return None
    return Hit(clause_id=found.clause_id, kind=found.kind, matched=found.matched,
               reason=found.reason)


def _has_actions(actions: Any) -> bool:
    """这一轮给没给动作账：None 就是没给（拿不到）。空清单是给了、只是账上空着，照样算判过。"""
    return actions is not None


def _fabricated(text: str, recalled: Any,
                fabricate: Any) -> Tuple[Optional[Hit], Any]:
    """编记忆这一关：引用了过去、细节又对不上就返回（闸那条 Hit, fabricate 那条证据）。

    返回两样是因为两处都要用：闸用 Hit 比身份、记账；「换成实话」那一步要用证据里的标记与细节，
    把「换掉了什么」写进账本。**没给 recalled（None）时两个都是 None，这不是放行**：判没判挂在
    Verdict.fabricate_status 上（「拿不到这一轮想起来的是什么」与「判过没问题」挤不进同一条通道，
    所以不塞假 Hit）；给了空清单 [] 时 fabricate 自己按「这一轮一条都没想起来」判，那才是真的判过。
    **这两种形状差一个字，差的却是「判没判」这件事**，所以这里只认 None。
    判据表形状坏掉由 fabricate 当场抛 FabricateBroken，照实往上抛——不许猜成「对得上」。
    """
    if not _has_recalled(recalled):
        return None, None
    found = fabricate_rules.check(text, recalled, fabricate)
    if found is None:
        return None, None
    return Hit(clause_id=found.clause_id, kind=found.kind, matched=found.matched,
               reason=found.reason), found


def _has_recalled(recalled: Any) -> bool:
    """这一轮给没给想起来的记忆：None 就是没给（拿不到）。空清单是给了、只是一条都没想起来，照样算判过。"""
    return recalled is not None


def _fabricate_status(recalled: Any) -> str:
    """编记忆这一关的读数：STATUS_OK（给了 recalled，真的判过）或 STATUS_NOT_CHECKED（没给，这一关没跑）。"""
    return (fabricate_rules.STATUS_OK if _has_recalled(recalled)
            else fabricate_rules.STATUS_NOT_CHECKED)


def _kin(text: str, known: Any, kin: Any) -> Tuple[Optional[Hit], Any]:
    """认亲这一关：跟着文本认亲就返回（闸那条 Hit, kin 那条证据）；没拿到人名录读数（None）就整段不跑。

    返回两样是因为两处都要用：闸用 Hit 比身份、记账；「中性化」那一步要用证据里的那个词——不知道
    命中的是哪个称呼 / 哪一段记忆，就不知道换哪一处。**没给读数（None）时两个都是 None，这不是放行**：
    判没判挂在 Verdict.kin_status 上（「没拿到人名录」与「判过、不是认亲」挤不进同一条通道，所以不塞
    假 Hit）；给了 True / False 时 kin 自己判，那才是真的判过。known 不是布尔也不是 None 时由 kin 当场
    抛 KinBroken，照实往上抛——不许猜成 True（那这一关就永远放行）。
    """
    if not _has_kin(known):
        return None, None
    found = kin_rules.check(text, known, kin)
    if found is None:
        return None, None
    return Hit(clause_id=found.clause_id, kind=found.kind, matched=found.matched,
               reason=found.reason), found


def _has_kin(known: Any) -> bool:
    """这一轮拿没拿到人名录读数：None 就是没拿到。True 与 False 都算拿到了（都判过，结论不同）。"""
    return known is not None


def _kin_status(known: Any) -> str:
    """认亲这一关的读数：STATUS_OK（这一轮拿到了人名录读数）或 STATUS_NOT_CHECKED（没拿到，这一关没跑）。"""
    return kin_rules.STATUS_OK if _has_kin(known) else kin_rules.STATUS_NOT_CHECKED


def _quota(text: str, quota: Any) -> Optional[Hit]:
    """动作配额这一关：这一轮没给写动作的资格、而这句里还有动作行，就返回一条 Hit；没标就整段不跑。

    clause_id 与 kind 都取自判据表（quota 带出来的那两个名字），闸不自己造名字——账本里看到的
    因此是一个数据里能指认的名字。**没标（None）就返回 None 不是放行**：这一关判没判挂在
    Verdict.quota_status 上（「没标」与「标了 allow、判过没问题」挤不进同一条通道，所以不塞假
    Hit）；标了 allow 时 quota 自己返回 None，那才是真的判过。认不出的写法由 quota 当场抛
    QuotaBroken，照实往上抛——不许猜成 allow。
    """
    if not _has_quota(quota):
        return None
    found = quota_rules.check(text, quota)
    if found is None:
        return None
    return Hit(clause_id=found.clause_id, kind=found.kind, matched=found.matched,
               reason=found.reason)


def _has_quota(quota: Any) -> bool:
    """装配标没标这一轮的动作配额：None 就是没标（这一关没跑）。别的形状一律当成标了，交给 quota 去报错。"""
    return quota is not None


def _dedup(text: str, recent: Any, table: Any,
           requested: bool = False) -> Tuple[Optional[Hit], Any]:
    """字面重合只作观察；明确请求复述仍保留原有审计记录。"""
    if not _has_recent(recent):
        return None, None
    return None, repeat.check(text, recent, table, requested=requested)


def _dedup_status(recent: Any, skipped: Any) -> str:
    """区分字面重合、明确请求复述、无重合和未比较。"""
    if isinstance(skipped, repeat.RepeatHit):
        return repeat.STATUS_OBSERVED
    if isinstance(skipped, repeat.RepeatSkip):
        return skipped.status
    return repeat.STATUS_OK if _has_recent(recent) else repeat.STATUS_NOT_CHECKED


def _skip_rows(skipped: Any, seen: Optional[AuditResult]) -> List[Tuple[str, str]]:
    """这一轮「按他说的放过」的账：(clause_id, 一句人话) 一关一行，顺序与判据顺序一致。

    顺序钉成内容重复在前、复述审计在后（与判「谁先判」的顺序同一个理由：报哪一条要看得出来）。
    每一关的 clause_id 与那句人话都取自数据——两关各是各的名字、各带自己的记号
    （skipped-by-request / audit-skipped-by-request），账本上因此读得出这是两关。
    没撞上的那一关不进表：没撞上就没有「判废」要放，记了只是噪音。
    """
    rows: List[Tuple[str, str]] = []
    if isinstance(skipped, repeat.RepeatSkip):
        rows.append((skipped.clause_id, skipped.reason))
    if seen is not None and seen.skipped:
        rows.append((seen.skip_clause_id, seen.skip_reason))
    return rows


def _note(sink: Optional[Callable[[str, str], None]],
          rows: Sequence[Tuple[str, str]]) -> None:
    """把这几行交给注入的 sink；sink 为 None 就什么也不发生（闸仍是纯函数，写盘在调用方）。"""
    if sink is None:
        return
    for clause_id, why in rows:
        sink(clause_id, why)


def _skipped(body: str, rows: Sequence[Tuple[str, str]], reason: str,
             sink: Optional[Callable[[str, str], None]], base: Dict[str, Any]) -> Verdict:
    """「按他说的放过」的放行：text 一个字不改、decision 仍 pass，但每一关各记一行账。

    记账用数据里的名字与那句人话（各自带着自己的 skip 记号）：跳过是对的，但它必须留痕——不然
    「她今天被放过三次」与「这一关没跑」在账本上长得一模一样。一句话同时撞上内容重复与复述审计
    时 rows 有两行，两行各记，不合并成一条：合并就要在账本上重新编一个名字出来。
    verdict 的 audit 那一节照旧带着 hit——豁免的是「判不判废」，不是「算不算撞上」。
    """
    _note(sink, rows)
    return Verdict(text=body, decision="pass", hits=[], reason=reason + "".join(
        why for _clause_id, why in rows), **base)


def _unrewritable_why(exc: BaseException) -> str:
    """重写抛错时那句「为什么改不动」：rewrite 自己带出来的原因原样用，别的异常照实写进去。

    为什么分两支：器官那一侧的失败（超时 / 空 / 不合规）本来就带着数据里那句人话，照抄最准；
    别的异常是没料到的，带上类型与原文，好让人一眼看出是哪一处坏的。
    """
    if isinstance(exc, rewrite_rules.Unrewritable):
        return str(exc)
    return rewrite_rules.error_why(exc)


def _kept(body: str, hit: Hit, why: str,
          sink: Optional[Callable[[str, str], None]], base: Dict[str, Any]) -> Verdict:
    """改不动就留着原话发：text 一个字不改、decision 记 pass、hits 留着那条命中，账本记一笔。

    这是「宁可发一句略有瑕疵的，也不许静默变哑」那一条：出口不许 fail-open（没说过的不能说），
    但也不许把她说的话当成没发生——所以 decision 是 pass（话发出去了），hits 里留着那条命中
    （闸本想过改它），reason 用 data/rewrite.json 里那句带 rewrite-failed 的说法，并且记账。
    """
    reason = rewrite_rules.failed_reason(why)
    if sink is not None:
        sink(hit.clause_id, reason)
    return Verdict(text=body, decision="pass", hits=[hit], reason=reason, **base)


def _fatal(text: str, table: Any) -> Optional[FatalHit]:
    """红线这一关：踩上就返回一条证据；**没给表（None）返回 None 不是放行**。

    没给表这一关就没跑，判没判挂在 Verdict.fatal_status 上（"ok" 真的判过，"not-checked" 没给表），
    与 action_status / repeat_status / quota_status 同一个写法——「没判」与「判过不是红线」挤不进
    同一条通道，所以这里不塞一条假命中进去。表本身形状坏掉由 fatal_hit 当场抛 FatalBroken。
    """
    if not _has_fatal(table):
        return None
    return fatal_hit(text, table)


def _has_fatal(table: Any) -> bool:
    """这一轮给没给红线表：None 就是没给（这一关没跑）。别的形状一律当成给了，交给 fatal_hit 报错。"""
    return table is not None


def _fatal_reading(one: Optional[FatalHit], table: Any) -> str:
    """红线这一关的三态读数：踩上了是 fatal,判过没踩是 ok,没给表是 not-checked。

    三态不许折叠成两态：「踩上红线」（这句话不许出去）、「判过、不是红线」（可以出去）、
    「没给表、判不了」（不是「不是红线」）是三件不同的事，面板与账本各要看得出是哪一件。
    """
    if one is not None:
        return FATAL_HIT
    return FATAL_OK if _has_fatal(table) else FATAL_NOT_CHECKED


def _from_fatal(one: FatalHit) -> Hit:
    """把红线那一条翻成闸自己的 Hit：名字全取自数据，reason 里带着哪一类与那个记号。

    为什么走同一条 Hit 通道：弃言账本、gate.blocked、面板读的是同一份东西；另开一条「红线专用」
    的通道，读的人就得在两处各看一遍，早晚有一处忘了跟着改。
    """
    return Hit(clause_id=one.clause_id, kind=one.kind, matched=one.matched, reason=one.reason)


def _rewrite_channel(second: Hit, retried: Any, noted: Any, repeated: Any,
                     audited: Any) -> str:
    """第二次拦下它的是哪一关：返回 rewrite.py 认的那个通道名，记号与说法仍旧由数据决定。

    四个名字是闸自己的判据通道（与 Verdict 上的 action_status / repeat_status 同一套口径）：
    条款 / 动作括注 / 内容重复 / 复述审计。**冒充做过与编记忆不在这四个里**：冒充做过二次命中不整条
    丢（它删那一句）、编记忆二次命中也不整条丢（它把整泡换成那句实话），两关都走不到这条路，用不着
    一个通道名。账本上要看出「改了一次没改掉的是哪一关」，通道名就得
    对得上；而那个记号（<通道>-after-rewrite）与整句人话都取自 data/rewrite.json 的 after_rewrite。
    对不上就当场响：这一句明明被拦下了，却说不出是哪一关拦的，账本就白记了。
    """
    if second is retried:
        return "clause"
    if second is noted:
        return "action"
    if second is repeated:
        return "repeat"
    if second is audited:
        return "audit"
    raise ValueError("闸的内部对不上：第二次命中的那一条既不是条款、也不是动作括注 / 内容重复 / "
                     "复述审计（%r）。" % (second,))


def _after_rewrite(body: str, first: Hit, second: Hit, channel: str, checked: bool,
                   sink: Optional[Callable[[str, str], None]], base: Dict[str, Any]) -> Verdict:
    """二次命中（且不是红线）：把**进门那句原话**发出去，记一行 <通道>-after-rewrite 的账。

    为什么留原话、而不是发那个新稿：新稿正是没改成的那一句（它自己又撞上了），能出去的还是她
    自己说的话。**在最该回答的那一句上沉默，比「说话带点毛病」糟得多**——她改了、也尽力了，
    这时候把整泡吞掉，人那边只看到「她没说话」。
    为什么还要记账：留原话不是「没问题」。账本上必须看得出「这句改过、还是不行、按原话发了」，
    不然「她今天被放过三次」与「这一关没跑」在账本上长得一模一样。
    hits 两条都留着（第一次与第二次），面板上看得见改的是哪一处、第二次又被哪一处拦下；
    kept_after_rewrite=True 让人一眼分出这一格与「压根没命中」。

    **checked=False（这一轮没给红线表）照样留原话**：判不了是不是红线，不等于可以吞掉这句话——
    账行里会补一句取自数据的「这一轮没拿到红线表」，FATAL_NOT_CHECKED 那句状态也照实带出去。
    把「给不给表」当成「吞不吞这句话」的开关，会把「她说了、但没人听懂」变成新的哑法。
    """
    why = rewrite_rules.after_rewrite_reason(channel, clause_id=second.clause_id,
                                             why=second.reason, checked=checked)
    if sink is not None:
        sink(second.clause_id, why)
    return Verdict(text=body, decision="pass", hits=[first, second], reason=why,
                   kept_after_rewrite=True, **base)


def _has_recent(recent: Any) -> bool:
    """给没给「她最近几条输出」：None 就是没给（拿不到历史）。空清单是给了、只是还没有历史，照样算判过。"""
    return recent is not None


def _has_inject(inject: Any) -> bool:
    """有没有真的给了一段注入原文：None、空清单、空白字符串都算「没给」。

    别的形状（数字、键值表）一律当成给了——让 audit 去报错并指名形状，不在这里替它猜。
    """
    if inject is None:
        return False
    if isinstance(inject, str):
        return bool(inject.strip())
    if isinstance(inject, (list, tuple)):
        return bool(inject)
    return True


def _first(*hits: Optional[Hit]) -> Optional[Hit]:
    """按判据顺序取第一条命中的：先到先报，顺序即优先级（条款 → 配额 → 动作括注 → 重复 → 冒充做过 → 编记忆 → 审计）。"""
    for one in hits:
        if one is not None:
            return one
    return None


def _scan(prepared: Sequence[Tuple[str, str, str, Any]], body: str) -> Optional[Hit]:
    """按清单顺序扫一遍，第一条命中的就停——顺序即优先级。"""
    for ident, kind, reason, compiled in prepared:
        found = compiled.search(body)
        if found is not None:
            matched = found.group(0)
            return Hit(clause_id=ident, kind=kind, matched=matched,
                       reason=_sentence(reason, matched, found.start()))
    return None


def _prepared(clauses: Any) -> Tuple[List[Tuple[str, str, str, Any]], List[str]]:
    """把整张清单校验并编译一遍，返回（生效条款, 停用条款 id 名单）。

    全部条款都要合格才返回，坏一条就抛 ClauseBroken（不做部分通过）。enabled=false 的那几条
    跳过匹配，但**照旧校验、照旧编译**：停用不是藏坏数据的角落，写坏了的 pattern 今天不跑、
    明天一开就炸，不如现在响。停用名单按清单顺序带回，供 api 层挂到 disabled_ids 上。

    空清单抛 ClauseBroken（没有闸就是没有闸）；**一条生效条款都没有（全部停用）同样抛**——
    那不是「没命中」，那是这次压根没法判。
    """
    if clauses is None:
        raise ClauseBroken("没给", "（None）：一条条款都没有就等于没有闸，不许静默放行。")
    items = list(clauses)
    if not items:
        raise ClauseBroken("是空的", "：一条条款都没有就等于没有闸，不许静默放行。")
    seen: Dict[str, int] = {}
    out: List[Tuple[str, str, str, Any]] = []
    disabled: List[str] = []
    for index, raw in enumerate(items):
        ident, kind, reason, compiled, enabled = _prepared_one(index, raw)
        if ident in seen:
            raise ClauseBroken("%r" % ident, "的 id 与第 %d 条重复（id 要能唯一指认一条条款）。"
                               % (seen[ident] + 1))
        seen[ident] = index
        if not enabled:
            disabled.append(ident)
            continue
        out.append((ident, kind, reason, compiled))
    if not out:
        raise ClauseBroken("全部停用", "：%d 条条款都写了 enabled=false（%s），一条生效条款都没有，"
                           "等于没有闸，不许当成「没命中」放行。"
                           % (len(items), "、".join(repr(one) for one in disabled)))
    return out, disabled


def _prepared_one(index: int, raw: Any) -> Tuple[str, str, str, Any, bool]:
    """校验一条条款并编译它的正则；坏在哪就说哪，报错里带着这条的 id。

    enabled 可以没有（缺省 true）；有了就必须是布尔——写成 "false" 这种字符串是在赌别人的
    truthy 判断，当场响比猜它的意思便宜。
    """
    where = "第 %d 条" % (index + 1)
    if not isinstance(raw, Mapping):
        raise ClauseBroken(where, "不是一份键值表，是 %s。" % type(raw).__name__)
    ident = raw.get("id")
    ident = ident.strip() if isinstance(ident, str) else ""
    if not ident:
        raise ClauseBroken(where, "没有 id（每条条款都要有一个稳定字符串做 id）。")
    for key in ("kind", "pattern", "reason"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ClauseBroken("%r" % ident, "缺 %s，或它不是一句非空字符串。" % key)
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ClauseBroken("%r" % ident, "的 enabled 只能是 true 或 false，读到的不是布尔。")
    try:
        compiled = re.compile(raw["pattern"])
    except re.error as exc:
        raise ClauseBroken("%r" % ident, "的 pattern 正则编译不过：%s。" % exc) from None
    return ident, raw["kind"], raw["reason"], compiled, enabled


def _sentence(reason: str, matched: str, at: int) -> str:
    """把条款自带的那句人话，与这次命中的原文拼成一句可以直接读的结论。"""
    if matched:
        return "%s；命中「%s」。" % (reason.rstrip("。"), matched)
    return "%s；这次是零宽命中（位置在第 %d 字）。" % (reason.rstrip("。"), at + 1)


def _lead(hit: Hit) -> str:
    """一句话抬头：这一句是先被哪一条拦下的。"""
    return "先被条款 %r（%s）拦下：%s" % (hit.clause_id, hit.kind, hit.reason)


# —— 红线判据：全部从 data/fatal.json 读，代码里不写一个词、一条正则 ————————

def _fatal_rules(table: Optional[Mapping[str, Any]] = None) -> _FatalRules:
    """把红线数据读成一份只读规则；table 为 None 就去仓库现读默认表。

    缺节缺键一律抛 FatalBroken——数据里没有的数不在代码里补默认值（补了就是第二处真源）。
    三条硬要求：名字非空；每类至少有一条能编译的正则；每类的人话里必须带着它自己的记号
    （mark 按 tag 填出来）——说不清「为什么连原话都没留下」的那一行，账本认不出来。
    """
    book = table if table is not None else load("fatal")
    if not isinstance(book, Mapping):
        raise FatalBroken("数据表", "不是一份键值表，读到的 %s。" % type(book).__name__)
    hit = _fatal_mapping(book, "hit", "数据")
    clause_id = _fatal_word(hit, "clause_id", "hit")
    kind = _fatal_word(hit, "kind", "hit")
    mark = _fatal_word(book, "mark", "数据")
    rows = _fatal_rows(book, "categories")
    names: List[str] = []
    tags: List[str] = []
    out: List[_FatalCategory] = []
    for index, raw in enumerate(rows):
        where = "第 %d 类" % (index + 1)
        if not isinstance(raw, Mapping):
            raise FatalBroken(where, "不是一份键值表（要写成 {\"id\": ..., \"tag\": ..., "
                                     "\"patterns\": [...], \"reason\": ...}），读到的是 %s。"
                              % type(raw).__name__)
        name = _fatal_word(raw, "id", where)
        tag = _fatal_word(raw, "tag", where)
        patterns = _fatal_patterns(raw, "patterns", where)
        reason = _fatal_word(raw, "reason", where)
        if name in names:
            raise FatalBroken(where, "的 id %r 与前面某一类重名：面板上就分不出是哪一类红线。"
                              % name)
        if tag in tags:
            raise FatalBroken(where, "的 tag %r 与前面某一类重名：账本上就认不出是哪一类。"
                              % tag)
        stamp = _fatal_fill(mark, tag, where)
        if stamp not in reason:
            raise FatalBroken(where, "的 reason 没有带上记号 %r（按 mark 与自己的 tag 填）："
                                     "账本靠它认出「这一句为什么连原话都没留下」。" % stamp)
        names.append(name)
        tags.append(tag)
        out.append(_FatalCategory(category=name, tag=tag, patterns=patterns, reason=reason))
    return _FatalRules(clause_id=clause_id, kind=kind, mark=mark, categories=tuple(out))


def _fatal_mapping(data: Mapping[str, Any], key: str, spot: str) -> Mapping[str, Any]:
    """取一节，必须是键值表。"""
    value = _fatal_need(data, key, spot)
    if not isinstance(value, Mapping):
        raise FatalBroken(spot, "的 %r 不是一份键值表，读到的 %s。" % (key, type(value).__name__))
    return value


def _fatal_word(data: Mapping[str, Any], key: str, spot: str) -> str:
    """取一句非空字符串；空着也算缺。"""
    value = _fatal_need(data, key, spot)
    if not isinstance(value, str) or not value.strip():
        raise FatalBroken(spot, "的 %r 必须是一句非空字符串。" % key)
    return value


def _fatal_rows(data: Mapping[str, Any], key: str) -> List[Any]:
    """取那张类清单：非空清单，不许拿一整串字冒充。"""
    value = _fatal_need(data, key, "数据")
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise FatalBroken("数据", "的 %r 必须是非空清单（每一类一条），不是一整串字。" % key)
    return list(value)


def _fatal_patterns(data: Mapping[str, Any], key: str, spot: str) -> Tuple[Any, ...]:
    """取一类的正则表，逐条编译好；空表、非清单、编译不过都算坏数据，当场抛并点名第几条。

    为什么当坏数据抛、而不是跳过那一条：跳过一条红线，就等于有一种说法永远能发出去——
    这种漏法没有声音，人只会觉得「她怎么把这句话说出去了」。
    """
    value = _fatal_need(data, key, spot)
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise FatalBroken(spot, "的 %r 必须是非空清单（每项一条正则），不是一整串字。" % key)
    out: List[Any] = []
    for index, one in enumerate(list(value)):
        if not isinstance(one, str) or not one.strip():
            raise FatalBroken(spot, "的 %r 里第 %d 条不是一句非空正则。" % (key, index + 1))
        try:
            out.append(re.compile(one))
        except re.error as exc:
            raise FatalBroken(spot, "的 %r 里第 %d 条正则编译不过：%s。"
                              % (key, index + 1, exc)) from None
    return tuple(out)


def _fatal_need(data: Mapping[str, Any], key: str, spot: str) -> Any:
    """取一项；缺了就抛，不补默认值。"""
    try:
        return data[key]
    except (KeyError, TypeError):
        raise FatalBroken(spot, "缺少 %r 这一项。" % key) from None


def _fatal_fill(template: str, tag: str, spot: str) -> str:
    """把记号模板按 tag 填出来；模板有问题当场抛，别把 {tag} 这种半成品写进要求里。"""
    try:
        return template.format(tag=tag)
    except (KeyError, IndexError, ValueError) as exc:
        raise FatalBroken(spot, "的 mark 填不出来：%s" % exc) from None


def _text_of(text: Any) -> str:
    """要判的那段输出：不是字符串就当空串，判据只认字。"""
    return text if isinstance(text, str) else ("" if text is None else str(text))


def _fill(template: str, spot: str, **fields: Any) -> str:
    """把模板填成人话；模板里有数据没填的占位符就抛，别把 {matched} 这种半成品发出去。"""
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise FatalBroken(str(spot), " 这句填不出来：%s" % exc) from None
