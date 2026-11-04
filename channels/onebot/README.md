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

核地址不可达时通道只记一行 `② 核不可达，本轮不回`，不下发、不崩溃。

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
