# channels/onebot —— OneBot v11 事件解析（M0）

> 以下 fixture 与日志里的 ID（QQ 号、群号、消息 id）**均为合成号，非真实账号**。

M0 只做一件事：把一条真实形态的 OneBot v11 消息事件，解析成「谁、在哪、说了什么、有没有图」。
不联网、不监听端口、不依赖任何协议端 SDK、零 npm 依赖（只用 Node 内置能力）。
代码只认 OneBot v11 标准字段（`post_type` / `message_type` / `message` 消息段数组 / `sender`），
不认任何具体协议端特有的扩展字段。

## 怎么跑测试

```bash
cd /Users/bowen/NewQQAgent && node --test channels/onebot/*.test.ts
```

最近一次实跑：5 pass / 0 fail（Node v24.19.0，直接跑 .ts，靠 Node 的类型剥离，无构建链）。

注意：Node 24 的 `node --test` 把路径参数当**文件**处理，不扫目录，所以
`node --test channels/onebot/` 会报 `Cannot find module .../channels/onebot`。
用上面的通配形式，或进目录后不带参数：`cd channels/onebot && node --test`。

## 文件

| 文件 | 作用 |
| --- | --- |
| `parse.ts` | `parseEvent()` 与解析产物类型，M1/M2 的唯一入口 |
| `parse.test.ts` | `node --test` 用例：3 段正常 + 畸形输入 |
| `fixtures/event.group.text.json` | 群文本事件（真实 OneBot v11 结构） |
| `fixtures/event.private.text.json` | 私聊事件（含 `json` 卡片段） |
| `fixtures/event.group.image.json` | 群消息含 `image` 段（+ reply/at/face/mface/json） |
| `fixtures/README.md` | 每个 fixture 的来源与脱敏说明 |

## 入口

```ts
import { parseEvent, type ParsedEvent, type ParseFailure } from "../onebot/parse.ts";
```

```ts
const result = parseEvent(raw); // raw: unknown —— 原始 JSON 字符串，或已 JSON.parse 的对象
if (result.ok) {
  result.who.id;        // 发送者 QQ 号（number）
  result.where;         // { scene: "group", groupId } 或 { scene: "private", userId }
  result.text;          // 纯文本 + 占位
  result.hasImage;      // boolean
} else {
  result.reason;        // 一句人话，可直接写日志
}
```

约定：

- **纯函数，不抛异常。** 任何输入（非法 JSON 字符串、数组、null、数字、缺字段的半截对象）都返回结果，不 throw。
- **可判定。** 用 `result.ok` 判别，类型上就是可穷尽的 union，不需要 try/catch 也不需要 `any`。
- **不改输入。** 不修改传入的对象。

## 解析产物：`ParsedEvent`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `ok` | `true` | 成功标记 |
| `messageType` | `"group" \| "private"` | 群聊还是私聊 |
| `subType` | `string` | `sub_type` 原值（`normal` / `friend` …），缺失时为空串 |
| `messageId` | `number \| null` | 消息 id（可能是负数，保留原值） |
| `time` | `number \| null` | 事件时间戳（秒） |
| `selfId` | `number \| null` | 机器人自己的 QQ 号 |
| `who.id` | `number` | 发送者 QQ 号（`sender.user_id`，回退 `user_id`） |
| `who.nickname` | `string` | 发送者昵称，缺失时为空串 |
| `who.card` | `string?` | 群名片，仅在有值时出现 |
| `who.role` | `string?` | 群身份 `owner`/`admin`/`member`，仅在有值时出现 |
| `where` | 判别对象 | 群聊：`{ scene: "group", groupId: number }`；私聊：`{ scene: "private", userId: number }`（私聊对象即发送者） |
| `text` | `string` | 消息段的纯文本拼接，非文本段留占位（见下表） |
| `hasImage` | `boolean` | 是否含 `image` 段 |
| `images` | `ImageSummary[]` | 每个图片段的摘要：`{ file?, url?, summary? }`，按出现顺序 |

同一个群里的「是群里谁」靠 `where.groupId` + `who.id`/`who.card` 组合读出：
`where` 说明在哪个群，`who` 说明是这个群里的哪个人（`card` 是群名片，`nickname` 是账号昵称）。

## 解析产物：`ParseFailure`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `ok` | `false` | 失败标记 |
| `reason` | `string` | 一句人话原因，可直接进日志 |

当前会走失败分支的情况（每条都是实测过的）：

