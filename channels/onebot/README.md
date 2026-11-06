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
| `kind` | 十种：`blocked` / `sent` / `failed` / `uncertain` / `unsent` / `dup` / `empty` / `error`，以及 M4 加的 `batched`（这条消息进了哪一段）与 `silent`（核主动说这轮不说话）——后两种见文末 M4 章 |
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
M4 起这条不再够用：同一条消息会先有一行 `batched`，再跟着它所在那一段的出站行——完整规则见文末 M4 章。

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
| `--group <id>` | **每次新造**（M4 起每群 1 小时配额，写死一个群会让一小时内的第二遍演练被 `quota_exceeded` 挡住） | 群号 |
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

# M4 合批：一段消息一次核调用

> **判据：合批的单位是「这一小段在聊什么」，不是「谁说了几句」。**

旧系数是「按人分档」的（同人一档 2s、换人一档 0.5s），于是合批几乎不发生：
实测旧口径下 **私聊 85/85 段、群里 21/27 段都是 1 条一段**——「一段话」只存在于群里两个人抢话的缝隙里，
私聊更是每条自成一段（0 延迟）。这不是合批，这是给「一条一条回」换了个名字。
现在续发**不分人**：谁接着说都算同一段（`tier=continuing`），一段的边界只由三件事决定——
这一段聊到哪儿了（`continueMs`）、这一段攒到第几条（`maxMsgs`）、这一段开了多久（`hardCapMs`）。

M4 只换**粒度**：原来是「一条消息一次核调用」，现在是「**一段消息一次核调用**」。
其余口径没变：解析走 `parse.ts`、出站走 `outbox.ts` 的 `runBatch`（分泡 + `send_at` + ack + 每泡一行账）、
门仍然是 `QQAGENT_CHANNEL_MODE`。新增两本账：`.state/decisions.jsonl`（每段一行决定）与
`.state/quota.json`（每群滑窗配额）。

## 文件（M4 新增 / 改动）

| 文件 | 作用 |
| --- | --- |
| `batch.ts` | 合批：什么时候开段、deadline 到哪、这段装了谁（**按会话分开**：`group:<id>` / `user:<id>` 各有自己的 open 与「上一条到达时刻」） |
| `segment.ts` | 段渲染：一行一条 `昵称(id)：原话`，原话里的换行原样保留 |
| `compose.ts` | 三道上限（字 / 句 / 条）：越界**整批换成一句短的**，绝不截半句 |
| `decide.ts` | 决策账：每段恰好一行，人话给人看、结构化给面板指认 |
| `quota.ts` | 每群 1 小时软配额。**M7 起 `mentionCanExceed=false`：@ 也计一条、且不能突破**（覆盖 M4 的旧口径） |
| `server.ts` | 接线：入站 → `batched` → 定时器 → 取件 → 配额 → 核**一次** → 上限 → 出站 → 决策 |
| `outbox.ts` | 只加一条支路：`send:false`（核主动沉默）；分泡 / 发送 / 每泡一行账一个字没动 |
| `gate.ts` | `LedgerKind` 增加 `batched`（这条进了哪一段）与 `silent`（核主动说这轮不说话） |
| `.state/decisions.jsonl` | 决策账：每段一行 |
| `.state/quota.json` | 配额状态：重启后接着数（`group:<id>` → 时间戳数组） |

## 系数：`data/policy.json` 的 `batch` 节

数字只有这一处落盘（`batch.ts` / `policy.ts` 里一个都不写死，少一个键就在读盘那一刻炸掉）：

| 键 | 值 | 管什么 |
| --- | --- | --- |
| `quietGapMs` | `30000` | 群里距「上一条到达」超过它，下一次开批就是安静档 |
| `openMs.quietOpen` | `2000` | 群里安静开批：等 2s |
| `openMs.activeOpen` | `5000` | 群里正热闹开批：等 5s（把这一段说完） |
| `openMs.privateOpen` | `3500` | 私聊开批：等 3.5s |
| `continueMs.group` | `3000` | 群里续发：每来一条把 deadline 推到 `now+3s` |
| `continueMs.private` | `3500` | 私聊续发：同上，3.5s |
| `hardCapMs` | `8000` | 一段最多开 8s（**任何系数都绕不过它**） |
| `maxMsgs` | `6` | 一段最多 6 条：第 6 条一到就出手 |
| `mentionCapMs` | `1000` | 被 @ 时最晚在「第一次被叫」+1s 出手 |
| `bareImageMs` | `4000` | 裸图想多等 4s |
| `stickerMs` | `500` | 纯表情只想等 0.5s |

