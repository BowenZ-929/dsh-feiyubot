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
记忆原文、心档位词、规矩句）被照搬一律拦；对方原话只许带引述标记转述，标记离得太远也拦。
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

**动作＋场景是第三道判据（第三轮 B）**：她可以写自己的动作与表情，但不许顺手写我们没给过的场景。
判据与词表全在 organs/mouth/action_note.py 与 data/action_note.json，闸只负责把它当成一次普通命中
接进来：action 是调用方给的判据表，**None 就整段不判**——不判绝不是通过，所以判没判挂在
Verdict.action_status 上（"ok" 真的判过，"not-checked" 这一轮没给表），由 api 层照实透出。
命中走同一条路（改一次还不行就弃），Hit 里的 clause_id 与 kind 也取自数据，弃言账本里看到的因此
是一个能指认的名字。判据表形状坏掉由 action_note 当场抛 ActionBroken，照实往上抛。

**内容重复是第四道判据（这一轮加的）**：同一动作行、或同一开场在最近几轮里又回来一次，就是
她在反复念那一条。判据与数值全在 organs/mouth/repeat.py 与 data/repeat.json，闸只把它当成一次
普通命中接进来，顺序排在动作括注之后、复述审计之前（复述管「说的事是不是抄来的」，这一关管
「这句话是不是又是我说的」——两件事都要判，先后只决定报哪一条）。recent 是她最近若干条输出的
原文（旧的在先，由调用方给），**None 就是这一轮拿不到历史**；拿不到不许当成「没重复」，所以判没判
挂在 Verdict.repeat_status 上（"ok" 真的比过，"not-checked" 这一轮没拿到），照 action_status 那个
写法。闸不记账、不取时钟：谁记着她说过什么，是调用方的事。

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
动作括注 → 内容重复 → 复述审计。**顺序不是风格问题**：验收句「（一段尾巴动作，里面带着我们没给
过的场景词）可爱归可爱，那也不是胖。」两关都踩——动作括注先判，这句就整条丢了，走不到配额兜底；
配额先判，她至少还能把正文留下（验收句原文收在 organs/mouth/quota.py 与 data/quota.json 的说明里，
闸里一个字都不抄：抄进来就又是一处真源）。

**配额命中是唯一一条「不丢整句」的路。** 其它判据第二次命中照旧弃掉整句（旧行为一个字节都不变）；
配额命中时第二次（以及没给重写器时）改为 quota.strip() 把动作行剥掉、正文接着往下判（其余判据
照跑），剥完只剩空白才 decision="dropped"。救回来那一句挂在 Verdict.salvaged 上，账本照样记一笔
（sink 收的是配额那条 clause_id 与剥行那句话），好让人看见「这句本来要丢、靠剥行留下」。