| 输入 | `reason` 示例 |
| --- | --- |
| 非法 JSON 字符串 | `不是合法 JSON：...` |
| 合法 JSON 但不是对象（数组/null/数字/字符串） | `事件必须是 JSON 对象，实际是 array` |
| 缺 `post_type`（含空对象） | `缺少 post_type 字符串字段，不像 OneBot v11 事件` |
| `post_type` 不是 `message`（notice/request/meta_event） | `暂不解析 post_type=notice 的事件（M0 只处理消息事件）` |
| `message_type` 缺失或不是 group/private | `不支持的 message_type=（缺失）` |
| 群消息缺 `group_id` | `群消息缺少 group_id，无法确定在哪` |
| 缺发送者 id | `缺少发送者 id（sender.user_id 或 user_id），无法确定是谁` |
| `message` 不是数组（例如 CQ 码字符串） | `message 应为消息段数组，实际是 string（M0 不解析 CQ 码字符串）` |

## 非文本段的占位

丢成空字符串会让人以为「什么都没说」，所以每段都有可读占位：

| 段类型 | 占位 |
| --- | --- |
| `text` | `data.text` 原文 |
| `at` | `@123456`；`qq=all` 时为 `@全体成员` |
| `reply` | `[回复:消息id]` |
| `image` | `[图片]` |
| `face` | `[表情:14]`（附 `data.id`） |
| `mface` | `data.summary`，例如 `[动画表情]` |
| `record` / `video` | `[语音]` / `[视频]` |
| `file` | `[文件:名字]` |
| `forward` | `[合并转发]` |
| `json` / `xml` | `[卡片]` |
| 其它未知类型 | `[类型名]` |

## M1 / M2 接哪里

- **M1（通道/连接）**：网络层拿到一条事件后，不必自己判断字段，直接
  `parseEvent(payload)`；`ok: true` 交给后续处理，`ok: false` 按 `reason` 记日志并丢弃。
  字符串和已解析对象两种形态都能直接喂进来（M0 内部会 `JSON.parse` 字符串）。
- **M2（回话/分发）**：用 `where` 决定往哪发（`scene === "group"` 用 `groupId`，否则用 `userId`），
  用 `who` 决定称呼谁，用 `text`/`hasImage` 决定这一轮要不要拉图。
- 需要新的段类型或新的事件类型（notice/request/meta_event）时，在 `parse.ts` 里扩，
  不动网络层；扩展后同步补 `parse.test.ts` 用例和本文档字段表。

## M0 没做的

- 不处理 `notice` / `request` / `meta_event`：明确返回 `ParseFailure`（`reason` 里带 `post_type`），
  不是静默吞掉。
- 不解析 CQ 码字符串形式（`message` 为 `string`）：返回 `ParseFailure`。
- 不下载/缓存图片：只把 `file` / `url` / `summary` 摘出来，怎么取图是 M1 以后的事。
- 没有适配层、重试、队列，也没有多协议端抽象：一个协议、够用就行。

---

# M1 反向 WS 通道 / M2 回声闭环

## 文件

| 文件 | 作用 |
| --- | --- |
| `server.ts` | 反向 WS 服务：绑 `127.0.0.1:6200`，路径 `/ws`。M1 只记日志；加 `--core` 后转给核并回话 |
| `tools/fake-client.ts` | 假协议端（自测用）：连我们的 WS、发 fixtures 里的事件、对我们下发的 action 回 `echo` 应答 |
| `package.json` / `.gitignore` | 唯一依赖 `ws`；`node_modules/` 不进库（仓库根 .gitignore 没动） |

解析仍然只有 `parse.ts` 一份，`server.ts` 直接 import 它，没有第二套字段判断。

## M1：连接与日志（不发任何东西）

```bash
cd /Users/bowen/NewQQAgent/channels/onebot && npm install      # 只需要 ws，一次就够
cd /Users/bowen/NewQQAgent && node channels/onebot/server.ts   # 不给 --core = M1
```

日志四条：`listen`（监听端口）、`conn`（连接/断开，带当前在线数）、
`recv`（收到的原始事件 JSON 原文）、`parse`（`parseEvent` 的 who/where/text/hasImage）。

假协议端：

```bash
node channels/onebot/tools/fake-client.ts                          # 三个 fixture 依次发
node channels/onebot/tools/fake-client.ts --events event.private.text.json --wait 3000
node channels/onebot/tools/fake-client.ts --interval 800 --wait 4000 --url ws://127.0.0.1:6200/ws
```

断开重连：客户端断开后服务进程不退出，客户端再拨一次就再记一条 `conn #N 已连接`。

