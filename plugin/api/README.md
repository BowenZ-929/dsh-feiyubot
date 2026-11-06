# api/ —— 核与壳之间的 HTTP 契约

> 这份文件只记「两侧怎么说话」。判据与算法不在这里，在 `organs/*`。
> 核是 Python（本目录 + `organs/`），壳是 TypeScript（`shell/` 与 `channels/onebot/`）。
> **两侧唯一的通道是 HTTP，只绑 127.0.0.1。** 核不 import 宿主，壳不 import 核。

## 0 谁开哪个端口

| 监听 | 谁 | 用途 | 说明 |
| --- | --- | --- | --- |
| `127.0.0.1:6200/ws` | 通道（`channels/onebot/server.ts`） | **反向 WS**：我们开端口，协议端连进来 | 现网的 6199 属别人的 AstrBot，**不许抢** |
| `127.0.0.1:8787` | 核（`api.devserver`） | 魂的端点 + 入站 `/inbound` + 面板页 | 端口可改，壳侧按配置指过来 |

反向 WS 的意思是：**我们监听，协议端拨号**。所以「重连」不是我们去连别人，而是协议端断开后自己再拨一次；我们这侧的责任只有两条——断开不崩、能再接上。

## 0.9 目录怎么拆的（2026-11-03）

两条线同时写 `api/api.py` 会互相覆盖，所以按器官拆文件，壳的入口不变：

| 文件 | 装什么 |
| --- | --- |
| `api/api.py` | 兼容壳：只 re-export `handle` / `ROUTES` / `READONLY_PAGES`，没有业务代码 |
| `api/core.py` | 路由注册表（`ROUTES` 汇总三张器官表，同名键直接抛）+ `handle()` + 公共工具 |
| `api/soul.py` | `/soul*` 十二个端点 + `SOUL_ROUTES` |
| `api/brain.py` | `/brain*` 六个只读端点 + `BRAIN_ROUTES` |
| `api/pages.py` | `/heart` 只读页 + `pages/` 的 html 读取（devserver 直接发字节） |

加端点只改自己那个器官文件：在它自己的 `*_ROUTES` 里登记，冲突会在 import 时炸出来。

## 1 核对外提供的端点

### 1.1 魂（`api/soul.py`，十二个；壳照旧只认 `api/api.py`）

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/soul` | 归一化四块 + raw 两视图 |
| GET | `/soul/check` | 自检问题清单 |
| GET | `/soul/history` | 变更史（只增不改） |
| GET | `/soul/render?audience=…` | 「她此刻会被注入的样子」；热更新的证据面 |
| POST | `/soul/edit` | 改待人姿态（人改值的唯一写入口；可选 `structural`；**不收 `probe`**） |
| POST | `/soul/migrate` | 形状变化的唯一入口（body: `actor/reason/block/value/expect_old_digest`） |
| POST | `/soul/proposal` | 接受提案（前面先过演化五闸，任一不过即拒，拒因 `GATE_*`） |
| GET | `/soul/pin` | 压缩前要 pin 的那一份（条目 id + 禁令首句，≤1200 字节） |
| GET | `/soul/revisions` | 整档归档里还留着的版本（环形 20 版，最新在前） |
| POST | `/soul/rollback` | 回到某一版（body: `actor/reason/revision`；回滚也记账、也先存整档） |
| POST | `/soul/redline` | 红线逐条加 / 逐条撤（`store.add_redline` / `retire_redline`；body: `actor/reason/op=add` 配 `item`，或 `op=retire` 配 `id`） |
| POST | `/soul/acceptance` | 验收探针：一次调用写 `value` 再写回 `restore_value`，两行都记 `kind="probe"`（body: `actor/reason/block/path/value/restore_value`） |

响应一律 `{"ok": bool, …}`，非 2xx 时带 `message` 与可选的 `issues`。

红线块没有「整块写」的入口：`edit` 动 `bounds.items` 整表、`migrate` 目标块是 `bounds`、
`/soul/redline` 的 `op` 不是 add/retire，一律 `BOUNDS_BLOCK_REPLACE_FORBIDDEN`；写盘前还有一道
红线断言（`REDLINE_TOO_MANY_LINES` / `REDLINE_DUPLICATED` / `REDLINE_SOFT_WORD`），不过就不落盘。
`REDLINE_TOO_MANY_LINES` 判的是**生效条目 ≤40 条**：本仓的红线段渲染成「；」拼接的一行，物理行恒为 1，
所以「≤40 行」那条照抄过来恒真，是翻译错误，已经删掉（拒因码沿用旧名）。

红线加/撤自己的三条：必须写 `reason`（空/空白即 `REDLINE_REASON_REQUIRED`）；**不占**那 2 次月提案
限额；同一条线撤下后 7 天（168 小时）内不许加回同一 id（`REDLINE_RERELEASE_COOLDOWN`，probe 行不计）。

### 1.1.1 写魂的动作清单（5 个）

| # | 动作 | 入口 | 一句说明 |
| --- | --- | --- | --- |
| 1 | `edit` | `POST /soul/edit` | 改一个已有叶子值（`structural=true` 才许换形状） |
| 2 | `migrate` | `POST /soul/migrate` | 形状变化的唯一入口（坏卡状态也跑得动，要带 digest） |
| 3 | `accept_proposal` | `POST /soul/proposal` | 接受提案改 `manner.stance`（演化五闸与月限额是 AND：只满足一条也拒） |
| 4 | `add_redline` / `retire_redline` | `POST /soul/redline {op=add\|retire}` | 红线逐条加 / 逐条撤（撤线只写 `active=false`，不物理删除） |
| 5 | `rollback` | `POST /soul/rollback` | 回到某一版整档（回滚本身也记账、也先存整档） |

验收探针（`POST /soul/acceptance`）不算第六个动作：它是「用同一只手写两次、再写回原值」的探针，
两行都记 `kind="probe"`，不改变她的魂；它有自己的日限额（每天 ≤3 对，超了 `PROBE_DAILY_LIMIT`），
也不许碰红线（`block="bounds"` 一律 `PROBE_ON_BOUNDS_FORBIDDEN`）。

> **判据只规定写者类别＝人；这份动作清单属实现，改动作不改判据。**
> 把两个动作合成一个、给动作改名、换一个入口，改的都是实现的形状；「只有人能动魂、程序侧不开门」
> 这条判据一个字都不跟着变。

### 1.2 入站（`api/inbound.py`，M2 新增，一个）

```
POST /inbound
Content-Type: application/json
body: 任意 JSON（通道当前送 {who, where, text, hasImage, messageId}）
→ 200 {"ok": true, "text": "回声：收到"}
```

**这个端点是写死的，没有分支。** 它存在的唯一目的，是证明「通道 → 核 → 通道」这条路是通的；怎么回话是后面里程碑的事，不许在这里长出判据。
不支持的方法不在本端点处理，落到 `api.handle` 的 404（`GET /inbound` → 404，不是 405）。这是有意的：核的六个端点一个字节都没为它改。

## 2 通道与核的对话（一次完整往返）

```
协议端 ──WS 事件──▶ channels/onebot/server.ts
                        │ ① 收到：记原始事件 JSON
                        │    parseEvent() → who / where / text / hasImage
                        │ ② 交给核：POST http://127.0.0.1:8787/inbound
                        ▼
                    api/inbound.py ──▶ ③ 核回 {"ok":true,"text":"回声：收到"}
                        │
                        │ ④ 发出：OneBot action
                        ▼
