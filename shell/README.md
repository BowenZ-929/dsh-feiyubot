# qqagent-shell —— 壳

DSH 插件包（TypeScript）。它只做四件事：**登记座位、搬运 HTTP、把核给的字符串原样交回、守着一个核进程。**
壳里没有任何她的判断：不读魂、不改魂、不猜心情，也不缓存或改写核的响应。

- **核**（Python，`../plugin/`）跑在 `127.0.0.1:8787`（`QQAGENT_CORE_PORT` 可改），面板页与 `/soul*` 接口都是它的；
  **壳负责把它拉起来并看着它**（探活 → spawn → 退避重启 → 收摊时收掉）。
- **壳**（本包）只挂**一个**席位「她的面板」，把面板的 HTTP 原样转给核。
- 两侧只经 HTTP 说话；壳不 import 核，核不知道壳存在。

## 一个席位，页内自己导航（2026-11-05 收口）

| 席位 | id / main key | 内容 |
| --- | --- | --- |
| 她的面板 | `qqagent-panel` | `<iframe src="/qqagent/panel/panel">` —— 核上的聚合页 `plugin/pages/panel.html`：一条导航（魂 · 心 · 脑 · 家 · 日子 · 聊天 · 设置）+ 一个 iframe |

**为什么不是一页一个席位**：席位是宿主契约，每加一页就多一处布局、多一处空态；而页面本来
就是一体的。所以导航长在核侧的聚合页里（`pages/panel.html`），**壳里不新增任何导航逻辑**。

- 壳**不再**为心与脑搬数据：原来那份 `/qqagent/heart`、`/qqagent/brain` 端点与 3s 轮询已经删掉，
  页面自己在 iframe 里取数。**别从这里长回第二条取数路径。**
- 前缀推导只有一份：`plugin/pages/panel-base.js`（`PANEL_BASE` + `panelFetch`）。页面各写一份
  `var MOUNT/var BASE` 的历史已经清掉，`api/tests/test_pages_fetch_targets.py` 看着这条。

每个页面都挂在三处（`docs/reference/panel.md` §1① 的形态）：

| 槽 | 登记 | 用途 |
| --- | --- | --- |
| `sidebar.panellist` | `{ id, order, label }` | 侧栏入口（一个图标，label 是悬停提示与无障碍名） |
| `main` | `{ key: <同一个 id> }` | 主区（keyed 槽；点侧栏进主区，key 不在就抛） |
| `settings.section` | `{ id, order, label }` | 设置页栏目 |

三处是**同一套组件**，不是三份实现：魂页一个 `SoulPage`，心/脑一个 `organPage(organ, title)` 共用。
心与脑的数据来自**一个** 3s 计时器（挂在模块级 store 上，谁先订阅谁启动、最后一个卸载才停），
四个页面实例（main 心、main 脑、设置页心、设置页脑）看到的是同一份快照——不各起计时器。
这条被 `tests/seat.test.mjs` 里的「计时器个数: 1」守着。

## 路由（宿主半边）

| 浏览器请求 | 核收到的 | 说明 |
| --- | --- | --- |
| `GET /qqagent/panel` | `GET /` | 面板页（剥前缀）；`/qqagent/panel/` 同结果 |
| `GET /qqagent/panel/soul` | `GET /soul` | 面板页里的相对路径 |
| `POST /qqagent/panel/soul/edit` | `POST /soul/edit` | 包体一字不改 |
| `GET /qqagent/soul` | `GET /soul` | 别名（剥掉 `/qqagent`） |
| `GET /qqagent/soul/history?audience=mate` | `GET /soul/history?audience=mate` | 查询串照搬 |
| `GET /soul*` | `GET /soul*` | 核的面板页里写死了绝对路径 `/soul*`，这一段前缀不剥 |
| `GET/HEAD /qqagent/heart` | `GET /heart` | 只读；**每请求重读核，壳里零缓存** |
| `GET/HEAD /qqagent/brain` | `GET /brain` | 同上 |
| 其它方法打 `/qqagent/heart`、`/qqagent/brain` | —— | **405**，一条也不转给核（`人不能改心与脑`） |