## M2：回声闭环（加 `--core`）

```bash
# 1. 核（另一个终端）
cd /Users/bowen/NewQQAgent/plugin && python3 -m api.devserver --root organs/soul/data --port 8787

# 2. 通道（给了 --core 才会回话）
cd /Users/bowen/NewQQAgent && node channels/onebot/server.ts --core http://127.0.0.1:8787

# 3. 假协议端
node channels/onebot/tools/fake-client.ts --wait 2000
```

一次往返在日志里是一条 `req#N` 的 ①②③④（同一 N 可追）：

```
req#1  ① 收到 3000000001 名片=小明 昵称=小明 @ 群 700000001 text="在吗"
req#1  ② 交给核 POST http://127.0.0.1:8787/inbound
req#1  ③ 核回 "回声：收到"（用时 16ms）
req#1  ④ 发出 send_group_msg echo=req-1（用时 17ms）
ack    收到下发应答 echo="req-1" status=ok retcode=0
```

## 核 ↔ 通道的 HTTP 契约（M2 只此一条）

| 项 | 值 |
| --- | --- |
| 端点 | `POST /inbound`（`plugin/api/inbound.py`，由 `devserver` 在转给 `api.handle` 之前分流） |
| 请求体 | `{"who": {…}, "where": {"scene": "group", "groupId": …} \| {"scene": "private", "userId": …}, "text": string, "hasImage": boolean, "messageId": number \| null}` |
| 响应 | `200 {"ok": true, "text": "回声：收到"}` —— 写死，无分支、不读魂、不读判据、不落盘 |
| 通道下发的 action | 群：`send_group_msg` + `{group_id, message:[{type:"text",data:{text}}]}`；私聊：`send_private_msg` + `{user_id, message:[…]}`；`echo` 为 `req-N` |

核地址不可达时通道只记一行 `② 核不可达，本轮不发`，不下发、不崩溃。

> 上面这张表是 **M2 时点**的记录（那时只有 `text` 一种响应）。M3 起请求体多了 `scene`、
> 响应认 `chunks`、`empty`/`error` 都会落账，口径以本页的 **M3 章**为准。

## 门禁（上公网前必须补，本轮不做）

- **没有任何鉴权**：谁能连上 `127.0.0.1:6200` 谁就能发事件、收下发。
- 只绑 `127.0.0.1`（代码里写死，不接受外部地址）。要上公网必须先加 token 校验或来源白名单，
  并重新评估"无鉴权"这条。
- 单连接场景验证过；多协议端同时连进来没有做隔离与协商。

## 排障：6200 被占用 / 怎么判断她到底有没有起来

连不上、或通道刚起就退出，先看端口是不是被别人占着：

```bash
lsof -nP -i :6200 -sTCP:LISTEN      # 谁在听 6200
```

被占用时 `server.ts` 打一行然后**以退出码 1 退出**，不会假装「还在跑」：

```
error  监听失败：listen EADDRINUSE: address already in use 127.0.0.1:6200，进程退出
```

- 占着 6200 的若是上一次没退干净的 `server.ts`，`kill` 掉再起。
- 若是别的进程，别抢端口，也**别降到 6199**（现网正在用）——换端口要同时改 `server.ts` 的 `PORT` 与调用方的连接地址，两边必须一致。
- 判断在跑还是没跑，别只看日志里有没有一行：看 `echo $?`（监听失败是 1）或 `kill -0 <pid>`。

监听成功之后才出现的 `error  服务出错：…` 是非致命的，进程继续；连接级的 `conn  #N 连接出错：…`
只废掉那一条连接。

---

# M3 门 / 分时 / 账 / 演练

M3 只加三件事：**一道出站门（默认关着）**、**一句话按 `send_at` 分几泡发**、**一本能回答「到底发了什么、想发什么」的账**。
silent 与 send 走的是同一段路——收到事件 → 去重 → 调核 → 算分泡计划 → 记账——差别只在最后一步要不要碰 socket。
所以 silent 下的日志与账，能真实反映「如果门开着，她会说什么」。

## 文件（M3 新增的部分）

| 文件 | 作用 |
| --- | --- |
| `gate.ts` | 模式解析（`readMode`）、去重键（`dedupKey`）、追加写的账（`Ledger`） |
| `outbox.ts` | 核响应解析（`parseCoreResponse`）、分泡计划（`planChunks`）、严格串行发送（`runBatch`） |
| `server.ts` | 接线：去重 → 调核 → 计划 → 出站；所有日志 `tag` 在这里 |
| `tools/drill.ts` | 一条命令的断言式演练：扮演协议端，跑完出结论表 + PASS/FAIL + 退出码 |
| `tools/fake-client.ts` | 假协议端；M3 起还能当「不听话的协议端」（失败码/慢回/重投/重连） |
| `.state/ledger.jsonl` | 账。目录自动创建，`channels/onebot/.gitignore` 里有 `.state/`，不进库 |

