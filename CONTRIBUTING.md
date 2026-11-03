# 贡献指南

*English: How to write docs, name files, and commit in this repo.*

## 1 文档怎么写

文档分三类，各有各的 frontmatter、章节与行数。共用风格：正文用中文，id 与字段名保持原文不翻译；文件名用小写 ASCII slug、连字符分词，文件名即器官 id；docs 里不放任何二进制（截图、PDF、drawio 一律不进仓库，图用 `star-map.html` 这类文本形式）。

- **模块计划类**：`docs/modules/<id>.md`，八份，一器官一文件。frontmatter 八字段，缺一不入库：`id` / `name_zh` / `name_en` / `axis` / `modules` / `reads` / `writes` / `status`（只取 `designed` | `collecting`）。正文固定七节，不增不减：**§0 一句话 / §1 小模块清单 / §2 小模块逐个 / §3 大模块内交互 / §4 跨大模块交互 / §5 不做的事 / §6 收编映射**。篇幅 100–200 行。§2 里每个小模块必须有一节「会被什么改变」，写成"什么事件 + 什么条件 + 变成什么"。
  字段语义：**reads = 我要读谁的持久状态才能工作｜writes = 我的产出落进谁（交办与痕迹）**。三条硬规矩：① **writes 只列跨大模块的目标（交办与痕迹），不列自己**——"我写我自己"由 modules 字段声明，重复列就是第二处真源；② **`heart` 与 `soul` 永远不出现在任何文件的 writes 里**，包括 heart.md 自己：心只由心写、魂只由人写，别的模块对它们只能**投递事件**（投递不是写入，不进 writes）；③ **"魂的写者是人"写在 soul.md 正文里**（已写），不进 frontmatter 枚举——`human` 不是模块 id，混进 writes 会破坏字段可机器校验。
- **概念类**：`docs/architecture.md`、`docs/current-state.md`。frontmatter 三字段：`title` / `status` / `updated`（`YYYY-MM-DD`）。章节自由，按论证需要排；全文 ≤ 500 行。
- **索引与规范类**：`README.md`、`docs/README.md`、`docs/glossary.md`、`CONTRIBUTING.md`。不写 frontmatter，≤ 90 行；且不许出现第二处术语定义——术语只在 `docs/glossary.md` 定义一次，别处引用不重述，两处不一致以 glossary 为准（这就是 R2 单真源）。

## 2 commit message

`type(scope): 中文摘要`，标题一行说完是什么；body 写为什么，不复述 diff。

- type：`docs` / `feat` / `fix` / `refactor` / `chore`
- scope：器官 id（`soul` `heart` `brain` `eye` `ear` `mouth` `hand` `home`）或 `docs` / `repo`
- 例：`docs(brain): 补人名录与熟稔的归属判定`

## 3 新增器官

必须先过 `docs/architecture.md` 第 7 节的三条准入标准，三条全过才谈命名与分册：

- **S1** 有独立输入或独立输出（关掉其它器官，它还能不能单独收到东西）；
- **S2** 有只属于它、且读写主体不同的状态（能不能就地放进隔壁器官而不丢语义）；
- **S3** 有不能被现有器官症状覆盖的故障（写一条只测它的验收，别的器官写不出来）。

过不了就写进现有器官的"不管什么"，不新增 id。
