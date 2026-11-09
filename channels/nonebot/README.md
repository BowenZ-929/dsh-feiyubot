# NoneBot 接入（2026-11-07）

当前链路：QQ 小号 → NapCat → **NoneBot 官方 OneBot v11 适配器**（127.0.0.1:6290）→ 既有业务通道（127.0.0.1:6200）→ 器官核（127.0.0.1:8787）→ DeepSeek。

NoneBot 负责外部协议连接、鉴权、事件解析和 QQ API 调用；`bridge.py` 只转接事件和调用回执。现有 Node 通道继续负责合批、指向判断、气泡、去重和送达记账，内部仍使用 OneBot 形状的 WebSocket。器官框架和 DeepSeek 配置没有因迁移重写。DSH 不在这条消息链上。

## 来源与适配

直接安装、调用上游实现，不复制其协议源码：

- [NoneBot2](https://github.com/nonebot/nonebot2)：连接钩子、事件预处理器、FastAPI 驱动（MIT）。
- [官方 OneBot 适配器](https://github.com/nonebot/adapter-onebot)：v11 事件、`Bot.call_api`、回执匹配（MIT）。
- [NapCat 文档](https://napneko.github.io/guide/napcat)：现有 QQ 登录与 OneBot 反向 WebSocket。

适配注意：转交业务时使用官方解析器保留的 `original_message`，完整保留 @、引用等消息段。`run.py` 的小型适配器子类只从匹配器用的 `message` 副本移除 reply，引用原文统一由业务通道有序查询；避免 NoneBot 先异步查询造成后来的更正抢先转发，也避免重复 `get_msg`。事件校验、鉴权、连接和 API 回执仍使用官方实现。这个进程只加载桥接插件，没有引入人格、记忆或依赖 `event.reply` 的匹配器插件。

## 安装与启动

需要 Python >=3.10（实测 3.12.15），原器官核仍可使用原来的 Python 3.9。

```sh
uv venv --python 3.12 .scratch/nonebot-venv
uv pip install --python .scratch/nonebot-venv/bin/python -r channels/nonebot/requirements.txt
```

QQ 停止后，首次迁移运行：

```sh
python3 channels/nonebot/configure.py "$HOME/Library/Application Support/QQ/NapCat/config/onebot11_<小号>.json"
```

只修改已启用的 `ws://127.0.0.1:6200/ws`，改为 `ws://127.0.0.1:6290/onebot/v11/ws`，同时配置随机连接令牌。原文件备份及令牌放在已忽略的 `channels/onebot/.state/`。重复运行不会悄悄覆盖备份或重置令牌。

以后继续使用根目录 `start-her.command send`（回复）或 `start-her.command silent`（只听）。`stop-her.command` 同时停止 NoneBot、业务通道和 QQ。启动脚本不会改变已经运行的业务通道的 send/silent 模式；改模式需要先停止再启动。

## 检查和日志

- `http://127.0.0.1:6290/healthz`：`bots` 非空，且 `business_connected`、`running` 均为 true，说明小号与业务通道已接通。空数组只是服务活着，小号还没连上。
- `channels/onebot/.state/logs/nonebot.log`：协议连接与错误。
- `channels/onebot/.state/logs/channel.log`：收到的聊天、分批与出站情况。
- `channels/onebot/.state/ledger.jsonl`：每个气泡的 sent / failed / uncertain 等结果。
- `python3 tools/think.py -n 10 --full`：核保留的近期模型调用、提示词及服务端返回的 reasoning 内容；不是 QQ 送达凭据。

器官核启动默认保留模型追踪（`QQAGENT_TRACE=0` 可显式关闭），包括 DSH 面板自动拉起核的情况。DSH 仍可能管理核进程和展示面板，但不转发 QQ 消息，不代替器官核调用模型。

超时或断线可能意味着“已经发了但没收到确认”；适配层关闭内部连接，让原通道记录 uncertain，**不自动重发**。明确失败保留原 retcode，成功保留 message_id 与原业务 echo。附件通过不代表识图、语音识别或合成已接通。

## 测试

```sh
uv pip install --python .scratch/nonebot-venv/bin/python httpx==0.28.1
.scratch/nonebot-venv/bin/python -m unittest discover -s channels/nonebot -p test_bridge.py -v
node --test channels/onebot/*.test.ts
cd plugin
python3 -m unittest api.tests.test_chat_api organs.mind.tests.test_conversation organs.brain.tests.test_recall
```

本次结果：6 项进程集成测试通过、149 项业务通道测试通过、251 项聊天/装配/召回测试与 4 项 devserver 测试通过。集成测试启动真实 NoneBot，模拟 NapCat；其中一项串联真实 Node 通道与假核，验证原始引用/附件、文本及媒体出站、送达回执、去重、错误、超时不重发、重连与鉴权。全部使用临时目录、随机本机端口及合成账号，不调用真实模型，不写实盘记忆，不向 QQ 发测试消息。

上线验收：2026-11-07 03:24（Asia/Tokyo）小号已连接，6290 健康状态的 `business_connected` / `running` 均为 true；6200 为 send 模式；8787 的运行源码指纹与磁盘一致。启动脚本完整执行通过，协议与业务日志无错误。此时尚未收到切换后的真人消息，因此真实 QQ 对话效果仍待下一条正常聊天验证；没有发送主动测试消息。

## 回退

先停止 QQ 和两层通道。将 `.state/nonebot.json` 中 `backup` 指向的原配置复制回 `napcat_config` 指向的位置，然后运行：

```sh
QQAGENT_TRANSPORT=direct ./start-her.command send
```

这会恢复 NapCat → 6200 的原通道。NoneBot 环境和备份可保留；回退不改变器官数据。