协议端 ◀─ send_group_msg / send_private_msg（message 里就是核回的那句话）
```

- 群消息回 `send_group_msg`（`params.group_id`），私聊回 `send_private_msg`（`params.user_id`）；回哪去由 `parseEvent` 的 `where` 决定，通道不猜。

### 怎么把核跑起来（唯一一条命令）

```bash
cd /Users/bowen/NewQQAgent/plugin && python3 -m api.devserver --root organs/soul/data --port 8787
```

**用终端跑，不要挂在别人（DSH 会话）的进程树下面**：把这条命令交给 DSH 里起的后台任务，宿主一重启就把核一起带走，面板随即变成 `核没有应答（127.0.0.1:8787）: ECONNREFUSED`。在自己终端里跑，它活到你自己关掉为止。

### 核可以由人独立起，壳只在探活失败时才 spawn

壳（`shell/src/core.ts`）**不抢人起的核**：启动时先 GET `/soul/check`，探到健康就一个进程都不起，只在日志里写一行「核已经在跑（人起的优先），壳不重复起」，然后继续每 5 秒探活。

所以「核挂了宿主没把它拉起来」要看日志里有没有那一行，别一律当故障：

| 壳日志 | 含义 | 该怎么办 |
| --- | --- | --- |
| 有「核已经在跑（人起的优先），壳不重复起」 | 情况二：壳正确地让位给人起的实例 | 正常，不用管 |
| 有「核没在跑，壳起了一个」+ 之后还有退避重启行 | 情况一：壳在管，只是当时还在退避窗口 | 等一个探测节拍（≤5s + 退避） |
| **一行都没有** | 壳根本没被宿主加载 | 查 profile 里还有没有 `qqagent-shell`（见下） |

**壳没加载最隐蔽的一种**：`~/.dsh/profiles/<名字>/node_modules/qqagent-shell` 这个软链还在，但 `package.json` 的 `dependencies` 与 `dsh.profile.bundles` 里的条目被别的操作重写掉了——软链看着像「装着」，宿主实际不加载。查法：

```bash
grep -c qqagent ~/.dsh/profiles/desktop/package.json   # 0 = 没装进去
```

修法：`dsh plugin --profile desktop add <仓库>/shell`，然后**重启 DSH**（插件图只在启动时读）。

面板空白/报错时按顺序查三样：

1. `lsof -nP -i :8787 -sTCP:LISTEN` —— 有输出 = 核在
2. `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/soul` —— 200 = 魂读得出来
3. 上面两条都好、面板还空 → 是壳或宿主的问题，不是核

### 改完核的代码，必须重启核进程（2026-11-03 踩过）

Python 在**进程启动时**把 `organs/*` 载进内存，之后改文件不会生效。改过 `organs/soul/model.py` 或 `data/*.json` 的校验规则后，**老进程会继续按旧代码拒载**，表现是 `/soul` 一直 500 说「多出某个键」，而磁盘上的代码其实早就允许了。

- 改 `organs/*` 或 `data/*` → **重启核**（`pkill -f api.devserver` 再起）。
- 改 `shell/*` → **pnpm build 再重启宿主**（壳的产物在宿主启动时载入）；核不用动。
- 壳不缓存、不解析魂的内容：核一旦好，面板下一条请求就正常，不需要重启壳。

**发现「源码里有、请求却 404/500」，先怀疑进程旧，再怀疑代码少**（CONTRIBUTING §1.3）。这条在本文件上被咬过两次：一次是 `organs/soul/model.py` 早已允许 `appearance`、老进程仍按旧规则拒载；一次是 `ROUTES` 里早有 `/soul/revisions`、`/soul/pin`、`/soul/redline`、`/soul/rollback`，老进程全 404——照那轮结果动手会误删四个好按钮。**验接口一律打当前进程，读源码不算验。**
- 下发带 `echo`，协议端的应答帧（`{status, retcode, data, echo}`）只用来打 `ack` 日志。
- 核不可达时：记一条「核不可达」，**本轮不回落**，进程不崩。
- 实测间隔（①→④）：15ms / 3ms / 4ms，验收线是 < 2s。

## 3 壳侧的义务（`shell/`）

- 面板页与 `/soul*` 由壳代理到核；壳**不解析、不缓存、不改写**核的响应。
- 壳里不许出现她的判断（心情、该不该回、说多少）。壳只搬。
- 面板的「魂」页是嵌核的面板页，不是重写一份。

## 3.5 与魂线的接口变更（2026-11-03）

魂线把 `manner` 从 `{tone, matrix}` 收成了 `{stance}`（`organs/soul/model.py` 的 `ALLOWED_KEYS`），数据已迁移。对壳的影响与归属：

| 项 | 现状 | 谁的活 |
| --- | --- | --- |
| `GET /soul` 的 `manner` | `{"stance": "…"}`，**不再有 `tone` / `matrix`** | 魂线（已改） |
| `POST /soul/edit` | `path` 必须是 `stance`；送 `tone` 会被拒：`{"ok":false,"message":"path 无法定位：tone"}` | 魂线（已改） |
| `plugin/pages/soul.html` | 已同步：读 `manner.stance`、提交 `path:"stance"`，对象档下拉随矩阵一起删掉，红线条目显示 `enforced_by` | 魂线（已改） |
| 壳 `shell/` | 只做代理、不解析响应 → **零改动** | 已完成 |

壳不需要跟着改。`edit` 只剩一个可选字段：

- `structural`（`true` 才许新建字段/整块替换/搬走字段，缺省一律拒），账记 `kind="migrate"`。

`edit` **不再接受 `probe`**：body 里带了这个键就是 400（`PROBE_VIA_EDIT_FORBIDDEN`，不看值是
`true` 还是 `"true"`）。探针只有 `POST /soul/acceptance` 一个入口——一次写 `value`、再写回
`restore_value`，两行都记 `kind="probe"`，返回 `history_lines`（两条）。这样探针再也借不到
「和真改值一样宽」的入口，也不能不还原、不能碰红线。

形状变化（换结构、加/删字段、整块替换）一律走 `POST /soul/migrate`，不要拼 `edit`：它要带
`expect_old_digest`（当前块文件的 sha256，带不带 `sha256:` 前缀都认），digest 不符即拒——防误改别人刚改的东西。
坏卡状态下 HTTP 层可能起不来（`load()` 会拒载），那种时候用 CLI：
`python3 -m organs.soul.cli migrate --block manner --value '<json>' --actor <人> --reason <话> --expect-digest <sha256>`。

## 4 门禁（上公网前必须补）

- 现在**无鉴权**，只绑 127.0.0.1。谁能连 6200 就能收发消息，谁能连 8787 就能改魂。
- 上公网前至少要：`/inbound` 与 `/soul/edit` 的来源白名单或 token、WS 的 `access_token`、TLS。
- 这一条现在**不做**，但它是发布前置条件，不是待办事项。

## 5 已知偏差（写在明处，别当没发生）

| 项 | 现状 | 为什么 |
| --- | --- | --- |
| `GET /inbound` | 404 | 没为了凑 405 去动 `api.py` |
| `/inbound` 未挂到宿主挂载点 | 只在 `api.devserver` 上 | 宿主的 `routes()` 是面板契约，入站不经过面板 |
| 真协议端连接 | **未验证** | 取证全部来自 `channels/onebot/tools/fake-client.ts`（loopback）；真连接需人在协议端加第二条 client |
| 心跳 / 半开连接 | 未做 | 只靠 TCP 感知，长静默死连接识别缺失 |
| 单连接 | 已按单连接验证 | 两条客户端同时在线的相互影响无实证 |
| 消息大小限制 | 未设 `maxPayload` | 超大帧、分片、二进制帧未测 |

## 6 嘴的干跑端点（第三批，只读）

四条全是 GET：干跑只算一遍给人看，不发出去；**账只记两本**（弃言 / 长度两份 trace），
**拒绝既不记账也不投心**——拒绝的账与心的 `refusals.json` 都等真实对话（`POST /chat`）时再写，
所以没有写方法（非 GET 一律 405）。判出拒绝时返回里 `verdict` 说判了什么、`delivered` 恒为没投
（另给 `would_deliver`，只算不写）。心的读数这一轮只能从查询参数来，不给就如实写 `heart:未接入`；
闸这一轮没有重写器，命中就是 `dropped`。

| 端点 | 参数 | 返回 | 只读理由 |
| --- | --- | --- | --- |
| `GET /mouth/split` | `text`（必填，原文）、`seed`（可选，间隔抖动的种子） | `parts[]`（`text`/`gap_before`/`send_at`/`degraded`/`reason`）+ `summary` + `chars` + `visible_chars` | 只把切分与节奏算一遍；阈值全在 `organs/mouth/data/policy.json`，不写盘 |
| `GET /mouth/address` | `who`（人名录 id）、`familiarity`（可选，覆盖这一次的读数）、`rename`（可选，对方这一轮要求的叫法） | `who`/`display`/`tier`/`relation`/`address`/`frequency`/`banned_self`/`note` + `renamed` + `source`（`人名录`/`会话覆盖`/`本轮改名`/`回退`）+ `memory_draft`（当场换时给出：投给脑的那条亲历记忆） | 只读人名录取一次称呼；查不到人就按生人档回退，不写回人名录。改名**当场换**，但端点**不落盘**；要落到记忆里，靠出口把 `memory_draft` 交给脑 |
| `GET /mouth/tone` | `who`、`familiarity`、`mood`、`energy`、`engage`、`offset`、`rename`（可选） | `addressee{…}` + `tone{tier,label,line,stance,self_name,heart_source,softness,ban_joke}`；给了 `rename` 时顶层另带 `renamed` 与 `memory_draft` | 心的读数只从查询参数来；没给就是 `heart:未接入`，不假装有读数；改名同样只算不写 |
| `GET /mouth/gate` | `text`（没给 `refuse` 时必填，原文）、`asked`（可选，对方那句问话；给了就先判软转向）、`answer`（可选，她说的话，缺省用 `text`）、`inject`（可选，URL 编码的 JSON 数组 `[{"kind":"魂","text":"…"}]`，交给照抄审计）、`refuse`（可选，`topic`/`mood`/`nobody`）、`tier`（可选，1..4，嘴的目标档位，与 `/mouth/tone` 的 `tone.tier` 同一套）、`hard_cap`（可选，整数，核给的上限；给了它就必须连 `tier` 一起给） | `hit{clause_id,kind,reason,matched}` 或 `null` + `decision` + `reason` + `disabled_ids` + `drops{today,last_clause_id,line,exists,errors,trace}` + `audit{status,hit,lcs_len,threshold,reason}`（恒在；`status` 为 `audit-unavailable` 时原样透出，**不许当成通过**）+ `verdict{kind,shape,full_name,reason,text,event,gate_decision,hit,soft}`（给了 `refuse` 或 `asked` 时才有；`kind` 是 `hard`/`soft`，没判成拒绝时是 `null`；`soft` 是软转向那四样 `{is_soft,status,opener,redirect,question,reason}`，`status` 不是 `ok` 就是没真的比过）+ `delivered{delivered,would_deliver,reason,why}`（**恒在**：`delivered` 恒 `false`、`reason` 写死「干跑只判不投；投递发生在真实对话里」，`would_deliver` 按真实投递的判法算——认得出话题名、今天还够额度——但只算不写）+ `gate_readonly{decision,hits,reason}`（软转向时才有：闸照跑但只报不改）+ `action{checked,where,env_word,reason}`（这段字里有动作括注的样子时才有；`checked:false` 是这一关没跑，不是查过没问题）+ `style{decision,target_lo,target_hi,chars,rewrites,over_hard_cap,reason}`（给了 `tier` 才有；这一轮没有重写器，`rewrites` 恒 0） | 条款在 `organs/mouth/data/clauses.json`、拒绝说法在 `refusal.json`、动作括注判据在 `action_note.json`、目标区间在 `style_target.json`；没有重写器，命中即 `dropped`，不代写也不放行；超目标只如实报「改不了、放行并记账」，超硬上限也不截断（截断是核的事）；**干跑只记弃言与长度两本账**（都吃 `MOUTH_TRACE_DIR`），拒绝的账与心的 `refusals.json` 一个字节都不写（等 `POST /chat`） |

`GET /mouth/gate` 的判的顺序钉死：**拒绝（含软转向）→ 闸（条款 → 动作括注 → 复述审计）→ 目标长度**
——先决定说不说、再决定这么说行不行、最后才管长短。六个可选参数（`asked` / `answer` / `inject` /
`refuse` / `tier` / `hard_cap`）都不给时，返回与从前逐字一致（只多一节 `audit`）；`inject` 读不出
数组是 400，段里 `kind` 不认识也是 400（带的正是 `organs/mouth/audit.py` 的报错原文，不是 500）。

软转向（`asked` 判出来是 `soft`）走的是另一条收尾：**她自己的那句话一个字都不改**——闸照跑，
但只报不改（判词进 `gate_readonly`），`verdict.gate_decision` 用数据里那个「没动过」的词；
**判出来只判不投**：`verdict` 里照样有事件（`{ts,shape,full_name,why}`），但 `refuse.jsonl` 一行
不添、心的 `refusals.json` 一个字节不动，`delivered.delivered` 恒为 `false`。
`would_deliver` 按真实投递的判法算：话题名只从心那份共用词表（`organs/brain/topics.json`）里认，
认不出就用 `why` 说清「真投也投不进去」——心不收词表以外的话题，这一层也不替它造分类。
`asked` 给了却是空白时 `verdict.soft.status` 是 `soft-unavailable`：**这是「没比过」，不是「不是拒绝」**。
目标区间这一轮收窄过一次（档 1–4 为 8–24 / 12–36 / 16–56 / 20–72），旧值在
`style_target.json` 的 `retired` 节里标作废、没删；`style.jsonl` 每行因此多一列 `target`（`lo-hi`），
好分辨这一句是拿哪一套区间判的。

### 6.1 投递只有一处：`POST /chat`

真实对话（`api/chat.py`）在 `conversation.answer()` 返回之后拿**对方那句**与**她那句**判一次拒绝：
软转向（`refusal.soft_scan`）或那三句标准拒绝之一（`refusal.enforce`）。判出来才投一次——
`heart.refuse.ingest` 写心的 `refusals.json`，`refusal.record` 写我们自己的 `refuse.jsonl`
（一行的 `shape` 记 `hard` / `soft`，`sent` 记这次投没投进去）。**这是全仓唯一的投递点**：
`/mouth/gate` 那条干跑路径只判不投，所以同一个拒绝要么落在 `/chat`（真说过话），要么谁都不写。

成功体里多一节 `refusal`（不破坏既有键）：

```json
{"refusal": {"kind": "soft|hard", "shape": "topic|soft", "verdict_reason": "…",
             "delivered": {"delivered": true, "topic": "猫", "until": "…",
                           "remaining_min": 720.0, "reason": "…"}}}
```

**这一节现在恒在，且冷却那一格永远答得出来**（本轮改的口径）：以前「什么也没判出来」时整节不
出现，于是人分不清「判过了、没在冷却」与「压根没看」；现在多一格 `cooling`（true / false）与一句
`note`。`kind` / `shape` / `verdict_reason` / `delivered` 仍是「判出拒绝才有」——那四样是拒绝
的读数，不是冷却的读数，没判出来就不加空壳：正面答了的那句、被闸弃掉（`reply` 是空串）的轮都只剩
`{cooling:false, topic, note}`。
话题名沿用同一认法（`refusal.topic_in` + 心的词表）：`asked` 里找不到话题名就
`delivered.delivered:false` 并说明为什么——拒绝事件照样在 `refuse.jsonl` 记一行（`sent:false`），
她拒了这件事与这一笔有没有落到心上是两件事。

**冷却里她会主动拒（本轮加的）：`/chat` 在装配之前先问一次心。** `conversation.answer()` 之前，
`api/chat.py` 拿对方这句认话题（`refusal.topic_in` + 心的词表，与投递那一层同一个认法），认得出就
`heart.refuse.check([topic])` 问一次心（**只读，一个字节都不写**）：还在冷却里就整轮**短路**——
不调模型（省一次真调用：心已经说不接这个话题，再问一遍模型只会把同一句话换个说法）、不投事件
（心那边的账上已经记着这一次拒绝，再投一次就是同一件事记两遍，所以 `refuse.jsonl` 与
`refusals.json` 都不会多一行），直接产出 `organs/mouth/data/refusal.json` 里 `refused-topic` 那一句
（说法归拒绝那边，嘴这边不造新文案）。几泡、每泡停多久仍走 `split.split` + `pace.fill`（与平常说话
同一份判据）；这一句照样过八股闸，但 `inject=None`：这一轮没有装配、没有注入原文，复述审计如实记
`audit-unavailable`（**不是通过**）；重写那一步**一次请求都不发**，命中就保留原话
（`rewrite.status` 是 `not-tried` / `failed`）。认不出话题、或冷却判据读不动，就不判、照常说话
（「没读到」不是「在冷却里」）。

```json
{"refusal": {"cooling": false, "topic": "医院", "note": "这一轮不在冷却里：…"}}
{"refusal": {"cooling": true, "topic": "医院", "remaining_min": 720.0, "reason": "refused-topic",
             "line": "她在拒绝「医院」，理由 refused-topic，还剩 12.0 小时",
             "say": "这个话题先不聊。", "event": null, "note": "冷却中：…"}}
```

冷却那一轮的 `event` 恒为 `null`——**这一轮不投事件**，写成「投了、只是没落盘」就是编。面板那一句
人话的模板、两个单位（小时 / 分钟）与「多少分钟以上才按小时说」全在
`organs/mouth/data/cooldown.json` 的 `line` 节（`template` / `minutes` / `hours` / `hours_from_min`）；
页面 `pages/chat.html` 的「命中门」因此多一行 `refusal-line`，原样转述 `refusal.line`——
话题、理由、还剩多久，页面一个词都不自己拼。

**出口认 audience：私密记忆在群里守边界（这一轮加的）。** `/chat` 的 body 多一个可选 `scope`，
只认 `group` / `private`（缺省 `private`——今天实盘都是私聊）；**非法值当场 400 并点名**，message
里带着读到的那个值，也列出认得的两个。场景只决定「私密记忆这一轮能不能被引用」，别的行为一个字都
不改。判据全在 `organs/mouth/data/refusal.json` 的 `audience` 节：记忆行上哪一栏（`field`）、哪个
值算私密（`private_value`）、两个场景值与缺省（`scopes`）、边界话（`say`）、模糊化备用（`blur`）、
事件形状（`event_shape`，指向 `shapes` 里那一节的完整名 `refused-topic`）、账本记号（`mark`）与
四个阈值（`criteria`：`overlap_min` / `gram_n` / `gram_cover` / `excerpt_chars`）。

装配之后、过闸之前，`recalled[*]` 里标成私密的那些条目在 `scope=group` 时**从出口剔除**：不再进
`recalled`（页面不再引用它，复述审计的「记忆原文」那一档也不再拿它当注入原文），剔了谁记在回包的
`audience_held` 里：

```json
{"audience_held": [{"id": "记忆-7", "text_min": "我妈住院那事", "source": "longterm"}]}
```

读数只给 id 与一小截摘要（截几个字也在数据里），**不贴正文**。**被直接问到就必须出声**：用户这句
与该条 `text` 去掉空白后的最长连续重合 ≥ `overlap_min` 字，或该条 `text` 的 `gram_n` 元组覆盖率
≥ `gram_cover`（两条判据都能离线复算）——这时她这一轮说的不是模型那句，而是数据里那句边界话
`say`，并走拒绝那条路（事件形状取 `event_shape`、`refuse.jsonl` 记一行、心那边记一次冷却，事件
的 `why` 与账本那一行的 `why` 里带 `mark` 记号）。**没被问到就不出声**：不为没发生的事拒绝，但
照样剔除（`audience_held` 里看得见）。`scope=private` 一律照常：既不剔除也不出声。

顺序钉死：**冷却 → audience 边界 → 闸 → 长度**。冷却排在前面，是因为它早于装配（那一轮没有
`recalled`，本就没有边界话可说），也因为「这个话题已经拒过」比「这条记忆是私密」更靠前——让边界话
抢在冷却之前，就是把一次已经记过的拒绝再说一遍、再投一遍。边界话也要过八股闸（她的话都过闸）。
判据读不动时：场景判不出就不判场景，私密判不出就**一条记忆都不引用**（没有判据，就没有哪一条敢说
它能往外讲），只记一行 WARNING。

**谁给记忆打这个标记，本轮不在边界内**：`audience` 那一栏的生产者是脑与家——脑的 `ingest` 早就收
`audience`（`organs/brain/model.py` 的 `AUDIENCE_PRIVATE`），`recall` 在 `scene=group` 时也已经会
给私密条目打 `retellable=False`；缺的一环是装配：`organs/mind/conversation.py` 的 `_recall` 只搬
固定那几栏，`/chat` 的 `scope` 也没有当成 `scene` 传下去。下一轮把这两处接上，这一层就自动认了。

**借阅读数也随成功体出去（这一轮加的）**：多一节 `borrow`（不破坏既有键）：

```json
{"borrow": {"status": "ok|failed",
            "files": [{"source": "clauses.json", "borrow_id": "借-1", "learned": 7}],
            "note": "这一轮借用了 2 份词条，读数已投给家：…"}}
```

名单是 `api/chat.py` 的 `_books()` **这一层真读过**的那几份（`clauses.json` / `policy.json`；
称呼表是装配那一步读的，这一层不替它记账），条数是这一轮自己的账：条款按「过闸真比过的那些」算
（有泡可过闸就是启用条款全表，一泡都没有就是 0），`policy` 只数这一层真读了的那几段
（`pace.round_digits`）。**`learned` 取不到就写 0，0 是一次真实读数**（这一轮一条都没用上），
不是缺失；缺失是「这一行根本不写」。家那侧：`borrow_ledger()` 的 `borrows` 里找不到该
`source` 就先 `register_borrow`（`version` 取 sha256 前 12 位、`total` 是整份几条、
`sha256` 是全量；一 source 一条，第二轮不再登记），再逐份 `record_readout` 写读数
（`organs/home/residents.py`：按 `borrow_id` 合并、整份原子换掉、追一行审计）。

**投递失败不许影响这一轮说话**：整段包在 `try` 里，失败只在 `borrow` 节里如实写
`status:"failed"` 与原因（家里只读、盘满、数据缺件都属于这一类），她的 `reply` / `chunks`
一个字节都不动——读数是账，账写不进去不许连话一起吞掉。读数怎么算在嘴那侧
（`organs/mouth/borrow.py`，纯函数：不读盘、不取时钟），措辞在
`organs/mouth/data/borrow.json`。

**闸在 `/chat` 生效（这一轮加的）**：`conversation.answer()` 返回之后、组装响应之前，把 `chunks`
里的**每一泡单独过一遍闸**（条款 → 动作括注 → 内容重复 → 复述审计）。命中就改一次、改不了就弃那一泡：

- `rewritten`：用闸给的 `Verdict.text` 换掉那一泡（`gap_before` 沿用原来的）；
- `dropped`：把那一泡从 `chunks` 里删掉，剩下的按原来的间隔**重排 `send_at`**（不留洞）；
- 全被弃掉：`chunks=[]`、`reply=""`，每一泡弃言各记一行 `organs/mouth/drops.py` 的账；
- 一泡都没被改时 `chunks` 与不接闸时**逐字相同**；判据读不动（缺件坏件）时整轮弃掉，**不 fail-open**。

注入复述审计的原文按 type 分段，六段各按本来的来源取（取不到就**如实少一段**，不拿别的东西凑）：
魂取 `store.raw` 的 identity / manner 卡正文；锚句取回包里**真的注进去的半句**
（`anchors.items[*].injected`）；记忆原文取 `recalled[*].text`；心档位词取 `heart` 里那几档词；
规矩句取装配说明的 `reply.instruction` 与格式要求；对方原话就是入站那句 `text`。
一段都取不到时交给审计的是 `None`，审计如实报 `audit-unavailable`——**不是通过**。
「她最近几条回话」只读 `mind/recent_turns.jsonl` 里她自己那一侧（`reply`），条数取
`data/repeat.json` 的 `rounds`（不写死），**在 `answer()` 之前读**（`answer()` 最后一步会写入本轮，
之后再读她这句就会跟自己比一次）；读不动就是 `None`，闸会把它记成 `repeat_status=not-checked`。

`gate` 节因此除了既有的 `decision` / `hit` / `drops`，还多五样（顶层键一个都不新增）：

| 字段 | 是什么 |
| --- | --- |
| `parts[]` | 逐泡判词：`{i, decision, hits[{clause_id,kind,reason,matched}], reason, before, after, audit{status,skipped,hit,lcs_len,threshold}, action_status, repeat_status, quota_status, fatal{category,tag,clause_id,kind,matched,reason}, fatal_status, kept_after_rewrite, rewrite{status,why,from,to}}`；`audit.skipped=true` 表示「对方要她重复／引用，这一关只记账没判废」，此时 `hit` 照旧带着；`fatal` 是踩上的那一类红线（没踩就是 `null`，**不是「不是红线」**，判没判看 `fatal_status`）；`kept_after_rewrite=true` 表示「改过一次还是命中、按进门那句原话发了」 |
| `in_parts` / `out_parts` | 进闸的泡数 / 出闸（真发出去）的泡数 |
| `blocked[]` | 这次拦下过她的 `clause_id`（去重、按出现顺序），一条都没拦就是空表 |
| `quota` | 这一轮交给闸的动作配额写法：`forbid`（装配标了本轮不许写动作）/ `allow`（标了放行）/ `null`（装配没标——闸记 `quota_status=not-checked`，**没标不是 allow**） |
| `note` | 这一节怎么读的说明 |

**动作配额从装配传进闸**：`answer()` 回包里的 `action.quota.blocked`（装配按
`organs/mind/data/assembly.json` 的 `action.quota` 数最近几轮算出来的那个布尔）为真就传 `forbid`、
为假传 `allow`，装配里没有这一项才传 `None`——**不许猜成 allow**：没标是这一关没跑，放行是真的判过。
配额命中是闸里唯一一条不丢整句的路：它把动作行剥掉、正文接着过其余判据（`quota.strip`），
所以那一泡是 `rewritten` 而不是 `dropped`，账本里那一行的 `reason` 带 `action-over-quota`。

**命中先改一次，二次命中才丢（本轮接上的老账）**：以前命中即弃，一句口癖就让她整泡消失。
现在 `gate.enforce` 拿到一个真的重写器——`api/chat.py` 把「她的原话 + 命中的那一条 + 一句人话
原因」（`organs/mouth/rewrite.py` 的 `prompt()`）交给模型改一次，走的是 `answer()` 用的**同一条
出口** `organs/mind/llm.complete`（同一份 `config` / key / transport，不另开 HTTP 客户端），
时限取 `organs/mouth/data/rewrite.json` 的 `budget_ms`（线程等出来的硬时限，transport 那一侧的
超时也一起收窄，`attempts` 定成一次不重试）。拿回来先过 `rewrite.accept()`：空 / 逐字相同 /
长度出区间 / 原文里的数字、英文串、引号里的原话少了，都算「没改成」；过了才用新稿再跑一遍闸，
**二次仍命中才 `dropped`**。

**改不动就留原话，绝不静默变哑**：超时、回空、不合规、出口抛错，一律**保留原文发出**
（`decision: pass`、`before == after`、`hits` 里留着那条命中），弃言账本记一行 `reason` 带
`rewrite-failed` 的账，`parts[*].rewrite` 记 `{status:"failed", why, from, to}`——`from` / `to`
各只放一小截（截多长也取自数据）。没命中的那泡是 `status:"not-tried"`（`why` 照样有话说），
改成了是 `"ok"`。**配额命中是例外**：它仍优先走剥行兜底（把动作行拿掉比留着原话强）。

**对方要她再说一遍时，内容去重只记账、不判废**：入站那句（`asked`）原样传给闸，`data/repeat.json`
的 `request_markers` 认「再说一遍／再说一次／再来一遍／重说一遍／你刚才说／刚才那句／上一句」这类
请求。**先真的撞上重复、再看 requested**：两步都成立才放过——`decision` 保持 `pass`、`text` 一个字
不改、`chunks` 里还是她那句原话，账本记一行 `clause_id` 取自数据 `hit.skip_clause_id`、`reason` 带
`skipped-by-request` 的账，`parts[*].repeat_status` 记 `skipped-by-request`（既不是 `ok` 也不是
`not-checked`）。`asked` 为空一律不豁免；只说了请求、本来就没重复的，什么都不记。

**同一句请求也豁免复述审计**：他要她重复／引用时，把她刚才那段设定原样再说一遍是她照办，审计
这一关同样只记账、不判废——否则「她照做的那一下」还是被吞一次（现场那句
「再说一遍你刚才说的是什么」就是这么哑的）。豁免开关由闸算**一次**：`repeat.requested(asked, 表)`
（标记词表只有 `data/repeat.json` 的 `request_markers` 那一份，`data/audit.json` 里没有、也不许
另抄一张），同一个布尔同时交给内容去重与复述审计，两关因此不会判出两个结论。跳过时那一泡照原样
发出（`decision: pass`、`before == after`），**但标记不丢**：`parts[*].audit` 里 `hit` 与
`status: ok` 照旧（面板上看得见她引了设定），另加 `skipped: true`；账本记一行 `clause_id` 取自
`data/audit.json` 的 `skip.clause_id`、`reason` 带 `audit-skipped-by-request` 的账。
**一句话同时撞上内容重复与复述审计时两关各记一行**（各带自己的记号），不合并成一条。为什么还要
标记与记账：我们不想让她养成引用设定的习惯，只是不在这句上拦——不拦而无声，账本上就与「这一关
没跑」长得一样。`asked` 里没有请求标记时豁免一点都不外溢：同一句照旧判废。

**改了一次还是命中 → 留原话，不再丢空（本轮加的）**：以前二次命中就 `dropped`——她明明改了、
也没改成红线，整泡却消失了。规矩是「**在最该回答的那一句上沉默，比说话带点毛病糟得多**」，所以
二次命中（非红线）改成 `decision: pass`、`text` 是**进门那句原话**（不是那个没改成的新稿），
`hits` 里两条命中都留着，`parts[*].kept_after_rewrite=true`，弃言账本记一行 `reason` 带
`<通道>-after-rewrite` 的账（通道＝条款 / 动作括注 / 内容重复 / 复述审计，记号与说法全在
`data/rewrite.json` 的 `after_rewrite` 节）——留原话不是「没问题」，账本上必须看得出「这句改过、
还是不行、按原话发了」。**配额剥行那条例外不变**：配额命中仍优先剥行（把动作行拿掉比留原话强），
剥完只剩空白才丢。

**红线是唯一不许留原话的一类（本轮加的）**：隐私（手机号、身份证、住址、账号密码）、辱骂、放弃式
（「反正我不疼」「我无所谓了」「死了算了」「不用管我」）这三类判的不是「这句有毛病」，是「这句
不能出去」。判据与全部说法在 `organs/mouth/data/fatal.json`，闸只把命中摊成 `FatalHit`：
`clause_id` / `kind` 取自数据，`category` 是哪一类，`reason` 里带着数据里那个记号
（`fatal-<tag>`）。踩上就 `dropped`、`chunks` 里一泡都没有、`parts[*].fatal` 里看得见是哪一类；
**红线也不交给重写器**（改一次不能保证它不出去），改完回来的新稿若自己踩上红线，一样丢掉。
`fatal_status` 三态**不许折叠**：`ok`（判过、不是红线）/ `not-checked`（这一轮没给红线表，判不了）
/ `fatal`（踩上了，看 `fatal` 那一节是哪一类）。**「给不给表」不是「吞不吞这句」的开关**：没给表也
照样留原话（`kept_after_rewrite=true`），只是 `fatal_status` 记 `not-checked`，账行里补一句取自
`data/rewrite.json` 的 `after_rewrite.unchecked`——判不了不等于可以把这句话吞掉。判据表在真链路上
三处都传：逐泡过闸、拒答那三句（`organs/mouth/refusal.py` 只透传、自己不读盘）、`/mouth/gate` 干跑。

五样「判没判」的读数**不许折叠**：`audit.status` 不是 `ok` 就是没真的比过，
`action_status` / `repeat_status` / `quota_status` / `fatal_status` 的 `not-checked` 是**这一关没跑**，
不是「查过没问题」（红线那一关尤其：`not-checked` 时 `fatal` 也是 `null`，那不等于「不是红线」；
`fatal` 非 `null` 时 `fatal_status` 一定是 `fatal`，三态在返回里分得开）。
`pages/chat.html` 的「命中门」那块因此多一行 `gate-line`：「这次过闸：N 泡进 / M 泡出 / 拦了什么」
（`blocked` 里的 `clause_id` 用顿号连起来，一条都没有就写「没拦」；泡数缺了就写「泡数没给」）。

两处跨端点的字段：

- **`disabled_ids`**（`/mouth/gate`）：清单里 `enabled:false` 因而被跳过的条款 id。跳过**不静默**——返回里看得见，免得「没命中」被当成「闸变松了」。全部条款都被停用时抛 `ClauseBroken`（HTTP 500 明说没有生效条款），不当没命中放行。
- **`drops`**（`/mouth/gate`，**pass 的那次也带**）：弃言账本的读数 `{today, last_clause_id, line, exists, errors, trace}`。`line` 就是那句人话「今天因闸弃言 N 次，最近一次命中 <clause-id>」。三件事分开报：账本不存在 = `today:0 + exists:false`（等于今天还没记过，不是读不到）；有坏行 = 跳过并计入 `errors`；写不进去 = 直接 500（记不上账就要响，不报 0 次）。账本属于「临时落脚」，家那一轮搬去家·痕迹。
- **`renamed` / `source` / `memory_draft`**（`/mouth/address`、`/mouth/tone`）：给了 `rename` 就**当场换**（`source=本轮改名`、`renamed=true`），档位不动；落不到盘，也改不了人名录——`memory_draft` 就是「他要求过叫他 X」这条**亲历记忆草稿**，形状恰好是脑 `ingest()` 的入参，由出口投递。违禁自称过不了这道门（`source=回退`、`banned_self` 记下被挡的词）。

**红线被拦不是「拒绝」，是「不许发」（本轮加的）**：拒绝那三句自己也会踩红线（改一次也救不回来，
红线是唯一不许留原话的一类）。这时 `RefusalVerdict` 把闸的读数原样带出来，`/mouth/gate` 的
`verdict` 节也跟着多两格：`fatal_status` 三态**不许折叠**——`ok`（给了红线表、判过、不是红线）/
`not-checked`（这一轮没给表，判不了，**不是「不是红线」**）/ `fatal`（踩上了）；`refused` 在红线
命中时是 `false`，读成「这句不是拒绝，是不许发」——她确实拒了（`shape` / `event` / 账本一样不少），
可那句话一个字都出不去，`text` 是空串。**三个读数各占一格、谁也不顶替谁**：`refused` 说「拒了、
这句发出去没有」，`fatal_status` 说「红线那一关判成什么」，`gate_decision` 说「闸最后怎么判的」
（红线命中时必然是 `dropped`）。那句人话（「这句不是拒绝，是不许发：…」）取自
`organs/mouth/data/fatal.json` 的 `not_refusal_note`，代码里一个词都不写死；软转向不过闸，所以它
的 `fatal_status` 恒为 `not-checked`（那一关真没跑，本层不假装它判过）。

**这一轮叫他什么：`addressed`（本轮加的）**：`POST /chat` 的成功体多一节
`addressed: {who, address, source}`（`who` 是人名录里的 id，`address` 是这一轮真用的称呼，
`source` 取 `address_table.json` 的 `source`：人名录 / 会话覆盖 / 本轮改名 / 回退）。判据全在
`data/address_table.json` 的 `rename_markers`：`named` 每条带一个捕获组（抓到的就是他这一轮要的
叫法：叫我老李 / 以后喊我Z / 我改名叫…），`denied` 不带捕获组（只否掉旧叫法：别叫我老王——这一句
**不给**新叫法，抽不到就回 `None`，不许编、也不许拿人名录里的名字顶上）。认人只认 id：昵称不是
身份。抽到就叫法当场换（`source=本轮改名`）、把 `address.rename_memory` 那条**亲历记忆**投给脑
一次（`brain.ingest(**draft)`），并记进**进程内**那本 `SessionAddressBook`（模块级单例，**只活这个
进程、不落盘**）；下一轮他再开口，`address.resolve(..., session=book)` 就用会话覆盖
（`source=会话覆盖`），换一个进程就是新的一本、回到人名录档位。**违禁自称照样过不去**
（`self_guard`）：退到中性叫法，那一轮既不投记忆、也不记会话账——改名不是绕过自称红线的后门。
接线点在 `conversation.answer()` 返回之后、过闸之前：称呼只读他那句话与脑的人名录（`answer()`
刚把他的原话落进脑，人名录里才查得到这个人），而闸可能把整轮弃掉——他改的叫法不该跟着她那句被弃
的话一起丢。提示词里那一段称呼仍由 `organs/mind/conversation.py` 按人名录算（不在本次边界内），
会话覆盖目前落在出口读数与脑那条记忆上。
### 6.1.1 两个 `mode`：人问她（passive）/ 她自己开口（proactive）

`POST /chat` 的 body 多一个**可选** `mode`：`passive`（缺省，人发消息触发的）与 `proactive`
（日子线给她一个由头，她自己开口）。**非法值不 400**，回落 `passive`——与 `scene` 同一个口径：
这一格只决定她这一轮的提示词与心判决，认不出就按常态走，不拿一个错字段把人拦在门外。

| 字段 | passive（缺省） | proactive |
| --- | --- | --- |
| `mode` | `"passive"` 或不给 | `"proactive"` |
| `text` | **必填**，对方那句话（缺了 / 全空白 → 400 并点名） | 不用给（给了也不用） |
| `seed` | 不用给 | **必填**，这一轮她开口的由头（缺了或全空白 → 400 并点名 `seed`） |
| `reason` | 不用给 | 可选，理由码（原样进提示词与回包的 `seed.reason`） |
| `who` / `scene` / `scope` | 照旧 | 照旧（同一个认法与同一个缺省） |
| `segment` | 刚刚这一小段（两种形状都收） | **给了也忽略**：这一轮没有别人说话，段连形状都不判 |

proactive 与常态的差别**只有输入**：

```json
{"mode": "proactive", "seed": "今天想问问他吃饭了没有", "reason": "evening",
 "who": "3425635473", "scene": "private"}
```

- 提示词换成 `organs/mind/data/assembly.json` 的 `prompt.proactive`（占位符 `{sections}` /
  `{reply_budget}` / `{reply_instruction}` / `{seed}` / `{reason}`），`user` 那一问是**空串**
  ——没有对方的话；没给 `reason` 时用 `prompt.proactive_reason_missing` 那句顶上。
- 「这一小段」写「这一段没有别的发言」；最近几轮照常只当背景（不许当开场白——见
  `docs/modules/mind.md`）。
- 心判决的意图是 `proactive`：**夜里主动判「不说」**（夜里被动只判「少说」），出站闸因此
  `send=false`；这一条路径与被动完全共用同一个 `_outbound_round`。
- 落脑**只有她那句**（`side=她说`）：没有对方那句可落，也不为凑形状写一条假的人话。

回包因此多两格：

```json
{"mode": "proactive",
 "seed": {"text": "今天想问问他吃饭了没有", "reason": "evening"}}
```

被动那一轮两格也在，值是 `"passive"` 与 `{"text": "", "reason": ""}`（形状不随状态变）。
**顶层的 `reason` / `level` 一直是出站闸的理由码**（`night` / `today-volume` …），与由头的理由码
不是一回事，两处不共用一个字段。

**空产出与 `send=false` 是两件事，别混：**

- `send=false`：门说这一轮**不发**（夜里主动、今天话太多、双低…）。回包 `chunks=[]`、
  `reply=""`，`send` / `reason` / `level` / `limits` 照门说的写，心那边照写一行出站账。
  `gate.outbound` **不出现**——沉默是门的结论，不是没话可说。
- 空产出：门说**发**，装配却一个字都没拿出来（模型那句被八股闸弃掉、或改完仍是空）。
  `chunks=[]`、`reply=""`，`gate.outbound` 写 `{"status": "core_empty"}`。**不重试**：
  这一轮就到这里，不为了凑出一句话再问一遍模型。

```json
{"send": true,  "reason": null, "reply": "", "chunks": [], "gate": {"outbound": {"status": "core_empty"}}}
{"send": false, "reason": "night", "reply": "", "chunks": []}
```

## 7 家（第四批：十间房 + 存取接口 + 审计）

十条路由：九条 GET、一条 POST。房名表、两道门、审计、索引卡都在 `organs/home/`，
这一层只把它们翻成 HTTP：**不重判一次写权、不给白名单兜底、不补审计行**。

| 方法 | 路径 | 参数 | 返回 | 一句话 |
| --- | --- | --- | --- | --- |
| GET | `/home/rooms` | — | `rooms[]{name,slug,purpose,writable_by_structure,writable_now,count,cap,headroom}` + `writable_now` + `writable_now_source` | 十间房的表：**结构可写**与**她此刻可写**分两栏答（前者改代码、后者改配置），另报件数与余量。读侧不看白名单，这一栏只用来给人看 |
| GET | `/home/access` | — | `writable` + `source` / `source_kind` / `file` / `env` + `default` + `readonly_rooms` | 「她写哪几间现在怎么配置」的对外答案：哪几间、哪一级解析出来的（构造参数 / `HOME_ACCESS_FILE` / 包内）、要改哪份文件。每次请求重读盘，不缓存 |
| GET | `/home/list` | `room`（必填）、`offset`、`limit` | `items[]`（折叠后视角，含 `mentions`）+ `total` + `truncated` + `bad_lines` | 读一间房的索引卡；按 `READ_MAX_LINES` / `READ_MAX_CHARS` 截断，截了就如实回 `truncated` 与 `total` |
| GET | `/home/read` | `room`、`name`（卡名或 `card_id`） | `card` + `body` / `external` / `verified` + `truncated` | 读一件；卡与盘对不上回 409 `CARD_DISK_MISMATCH`。纯说明卡如实回 `body=null, external=true`，不编内容 |
| GET | `/home/audit` | `limit`（默认 20、上限 200） | `entries[]{seq,at,role,by,room,name,chars,bytes,ok,code}` | 审计尾。读账不算写账，不碰高水位 |
| GET | `/home/audit/verify` | — | `lines` / `seq` / `mark` / `code` / `repaired:false` | 行数／序号 vs 高水位；变少回 409 `AUDIT_SHRUNK`，正文写明「只报不修」——不补行、不重写高水位 |
| GET | `/home/drawer` | — | `status`（`已接入` / `未接入`）+ `body` + `template` | 抽屉资料卡，只读；缺件回 200 + `status="未接入"` + 模板路径，**不报 0 字、不编内容** |
| GET | `/home/archive/borrow` | — | `room`（名片盒）+ `borrows[]{borrow_id,source,version,total,learned,readout_status}` + `readout_status` | 借阅条账（落名片盒，一条一借）+ 读数合并；缺读数一律 `learned=null, readout_status="未接入"`，不报 0 |
| GET | `/home/warehouse` | — | `pointers[]{name,path,sha256,aligned,code}` + `aligned` + `stale` / `missing` | 仓库指针卡 + 现场重算 sha256；对不上回 409 `POINTER_STALE`（只报，不自动更新卡）。一张卡都没有时回 `status="未接入"` |
| POST | `/home/write` | body：`{role,by,room,cards[]}` | `cards[]{card_id,room,kind,name,…}` + `audit[]{…}` | 整批登记：整批 ≤20 张、每张 ≤2000 字。**门在 access 里**（结构门＋白名单门），这一层不判一次，也不兜底 |

### 7.1 两道门与状态码

```text
400  ROOM_MISS（body 带 nearest：最接近的那一间）／PATH_TRAVERSAL／PATH_ABSOLUTE／PATH_SHAPE
     ／CHARS_OVER／FILES_OVER／BAD_ARGUMENT
403  ROLE_UNKNOWN／门挡下来的（code 是库里的原码，detail.door 说明是哪道门）
     ├─ door="structure"  这间房从不放东西（只读五间：客厅、卧室、台历、橱柜、抽屉），三个角色一律拒
     └─ door="whitelist"  她今天被收回这间（只管 role=agent；organ／human 不经这道门）
404  CARD_NOT_FOUND（房名对、件不在）
409  CARD_DISK_MISMATCH／AUDIT_SHRUNK／POINTER_STALE／ROOM_CAP_REACHED／DAY_QUOTA_REACHED
```

错误一律 `{"ok": false, "code": …, "message": …, "detail": …}`；`detail.door` 与 `detail.nearest`
另在顶层各露一份——人要能一眼分清「这间房从不放东西」和「她今天被收回这间」（`docs/plan.md:45`
「能不能写看路由表，不看 UI 灰化」）。房名不在十间时，读侧与写侧都回同一个 `ROOM_MISS`
（库里另一条线叫 `ROOM_UNKNOWN`，原码留在 `detail.code_from`）。

方法不对由 `core.handle` 统一回 405，家的措辞说清「只有 `POST /home/write` 这一条是写」。
`store=None`（家未载入）回 200 + `status="未接入"`，写的那条另说 `wrote:0`——不 500，
也不假装写成功。白名单只在 `/home/access`（与 `/home/rooms` 那一栏）里**展示**：
改 `access.json` 的 `writable` 数组，下一次请求立刻照新的算，不重启、不缓存。