核没起时面板上是 `502` + 一句可读的错误（`核没有应答（http://127.0.0.1:8787）：connect ECONNREFUSED`），
心/脑两条端点是 `200` + `{ ok: false, data: null, errors: [...] }`：页面降级成「未接入」，不白屏，也不把宿主进程带崩。

### 心与脑的载荷

```json
{ "organ": "heart", "readonly": true, "ok": false, "data": null,
  "errors": ["核 /heart 回的不是 JSON（text/html; charset=utf-8）"] }
```

- `ok`：核在 `/heart`、`/brain` 上给了**一个 JSON 对象**才算 true，此时 `data` 就是那个对象（原样，壳不解释字段）；
- `errors[]`：这一趟读不到的源，逐条人话。源坏掉只进这个数组，不整页崩（照 `dsh-affinity` 的逐文件容错）；
- 壳不缓存、不 fs.watch、不记版本号：核哪天在这两个路径上给 JSON，壳不改一行就自动接上。

**现状（2026-11-03）**：核在 `/heart`、`/brain` 上给的是它自己的只读 HTML 页（`plugin/api/devserver.py:23`），
不是数据，所以两条端点现在都落成 `ok: false` + 一条「回的不是 JSON」，页面上就是「未接入」。

## 宿主路由 API 的实证（以及照它改了什么）

以前 `ctx.inject(['webServer']) + webServer.register({ kind: 'prefix', path, handler })` 是照 `dsh-world`
抄来的、没在真宿主里跑过。现在对着本机宿主的实际代码核过了：

**证据**（`/Applications/DeepSeek Harness.app/Contents/Resources/app.asar` 里的
`dsh/node_modules/@deepseek-ai/dsh-host-webserver/lib/index.js`；asar 是单文件，用 node 解头部 JSON 索引后按 offset 取文件）：

| 问题 | 原文 | 结论 |
| --- | --- | --- |
| 服务名 | `var WebServer = class extends Service { ... constructor(ctx, config) { super(ctx, "webServer"); } }` | 服务名就是 `webServer` |
| register 入参 | `register(route) { const table = route.kind === "exact" ? this.exact : this.prefixes; if (table.has(route.path)) throw new Error(...duplicate...); table.set(route.path, route); ... }` | `{ kind: 'exact' \| 'prefix', path, handler }`；同一 (kind,path) 重注册会抛 |
| 返回什么 | `return () => { table.delete(route.path); };` | 返回**卸载函数**，正好交给 `ctx.effect` |
| 卸载 | `webCtx.effect(() => webServer.register(route), label)` | 与 `dsh-affinity/index.js:134` 同一写法 |
| handler 能不能 async | `if (route !== void 0) { await route.handler(req, res); return; }` | 可以（心/脑两条端点就是 async） |
| 前缀怎么匹配 | `if (pathname !== prefix && !pathname.startsWith(\`${prefix}/\`)) continue;` + 注释 `Longest-prefix-wins over the prefix table after an exact-table miss.` | `/qqagent/panel` 同时吃 `/qqagent/panel` 与 `/qqagent/panel/...`；匹配用的是 `new URL(req.url ?? "/", "http://x").pathname`，查询串不参与匹配 |

**据此改了什么**：`register` 的形状、`ctx.inject(['webServer'])`、`webCtx.effect` 三处**都对**，一行没改
（真宿主实测 `/qqagent/panel` 200 已经是这条路的证据）。改的是功能面：

1. 新增 `/qqagent/soul*` 别名（前缀 `/qqagent`，剥完落到核的 `/soul*`）；
2. 新增心与脑两条只读端点（`src/organ.ts`），非 GET/HEAD 一律 405、每请求重读、坏源进 `errors[]`；
3. 席位从「只挂 `settings.section`、心/脑是一句死文案」改成「三处槽 × 三个页面 + 一份共享轮询」；
4. `allRoutes()` 把五条路由收成一张表，`apply` 逐条 `effect`。