## 开关：`QQAGENT_CHANNEL_MODE`

| 取值 | 结果 |
| --- | --- |
| `silent`（默认，含不设） | 调核、算计划、记账；**一个 action 都不发** |
| `send` | 同上，但每泡真的发出去，等 ack 再发下一泡 |
| 其它（空串、`Send`、拼错） | **按 silent**，并打一行 `warn` 带着原值；绝不 fail-open |

启动第一行就是门的自述（实测）：

```bash
cd /Users/bowen/NewQQAgent
QQAGENT_CHANNEL_MODE=silent node channels/onebot/server.ts --core http://127.0.0.1:8787
# gate   mode=silent（想发但被门挡住）
QQAGENT_CHANNEL_MODE=send node channels/onebot/server.ts --core http://127.0.0.1:8787
# gate   mode=send（会真发出去）
```

认不出的值不会「猜一个」（实测）：

```bash
QQAGENT_CHANNEL_MODE=Send node channels/onebot/server.ts --core http://127.0.0.1:8787
# warn   QQAGENT_CHANNEL_MODE= 认不出 "Send"，按 silent 处理（绝不 fail-open）
# gate   mode=silent（想发但被门挡住）
```

环境变量**只在启动时读一次**：改了要重启通道；运行中改它不会让门忽然开或关。

## 核的地址：`--core` 与 `--core-path`

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `--core <base-url>` | 不给 | 不给就是纯 M1：只记日志，不调核、不发任何东西 |
| `--core-path <path>` | `/chat` | 给了 `--core` 时打到 `{CORE_URL}{CORE_PATH}` |
| `--core-timeout <ms>` | `60000` | 送核 fetch 的超时；非数字 / ≤0 / 缺值 → 回落 60000 并打一行 `warn` |

**为什么默认 60s**：真模型调用常态就是 3–6 秒，偶尔更久。卡在 5s 会把正常的一轮判成失败；
而被判失败的那条消息会按 `error` 在账上锁死（之后同 `messageId` 重投只记 `dup`），
等于白丢一句——所以宁可等，也别误杀。

```bash
QQAGENT_CHANNEL_MODE=silent node channels/onebot/server.ts --core http://127.0.0.1:8787 --core-timeout 1200
# 核超时 → 账上 1 行 error：note "核超时（1200ms）：The operation was aborted due to timeout"
```

向后兼容：README 里旧的 `--core http://127.0.0.1:8787` 现在打到 `/chat`；
要退回老的回声端点就显式写 `--core-path /inbound`（实测这行确实打到 `/inbound`）：

```bash
QQAGENT_CHANNEL_MODE=silent node channels/onebot/server.ts --core http://127.0.0.1:8787 --core-path /inbound
# mode   M2：核地址 http://127.0.0.1:8787/inbound，收到消息会转给核并回话
# req#1  ② 交给核 POST http://127.0.0.1:8787/inbound
```

## 送核 / 收核：字段只增不改

送核的请求体（`parse.ts` 的产物 + 一个 `scene` 投影）：

```json
{ "who": { "id": 3000000001, "nickname": "小明", "card": "小明", "role": "member" },
  "where": { "scene": "group", "groupId": 700000001 },
  "scene": "group", "text": "在吗", "hasImage": false, "messageId": 2000000001 }
```

核的响应按顺序认，认不出就**本轮不发**（绝不猜着发）：

| 响应 | 通道怎么做 |
| --- | --- |
| `{"ok":true,"chunks":[{"text":"在的","gap_before":0,"send_at":0}, …]}` | 按 `send_at` 分时发，见下 |
| `{"ok":true,"text":"在的"}` | 单泡，等价于 `chunks=[{text, gap_before:0, send_at:0}]` |
| `{"ok":true,"chunks":[]}` | 她这轮憋回去了：一个 action 都不发，日志 `plan  req#1 chunks=0（她这轮没说）`，账上恰好 1 行 `empty`（`text=null`） |
| `ok:false` / 两者都缺 / 非 JSON / 非 2xx / 核不可达 / 核超时 | 一个 action 都不发，账上恰好 1 行 `error`，note 说明原因（前缀见账那节）；不崩、不重试 |