**旧值 `tiersMs`（四档）与 `privateCapMs` 已删**：同一个数字只许有一处出处。
`tiersMs.sameSpeaker` / `tiersMs.switchSpeaker` 随「续发不分人」一起没了——
读旧账时把这两个值当成「续发」看（见下面的 tier 表）。

判定（每个会话各自一批，会话键仍是 `gate.targetOf(where)`；到期判据是 `now > deadline`，取件是 `now >= deadline`）：

1. **群里开批**：距该会话上一条 > `quietGapMs` → `quietOpen`，否则 `activeOpen`；`deadline = min(now + openMs[tier], firstAt + hardCapMs)`。
2. **群里续发**：**不分人**，`tier` 恒为 `continuing`；`deadline = min(now + continueMs.group, firstAt + hardCapMs)`，
   若本段**曾有人 @ 她**，再 `min(atFirst + mentionCapMs)`。
3. **私聊开批**：`tier = privateOpen`，`deadline = min(now + openMs.privateOpen, firstAt + hardCapMs)`——私聊**不**分安静/活跃。
4. **私聊续发**：`tier = continuing`，`deadline = min(now + continueMs.private, firstAt + hardCapMs)`。
5. **@ 只认 `parse` 的 `mentionedSelf`**（含「从第一条起最早那次」）；`atFirst` 只 `min` 不改。
6. **裸图** `deadline = max(deadline, now + bareImageMs)`；**纯表情** `deadline = min(deadline, now + stickerMs)`；
   两者之后统一被 `hardCap` 与 `mentionCap` 封顶（顺序不变，谁也别想绕过硬顶）。
7. **条数上限**：任一批（群/私聊）达到 `maxMsgs` 的那一刻就**结束它**——`deadline` 压到这一条的到达时刻（定时器 0ms 内取件），
   已满的段进待取队列，让下一句话有地方开新的一段。
   注意不能「只把 deadline 推成 now」：那样第 7 条会走「已到期 → 只并入、不延长」被塞进同一批。
   `observe` 必须在凑满的那一刻**结束当前批**，每段才 ≤ `maxMsgs` 条（drill G18：连发 8 条、间隔 0.3s → 两段 `[6,2]`）。

### `tier` 的四个取值

| tier | 什么时候是它 |
| --- | --- |
| `quietOpen` | 群里开批，且距上一条 > `quietGapMs`（安静久了，她可以早点接话） |
| `activeOpen` | 群里开批，但刚才就有人在说话 |
| `privateOpen` | 私聊开批 |
| `continuing` | 续发——**不分人**、不分群/私聊 |

旧账里的 `sameSpeaker` / `switchSpeaker` **只是历史**：这两档随「续发不分人」一起删了，
新判断里不要再认它们（面板读旧账时按「续发」渲染）。

## 一段消息是怎么走出去的

1. **入站**：`parseEvent` → `ledger.claim(key)` 判重（命中就落 `dup` 行、丢弃，与 M3 一模一样）→ `hub.observe(...)`。
2. **落一行 `batched`**：字段沿用 `makeRow`（`key` / `messageId` / `scene` / `target`），
   note 写 `batch=b3 tier=continuing @=否`。消息账从此讲「这条**进没进来**」，不再讲「发没发」。
3. **定时器**：每次 observe 后按 `hub.state().deadline` 重排一次 `setTimeout`；
   **已到期绝不延长**——秒回优先于等齐，8 秒硬顶不是目标只是上限。
4. **取件**：到点 `hub.consume(now)`；返回 `null` 就什么都不做（deadline 被后到的消息推后了 / 已经取走）。
5. **配额（只对 send）**：`quota.charge(targetOf(where), now, mentioned)`。不 ok → 决策账 `quota_exceeded`，
   **连核都不调**；ok 且 `overridden` → 继续，并在 gates 里留一条 `{name:"mention",code:"sent",hit:false}`。
   silent **不调 `charge`**（本来就不会出手，扣了纯属白占这个群的额度）。
