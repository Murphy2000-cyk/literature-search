# 更新记录

本项目的版本号描述**检索行为与产出格式的变化**。每条都附了当初的实测依据 ——
这个 skill 的绝大多数设计取舍来自实测踩坑，而不是先验推理。

## v2.0.1

**文档修订，检索行为无变化。目标是让不使用 DSH 的人也能装上并跑起来。**

已安装 v2.0.0 并正常使用的 DSH 用户**不需要升级**。

### 新增

- `INSTALL.md` 新增第七节「装到 DSH 以外的 Agent」：装到哪里、Key 放哪、
  `searchts` 这个可选外部命令缺了会怎样、`description` 的语言与长度上限、装完怎么自检。
  并给出判断依据（Agent Skills 规范）与写法参考（`anthropics/skills` 里的 `skill-creator`）。
- `README.md` 开头补一句指向该节。

### 变更

- `README.md` 原先写「安装到其他 Agent 时需要修改 skill 的触发条件」，这个说法不准确。
  frontmatter 只有 `name` 和 `description` 两个字段，正是 Agent Skills 规范要求的全部，
  **格式本身不需要改**；要改的是安装位置、密钥来源和可选依赖。已改为指向 `INSTALL.md` 第七节。

### 修复：清掉 v1.9 遗留的旧口径

v2.0.0 改了口径，但文档只改了一半，同一份 `README.md` 里前后矛盾。本版统一。

- **`README.md`**：「检索流程」与「快速开始」还在写每轮 200 篇（候选池 600、6–8 次请求），
  实际是 500 篇（候选池 1500、15–20 次请求）；分区来源仍写 journalmetrics.org，实际是 LetPub；
  `--quartile Q1`、`--enrich-quartile` 已改为 `--zone 1`、`--enrich-zone`；
  「仓库结构」的文件数、行数是 v1.9 的，已按当前仓库重数（35 个文件、394 KB、Python 5748 行、Markdown 2784 行），
  并补上 v2.0 新增的 `abstract_fill.py`、`venue_rules.py` 与两套自测。
- **`SKILL.md`**：开头「一次检索 6–8 次 API 调用」与同文件后文的 15–20 次矛盾，已改；
  排错表里 `--letpub --issn <ISSN>` 这个参数**根本不存在**，改成
  `journal_quartile.letpub_zone(issn=...)`。
- **`AGENTS.md`**：§2.1 架构图仍写「JCR 分区」；§2.2 模块职责表缺 `abstract_fill.py` 与 `venue_rules.py`；
  §6 排错表仍写「分区大量 unknown」。均已改。
- **`references/examples-and-details.md`**：6 处 JCR / Q1 / `quartile` 示例改为中科院 1–4 区口径。
- **`evals/test-cases/bge-ranking.md`**：2 处同上。
- **Python 版本**：`README.md` 兼容性表写 3.8+，`INSTALL.md` 写 3.10+，已统一为 **3.10+**（实测环境 3.13）。

### 修复：会议论文集篇数口径

「Italian National Conference on Sensors」在 `CHANGELOG.md` v2.0.0 段与 `AGENTS.md` §3.8 里
都写作 **16 篇**，但按同一份 553 篇全池表用 `venue_rules.is_conference_venue()` 重新统计是 **41 篇**，
其余五项（47 / 8 / 3 / 2 / 3）都能对上。已改为 41。

顺带核实了一遍总数：该表按刊名判为会议 126 篇，其中 27 篇平台已标，**差额正是 99 篇**，
与 v2.0.0 的说法一致；`126 − 27 = 99`，这条不用改。

### 已知不一致

- 密钥来源三个脚本不统一：`semantic_scholar_search.py` 与 `translator.py` 会依次尝试
  `~/.dsh/env.json`、`~/.claude/settings.json`、环境变量；`abstract_fill.py` 只认前两者中的
  `~/.dsh/env.json` 和进程环境变量。跨宿主时**请统一使用环境变量**，这一条已写进 `INSTALL.md`。
  代码本身未改动，留待后续版本统一。

## v2.0.0

**分区口径重做，并补上摘要与会议论文集两块长期缺口。**

### 新增

- **摘要补全 `abstract_fill.py`**：S2 自带摘要覆盖率约 84%，缺的几乎全是 Elsevier
  （2019 年起不向第三方提供摘要）。五源逐级兜底：Crossref → OpenAlex → Elsevier API →
  PubMed（按 DOI 反查 PMID）→ 页面渲染。实测覆盖率由 80% 提到 **98%**。
  Elsevier 通道无权限时仍返回 HTTP 200，判据看「有没有 `dc:description` 字段」而非状态码。