## 核的守护（壳自己起核、自己收核）

从这一版起，**不需要人先起核**：宿主加载本包时 `src/core.ts` 先探活，没人应答就自己 spawn 一个。

| 事 | 做法 |
| --- | --- |
| 探活 | `GET http://127.0.0.1:<port>/soul/check`，200 + 一个 JSON 对象才算健康（1 秒超时） |
| 启动 | **已经有人占着且健康就一个进程也不起**——人手动起的核优先，不打架 |
| 节拍 | 每 5 秒探一次 |
| 重启 | 不健康就收掉自己起的那个，按 1s → 2s → 4s → … 退避重启，**上限 30 秒** |
| 归零 | 核恢复健康立即归零，下一次失败重新从 1s 起 |
| 收摊 | `ctx.effect` 的清理函数里 SIGTERM，3 秒没退再 SIGKILL |
| 起不来 | `python3` 不在 / 核目录不存在：只记一句日志，宿主照常活着（面板 502 + 可读错误） |

**端口与目录都从环境变量来**（`src/core.ts` 的 `coreSettings()`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `QQAGENT_CORE_PORT` | `8787` | 核监听的端口；`UPSTREAM = http://127.0.0.1:<port>` 由它推导——代理、探活、`--port` 全跟着变 |
| `QQAGENT_CORE_DIR` | `<repo>/plugin`（本包目录的上一级 + `/plugin`） | 核的工作目录（`python3 -m api.devserver` 的 cwd）；`--root` 是它下面的 `organs/soul/data` |

```bash
QQAGENT_CORE_PORT=8788 QQAGENT_CORE_DIR=/Users/bowen/NewQQAgent/plugin dsh --profile web
```

端口的坏值直接抛（`QQAGENT_CORE_PORT 不是端口：haha`），不静默连错地方。
守护**只认自己起的那个子进程**：人手动起的核永远不 kill。
`tests/core.test.mjs` 用注入的假进程 / 假探活 / 假计时器把这七条全覆盖了（**不真起 python**）。

实测（2026-11-03）：`kill` 掉 8787 上的核，面板 5.4 秒后自己回到 200（探活 5 秒一拍 + 1 秒退避 + python 起得比 1 秒快）。

## 安装

把包装进 DSH（本包作者没有对 `~/.dsh` 写过，那需要写权限；由总指挥决定）：

```bash
dsh plugin add /Users/bowen/NewQQAgent/shell
```

包里的 `cordis.patch.yml` 已经带一行 insert，装进去即出现在插件图里：

```yaml
- insert:
    - id: qqagent-shell
      name: qqagent-shell
```

**装之前不用 build，也不用先起核**——两条都是实测过的：

- **产物入库**：`lib/`、`client/` 不再在 `.gitignore` 里，clone 下来装进去就有货；
- **核由壳守护**：见上一节。

改源码之后才需要重新构建：

```bash
cd shell
pnpm install
pnpm build          # 产物：lib/*.js（宿主半边）+ client/index.js（浏览器半边）
pnpm typecheck
pnpm test           # 20 条：守护 / 座位 / 共享轮询 / 代理 / 只读端点，都不依赖宿主
pnpm verify:build   # 构建门禁：入库的 lib/、client/ 必须等于源码重算结果，不一致 exit 1
```

### 为什么产物入库，而不是靠 `prepare`

实测（pnpm 11.22.0 / node 24.19.0，本机）：

| 动作 | 跑 `prepare` 吗 | 证据 |
| --- | --- | --- |
| `dsh plugin add <dir>`（就是 `pnpm add <dir>`，落成 `link:`） | **不跑** | 先 `rm -rf lib client`，`add` 完 `ls lib client` 仍是 `No such file or directory`——装进去是个空包 |
| `pnpm install`（在 `shell/` 里，依赖没变） | 不跑 | 只有 `Already up to date`，产物没重建 |
| `pnpm pack` | 跑 | 删产物后 `pnpm pack`，tarball 里 `package/lib/*` 与 `package/client/index.js` 都在 |