空白泡（`text` 全是空格）在算计划时就被丢掉：发出去只会让协议端报空消息，还白占一个泡序号；
整批都是空白泡时和 `chunks: []` 一样算「她这轮没说」，落 1 行 `empty`。

## scene：三个值，拿不到就 unknown

`scene` 是 `where` 的投影，但**只认三个值**：`group` / `private` / `unknown`。

- `group`：`where.groupId` 是数字；`private`：`where.userId` 是数字；
- 拿不到 id（`where` 畸形、缺字段）→ `unknown`，**绝不猜**（猜一个就是替协议端决定发给谁）；
- `unknown` 时**照常调核**（她的话还是要算的），但**不构造任何 action**，账上落 1 行 `error`，
  note 是 `scene 认不出（unknown），不发`，`scene`/`target` 字段就写 `unknown`——
  不会出现 `group:undefined` 这种看着像地址的脏东西。

口径归谁：

- **通道负责判定**：一处 `sceneOf(where)` 出三值，核**不要再从 `where` 的形状推一遍**，否则两边迟早不一致；
- **核怎么读**：判场景读 `scene`，取 id 读 `where.groupId` / `where.userId`；
- **私聊**：`where.userId` 就是发送者本人（协议端只给了这个），不是「会话 id」；
- **为什么冗余一个字段**：核少一处 if，`where` 也保持 `parse.ts` 的原样、将来加字段不破坏核。

真要改这套口径，改 `parse.ts` 与 `gate.ts` 的 `sceneOf`，别在核里补分支。

## 分时：一批泡怎么出去

- `send_at` 是**从这一批算起**的第几秒（浮点）。目标时刻 = 本批起点（`performance.now()`）+ `send_at` × 1000。
- **严格串行**：第 i 泡必须等 i-1 泡的 ack 或超时之后，才轮到它按自己的目标时刻发；不会并发抢跑。
- 目标时刻已经过去就**立刻发**：不补等、不追赶、更不会连发两次。
- 每一泡发之前都**复查**：门还开吗、连接还是 OPEN 吗。任一不满足 → 这一泡及其后每一泡各记一行 `unsent`，停。
- 泡与泡之间新来的消息不会让这一批重排或取消：一批就是一批。

日志按批看得见（实测）：

```
plan   req#1 chunks=3 send_at=[0,0.4,0.8]
chunk  #0/3 echo=req-1-0 text="第一泡：在的"
ack    收到下发应答 echo="req-1-0" status=ok retcode=0
chunk  #1/3 echo=req-1-1 text="第二泡：别急"
ack    收到下发应答 echo="req-1-1" status=ok retcode=0
```

action 形态（`echo` = `req-<请求序号>-<泡序号>`，泡序号从 0 起）：

```json
{"action":"send_group_msg","params":{"group_id":700000001,"message":[{"type":"text","data":{"text":"在的"}}]},"echo":"req-1-0"}
{"action":"send_private_msg","params":{"user_id":3000000003,"message":[{"type":"text","data":{"text":"在的"}}]},"echo":"req-1-0"}
```

## ack：说什么算成功

协议端回 `{status, retcode, data, echo}`（没有 `post_type`）。`echo` 是回指哪一泡的唯一凭据。

| 情形 | 通道怎么做 |
| --- | --- |
| `retcode === 0` 且 `status !== "failed"` | 该泡记 `sent`（retcode/status 如实填），继续下一泡 |
| `retcode !== 0` 或 `status === "failed"` | 打 `fail` 行（带 retcode/status/原因），该泡记 `failed`，**批次中止**；不重试、不降级、不换目标 |
| 10 秒没回（`ACK_TIMEOUT_MS`） | 打 `fail  ack 超时（echo=…）`，该泡记 `uncertain`，批次中止，**绝不自动重发**（可能已经发出去了） |
| 等 ack 时连接断了 | 该泡记 `uncertain`，批次中止 |

`ack` 那一行会把协议端给的原始说明一并摊出来（`data.message` / `data.wording` / 顶层 `message`），不吞错误码。

实测：协议端故意慢 12 秒才回，通道 10 秒就收尾，后面两泡记 unsent：

```
chunk  #0/3 echo=req-1-0 text="第一泡：在的"
fail   ack 超时（echo=req-1-0）
unsent #1/3 起 2 泡未发：ack 超时（echo=req-1-0）
ack    收到下发应答 echo="req-1-0" status=ok retcode=0      # 来晚了，只能记一笔
```