纯函数：入参只有一段文本与一张只读的条款清单，出参只有结构化数据；不碰时钟、不碰随机数、
不联网、不碰宿主 IO（本文件不 import time / datetime / random），所以能离线预览，也能被单独替换。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import action_note, repeat
from . import quota as quota_rules          # 参数名就叫 quota（调用方按这个名字传），模块用别名进来免得被挡住
from . import rewrite as rewrite_rules      # 参数名就叫 rewrite（调用方按这个名字传），模块用别名进来免得被挡住
from .audit import AuditResult, audit as _audit

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
    # 内容重复那一关判没判过：repeat.STATUS_OK（拿到了 recent，真的比过）或 STATUS_NOT_CHECKED
    # （这一轮 recent 是 None，拿不到历史）。同理——「没比」不是「没重复」，默认就是 not-checked。
    repeat_status: str = repeat.STATUS_NOT_CHECKED
    # 动作配额那一关判没判过：quota.STATUS_OK（装配标了这一轮的配额，forbid 与 allow 都算）或
    # STATUS_NOT_CHECKED（装配没标，quota 是 None）。同理——「没标」不是「标了 allow」。
    quota_status: str = quota_rules.STATUS_NOT_CHECKED
    # 这一句是不是靠剥行救回来的：配额命中之后剥掉动作行、正文过了其余判据才为真。为真时
    # decision 是 "rewritten"（剥行也是一种改写），text 是剥完的正文——账本上因此看得见这件事。
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
          fatal: Any = None) -> Optional[Hit]:
    """看这段字有没有踩条款、有没有超动作配额、有没有在动作里带场景、有没有在反复念、有没有在复述注入的原文：命中给一条 Hit，否则 None。

    **返回的不是布尔。** Hit 里带一句人话原因，原因里含这次命中的那段原文，所以日志、面板、
    复盘都能直接用这句话，不必再翻译一遍。

    顺序钉死：条款 → 动作配额 → 动作括注 → 内容重复 → 复述审计。条款按 clauses 的先后顺序逐条
    re.search，返回第一条命中的（顺序即优先级）；零宽正则也会命中，那种情况下原因里改说位置，
    免得出现一对空引号。**动作配额排在动作括注前面，因为「本轮根本没资格写动作」比「动作写得对
    不对」更早**：验收句是一段行首括注的动作（里面带着没给过的场景词）加上「可爱归可爱，那也不
    是胖。」，两关都踩——动作括注先判就整条丢了，走不到配额兜底。动作括注与条款一样是「怎么写」
    的形状判据，所以紧跟其后；内容重复接着判「这句是不是又是我说的」；审计管的是「说的事是不是
    抄来的」，仍是最后一关。

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
            fatal: Any = None) -> Verdict:
    """过闸：命中一次 → 用注入的 rewrite 改一次 → 再 check；还命中就弃掉这句（配额那一条另有兜底）。

    rewrite 是调用方注入的 callable（text -> text），本文件没有模型客户端。六种结局：

    - 没命中：decision="pass"，text 原样发出。
    - 命中但 rewrite 是 None：decision="dropped"、text=""——改不了就不许发，不许 fail-open。
    - rewrite 抛错、或改回来是空的：同样 dropped，并把「重写失败」这件事本身写进 reason。
    - 改完不再命中：decision="rewritten"，text 是改写后的那句。
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

    复述审计接在条款后面，走同一条路：审计拦下的那一条也当成一次命中，改一次还不行就弃。
    返回值里的 audit 是这一轮审计的读数（audit.AuditResult）：status 为 "ok" 才算真的比过，
    为 "audit-unavailable" 表示没拿到注入原文、判不了——**这时决策照常按条款走，不弃这句**，
    但状态如实带出来，api 层照实透出（缺料不是清白，也不是拦）。

    动作括注（第三轮 B）同样接在这条路上，判在条款与审计之间：命中就当一次普通命中。给没给判据表
    看 action_status——"ok" 表示这一轮真的判过，"not-checked" 表示没给表、这一关没跑；**没跑不是
    通过**，两者在返回值里必须分得开（这也是它不肯塞一条假 Hit 进 check 的原因）。

    内容重复接在动作括注后面、复述审计前面，走的也是同一条路：同一动作行或同一开场在最近几轮里
    又回来一次，就当一次普通命中，改一次还不行就弃。给没给 recent 看 repeat_status——"ok" 表示这
    一轮拿到了历史、真的比过，"not-checked" 表示 recent 是 None、这一关没跑；**没比不是没重复**，
    两者同样必须分得开。

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
    judged = action_note.STATUS_OK if _has_action(action) else action_note.STATUS_NOT_CHECKED
    deduped = _dedup_status(recent, skipped)
    quotaed = quota_rules.STATUS_OK if _has_quota(quota) else quota_rules.STATUS_NOT_CHECKED
    base = dict(disabled=disabled, audit=seen, action_status=judged,
                repeat_status=deduped, quota_status=quotaed,
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
        after = _first(_scan(prepared, bodies), _noted(bodies, action), again_repeated,
                       _from_audit(again_seen))
        if after is not None:
            return _dropped(Verdict(text="", decision="dropped", hits=list(hits) + [after],
                                    reason=quota_rules.leftover_reason(after.clause_id,
                                                                      after.matched), **base), sink)
        if sink is not None:
            sink(hits[-1].clause_id, kept)
        _note(sink, _skip_rows(again_skipped, again_seen))
        return Verdict(text=bodies, decision="rewritten", hits=list(hits), reason=kept,
                       salvaged=True, **base)

    first = _first(found, quoted, noted, repeated, _from_audit(seen))
    if first is None:
        reason = "没命中任何条款，这句照发。"
        if _has_action(action):
            reason += action_note.clear_reason(action)     # 放行要有据：这一关查过了也要说出来
        if _has_recent(recent):
            reason += repeat.clear_reason(repeat_table)    # 这一关查过了，也要说出来
        if _has_quota(quota):
            reason += quota_rules.clear_reason()           # 配额这一关查过了，同样要说出来
        rows = _skip_rows(skipped, seen)
        if rows:
            return _skipped(body, rows, reason, sink, base)  # 这几关本来要判废，按他说的放
        return Verdict(text=body, decision="pass", hits=[], reason=reason, **base)
    if rewrite is None:
        if first is quoted:
            return _salvage(body, [first])                 # 配额自带兜底：没有重写器也剥
        if keep_unrewritten:
            return _kept(body, first, rewrite_rules.no_rewriter_why(), sink, base)
        return _dropped(Verdict(text="", decision="dropped", hits=[first],
                                reason="%s也没给重写器，这句改不了，弃掉不发。" % _lead(first),
                                **base), sink)
    try:
        again = rewrite(body)
    except Exception as exc:          # 重写失败只算「改不了」，出口不许抛错
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
    # 改完这句自己过的那一遍同样按他说的放：他要求的是重复，不是允许她抄一段设定出去——
    # 所以这里的审计若给出 skipped，second 就是 None（不弃），但账不再记一笔：发出去的是新稿，
    # 新稿没撞上，跳过的那一句没被发出去，记了就与事实对不上。
    again_seen = _audit(rewritten, inject, policy, requested=wanted)
    again_hit = _from_audit(again_seen)
    # 新稿自己踩上红线：一个新稿都还没判它撞不撞条款，就已经不能发了——所以红线排在 second 前面。
    dead_new = _fatal(rewritten, fatal)
    if dead_new is not None:
        return _dropped(Verdict(text="", decision="dropped",
                                hits=[first, _from_fatal(dead_new)],
                                reason=dead_new.reason, fatal=dead_new,
                                **dict(base, fatal_status=FATAL_HIT)), sink)
    second = _first(retried, requoted, again_noted, again_repeated, again_hit)
    if second is None:
        return Verdict(text=rewritten, decision="rewritten", hits=[first],
                       reason="%s改写一次后不再命中，发改写后的这句。" % _lead(first), **base)
    if requoted is not None and second is requoted:
        return _salvage(rewritten, [first, second])        # 配额第二次命中：剥行兜底，不丢整句
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
    """内容重复这一关：返回（命中, 跳过）——两者互斥，都没有就是（None, None）。

    命中与跳过是两件事，所以是两样返回值、不是一种：命中要把这句拦下来（走改一次还不行就弃的
    老路），跳过要把这句放出去（只记账）。**没给 recent 就两个都返回 None 不是放行**：这一关判
    没判挂在 Verdict.repeat_status 上（拿不到历史与「比过没重复」挤不进同一条通道，所以不塞假 Hit）。

    clause_id 与 kind 都取自判据表（repeat 带出来的那两个名字），闸不自己造名字——账本里看到的
    因此是一个数据里能指认的名字；跳过那一笔的 clause_id 也是数据里的另一个名字（skip_clause_id），
    因为它记的不是判废那一笔，是「按对方要求再说一遍」那一笔。
    requested 是调用方算过一次的「他是不是在要她重复」（repeat.requested 的结论）：为真时撞上
    重复只记一笔账，不判废。**结论只算一次**，同一轮里这一关与复述审计看的是同一个布尔——各算
    一次就有分岔的风险（同一句话在账本上两种说法）。
    recent 或判据表本身形状坏掉由 repeat 当场抛 RepeatBroken，照实往上抛。
    """
    if not _has_recent(recent):
        return None, None
    found = repeat.check(text, recent, table, requested=requested)
    if isinstance(found, repeat.RepeatSkip):
        return None, found
    if found is None:
        return None, None
    clause_id, kind = repeat.names(table)
    return Hit(clause_id=clause_id, kind=kind, matched=found.matched, reason=found.reason), None


def _dedup_status(recent: Any, skipped: Any) -> str:
    """内容重复这一关的状态：跳过 > 真的比过 > 没拿到历史。

    为什么跳过排在最前：这一轮确实比过了、也确实撞上了，只是按对方的要求放过去——把它记成 "ok"
    会让人以为这一關判过没问题，记成 "not-checked" 又会让人以为这一关没跑。三种读数各是各的。
    """
    if skipped is not None:
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
    if skipped is not None:
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
    条款 / 动作括注 / 内容重复 / 复述审计。账本上要看出「改了一次没改掉的是哪一关」，通道名就得
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
    """按判据顺序取第一条命中的：先到先报，顺序即优先级（条款 → 配额 → 动作括注 → 重复 → 审计）。"""
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
