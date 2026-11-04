# plugin —— AstrBot 插件壳 + 器官包

> 这个目录就是喂给 AstrBot 的那个插件包：外壳沿用 AstrBot 形制，`organs/` 里一个器官一个子包，
> `api/` 是器官共用的面板后端（宿主无关的四个端点）。每个器官的代码 / 数据 / 测试都在自己的目录里，互不串门。

## 1 外壳这一层

| 文件 | 作用 |
| --- | --- |
| `main.py` | 最小壳：定位魂、载入一次、醒时自检打日志。不注册命令、钩子、LLM 工具 |
| `metadata.yaml` | AstrBot 插件元信息 |
| `_conf_schema.json` | 宿主配置项；`soul_dir` 默认 `organs/soul/data`（相对本插件目录） |
| `pages/.gitkeep` | AstrBot WebUI 页面位，当前只有占位 |

## 2 目录

```
plugin/
├── main.py   metadata.yaml   _conf_schema.json
├── pages/
├── organs/                  一个器官一个子包；这里只放器官，不放别的东西
│   ├── __init__.py          器官包：每个器官一个子包，不许 import 宿主
│   └── soul/                魂：她是谁。人写、程序只读
│       ├── __init__.py  model.py  store.py  render.py  bounds.py  checks.py  cli.py
│       ├── data/            五份卡：identity / baseline / manner / bounds / history
│       ├── fixtures/        测试用样例卡（valid + 三套坏卡 + 红线样本表）
│       └── tests/           魂的测试 + helpers.py
└── api/                     器官共用的面板后端：四个端点的纯函数 + 本地开发服务器
    ├── __init__.py  api.py  devserver.py
    └── tests/               端点测试 + helpers.py
```

`api/` 之所以不在 `organs/` 里：它不是某个器官，而是**所有器官共用的出口**（看魂 / 看账 / 自检 / 改一处四个端点），
并且和器官一样不许 import 宿主——`api/api.py` 只有 `(int, dict)` 的纯函数，`api/devserver.py` 只用标准库。

## 3 每个模块的代码 / 数据 / 测试放哪

| 模块 | 代码 | 数据 | 测试 |
| --- | --- | --- | --- |
| `soul`（器官） | `organs/soul/`：`model` `store` `render` `bounds` `checks` `cli` | `organs/soul/data/` | `organs/soul/tests/`（样例卡在 `organs/soul/fixtures/`） |
| `api`（共用出口） | `api/`：`api` `devserver` | 无自有数据，读 `organs/soul/data/` | `api/tests/` |

三条约定：

- 数据只放 `organs/<id>/data/`，代码不往数据目录里写（唯一写路径是 `store.edit()`，它只追加 history）。
- 样例卡放 `organs/<id>/fixtures/`，测试只读副本，不改真卡。
- 测试放各自的 `tests/`，两侧都要用的小工具各自留一份副本（`organs/soul/tests/helpers.py`、`api/tests/helpers.py`），不跨包 import。

## 4 加一个新器官要碰哪几个位置

1. `organs/<id>/__init__.py` —— 包的出口，只导出这个器官对外的名字。
2. `organs/<id>/*.py` —— 代码本体，只依赖本器官内部；跨器官只走别人 `__init__.py` 里导出的名字。
3. `organs/<id>/data/` —— 这个器官自己的数据（有才建）。
4. `organs/<id>/fixtures/` + `organs/<id>/tests/__init__.py` + `organs/<id>/tests/test_*.py` —— 样例与测试。
5. `main.py` —— 接线（本轮只有魂接上了）。
6. `_conf_schema.json` —— 只有当这个器官需要外部配置时才加一项。
7. 文档三处：`docs/modules/<id>.md`、`docs/reference/<id>.md`、`docs/organs/<id>/`。

面板要暴露新东西时改的是 `api/`，不是 `organs/`：端点仍是 `(int, dict)` 纯函数，事件循环与 HTTP 归 `api/devserver.py`。

## 5 常用命令（都在 `plugin/` 下执行）

```
python3 -m unittest discover -s . -t .                                 # 全部测试（organs/ 与 api/ 一起收）
python3 -m organs.soul.cli check                                       # 自检：0 error 才算过
python3 -m organs.soul.cli render --audience 熟人                       # 看她现在被注入的样子与预算
python3 -m api.devserver --root organs/soul/data --port 8787           # 本地验收用的 HTTP
```

只跑一个包时：`python3 -m unittest discover -s organs -t .` 或 `-s api`。

## 6 宿主边界（§9.5 硬约束）

- **核 = `organs/` + `api/`**，不许 import 宿主：不 import AstrBot 的 `Star`／事件与消息类型，也不 import DSH 的 `cordis`／`Context`。
- 宿主只经**三条接口**进来：入站、出站、面板。别的一律不算。
- 这条约束由 `tests/test_kernel_boundary.py` 看住（`organs/`、`api/` 里出现宿主字样即红；宿主 import 只许出现在 `main.py`）。
- 壳当前是 AstrBot，且只是**过渡壳**（先跑来喂通道）；目标壳是 DeepSeek Harness（基座 fork `NimuStudio/NimuQDock-dsh`，通道 NapCat OneBot v11）。
- **壳可换、核只有一份，不许复制**：换宿主只换壳，核按原样长。

*English: organs/ holds one subpackage per organ; api/ is the framework-free panel backend (four endpoints); data, fixtures and tests live inside each package.*
