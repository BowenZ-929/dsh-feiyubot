# B 站读取与学习的来源

- 用户旧项目 `/Users/bowen/qqAgent/AstrBot/data/plugins/astrbot_plugin_phone_control/bili_api.py`：适配 WBI 混淆表/签名、视频和字幕端点、中文轨优先、请求至少间隔 600ms。没有导入 AstrBot 宿主、平台写操作或旧进程状态。
- [zjsss900/dsh-bili-learn](https://github.com/zjsss900/dsh-bili-learn)，本地参考目录 `.scratch/refs/dsh-bili-learn/`：参考 `lib/bili.js` 的分 P/元数据结构、`lib/text.js` 的字幕分段与中英混合检索、`lib/distill.js` 的带时间引用知识卡、`lib/kb.js` 的带来源落盘。该仓采用 **CC BY-NC 4.0**，完整许可保存在 `../mind/BILI-LEARN-LICENSE.txt`；此改编同样保留非商业使用条件与归属。

改动：使用本项目 Python 器官框架；B 站读取归眼，提炼归脑核，资料归家。字幕 CDN 不接收登录 Cookie；API/字幕重定向停止读取。b23 分享链接只读取一次跳转，确认目标为 B 站视频后提取 BV/分 P；不访问目标网页、不向短链发送 Cookie。使用旧项目已验证的 WBI 表，未复制参考仓的另一份混淆表。每张卡必须通过时间与逐字引文校验，不使用参考仓“缺证据时补第一句”的兜底。坏卡单独拒绝并记录原因，不把候选内容当成已学习知识。每次最多三个分段，后续明确点击继续；无自动重试、无自动平台发言。

2026-11-07 短链入口参考 `.scratch/refs/dafeiyu-qq-bot/plugins/dsh-web/main.py` 的 `_resolve_b23` 思路（MIT，Copyright (c) 2026 dafeiyu）：显式关闭自动跳转后检查目标。本项目用标准库实现一次跳转解析，后续复用眼的官方 API 读取，不移植其通用网页抓取器。