## 账：`channels/onebot/.state/ledger.jsonl`

一行一条 JSONL，字段固定（顺序也固定，方便 grep）：

| 字段 | 含义 |
| --- | --- |
| `ts` | 记账时刻（ISO 字符串） |
| `kind` | 八种：`blocked` / `sent` / `failed` / `uncertain` / `unsent` / `dup` / `empty` / `error` |
| `mode` | 那一刻的门：`silent` 或 `send` |
| `key` | 去重键（见下） |
| `messageId` | 原事件的 `message_id`，没有就是 `null` |
| `scene` / `target` | `group`/`private`/`unknown`；`group:<id>` / `user:<id>` / `unknown` |
| `index` | 泡序号（整批级的行是 `null`） |
| `text` | 这一泡的原文；`blocked` 行是**整批拼起来那句**；`dup`/`empty`/`error` 行是 `null` |
| `send_at` | 这一泡的 `send_at`；整批级（`blocked`/`dup`/`empty`/`error`）是 `null` |
| `retcode` / `status` | ack 里拿到的原值，没拿到就是 `null` |
| `note` | 人话：`chunks=3 send_at=[0,0.4,0.8]` / `ack 超时（echo=…）` / `已有 kind=sent` … |

**行数规则**（钉死，好对账）：一条入站消息，要么**恰好 1 行**，要么（send 且真走到发）**N 行**。

**恰好 1 行**（这一轮压根没走到发，或者被挡下）：

| kind | 什么时候 | 关键字段 |
| --- | --- | --- |
| `dup` | 被去重挡下（连核都没调） | note `已有 kind=<先前那个 kind>` |
| `blocked` | silent：计划算完了，被门挡住 | `text` = 整批拼起来那句，note `chunks=N send_at=[…]` |
| `empty` | 核回得成但一个泡都没有（`chunks: []`、或全是空白泡） | `text=null`，note `chunks=0（她这轮没说）` / `chunks=N 全是空白泡` |
| `error` | 这一轮没走到发 | note 前缀就这六种，**互不为前缀、可直接 grep**：`核不可达：` / `核超时（<ms>ms）：` / `核 HTTP <status>：` / `响应认不出：` / `scene 认不出（unknown），不发` / `处理异常：` |

**N 行**（send，且真的开跑）：每泡一行，取 `sent`（ack ok）/ `failed`（ack 拒绝或 send 抛错）/
`uncertain`（等 ack 超时或连接断开）/ `unsent`（前面出事或门关了/连接断了，没轮上）。

silent 的账（实测，核回 `{"ok":true,"text":"回声：收到"}`）：

```json
{"ts":"…","kind":"blocked","mode":"silent","key":"mid:2000000001","messageId":2000000001,"scene":"group","target":"group:700000001","index":null,"text":"回声：收到","send_at":null,"retcode":null,"status":null,"note":"chunks=1 send_at=[0]"}
```

没走到发的那两种（实测）：

```json
{"…","kind":"empty","scene":"group","target":"group:700000001","index":null,"text":null,"send_at":null,"retcode":null,"status":null,"note":"chunks=0（她这轮没说）"}
{"…","kind":"error","scene":"group","target":"group:700000001","index":null,"text":null,"send_at":null,"retcode":null,"status":null,"note":"核 HTTP 500：{\"error\":\"boom\"}"}
{"…","kind":"error","note":"核不可达：fetch failed"}
{"…","kind":"error","note":"响应认不出：不是合法 JSON：Unexpected token 'o', …"}
```

send 的账（实测，失败那次）：

```json
{"…","kind":"failed","mode":"send","key":"mid:2062705105","index":0,"text":"第一泡：在的","send_at":0,"retcode":100,"status":"failed","note":"ack 失败"}
{"…","kind":"unsent","mode":"send","key":"mid:2062705105","index":1,"text":"第二泡：别急","send_at":0.4,"retcode":null,"status":null,"note":"前一泡 ack 失败（echo=req-1-0）"}
```

### 去重键

- `messageId` 是数字 → `mid:<messageId>`；
- 没有 `messageId` → `fp:` + sha1(`scene|target|who.id|time|text`) 前 16 位十六进制。

### 去重规则

去重有两道，顺序不能换：

1. **认领（claim）**：调核之前一次**同步的内存占位**，占位**不落任何账行**；进程启动时还会把整本账读进去重集合（重启、重连都还算数）。
2. **判重**：账里已有同 `key` 的任何一条记录（八种 `kind` 全算，含 `error`/`empty`）**或**这条正在处理中
   → 整条丢弃：打一行 `dedup`、记恰好 1 行 `dup`，**连核都不调**——重投一次就是重写一次脑记忆，代价比多发一句话大得多。

