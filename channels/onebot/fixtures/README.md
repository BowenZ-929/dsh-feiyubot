# fixtures 来源与脱敏

三段 fixture 都是**真实形态**的 OneBot v11 事件：字段名与嵌套结构照抄现成报文，没有凭空发明字段。

| 文件 | 来源 | 脱敏 |
| --- | --- | --- |
| `event.group.text.json` | `.scratch/refs/dsh-world/tests/fixtures/qqSdk/event.group.text.json`，原样复制 | 无需脱敏：该文件本身就是合成 id（`self_id` 1000000001、群 700000001、发送者 3000000001） |
| `event.private.text.json` | `.scratch/refs/dsh-world/tests/fixtures/qqSdk/event.private.text.json`，原样复制 | 同上（发送者 3000000003）；`message_id` 为负数，保留原值以覆盖真实形态 |
| `event.group.image.json` | `.scratch/refs/dsh-world/tests/fixtures/qqSdk/event.group.mixed.json`（reply/at/text/image/face/mface 六段 + `sender`），**追加**一段来自现网日志的 `json` 卡片段 | 见下 |

## event.group.image.json 的两部分

### 1. 图片段（来自参考 fixture，原样）

```json
{ "type": "image", "data": { "file": "ab12cd34.image", "url": "https://example.invalid/img.png", "summary": "[图片]" } }
```

这是 OneBot v11 标准图片段形态：`data.file`（协议端内部文件名）、`data.url`（可直接取图）、
`data.summary`（展示文本）。参考 fixture 里 url 已是 `example.invalid`，file 是合成串，
身份 id 也都是合成的，未再改动。

### 2. 追加的 json 卡片段（来自现网日志，已脱敏）

取自 `/Users/bowen/qqAgent/logs/json_segments.jsonl` 第 1 条（2026-10-27T00:08:17 一条小程序卡片消息）：

```json
{ "type": "json", "data": { "data": "<卡片 JSON 字符串>" } }
```

该日志行记录的是协议端的段类型名 `componenttype.json` 与完整卡片内容
（`config` / `prompt` / `app` / `ver` / `appID` / `view` / `meta.detail_1`）。
搬到 OneBot v11 线上时，这种卡片段的标准形态是 `{"type": "json", "data": {"data": "<卡片 JSON>"}}`
——与本目录 `event.private.text.json` 里的 `json` 段结构一致。**这一步是「段类型名到 v11 标准类型」的映射，
不是逐字照搬**，如实记在这里。

脱敏项（原值一律换掉，字段名与层级全保留）：

| 字段 | 处理 |
| --- | --- |
| `config.token` | 换成 `0000…`（原为真实 md5 token） |
| `config.ctime` | 保留原值（非身份信息） |
| `appID` / `meta.detail_1.appid` | 真实小程序 appID（两个）→ `100000001` / `100000002` |
| `view` / `meta.detail_1.shareTemplateId` | 真实 32 位 hex → 全 0 占位 |
| `meta.detail_1.preview`（pic.ugcimg.cn） | → `https://example.invalid/preview.png` |
| `meta.detail_1.icon`（open.gtimg.cn，含 appID 与时间戳） | → `https://example.invalid/icon.png` |
| `meta.detail_1.qqdocurl`（短链） | → `https://example.invalid/card` |
| `meta.detail_1.host.uin` / `host.nick` | 真实 QQ 号 / 真实昵称 → `3000000009` / `示例` |
| `prompt` / `desc` / `title` | 真实文案 → `示例` 系列占位 |
| 日志行的 `ts` / `sender` 外层字段 | 不进入 fixture（那是不在 OneBot v11 事件里的日志元数据） |

另有一处已知不一致：`raw_message` 保留了参考 fixture 原来的 CQ 码串（只枚举前六段），
没有为追加的 `json` 段补写 CQ 码——`raw_message` 不参与解析，测试也不读它。

因此出现在 fixture 里的 id 只有三类：参考 fixture 的合成 id（1xxx/7xxx/3xxx 前缀）、
占位 id（100000001/100000002/3000000009）、以及脱敏后的全 0 token —— 没有真实 QQ 号、群号或 token。

## 与任务书的偏差（如实记）

任务书要求「从 `json_segments.jsonl` 取一段真实的**图片段**形态并进带图 fixture」。实际情况是：
该文件 11 行、14KB，**全部是 `componenttype.json`（小程序卡片）段，没有任何 image 段**（`grep -c image` = 0）。

处理方式：图片段形态改用同一批参考 fixture 里的 `event.group.mixed.json`（同样是真实 OneBot v11 结构，
且任务书明确列为可用来源）；现网日志的那一段卡片则以 `json` 段形式追加进同一个带图 fixture，
使「现网真实报文内容」这一要求仍有落地。没有为了让「图片段出现在日志里」而编造日志内容。