`"prepare": "pnpm build"` 留在 `package.json` 里（`pnpm pack` 和手动 `pnpm run prepare` 用得上），
但它**救不了 `dsh plugin add`**：那条路是 `link:`，pnpm 不跑被链接包的 lifecycle 脚本。

入库还顺手堵掉一个更隐蔽的坑：`.gitignore` 里的 `/lib/`、`/client/` 会被 `pnpm pack` 当排除规则——
那一版 tarball 里 `lib/` 进了 8 个文件、`client/` **一个没有**，换台机器照样开不了面板。
现在两条都不再忽略；`node_modules/`、`.pnpm-store/`、`*.tsbuildinfo` 继续忽略。

**人怎么复验（三条，都不动 `~/.dsh`）**：

```bash
cd shell
git check-ignore lib/index.js client/index.js      # 无输出 = 不再被忽略
pnpm build && rm -rf lib client && pnpm pack --pack-destination /tmp/x   # prepare 重建，tarball 里 lib/ 与 client/ 都在
```

新 clone 那一侧（模拟另一台机器）：`rsync -a --exclude node_modules --exclude .pnpm-store shell/ /tmp/newclone/shell/`，
`dsh plugin add /tmp/newclone/shell`，宿主起来 `curl /qqagent/panel` 200——中间一次 `pnpm build` 都没跑。

### 构建门禁：`pnpm verify:build`

产物入库（上一节）换来一个必须守住的前提：**库里的 `lib/`、`client/` 必须永远是源码算出来的那一份。**
改了 `src/` 忘了 `pnpm build`，库里就躺着一份骗人的旧产物——宿主加载它，跑的是上一版代码，
而且「`ls lib` 有文件、测试也绿」，谁都不会怀疑。这道门禁就是为了把这种静默漂移变成一次红色退出。

```bash
pnpm verify:build            # 干净：无输出，exit 0
pnpm verify:build --verbose  # 干净也说一句：9 个产物文件逐字节一致 / 第二层 git diff 的状态
```

漂移时（实测）：

```
$ printf '\n// probe\n' >> src/proxy.ts && node scripts/verify-build.mjs   # 故意不 build
[verify:build] 构建产物与源码不一致 —— 改了源码忘了 build。

  lib/proxy.js —— 内容不一致

  重跑 pnpm build，然后把 lib/ 与 client/ 一并提交。
  （仓库里那份产物已原样放回，本门禁不会替你修）
[verify:build] 第二层 git diff：跳过 —— 产物还没入库（git ls-files lib client 为空），git diff 在这条路上永远为空
$ echo $?
1
```

**怎么判的（`scripts/verify-build.mjs`，只用 node 内置模块）**

1. **重算**：把库里的 `lib/`、`client/` 快照到 `os.tmpdir()`，然后**原地跑与 `pnpm build` 相同的两步**
   （`rm -rf lib client` + 用 `node_modules/typescript/bin/tsc` 跑 `tsconfig.build.json`、`tsconfig.client.json`）。
   之所以不搞 `--outDir` 重定向：那样一旦 tsconfig 以后加了 `declarationDir`/project references，
   重算就跟真正的 build 悄悄分家；跑真 build 则永远同源。慢 0.4 秒，值得。
2. **比**：把重算结果按相对路径读成 `Map<path, Buffer>`，与快照**逐字节** `Buffer.equals`。
   三类都算不一致：内容不同、仓库有重算没有（源码里删了文件）、重算有仓库没有（新文件没入库）。
3. **放回**：无论结论如何，都把快照原样拷回 `lib/`、`client/`（实测跑门禁前后产物 sha 不变）。
   门禁**不替你修**漂移——否则你只会看到一次绿色通过，然后继续以为库里的产物是对的。