6. **送核一次**：`POST {CORE_URL}{CORE_PATH}`。body 除 M2 的 `who` / `where` / `scene` / `hasImage` / `messageId` 外，
   `text` 换成整段逐行渲染，另加 `batch`：

   ```json
   { "text": "小明(3000000001)：在吗\n小红(3000000002)：在的",
     "batch": { "id": "b3", "count": 2, "waited_ms": 300, "tier": "continuing",
                "mentioned": false, "at": "…", "scene": "group", "groupId": 700000001,
                "msgs": [ { "id": 3000000001, "name": "小明", "text": "在吗", "at": "…", "mentioned": false } ] } }
   ```

   超时 / 不可达 / HTTP 非 2xx / 响应认不出 → 决策账 `core_error`，**不重试**
   （账上那行 `error` 的文案与前缀沿用 M3：`核不可达：` / `核超时（<ms>ms）：` / `核 HTTP <status>：` / `响应认不出：`）。
7. **三道上限**：`enforceLimits(chunks, batchId, policy)`。
8. **出站**：仍然跑既有 `runBatch`（分泡 + `send_at` + ack + 每泡一行账），一行都没重写。
9. **决策账**：这一段**恰好一行**。

日志按这段看得见（实测，silent 模式）：

```
batch  key=mid:… → b1 tier=quietOpen count=1 deadline=2000ms 后
req#1  段 b1 msgs=1 tier=quietOpen waited=0ms @=false → group:700000001
blocked mode=silent，想发但被门挡住 chunks=1：在的
```

## 决策账：`.state/decisions.jsonl`

一行一段，`kind` 固定是 `decision`（实测）：

```json
{"ts":"…","kind":"decision","scene":"group","human":"没回：这个群这个小时说得够多了","machine":{"batch":{"id":"b19","msgs":2,"waited_ms":121.38,"tier":"continuing","at":"2026-11-06T03:05:47.221Z"},"gates":[{"name":"mode","hit":false},{"name":"quota","hit":true}],"decided":"silent","why":"第 2 道门 quota 拦下了","reason_code":"quota_exceeded","scene":"group","target":"group:734723709","reason":null,"source":"inbound","gate":null}}
```

- `machine.batch.msgs` 是**条数**，不是 `segment.ts` 里那份消息数组（名字撞了，写错会让面板把条数算成 NaN）。
- `machine.decided`：`sent`（真发了）或 `silent`（没出手）。
- `machine.why` 由`第一个 hit 的门`决定；门名与 code 都写进 `gates`。
- `machine.target`：`group:<id>` / `user:<id>`（与 `gate.targetOf` 同一套写法），面板不用再回头猜「发给谁」。
- `machine.reason`：核给的自由文本（例如主动沉默的理由），没有就是 `null`。
  **`why` 只描述门**（`第 N 道门 X 拦下了`）——理由绝不许塞进门名里，否则面板会把它当成一道门。
- `machine.batch.at` 是**段起点**，而且是**墙钟**（`hub` 的时钟是 `Date.now()`）：
  它能和 `ts` 直接对表，实测 `ts−at` 落在 2.0–5.0s（上界是 `hardCap+1s`，见文末「已知差异」第 3 条）。

`reason_code` 只取 `policy.decide.reasonCodes` 里的七个值（多一个值会让面板的着色表失配，直接抛）：

| reason_code | 什么时候 | 人话 |
| --- | --- | --- |
| `sent` | 出了手 | `回了：被 @` / `回了：接话` |
| `replaced_too_long` | 三道上限超了，整批换成一句短的（照发） | `回了：本来太长，换成一句短的` |
| `silent_mode` | 门关着（silent）：照旧调核、只在出站挡住，不扣配额 | `没回：门关着（silent）` |
| `quota_exceeded` | 这个群这个小时说得够多了（**M7 起 @ 也不能突破**） | `没回：这个群这个小时说得够多了` |
| `core_error` | 核超时 / 不可达 / HTTP 非 2xx / 响应认不出 | `没回：核出了问题` |
| `outbound_failed` | **出站全败**：`runBatch` 收尾 `sent === 0`（`failed` / `uncertain` / `unsent` / ack 超时 / 发送前连接已断） | `没回：发出去了但没确认（协议端没回执）` |
| `core_empty` | 她这轮没说话（`chunks:[]`、全是空白泡，或**主动沉默**） | `没回：她这轮不想说` |
| `no_mention` | 保留值，M4 不用 | — |