`dup` 的 note 分两种，说清是哪一种才算排障有用：

| note | 含义 |
| --- | --- |
| `已有 kind=<原 kind>` | 早走过了（账里有真行） |
| `同一条正在处理中（在飞窗口重投）` | 上一条还在跑：已认领、还没落账 |

```
dedup  key=mid:7006835 同一条还在处理中，本次丢弃（不调核、不发）
dedup  key=mid:7006834 已有记录 kind=sent，本次丢弃（不调核、不发）
```

**认领在调核之前，占位不落账**——这一步专治「第一批还没发完就重投」：send 模式下每泡的行要等 ack 才落，
只查账的话，这段窗口里 key 在账上是隐形的，同一条会被领两次、发两遍。行数规则不受影响（占位不是行）。

### 已知取舍：宁丢一句，不重发、不重记

`error`（核不可达 / 超时 / HTTP 非 2xx / 响应认不出 / scene 认不出 / 处理异常）和 `empty`（她这轮没说）**都会落账**，
于是那条消息在账上就锁死了：之后同一个 `messageId` 重投一律记 `dup` 丢弃，既不重发也不重记。
口径是「**宁丢一句，不重发、不重记**」——核抖动时她可能丢一句，这是刻意的，不是 bug。

在飞标记（claim）是**纯内存态**，进程崩溃重启即消失：若某条已经发出、但还没来得及落账，重启后它会再发一次。
根治要把认领也落盘，那会破坏「每条入站恰好 1 行或 N 行」，本轮不做。

### 账写不进去时

`.state/` 自动 `mkdir -p`；写盘失败只打一行 `warn`（`账写不进去（…），通道继续`），
但那条 key 仍然记在内存里去重集合里——不能让同一条消息因为磁盘满被处理第二次。

## 演练：`tools/drill.ts`

它是「一条命令的验收」：假设通道已在 6200 跑着、核也在跑，自己扮演协议端，投事件、收 action、回 ack、读账、出结论表。
**它不改门**——通道是什么模式就按什么模式断言，所以先起通道（silent 或 send），再用 `--expect-mode` 告诉 drill 你起的是哪个。

```bash
node channels/onebot/tools/drill.ts --help
node channels/onebot/tools/drill.ts --expect-mode silent              # 断言 A + 断言 C
node channels/onebot/tools/drill.ts --expect-mode silent --once       # 只断言 A
node channels/onebot/tools/drill.ts --expect-mode send                # 断言 B + 断言 C
node channels/onebot/tools/drill.ts --expect-mode send --fail-retcode 100   # 断言 B 的失败分支
node channels/onebot/tools/drill.ts --expect-mode send --reconnect    # 再加断言 D（断开 1s 后重连）
```

| 断言 | 它在验什么 |
| --- | --- |
| A1/A2/A3 | silent：窗口内一个 action 都没有；账里有该 `messageId` 的 `blocked` 行；该行 `text` 非空且 `note` 带 `chunks=` |
| B1/B2/B3 | send：收到 N≥1 个 action；动作名与 scene 匹配、目标 id 对、`message` 是 text 段数组、`echo` 是 `req-<n>-<i>` 且泡序号从 0 严格递增 |
| B4/B5 | 账里每泡一行 `sent`、`retcode=0`、`index` 覆盖 0..N-1 |
| B4/B5/B6/B7（`--fail-retcode`） | `failed` 行保留原始 retcode；失败后不再发后面的泡；不把失败降级记成 `sent` |
| C1..C4 | 同一条事件（同 `messageId`）再投一次：没有新 action、多一条 `dup`、`sent` 不增加、该 key 只多 1 行 |
| D1..D4 | `--reconnect`：断开 1s 重连后再投第三次，仍然不出站、`dup` 再 +1、`sent` 不增加 |
| E1..E4 | 自动跑：拿账里**本次运行之前就存在**的旧 key 重投——仍然不出站、该 key 的 `dup` 再 +1、发送类行数（`sent/failed/uncertain/unsent`）不变、只多 1 行账。去重集合是启动时从账里全量读的，所以这条顺带把「进程重启后重投」也钉住了 |
| F1..F4 | 自动跑（仅 send）：**在飞窗口重投**——收到第一帧就立刻再投同一条（不等 ack），ack 每帧故意延后 900ms。验：action 总数 == 该批泡数 N（不是 2N）、账里发送类行数 == N、恰好多 1 行 `dup` 且 note 含「在飞」、通道日志里这批只有 1 行 `plan` |