4. **第二层**：用 `git ls-files lib client` 先问「产物入库了吗」：
   - 空了 → 打印「跳过」，解释为什么（见下）。**跳过不等于通过**，此时判定完全由第 2 步的字节比较决定；
   - 非空 → 再跑 `git diff --quiet -- lib client`，脏了就 exit 1 并列出 `git status` 的文件名。
     它能独立抓住字节比较抓不到的一类：**index 落后于工作区**（比如旧产物被 `git add` 过、随后又被重建），
     这时重算 vs 工作区是一致的，但提交上去的那份不是。

**为什么不能只写一句 `git diff --quiet -- lib client`**

因为 `shell/` 这个目录在本仓库里**整体还是未跟踪的**（`git status` 里是 `?? shell/`）。实测：

```bash
$ git ls-files shell | wc -l      # 0
$ cd shell && git diff --quiet -- lib client; echo $?
0                                  # 库里的产物是旧的，git 也照样说"没改动"
```

未跟踪路径上的 `git diff` 永远是空的——拿它当门禁，等于挂了一个永远绿的灯泡。所以第一层必须
**脱离 git、自己重算再比字节**；`git diff` 只能当产物入库之后的补充，而且要能明确报「跳过」。
等 `shell/` 入库以后两层都有效，这段代码不用改一行。

**什么时候跑**：改完 `src/`、提交之前；发版/评审前；以及 `shell/` 正式入库、真上 CI 的时候挂在测试前面。

## 真宿主验证（可复跑）

`~/.dsh/profiles/desktop` 是正在跑的会话宿主，不能动；`~/.dsh/profiles/web` 已链好本包。
用一份**隔离的 DSH_HOME** 起一个独立实例（不碰 51350，也不碰任何已有实例）：

```bash
rm -rf /tmp/dshhome-sub && mkdir -p /tmp/dshhome-sub/profiles
cp -R ~/.dsh/profiles/web /tmp/dshhome-sub/profiles/web
ln -sfn /Users/bowen/NewQQAgent/shell /tmp/dshhome-sub/profiles/web/node_modules/qqagent-shell
cd /tmp/dshhome-sub && DSH_HOME=/tmp/dshhome-sub nohup dsh --profile web --port 52341 --no-open > /tmp/dshhome-sub/host.log 2>&1 &
```

**核不用先起**：宿主一起来，壳就自己 spawn（日志里是 `[qqagent-shell] 核没在跑，壳起了一个（pid …）`）。
要换端口/目录就在同一行前面加 `QQAGENT_CORE_PORT=8788 QQAGENT_CORE_DIR=…`。

想在隔离家里再用 `dsh plugin add` 装一次的话，先把那份 profile 的 `node_modules` 重装一遍对齐 pnpm store
（拷过来的 `node_modules` 指向 `~/Library/pnpm/store`，而 `/tmp` 下 pnpm 想用 `/private/tmp/.pnpm-store`，
不先对齐会报 `ERR_PNPM_UNEXPECTED_STORE`）：

```bash
cd /tmp/dshhome-sub/profiles/web && rm -rf node_modules pnpm-lock.yaml \
  && pnpm install --store-dir=~/Library/pnpm/store/v11
cd /tmp/dshhome-sub && DSH_HOME=/tmp/dshhome-sub dsh plugin --profile web add /Users/bowen/NewQQAgent/shell
```

```bash
B=http://127.0.0.1:52341
curl -s -o /dev/null -w '%{http_code} %{size_download}\n' $B/qqagent/panel      # 200 9026（魂面板 HTML）
curl -s -o /dev/null -w '%{http_code} %{size_download}\n' $B/qqagent/panel/     # 200 9026
curl -s -o /dev/null -w '%{http_code} %{size_download}\n' $B/qqagent/soul       # 200（魂 JSON；核的卡坏掉时它自己回 500，壳原样透传）
curl -s -X POST -w '%{http_code}\n' $B/qqagent/heart                            # 405
curl -s $B/qqagent/heart    # {"organ":"heart","readonly":true,"ok":false,"data":null,"errors":["核 /heart 回的不是 JSON（text/html; charset=utf-8）"]}
```