**两格不是一回事（`gates` vs `gate`）**：

| 字段 | 谁写的 | 是什么 |
| --- | --- | --- |
| `machine.gates` | **通道自己** | 通道的**门链**：`mode → quota → core → outbound → limits`，每道门 `{name, hit}`，第一个 `hit:true` 决定 `reason_code` |
| `machine.gate` | **核（原样抄）** | 核那一轮的**闸判摘要**：整轮一行 `{at, audit:{status,hit,lcs_len,threshold}, rewrite:{status}}` |

判据（钉死）：**`gate` 缺了就写 `null`，绝不拼空壳**。
没进闸的那一轮回包里根本没有 `gate.summary`，`null` 是「没判」的唯一证据；
把它补成 `{}` 或 `{status:"ok"}` 等于**伪造「判过」**，面板会把没判的轮次也算进命中率。
`gate.summary` 不是普通对象时（字符串 / 数组 / null）同样一律 `null`：不搬运垃圾，也不把字符串包成对象。

哪几轮会带上它：只有「核回包了、闸判拿到了、并且**真的发出去**（`sent ≥ 1`）」的那一轮。
`/say`（主动开口，不调核）、`silent_mode`、`quota_exceeded`、`core_error`、`core_empty`、
`outbound_failed` 都一律 `null`——它们没拿到核的闸判。

**`outbound_failed` 为什么叫「没确认」**：ack 超时或连接断开时，我们并不知道协议端到底收没收到——
所以措辞不能是「没发」（可能已经发出去了），也不能是「核出了问题」（核好着呢）。
它和 hand-permit 里「远端不确认就改判 `uncertain`、永不自动重发」是同一条口径：
账上如实写 `uncertain` / `unsent`，决策账写 `outbound_failed`，配额退回（她一条都没确认发出去）。

**门与顺序**（`gates` 的顺序就是判定顺序，第一个 `hit:true` 的 code 决定 `reason_code`）：

| # | 门名 | code | 命中时 |
| --- | --- | --- | --- |
| 1 | `mode` | `silent_mode` | 照旧调核，只在出站挡住：落 `blocked` 行（`text` 是她本来要说的），不扣配额 |
| 2 | `quota` | `quota_exceeded` | 不调核、不发 |
| 3 | `core` | `core_error` | 不重试、不发 |
| 4 | `limits` | `core_empty` / `replaced_too_long` | 前者不发，后者换一句短的照发 |
| 5 | `mention` | `sent`（`hit:false`） | **M7 起 `mentionCanExceed=false`，这一档不会再 hit**（保留记录位，防回滚时没人记得它本来在哪） |

**核主动沉默（`send:false`）**：理由进 `machine.reason`，门名照旧只写门（实测）——

```json
{"ts":"…","kind":"decision","scene":"group","human":"没回：她这轮不想说","machine":{"batch":{"id":"b11","msgs":1,"waited_ms":0,"tier":"activeOpen","at":"2026-11-06T03:06:07.754Z"},"gates":[{"name":"mode","hit":false},{"name":"quota","hit":false},{"name":"core","hit":true}],"decided":"silent","why":"第 3 道门 core 拦下了","reason_code":"core_empty","scene":"group","target":"group:734723707","reason":"刚在忙"}}
```

## 核的响应：`send:false` 是「不想说」，不是故障

| 响应 | 账上 | 决策账 |
| --- | --- | --- |
| `{"ok":true,"chunks":[…],"send":false,"reason":"刚在忙"}` | 恰好 1 行 `silent`，`text/send_at/retcode/status` 全 `null`，note `她这轮不想说：刚在忙` | `core_empty` |
| 同上但没给 `reason`（或 `reason` 不是非空字符串） | note `她这轮不想说（核没给理由）` | `core_empty` |
| `{"ok":true,"chunks":[]}`（**没有** `send` 字段） | 仍然 1 行 `empty`（`chunks=0（她这轮没说）`） | `core_empty` |
| 其它 | 与 M3 一致 | 与 M3 一致 |

