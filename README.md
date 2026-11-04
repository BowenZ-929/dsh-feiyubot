# 七器官与家 · qqAgent 人格架构

一个 QQ 机器人的人格架构设计。把"她"拆成七个器官加一个家：眼、耳把世界收进来，魂、心、脑在里面过一遍，嘴、手把结果做到外面；家装她身外的一切——物件、痕迹、场景。

*English: A design repository for one QQ chatbot's personality architecture — seven organs plus one home, the home holding everything outside her body.*

## 1 七器官与一个家

| id | 中文 | 英文 | 一句话 | 输入输出轴 |
| --- | --- | --- | --- | --- |
| `soul` | 魂 | Soul | 她是谁：身份、性格基线、待人口吻、红线 | 内 |
| `heart` | 心 | Heart | 她此刻如何：心情、精力、性格偏移、想不想说 | 内 |
| `brain` | 脑 | Brain | 记得什么 + 怎么想：今日、热记忆、长期记忆、人名录、回想、判断 | 内 |
| `eye` | 眼 | Eye | 看得见、看得懂：看图、看屏、看链接、读上下文 | 入 |
| `ear` | 耳 | Ear | 听语音、听语气、听见谁（当前留位，零实现） | 入 |
| `mouth` | 嘴 | Mouth | 怎么说：称呼、措辞、分段、表情包、语音 | 出 |
| `hand` | 手 | Hand | 做出什么：看手机、画画、发说说、社交动作、动作许可 | 出 |
| `home` | 家 | Home | 身外之物：物件、痕迹、场景、档案、存取、房名表 | 轴外 |

入按物理载体切成两个（图像、声音），内按读写权限切成三个（人写只读 / 程序写人只看 / 又读又写），出按落点切成两个（说给人听的话、落在世界上的动作）。家不在任何一条轴上：删掉一间房，她还是同一个她；丢掉魂，家里东西再全也不是她。

*English: Two inputs split by physical carrier, three internal organs split by write permission, two outputs split by where they land; the home sits off every axis.*

## 2 三条规矩

- **R1 分界**：能以"一件东西"被指认的 → 进家；只能以"她此刻如何"描述的 → 留器官体内。
- **R2 单真源**：每个概念只有一处真源。魂是基线（人写、程序只读），心是偏移（程序写、人只看），脑又读又写；家只收物件，绝不存状态。
- **R3 一天循环**：醒（魂自检 + 扫家）→ 日（眼耳收进 → 脑心判断 → 嘴手做出 → 痕迹落家）→ 夜（家中痕迹压成脑的记忆，心被今天改掉一点）→ 明日醒来仍是她。

拿不准一个概念归谁：先试着指认它。"把那张画给我看"指得出来，进家；"她今天挺高兴"指不出来，留体内。再问一句"这里和那里不一致听谁的"，答不上来就是双真源，必须删一个。

*English: Three hard rules — the thing/state dividing line (R1), one source of truth per concept (R2), and the wake/day/night cycle every state variable must fit into (R3).*

## 3 仓库结构

```
NewQQAgent/
├── README.md            本文件：这是什么、七器官一张表、三条规矩
├── LICENSE              MIT
├── CONTRIBUTING.md      文档写法、commit 格式、新增器官的准入
└── docs/
    ├── README.md        文档索引与阅读顺序
    ├── architecture.md  概念总纲：什么归谁、凭什么这么判、S1/S2/S3 准入标准
    ├── current-state.md 现状映射与 4 批收编顺序（每条含验收与回滚）
    ├── glossary.md      术语表：id、房间、规矩、读写权
    ├── star-map.html    星图：器官与家的可视化
    ├── modules/         逐器官分册，一器官一文件，文件名即 id（soul/heart/brain/eye/ear/mouth/hand/home）
    └── reference/       参考分册：社区十个仓库里各器官可抄的做法与代价，索引在 README.md
```

*English: Repo layout — three root files plus docs, with a per-organ module directory named by organ id.*

## 4 文档导航

| 去哪 | 它回答什么 |
| --- | --- |
| [docs/plan.md](docs/plan.md) | **先读这个**：已定了什么、还没定什么、后续对话各自领哪一条施工条目 |
| [docs/README.md](docs/README.md) | 有哪些文档、按什么顺序读 |
| [docs/architecture.md](docs/architecture.md) | 谁归谁、凭什么这么判；第八个器官的 S1/S2/S3 准入标准 |
| [docs/current-state.md](docs/current-state.md) | 现有实盘与这套架构怎么对账、4 批收编怎么排 |
| `docs/modules/*.md` | 单个器官管什么、不管什么、边界在哪 |
| [docs/reference/](docs/reference/) | 写某个器官时，别人是怎么做的、参数多少、哪条别抄 |
| [docs/glossary.md](docs/glossary.md) | 术语一句话定义，防止同名不同义 |
| [docs/star-map.html](docs/star-map.html) | 星图全貌，一屏看完七器官与家 |

*English: Read architecture first, then the per-organ modules, then current-state; glossary and star map are lookups.*

## 5 状态

设计阶段：没有代码，只有文档。本体论（七器官 + 家、R1/R2/R3）已定稿，命名不增不改；线上那台 bot 尚未按这套架构接线，差距逐条记在 `docs/current-state.md` 里。

**最终形态已定**：合并成**一套完整插件**，带**一个 WebUI 页面**，人在页面上随时看、随时调；宿主接 **DeepSeek Harness**，基座 fork `NimuStudio/NimuQDock-dsh`（NapCat 个人号通道 + 人格引擎 + 控制台）。面板能改什么、只能看什么见 `docs/architecture.md` §9，宿主迁移的账与缺口见 `docs/current-state.md` §7。

*English: Design stage — documents only, no code; the ontology is settled and the live bot is not yet wired to it.*

## 6 License

[MIT](LICENSE)，版权 `Copyright (c) 2026 qqAgent contributors`。

*English: MIT — see LICENSE.*