**「宿主真的加载了本包」的证据**（宿主的 boot 图里那一行，来自 `GET /` 的 HTML）：

```json
{"id":"qqagent-shell","url":"/plugins/qqagent-shell/client.js?rev=f7766d76a950","rev":"f7766d76a950","inject":["@deepseek-ai/dsh-client-ui-settings"]}
```

宿主的客户端模块系统把本包编成 `/plugins/qqagent-shell/client.js` 并发了出去（`200`，9504 字节，
`text/javascript`）；把它取回来在假 slots 上跑一遍，登记出 9 条席位（见 `tests/seat.test.mjs` 的同款断言）。

## 门禁

**当前没有任何鉴权。** 这是有意为之的本地形态，也只在这个形态下成立：

- 代理只连 `127.0.0.1:8787`，核自己只绑 `127.0.0.1`；壳不对外开放任何端口。
- 但五条路由挂在**宿主的 webServer** 上，因此可达范围等于宿主 web 可达的范围。
  宿主若绑在 `0.0.0.0` 或经隧道/反向代理暴露，任何人都能读到魂、改魂、翻变更史。
- `/soul*` 是挂在宿主**根路径**上的一段（核的面板页写死了绝对路径）。它比较占地方：
  宿主其它插件若也想用 `/soul`，注册会抛「duplicate prefix route」。
- **上公网前必须补鉴权**：至少（1）宿主只绑回环或前面加一层带鉴权的反向代理；
  （2）给 `/qqagent/*` 与 `/soul*` 加令牌或会话校验；（3）改魂这类写操作单独再校验一次。
  这三条现在都没做，也不在本里程碑内。

## 边界（写在这里，免得以后自己骗自己）

- 不判断她该不该回、该说多少、心情如何——这类判断全在核里。
- 不缓存、不改写、不重排核的响应；代理是字节搬运，心/脑端点也只是把核的 JSON 对象换个信封搬出来。
- 心与脑的页面上没有任何写控件：没有表单、没有按钮、没有链接；写方法在路由层就 405，不靠 UI 灰化。
- 不出现协商端的任何名字，不 import 协议端 SDK。

## 取舍与没做到（如实）

1. **宿主被信号打死时，壳没机会收核。** `ctx.effect` 的清理函数确实会 SIGTERM → SIGKILL 收掉子进程
   （`tests/core.test.mjs` 两条守着），但那是 cordis 卸载插件时跑的；实测 `kill -TERM <宿主 pid>` 之后
   宿主直接退出、清理没跑，8787 上留下一个还活着的核进程。没做信号钩子：那会改宿主自己的关停语义
   （装了 SIGTERM 处理函数就得自己 `process.exit`，反而可能截断宿主的收尾）。
   代价可控——下一次宿主起来时探活会看到这个核、**直接复用，不起第二份**，面板照常 200。
2. **`node --test` 在 node 24.19.0 上并发跑 4 个测试文件会偶发炸。**
   报错是 `Unable to deserialize cloned data due to invalid or unsupported version`（runner 的 IPC 侧），
   实测：3 个文件 6/6 干净，4 个文件 6 次里 4 次复现（换成一个空文件也一样，与本包无关）。
   于是 `pnpm test` 加了 `--test-concurrency=1`：串行后 6/6 全绿、20 条全过。
3. **`prepare` 在 `dsh plugin add` 上不触发。** 所以产物入库（见「为什么产物入库」），
   `prepare` 只留着给 `pnpm pack` / 手动构建用。
4. **核目录默认是「本包上一级的 `plugin/`」**，这个假设只在「壳住在仓库里」时成立。
   把壳拷到仓库外（例如 `/tmp/newclone/shell`）又不给 `QQAGENT_CORE_DIR` 时，壳会记一句
   「核目录不存在：…（用 QQAGENT_CORE_DIR 指到真正的 plugin/）」，面板 502——**宿主不会崩**（这条是修过的，
   之前 `spawn` 的异步 `error` 事件没人接，会直接把宿主带走）。