`send === false` **优先于 `ok`**：`ok` 缺失或写成 `false` 也一样按主动沉默处理
（否则核少写一个字段就会被记成 `error`，把「不想说」误判成故障）。
主动沉默不重试、不记 `failed` / `error` / `uncertain`、不构造任何 action。

## 账（`ledger.jsonl`）在 M4 的行数规则

一条入站消息：**1 行 `batched`**（永远），外加它所在那一段的结论行（另一种行数规则不变）：

| 这一段的结局 | 这个 key 多出来的行 |
| --- | --- |
| 真发了 | 每泡一行 `sent` / `failed` / `uncertain` / `unsent` |
| silent 门挡着 | 1 行 `blocked`（`text` 是**她本来要说的那句**：silent 也调核，门是唯一变量） |
| 三道上限换成短句 | 1 行 `sent`（就是那句短的） |
| `chunks:[]` / 全空白泡 | 1 行 `empty` |
| 主动沉默 `send:false` | 1 行 `silent` |
| 核出错 | 1 行 `error` |
| 重投 | 1 行 `dup`（`batched` 已经落地，所以 note 是 `已有 kind=batched`） |

一行 `batched` + 一行 `silent` 的实测：

```json
{"ts":"…","kind":"batched","mode":"send","key":"mid:2071963246","messageId":2071963246,"scene":"group","target":"group:700000001","index":null,"text":null,"send_at":null,"retcode":null,"status":null,"note":"batch=b2 tier=activeOpen @=否"}
{"ts":"…","kind":"silent","mode":"send","key":"mid:2100088878","messageId":2100088878,"scene":"group","target":"group:730888507","index":null,"text":null,"send_at":null,"retcode":null,"status":null,"note":"她这轮不想说：刚在忙"}
```

## 配额：`quota.charge(target, now, mentioned)`

- 冷却单位是 **群**（`group:<id>` 前缀）；私聊（`user:<id>`）**不记账**，配额对它恒 ok。
- 窗口 `windowMs` 内最多 `perGroupMax` 次；到点后不搭话。
- **M7 覆盖**：`mentionCanExceed` 已改成 `false` —— **@ 也计一条、也不能突破**（被 @ 不是无限通行证）。
- 一次「段」算一次，不管这一段里有几条消息、出去几泡。
- 状态写在 `.state/quota.json`，重启后接着数（临时文件 + rename，半截写不会把整本账作废）。

**退回（`refund(key, now)`）**：只在「确认她没出手」之后调用——

| 结局 | refund |
| --- | --- |
| 主动沉默 `send:false` / `chunks:[]` / 全是空白泡 / 核不可达·超时·HTTP 错·响应认不出 | **退**（扣了没回，等于白占一次额度） |
| **出站全败**：`runBatch` 收尾 `sent === 0`（`failed`/`uncertain`/`unsent`/ack 超时/发送前连接已断） | **退**（她一条都没发出去） |
| `replaced_too_long`（她真发了一条短的） / 正常 `sent`（`sent ≥ 1`） | 不退——部分发出去也算出手过 |
| `silent_mode` | 本来就没扣，也没得退 |

退的是窗口内**最新的一条**时间戳；没得退就什么都不做（不抛、不造负数）。实测：同群连打 6 段 `send:false`
后该群配额仍是 **0 条**，第 7 段照样出手（drill G11）；6 段真出手之后第 7 段 `quota_exceeded`（G11c / G8）。

**一条路径只退一次**：所有退款入口共用 `runSegment` 里同一个 `refund()`，它带 `charged` 标志——
第一次调用真正退回并把 `charged` 置 false，之后同一条路径再调都是空操作（silent 模式压根没 `charge`，`charged` 一直是 false）。

## 私聊

私聊不再「每条即批、0 延迟」——对面就一个人，但一个人也会连发三句，逐条回就是三条消息三个回复。
现在私聊和群聊走同一套攒段，只是**只有一档开批窗**（私聊没有「安静/活跃」之分）：