- **会议论文集识别 `venue_rules.py`**：`publicationTypes` 挡不住会议论文集——实测 553 篇里
  标 `Conference` 的仅 27 篇，另有 **99 篇刊名明显是会议论文集**却标 `JournalArticle`
  （e-Journal of NDT 47 篇、Italian National Conference on Sensors 41 篇等）。
  按刊名规则判定，并带白名单避免误杀 `Proceedings of the Institution of ...` 这类正规期刊。
- **`evals/test_abstract_fill.py`、`evals/test_venue_rules.py`**：两块新能力的自测。

### 变更

- **分区改为中科院《新锐期刊分区表》（LetPub，1–4 区）**，删除 JCR 分支。
  JCR 无免费数据源，第三方站点给的是自算四分位，拿来当 JCR 汇报会出错。
- **每轮取 500 篇（5 页）**，单查询仍受平台 `offset + limit ≤ 1000` 的硬上限约束。
- **LetPub 抓取改为 curl_cffi 优先**：单本 7.0–9.3 s 降到 2.6–3.6 s，215 本刊从约 29 分钟降到约 10 分钟。

### 修复

- **分区缓存毒化**：LetPub 偶发返回空页，旧实现把 `None` 永久写进缓存，
  导致 215 本刊里 185 本被判「未收录」。现改为未收录项 30 天 TTL 后自动重查。
- `--select` 此前只影响显示，`--out` 导出仍给全部列；现在两者一致（v1.9.1 已修，此处并入）。

### 破坏性变更

- `--quartile` → `--zone`，`--enrich-quartile` → `--enrich-zone`
- 分区取值 `Q1`/`Q2` → `1`/`2`/`3`/`4`
- 传旧值一律**报错并提示**，不会静默返回空表

## v1.9.1

- **修正 `paper_table.py --select` 的作用范围**：此前 `--select` 只影响显示，
  `--out` 导出仍给全部列，与 `--limit` / `--top-n` 是同一类缺口。现在两者一致。

## v1.9.0

- **`RelevanceScorer(lang="en")` 固定英文分支**。旧版按查询的字符比例自动判断，
  实测**纯中文查询会加载 `bge-base-zh-v1.5`（780 MB）**；现在即使纯中文查询也只加载
  `bge-base-en-v1.5`。`lang="auto"` 保留旧行为。
- **新增 `unload()` / `with` 上下文管理**，同时如实标注它**不能**把内存归还操作系统。
- **新增 `search_pipeline.py`**：一次调用做完 检索 → BGE 每组评分 → 合并排序 → 落盘。
  内存实测：`import torch` + `sentence_transformers` 就要 732 MB；
  `del` + `gc.collect()` **只还 7 MB**；Windows 工作集修剪是假象（再分配即回到 250 MB）。
  结论：**只有进程退出能真正释放**，所以设计成"一个进程做完整个任务再退"。

## v1.8.0

- **BGE 模型由 `bge-small-*`（384 维）换成 `bge-base-*`（768 维）**。
- **单篇得分由「各关键词相似度之和」改为「平均」**。组内排序等价，
  但**跨组比较必须用平均**，否则关键词多的那组得分天然偏高、合并排序时整体占优。
- **新增 `RelevanceScorer.rank_groups(groups, total_limit)`**：
  **每组只用自己那组的关键词评分**（硬性约束），合并去重后按分全局降序。
  实测正负对照：本组平均 **0.5843** vs 别组 **0.5121**，5/5 全部本组更高。
- 首次加载实测 **101 秒**（CPU），之后编码 + 排序 **0.2 秒**。

## v1.7.3

- **修掉「候选池白建」的选择偏差**。旧实现按「查询 1 的全部 → 查询 2 的全部 → …」
  合并后取前 N，实测 6 查询 × 100 篇 = 600 候选取前 200 时，
  **查询 3–6 全部 0 篇** —— 连它们各自排第 1 的论文都进不了表。
- 新增 `paper_table.interleave_by_query()`：按**轮转**融合各查询结果再截断。
  修复后各查询约 33 篇，6 个头名全部进表。

## v1.7.2

- **修掉一个会无限发请求的死循环**。循环条件看的是**去重后**的篇数而 `offset` 照涨，
  一旦 S2 跨页返回重复结果就永不终止（mock 实测 `offset` 涨到 4800 万仍未停）。
  加两个安全阀：**绝对页数上限**（`offset + limit ≤ 1000` → 10 页）与**无进展即停**。
  修复后重复场景 **2 次请求即停**。

## v1.7.1