5. **还没在真浏览器里看过渲染。** 本机 shell 里起不了无头浏览器（Chrome 的 sandbox/crashpad 初始化被拒，
   `Operation not permitted`），所以「侧栏那一行图标、主区那一页、iframe 高度」没有截图，也没有真 DOM。
   能给的是：宿主 boot 图里有本包那一行、宿主的 `/plugins/qqagent-shell/client.js` 200、
   把这份产物在假 slots 上跑出 9 条登记、以及 `/qqagent/panel` 200 + 魂面板 HTML。视觉验收仍待人在浏览器里点一遍。
6. **51350 上的宿主仍然不会出现这个面板。** 它跑的是 `desktop` profile（`pid 12961`，cwd `~/.dsh/profiles/desktop`），
   而本包只链在 `web` profile（`~/.dsh/profiles/web/node_modules/qqagent-shell`，bundles 里有 `qqagent-shell`）。
   本机对 `~/.dsh` 没有写权限（`dsh --profile web --help` 都会 EPERM 在 `profiles/web/cordis.yml`），
   所以「让 desktop 也加载本包」这一步没做，也不该由这里做。
7. **心与脑现在一定是「未接入」。** 核在这两个路径上给的是它自己的只读 HTML 页
   （`plugin/api/devserver.py:23` 把 `/heart`、`/brain` 指到页面），所以壳每次都记一条
   「回的不是 JSON」。这是**按设计降级**，不是故障；等核给 JSON 就自动接上。壳没有为了让页面好看去解析那个 HTML。
8. **核的面板页里的 `/heart`、`/brain` 链接在 iframe 里会 404。** `plugin/pages/soul.html:48` 写的是绝对路径，
   宿主根上并没有这两条路由（本包只挂了 `/qqagent/heart`、`/qqagent/brain`）。要修得改核的页面或再多占两个根路径，
   两条都越界，所以留着。
9. **浏览器半边必须编译成脚本，不能是 ES 模块。** 宿主的客户端模块契约是
   `window.__ModuleLoader__.load({ id, factory })`，产物里多一行 `export {}` 就会让它加载失败
   （`module: NodeNext` + `"type": "module"` 会把每个 .ts 都当模块，所以 tsconfig 里钉了
   `moduleDetection: "legacy"`）。`tests/seat.test.mjs` 有一条测试守着这件事。
10. **store 钉在包内。** 仓库根不是 pnpm workspace，不钉的话 `pnpm install` 会在**仓库根**留下
   `.pnpm-store/`（28 MB）。`pnpm-workspace.yaml` 里 `storeDir: .pnpm-store` 把它留在 `shell/` 内。
11. **只做了读的联调。** 对着已经在跑的核（`127.0.0.1:8787`）跑通了面板页与 `/soul`、`/soul/history`；
   **没有**对真数据发 `POST /soul/edit`——那会改 `organs/soul/data`。写路径只在假上游上验过（包体一字不改地转过去）。
12. **验证中途核的卡坏了一角（不是壳的问题）。** 20:59 时 `GET /qqagent/soul` 还是 `200 / 21310 字节`；
   21:00 有另一个 agent 往 `plugin/organs/soul/data/identity.json` 里加了 `appearance` 键（该文件 mtime 21:00），
   核的校验当场拒载，`GET http://127.0.0.1:8787/soul` 回 `500 {"code":"SOUL_JSON_UNREADABLE"}`，
   壳于是原样透传同一个 500——两边的响应体逐字一致，这正是「壳只搬不判断」。壳这边没改，核那张卡修好后同一条 curl 就回 200。
13. **`dsh.client.inject` 保留原样。** 它是本机宿主 boot 图里真的被消费的字段（见上面那一行 `"inject":["@deepseek-ai/dsh-client-ui-settings"]`），
   虽然包的 README 只写了 `platform` / `external` 两个字段。