| 时机 | tier | deadline |
| --- | --- | --- |
| 开批（上一条已取走） | `privateOpen` | `min(now + 3500, firstAt + 8000)` |
| 续发 | `continuing` | `min(now + 3500, firstAt + 8000)` |

# M7 定向门：群里「要不要回」终于有判据

M7 之前群里是零判据：只要有人说话她就接（27 条里 12 条没点名也接）。M7 加一道**定向门**：
先按一串便宜的判据判断「这条到底是不是在叫她」，判不出来才去问核（核那一次调用同时就是生成回复的那次，不额外多调）。

## 管线（钉死，本轮最容易做错的地方）

```
mode 门 → 第一层布尔链 → 第二层(a)(b) 便宜判 → burst 阀 → 配额(charge) → 调核 → 第二层(c) recalled
        → 没定向就 refund + no_mention（不发） → 三道上限 → runBatch → 决策账一行
```

- **定向的**（@ / 引用她 / 私聊）：`mode → 定向过 → burst → charge → core → limits → send`。
- **非定向**（(a)(b) 没中）：`mode → burst → charge → core → (c) → 不过则 refund + no_mention`。
- 「先拦再调」仍然成立：配额满了连核都不调；(c) 用的就是生成回复那一次核调用。
- `silent` 不扣配额，但**照样走完定向门与调核**（门是唯一变量：账要能回答「她本来会不会说」）。
  定向门拦下的那一轮在 silent 下**不会**产生 `blocked` 行——本来就不会说的话，没有「想发什么」可记。

## 第一层：布尔链（顺序：命令 > 定向 > 纯表情/纯图）

| 判据 | 结论 |
| --- | --- |
| 去掉行首空白后以 `/` 或 `／` 开头 | **不过**，`command`（人话「没回：那是一条命令」） |
| 私聊 | **过**，`rule=private`（私聊不做第二层判定） |
| 群里被 @ 她（`mentionedSelf`；`@全体` 不算） | **过**，`rule=at` |
| 引用的是**她自己发过的**消息（`replyToId` 命中「她说过的话」集合） | **过**，`rule=quote` |
| 纯表情包（`isSticker`） | **不过**，`sticker`（「没回：只发了个表情」） |
| 纯图片无字（`isBareImage`） | **不过**，`media_only`（「没回：只发了张图」） |

**一条「@ 她的命令」仍算命令不过**——命令优先于定向，这是刻意的：`/xxx` 归命令处理，不该触发闲聊回复。

## 第二层：只对未定向的群消息

- **(a) 问号 + 人称**：句子里出现 `questionMarks` 里的任一个（`？ ? 吗 呢`）**且**出现 `personWords` 里的任一个
  （`你 您 你们 江迟 她`）→ 过，`rule=question+person`。
- **(b) 外号**：全文命中 `nicknames`（`小肥鱼 哦鲸鲸 本鱼 大肥鱼`）→ 过，`rule=nickname`。
- **(c) 核记得**：核回包 `recalled` 里的**最高 score** ≥ `recalledMinScore` → 过，`rule=recalled`，
  并把那个分数记进决策账（`machine.direct.score`）。
- 全都没中 → `no_mention`（「没回：没叫我」）。**没定向的群消息默认不回**。

### `recalledMinScore` 是占位值

现在是 **0.75**，**未经任何数据校准**——手上没有真实核的 recalled 分数分布。
选它的理由只有两条：正余弦类相似度里 0.75 大致是「同一件事」的下沿；而且它比 0.8 松一点，
宁可在校准前多放几条进来、也别把真叫她的话拦死。
决策账把每次的 `machine.direct.score` 都记下来，就是为了**以后拿真数据重新校准这个数**。
它不是「概率门控」，没有贝叶斯、没有置信度——就是一个阈值比较。

## 第三层：两道阀

| 阀 | 判据 | 记账 |
| --- | --- | --- |
| burst | 同一会话距**上次真正发出**不足 `burstMs`（5000ms）→ 压掉 | `burst_guard`（「没回：刚说过，别连发」） |
| 配额 | 1 小时 / 群 6 条，`mentionCanExceed=false`（**@ 也计、不能突破**） | `quota_exceeded` |