- **澄清两个"上限"**：`search_paginated(total_limit=N)` 是**每个查询**的上限（默认 100 = 一页），
  `DEFAULT_TOTAL_LIMIT` 是**写入元数据表的篇数上限**（默认 200）。旧文档把两者写混了。
- 新增 `save_papers_xlsx(total_limit=N)` 与 CLI `--top N`（0 = 不限）；
  **候选不足上限时有多少存多少**，不补齐、不报错。

## v1.7.0

按确认的标准流程做了 7 处改动：**按主题检索 → 不设分区要求 → 排除会议与预印本 →
取前 200 篇一次性落盘成一个 Excel → 之后按需读取、按需查分区回填**。

1. `journal_quartile.py` 改用**只读抓取**（旧版会下载文件到磁盘）；
2. 分区解析改为读 markdown 表格（旧版靠网页 `<title>` 正则，页面改版后不可靠）；
3. **新增缓存版本号 `CACHE_VERSION = 2`**：实测旧缓存把某 Q1 期刊记成 Q2，**v1 旧缓存一律作废**；
4. **新增 `paper_table.py --enrich-quartile`**：查分区并回填到已有 Excel 的「分区」列；
5. 统一"默认 200 篇"；
6. **`DEFAULT_QUARTILE_FILTER` 由 `["Q1","Q2"]` 改为 `None`** ——
   消除"先查分区再筛选就会突然砍掉大量文献"的顺序陷阱；
7. **`journal_only` 放宽为「非会议 + 非预印本」**，去掉 `not is_journal` 强制条件
   （实测该条件会丢掉 S2 未返回 `publicationTypes` 的正常期刊论文）。

## v1.6.0

- **新增 `paper_table.py`：论文元数据表一次性落盘 + 按需读取**。
  要解决的问题：565 篇全量 JSON 约 **10.6 万 token**，整表进上下文 ≈ **9.7 万 token**；
  改用 `--filter --quartile Q1 --top-n 10 --quiet` 后 stdout **只有 2 行 ≈ 47 token**。
- 三条硬约束：`PaperID`/`DOI` 强制文本格式；筛选前校验字段（含"整列为空"）；
  xlsx 只是视图、json 仍是权威源。

## v1.5.0

- **补齐 Semantic Scholar 的检索机制**（此前文档只写了 Crossref 的，两种相反的心智模型极易混用）：
  关键词检索而非语义检索、**无布尔语法**、匹配范围含标题+摘要、
  多词是"提高相关度"而非严格求交、**`total` 不可信**、中文查询噪声、查询词写法建议。
- 显式标注 §3.4.2 描述的是 Crossref，与 S2 机制**恰好相反**。

## v1.4.0

实测驱动的一次大改：

- 重试策略改为**首次 + 2 次重试（各等 10s，共 3 次请求）**，到点即停；
- 三次全败后**强制向用户提出二选一问题**（降级 Crossref / 等待 3–5 分钟）；
- `REQUEST_INTERVAL` 由 1.5 秒提高到 **2.5 秒**（实测 1.5 秒仍会触发 429）；
- **纠正失败归因**：不再报"密钥失效"，改报"服务端限流封锁（约 60–110 秒）"。
  证据：封锁期内伪造 key 返回 `403`（先鉴权后限流）→ **key 本身有效**；
  静默 60s 后仍 429、约 44s 后自行恢复 200。

## v1.3.4

修掉两个真实缺陷（**不得回退**）：

1. **任何请求层失败都必须记为 `failed`**。曾出现 HTTP `504` 返回 `None`，
   被上层当成"结果为空 = 查询完成"，报告写成 `[ok] … 取回 0 篇`，
   把网络故障伪装成"确实未命中"。
2. **失败时必须保全已取回的部分结果**。曾出现翻页中途抛异常时 `return` 未执行，
   上一页已取回的文献随异常丢失。

## v1.3.3

- **换源决策权在用户**：S2 失败时不得自行改用 Crossref，必须先汇报、说明代价、等用户决定。
- 脚本层加硬门禁：未传 `approved_by_user=True` 直接拒绝执行（退出码 3），且**不发任何网络请求**。

## v1.3.1

- 重试策略硬性化，失败显式抛 `ApiUnavailableError`，**禁止静默返回空**；
- **禁止匿名降级**：未配置密钥直接报错；
- 移除无效的 Header ↔ URL 切换重试（同一密钥，换传参方式对限流毫无作用）；
- 新增 Crossref 备用源；
- 文档补齐"密钥 vs 共享池"、"一次 200 一次 429 的含义"。

## v1.0.0

- 首个版本。两阶段架构：**先便宜地筛选（Phase 1），再按需地取详情（Phase 2）**。
