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

面板空白/报错时按顺序查三样：

1. `lsof -nP -i :8787 -sTCP:LISTEN` —— 有输出 = 核在
2. `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/soul` —— 200 = 魂读得出来
3. 上面两条都好、面板还空 → 是壳或宿主的问题，不是核

### 改完核的代码，必须重启核进程（2026-11-03 踩过）

Python 在**进程启动时**把 `organs/*` 载进内存，之后改文件不会生效。改过 `organs/soul/model.py` 或 `data/*.json` 的校验规则后，**老进程会继续按旧代码拒载**，表现是 `/soul` 一直 500 说「多出某个键」，而磁盘上的代码其实早就允许了。

- 改 `organs/*` 或 `data/*` → **重启核**（`pkill -f api.devserver` 再起）。
- 改 `shell/*` → **重启宿主**（壳是插进宿主的）；核不用动。
- 壳不缓存、不解析魂的内容：核一旦好，面板下一条请求就正常，不需要重启壳。
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

四条全是 GET：干跑只算一遍给人看，不发出去、不落盘，所以没有写方法（非 GET 一律 405）。
心的读数这一轮只能从查询参数来，不给就如实写 `heart:未接入`；闸这一轮没有重写器，命中就是 `dropped`。

| 端点 | 参数 | 返回 | 只读理由 |
| --- | --- | --- | --- |
| `GET /mouth/split` | `text`（必填，原文）、`seed`（可选，间隔抖动的种子） | `parts[]`（`text`/`gap_before`/`send_at`/`degraded`/`reason`）+ `summary` + `chars` + `visible_chars` | 只把切分与节奏算一遍；阈值全在 `organs/mouth/data/policy.json`，不写盘 |
| `GET /mouth/address` | `who`（人名录 id）、`familiarity`（可选，覆盖这一次的读数） | `who`/`display`/`tier`/`relation`/`address`/`frequency`/`banned_self`/`note` | 只读人名录取一次称呼；查不到人就按生人档回退，不写回人名录 |
| `GET /mouth/tone` | `who`、`familiarity`、`mood`、`energy`、`engage`、`offset` | `addressee{…}` + `tone{tier,label,line,stance,self_name,heart_source,softness,ban_joke}` | 心的读数只从查询参数来；没给就是 `heart:未接入`，不假装有读数 |
| `GET /mouth/gate` | `text`（必填，原文） | `hit{clause_id,kind,reason,matched}` 或 `null` + `decision` + `reason` | 条款全在 `organs/mouth/data/clauses.json`；没有重写器，命中即 `dropped`，不代写也不放行 |