- burst 窗口**按会话各算**（群是群、私聊是私聊）；`silent` 不更新这个时刻（没发出去就不算说过）。
- burst 只在「本来要回」的那条路上判（定向的、或非定向但要等 (c) 的）——它挡的是连发，不是判断。

## `data/gate.json`（键名冻结，系数只此一份）

```json
{ "commandPrefixes": ["/", "／"],
  "questionMarks": ["？", "?", "吗", "呢"],
  "personWords": ["你", "您", "你们", "江迟", "她"],
  "nicknames": ["小肥鱼", "哦鲸鲸", "本鱼", "大肥鱼"],
  "burstMs": 5000,
  "recalledMinScore": 0.75 }
```

代码里不许出现第二个数字：`direct.ts` 只做判定，词表与阈值全部注入（`--gate <path>` 只注入**路径**，
演练用它给 G1–G19 一份 `burstMs=0` 的临时表，因为那些用例测的不是这道阀）。

## 决策账的 `machine.direct`

```json
"direct": { "rule": "at", "score": null }              // 定向通过（private / at / quote）
"direct": { "rule": "recalled", "score": 0.91 }        // 非定向通过（question+person / nickname / recalled）
"direct": { "rule": null, "score": 0.2 }               // 被门拦下（调过核就记 top score）
"direct": { "rule": null, "score": null }              // 被门拦下（压根没调核）
```

新增 `reason_code`：`command` / `sticker` / `media_only` / `burst_guard`（都在 `policy.reasonCodes` 白名单里，
人话在 `decide.ts`）。`no_mention` 沿用。**ledger 行 schema 一个字没加**：这些理由全在决策账。

## 判据（人拍的，写死在这里）

1. **没定向的群消息默认不回**——这是看场合，不是冷淡。
2. **@ 也计额度**：被 @ 不是无限通行证（`mentionCanExceed=false`，**覆盖** M4 的「@ 可突破」）。
3. 以后要上小模型，**只替换第二层**；第一层与两道阀不动。

## 演练：G20（在 `--batch` 里，用真实 gate.json）

10 条群消息：@她 ×2、点名问 ×2、无定向闲聊 ×5、外号 ×1，其中**恰好 1 条**定向落在上一次发出后 5 秒内：

| 断言 | 它在验什么 |
| --- | --- |
| G20a | 定向层放行 5 条（2 @ + 2 点名问 + 1 外号），10 行决策账 `direct.rule` 序列可读 |
| G20b | burst 压掉 1 条（`reason=burst_guard`） |
| G20c | 实际发出 4 条（4 个 action 帧） |
| G20d | 5 条无定向闲聊全部 `no_mention`，且 `direct.score=0.2`（核的 recalled 真被比过） |
| G20e/f/g | 命令 → `command`、纯表情 → `sticker`、纯图无字 → `media_only` |
| G20h | 打满 6 次后第 7 条是 @ 她 → 仍 `quota_exceeded`（@ 不是通行证） |

## 已知边界

1. **A–F 组需要一个会回 `recalled` 的核**：drill 的 fixture 是「在吗」（没有称人词/外号），
   所以它靠 (c) 放行；核不回 recalled 时它会被 `no_mention` 挡下——**那是新口径的正常结果**，
   不是回归。drill 自带的假核默认回 `recalled [{score:0.9}]`，带 `LOWRECALL` 标记的回 0.2。
2. **一段多句时，判词用整段原话拼接**（`meta.texts.join("")`）：只要这一段里有一条 @ 她 / 引用她，
   整段就算定向；命令前缀按「这一段最后一条」的开始字符判。
3. **burst 时刻只存内存**：进程重启后第一次不会再被「刚说过」压住（账里查得到，但这道阀不追求跨重启一致）。
4. **定向门拦下的轮次在 ledger 里没有结论行**（只有 `batched`）：理由全在决策账，这是 M4 就定下的分工。
5. **`replyToId` 只认 `reply` 段的 `id`**：协议端若把引用塞在 CQ 码或别的字段里，这里读不到——
   读不到就按「没引用」处理（宁可少搭一句，也不误判成引用她）。