参数（`--help` 里也有一份）：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--expect-mode silent\|send` | `silent` | 通道应有模式，决定跑哪组断言 |
| `--url <ws url>` | `ws://127.0.0.1:6200/ws` | 通道地址 |
| `--group <id>` | `700000001` | 群号 |
| `--private` + `--user <id>` | `3000000003` | 改跑私聊事件 |
| `--wait <ms>` | `8000` | 每个观察窗口。**窗口内不早退**，所以必须盖住整批节奏（`send_at` 首泡到末泡 + ack 往返） |
| `--fail-retcode <n>` | 无 | 收到 action 就回 `retcode=<n>` 的失败 ack |
| `--reconnect` | 关 | 中间断开 1s 重连，再投第三次 |
| `--ledger <path>` | `channels/onebot/.state/ledger.jsonl` | 账文件；必须和通道写的是同一个 |
| `--once` | 关 | 只跑第一条断言（A 或 B），不跑去重、重连、旧账重投、在飞重投 |
| `--no-inflight` | 关 | 不跑断言 F（不想让 drill 在通道里制造在飞并发时） |
| `--channel-log <path>` | 无 | 通道的 stdout 文件；给了就真数 `plan` 行（F4），不给按 echo 请求号唯一反推 |

每次投递用一个**新造的 `messageId`**（打印在首行），所以重复跑不会被上一次的账挡住；
断言 E 反过来故意用旧 key——它要的就是「账里早有这条」这个前提。
结论表长这样（实测 silent，7/7 PASS，退出码 0）：

```
断言  期望                                            实测                        结果
------------------------------------------------------------------------------
A1    silent：窗口内 0 个 action 帧                   收到 0 个 action 帧          PASS
A2    账里有该 messageId 的 kind=blocked 行           blocked=1 行，末行 {…}      PASS
…
合计 7 条：7 PASS / 0 FAIL
```

退出码：全 PASS = 0，任何一条 FAIL 或连不上 = 1。拿它进 CI 或收尾验收：

```bash
node channels/onebot/tools/drill.ts --expect-mode silent && echo "silent 侧过了"
```

**故意让它失败**也是日常用法：通道跑 silent 却用 `--expect-mode send` 断言，B1..B5 全红、退出码 1；
想验「协议端拒绝」，用 `--fail-retcode 100` 跑一遍 B 的失败分支。

## 不听话的协议端：`tools/fake-client.ts`

手动看日志时更顺手的那个。老用法一行没变（默认仍是「三个 fixture 依次发、ok/retcode 0、停留 4s」）：

```bash
node channels/onebot/tools/fake-client.ts
node channels/onebot/tools/fake-client.ts --events event.private.text.json --wait 3000
```

新增的「不听话」参数：

| 参数 | 默认 | 造什么局面 |
| --- | --- | --- |
| `--ack-status <s>` | `ok` | 应答里的 status（可填 `failed`） |
| `--ack-retcode <n>` | `0` | 应答里的 retcode；非 0 就是「协议端拒绝」 |
| `--ack-message <文本>` | 空 | 塞进 `data.message` 与顶层 `message`，验通道有没有把原始说明摊出来 |
| `--ack-delay <ms>` | `0` | 延迟多久才回；调到 >10000 就逼出 ack 超时 |
| `--duplicate` | 关 | 每条事件紧跟着再投一次（同 `message_id`），验去重 |
| `--reconnect [ms]` | 关（1000） | 一轮发完断开、重连、把同一批事件再发一轮，验「去重不随连接丢」 |

```bash
node channels/onebot/tools/fake-client.ts --ack-retcode 100 --ack-message 权限不足
node channels/onebot/tools/fake-client.ts --ack-delay 12000     # 逼出 ack 超时（10s）
node channels/onebot/tools/fake-client.ts --duplicate --reconnect
```

## 一行回滚

**运行时**把门关回去（代码不动、账不动，重启通道即可）：

```bash
QQAGENT_CHANNEL_MODE=silent node channels/onebot/server.ts --core http://127.0.0.1:8787
```

在飞的那一批不必担心：每一泡发之前都会复查门，门关了就后面的泡全记 `unsent` 停住；
已经发出去的那几泡收不回来，账里会如实留着。

想让同一批消息**重来一遍**（去重会拦），把账清掉——注意证据也一起没了：

```bash
rm -rf channels/onebot/.state
```


