# 文献检索 Skill · 详细说明（AGENTS.md）

> 精简版见同目录 `SKILL.md`。本文档面向执行检索的 Agent，包含驱动脚本模板、失败处理规则、排错手册与文件归档规范。

---

## 1. 目的

学术文献调研的痛点是"信息量与成本的错配"：用户只需要快速浏览标题挑选少量论文，却往往被迫一次性拉取全部摘要，浪费 token、翻译调用与浏览时间。

本 skill 的解决方式是把检索拆成两阶段：**先便宜地筛选，再按需地取详情**。

---

## 2. 功能

### 2.1 两阶段架构

```
Phase 1  多轮检索 → 合并去重 → 客户端筛选 → JCR 分区 → BGE 排序 → 翻译标题
         产出：文献检索_筛选结果_*.txt/.json（含 PaperID，不含摘要）
                              ↓
                     用户审查，挑选 PaperID
                              ↓
Phase 2  按 paper_id 批量查询 → 摘要 + DOI + PDF → 翻译摘要
         产出：论文详情_*.txt/.json
```

**结果呈现方式（硬性约定）**：Phase 1 完成后，把结果**一次性全部呈现**给用户（BGE 排序 + 中文标题），
**不分组、不分页、不截断**。由用户自行浏览并挑出要进 Phase 2 的 PaperID。
理由：Phase 1 的产物本来就是给用户"扫一眼"用的，分批呈现会打断浏览。

**paper_id ↔ 标题映射**：Phase 1 会自动保存 `paper_mapping_*.json`；
Phase 2 允许用户**直接报论文标题**，用 `find_paper_id_by_title()` 先查本地映射，
查不到才回退到 API 反查（此时**会多消耗 1 次 S2 请求**）。

### 2.2 模块职责

| 文件 | 职责 |
|---|---|
| `semantic_scholar_search.py` | 主流程：检索、分页、合并去重、筛选、分区集成、保存；含失败重试与上报 |
| `paper_details.py` | Phase 2：按 paper_id/标题批量取详情并翻译 |
| `translator.py` | DeepSeek Flash 批量翻译（标题 30 条/批、摘要 5 条/批，关闭推理链）。无推理链、纯翻译，速度快，学术术语处理良好 |
| `journal_quartile.py` | 分区查询。**只做 LetPub 中科院《新锐期刊分区表》（1–4 区，`letpub_zone`）**；JCR 无免费数据源，v2.0 起不再提供（原 journalmetrics 那套已删除） |
| `relevance_scorer.py` | BGE 多组关键词相关性排序（**`bge-base`，与结构化知识库同一套模型**；`rank_groups()` 支持每组独立评分后全局排序；**`lang="en"` 固定英文分支**） |
| `search_pipeline.py` | **一步流程**：检索 → BGE 每组独立评分 → 合并排序 → 落盘 Excel（内存生命周期见 §3.6） |
| `bge_status.py` | **查看本机 BGE 占用**：谁在跑、中文/英文分支、各占多少内存；含"怎么单独关掉英文侧"的操作说明 |
| `crossref_fallback.py` | **备用数据源**：仅在用户同意后使用（带门禁）；只覆盖有 DOI 文献 |
| `evals/test_ratelimit.py` | 失败重试策略自测（请求次数、密钥携带、无匿名降级） |
| `evals/test_report.py` | 如实汇报留痕自测（确实未命中 / 查询失败 / 达到抓取上限） |
| `evals/test_crossref_gate.py` | 备用源门禁自测（未获用户同意时拒绝执行且不发请求） |

### 2.3 检索参数与默认值

| 参数 | 默认值 | 覆盖方式 |
|---|---|---|
| 作者 | 不筛选 | 传 `author_filter` |
| 年份 | 近 10 年 | 传 `year_range=(2016, 2026)` |
| 预印本 | **排除** | `paper_type="all"` |
| 会议论文 | **排除** | `paper_type="all"` 或 API 层 `publication_types` |
| 分区 | **不筛选**（中科院新锐分区，1–4 区） | 显式传 `quartile_filter=["1","2"]`；检索阶段分区本来就是空的 |
| **写入元数据表的篇数上限** | **不限（None）** | `DEFAULT_TOTAL_LIMIT` / `save_papers_xlsx(total_limit=N)` / CLI `--top N`（None 与 0 = 不限） |
| 每个查询取回的上限 | 100（= 一页，API 单页上限） | `search_paginated(total_limit=N)` |
| 单查询结果硬上限 | **1000 条**（`offset + limit ≤ 1000`，平台限制，旧文档的 10000 已失效） | 无法覆盖；需要更多须改用 `/paper/search/bulk`（分页至千万级，且支持布尔语法） |
| 查询语法 | **纯文本关键词，无布尔语法**（写 `AND`/引号会被当普通文本） | 需要 `+` / `\|` / `-` / `""` / `*` / `~N` 须改用 `/paper/search/bulk` |
| `total` 字段 | **近似值，不可信**（实测加一个词会涨 65 倍） | 禁止用于覆盖率汇报，见 §3.5.5 |
| 请求间隔 | **2.5 秒** | `REQUEST_INTERVAL` |
| 失败重试等待 | **10s / 10s（共 3 次请求，累计约 20 秒）** | `RETRY_WAITS_S` |
| 用户选择长等时的静默时长 | 240 秒（3~5 分钟取中值） | `LONG_WAIT_S` + `long_wait_on_failure=True` |

**两条必须记住的语义（v1.7.0 修正）**：

1. **`paper_type="journal_only"` 的语义 = 「非会议 + 非预印本」**，
   **不再**要求 `is_journal=True`。旧实现在此多一条强制条件，会把
   S2 没返回 `publicationTypes` 的正常期刊论文**静默丢掉**（实测确认的漏检点）。
2. **分区默认不筛**（口径已改为中科院 1–4 区）。旧默认 `["Q1","Q2"]` 是个顺序陷阱：检索阶段 `paper.quartile` 为空，
   靠 `filter_papers` 里 `and paper.quartile` 侥幸没生效；一旦改成"先 `enrich_quartiles()`
   查分区、再 `filter_papers()`"，就会突然按分区砍掉大量文献。传 `Q1/Q2` 会直接报错（旧 JCR 口径已不再提供）。
   现在 `DEFAULT_QUARTILE_FILTER = None`，**要按分区筛必须显式传**。

**⚠️ 两个"上限"不要混淆（v1.7.1 澄清）**：

| 概念 | 常量 / 参数 | 默认 | 含义 |
|---|---|---|---|
| **写入表的篇数上限** | `DEFAULT_TOTAL_LIMIT` / `total_limit` / `--top` | **不限（None）** | 所有查询**合并去重之后**取前 N 篇；**候选不足 N 篇则有多少存多少**（不补齐、不报错）；传 0 表示不限 |
| 每个查询取回的上限 | `search_paginated(total_limit=N)` | 100 | **单个查询**最多取多少条，100 = 一页 |

例：4 轮 × 每轮 500 篇（5 页）= 最多 2000 条候选 → 合并去重 → 过闸门 → **全量入表（不截断）**。
`search_paginated` 的参数叫 `total_limit` 是指"该查询的总上限"，**不是**全表总数
（v1.7.1 之前文档把两者写混了，此处为准）。

---

## 3. 使用方法

### 3.1 环境准备

**Python 依赖**（首次使用需安装；本机第三方包装进 conda 环境 `E:\Anaconda3\envs\py313`）：

```bash
& "E:\Anaconda3\envs\py313\python.exe" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple requests sentence-transformers torch numpy
```

脚本内已 `sys.path.insert` 该目录；也可用 `PYTHONPATH` 引入。
`requests` 必需；`sentence-transformers` + `torch` 仅 BGE 排序用，缺失时跳过排序（属降级，不算失败）。

**API Key 获取与配置**：

1. 申请：https://www.semanticscholar.org/product/api#api-key-form （免费，秒批发到邮箱，格式 `s2k-` + 40 位）。
   邮件中会写明速率上限（1 请求/秒，跨所有端点累计）与传参方式（`x-api-key` 请求头）。
2. 配置方式（任选其一，脚本自动兼容）：

   - **方式 A（推荐）**：写入 `~/.dsh/env.json` 的 `env` 块。脚本启动时由 `_load_env_from_dsh_env()`
     自动补载，**即使由不继承环境的子进程运行也能生效**。兼容旧位置 `~/.claude/settings.json`
     （格式相同，`~/.dsh/env.json` 优先）。
   - **方式 B**：`setx SEMANTIC_SCHOLAR_API_KEY "你的Key"`。
     ⚠️ `setx` **只影响之后新启动的进程**；已在运行的父进程派生的子进程不会继承，需重启或手动 set。

密钥与翻译凭据也可显式传入环境变量：

- `SEMANTIC_SCHOLAR_API_KEY`（必需；**缺失时直接报错，不会退化为匿名请求**）
- `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`（翻译用，可选）
- `LITSEARCH_OUT_DIR`（可选，覆盖默认输出目录）

### 3.2 Phase 1 驱动脚本模板

**必须捕获 `ApiUnavailableError` 并立即向用户汇报，绝不能把失败当成"0 篇结果"。**

```python
# -*- coding: utf-8 -*-
import sys, os, json
sys.path.insert(0, r"C:\Users\lenovo\.agents\skills\literature-search")

from semantic_scholar_search import (
    SemanticScholarSearcher, filter_papers, save_filtered_results,
    enrich_quartiles, ApiUnavailableError,
)
from relevance_scorer import RelevanceScorer

OUT = r"F:\Agent\DeepseekHarness\AcademicResearch\记录\<日期>_<主题>"
QUERIES = ["...", "..."]        # 3 到 6 组英文关键词

searcher = SemanticScholarSearcher()
merged = {}
try:
    for q in QUERIES:                              # 串行，不要并发
        for p in searcher.search_paginated(
                query=q, total_limit=100,
                publication_types=["JournalArticle", "Review"]):
            merged.setdefault(p.paper_id, p)
except ApiUnavailableError as e:
    # 首次 + 2 次重试（各等 10s）均失败；已自动暂停，不再重试、不再匿名降级
    print(f"[FATAL] {e}")
    # 【必做】保全部分结果。必须"合并"两个来源，禁止用 `or` 短路：
    #   merged              = 已成功完成的查询结果
    #   collected_partial() = 中断查询在失败前已取回的结果
    # 若写成 `collected_partial() or list(merged.values())`，一旦有部分结果
    # 就会整批丢掉 merged —— 表现为"报告说取回 300 篇、落盘文件里只有 100 篇"。
    combined = dict(merged)
    for p in searcher.collected_partial():
        combined.setdefault(p.paper_id, p)
    if combined:
        save_partial(list(combined.values()), OUT)   # 自行落盘 s2_partial.json/.txt
    searcher.save_search_report(OUT, prefix="检索完整性报告_S2失败")
    print("[ACTION] 立即向用户提出二选一问题：1) 降级使用 Crossref 备用源；"
          "2) 持续等待 3~5 分钟后重试（置 long_wait_on_failure=True 重跑）。"
          "注意：这是服务端限流封锁，不要说成'密钥失效'。")
    raise SystemExit(2)

papers = list(merged.values())
# ... 筛选 → enrich_quartiles → RelevanceScorer().rank(query, papers) → 翻译 → 保存

# 【必做】落盘检索完整性报告，并据此如实汇报（见第 5 节）
searcher.save_search_report(OUT)
rep = searcher.search_report()
if not rep["complete"]:
    print(f"[WARN] 结果不完整：失败 {rep['queries_failed']} / 截断 {rep['queries_capped']}，"
          f"汇报时必须说明，不得声称'已完整检索'。")
```

### 3.3 Phase 2

```bash
python paper_details.py <paper_id> [<paper_id> ...]
python paper_details.py --json 文献检索_筛选结果_xxx.json
python paper_details.py --file ids.txt
```

失败时同样以退出码 2 结束，并提示向用户汇报。

### 3.4 备用源 Crossref（**必须先获用户同意**）

**硬性前置条件**：Semantic Scholar 不可用时，**先向用户汇报失败情况，由用户决定是否改用 Crossref**。
未经同意擅自换源属于违规操作，脚本带门禁会直接拒绝（抛出 `UserApprovalRequired`，CLI 退出码 3）。

```bash
python crossref_fallback.py --approved-by-user --out "<归档目录>" \
  --keywords "nonlinear,contact,debonding,delamination,harmonic" \
  "breathing crack nonlinear vibration damage detection" \
  "contact acoustic nonlinearity interface debonding"
```

编程调用（`approved_by_user=True` 必填）：

```python
from crossref_fallback import search_crossref, save_crossref
papers = search_crossref(QUERIES, per_query=1000, max_records=3000,
                         from_year=1990, keywords=KWS, approved_by_user=True)
save_crossref(papers, OUT, prefix="crossref", min_rel=3)
```

#### 3.4.1 Crossref 的硬性局限（必须在汇报中同时告知用户）

| 局限 | 说明 |
|---|---|
| **只能检索到有 DOI 的文献** | 无 DOI 的会议论文、区域性期刊、技术报告、标准等**一律检索不到**，存在系统性覆盖缺口 |
| 摘要覆盖率低 | 实测 583 篇中仅 **28.1%** 有摘要，多数条目只能靠标题判断相关性 |
| 无"确实未命中"概念 | `query` 是相关性排序检索，总会返回"最相关的前 N 条"，几乎不会返回 0 条 |
| 覆盖偏倚 | 偏向有 DOI 的英文期刊；非英语文献、非 DOI 会议文献覆盖弱 |

#### 3.4.2 Crossref 的检索机制与分页能力（实测）

| 机制 | 说明 |
|---|---|
| **检索方式** | **不是关键词 AND 匹配，而是相关性排序检索**。底层为 Elasticsearch，把语料按相关性打分后返回前 N 条，**没有相关阈值**。<br>⚠️ **这是 Crossref 的特性，与 Semantic Scholar 恰好相反** —— S2 是关键词（词法）检索，见 §3.5 |
| 检索字段 | `query` / `query.bibliographic`（书目，推荐）；另有 `query.title`、`query.author`、`query.container-title`、`query.affiliation` 等限定字段 |
| 精确过滤 | `filter=` 是**结构化等值过滤**（类型、年份、ISSN、是否有摘要等），与 `query` 相互独立 |
| `rows` | 单次返回条数，**上限 1000**（实测 `rows=1000` 成功返回 1000；`rows=2000` 报 400，提示须 ≤ 1000） |
| `cursor` | 深分页游标：首次传 `cursor=*`，之后用返回的 `next-cursor` 继续，**可翻越 1000 条上限**。**必须同时显式传 `sort=relevance`**，见下方警告 |
| `total-results` | 该 `query` 下被排序的语料条数。实测某查询为 **379,102** 条，若只取 100 条则仅占 0.026% |

**结论**：分页深度**完全可以由自己定义**（`rows` ≤ 1000，或 cursor 翻页）——只取第一页是脚本参数写死所致，不是接口限制。

#### 3.4.3 ⚠️ cursor 分页的致命陷阱（2026-09 实测，务必遵守）

**使用 `cursor=*` 时必须显式传 `sort=relevance`；漏掉它会让返回结果彻底失去相关性排序。**

实测对照（同一 query、同一 rows=100）：

| 取法 | 前 100 条中相关条目 | 内容特征 |
|---|---|---|
| 无 cursor | 44 / 100 | 完全对题（contact acoustic nonlinearity 等） |
| `cursor=*`（**漏 sort**） | **1 / 100** | 牙种植体界面、超声心动图、白细胞活化等**完全无关**内容 |
| `cursor=*` + `sort=relevance` | 44 / 100 | 与"无 cursor"首页标题集合**完全相同** |
| 无 cursor，`rows=1000` | 280 / 1000 | 相关性正常 |

**判定特征**：若深分页结果的"含摘要比例"异常偏低（实测错误结果仅 1.8%，正确结果约 28%），
即说明相关性排序已失效，必须检查是否漏传 `sort=relevance`。

`crossref_fallback.py` 已固化该参数；**任何重写分页逻辑的改动都不得删除它**。

### 3.5 Semantic Scholar 的检索机制（2026-09-11 实测）

> ⚠️ **本节与 §3.4.2 的 Crossref 机制恰好相反，不要混用两者的心智模型。**
> **S2 = 关键词（词法）检索**；**Crossref = 相关性排序检索**。

#### 3.5.1 是关键词检索，不是语义检索

| 证据 | 内容 |
|---|---|
| 官方/指南口径 | `/paper/search` = "Relevance-ranked paper search"，**"plain text only. No advanced boolean syntax."** |
| 中文实测 | 查询 `脱粘 非线性 振动`，返回的是**标题中含中文字符**的文献 → 词法匹配，非跨语言语义理解 |

注意区分：S2 确实有 SPECTER2 向量，但那是**可请求的字段**（`embedding.specter_v2`）与
Recommendations API 的机制，**不是** `/paper/search` 的匹配方式。

#### 3.5.2 ⚠️ 没有布尔语法

`AND` / `OR` / `NOT` / 引号短语 / 通配符 **全部无效**，会被当作普通文本。

| 能力 | 端点 |
|---|---|
| 纯文本关键词（**当前使用**） | `/paper/search` |
| `+` AND、`\|` OR、`-` 排除、`""` 短语、`*` 前缀、`~N` 编辑距离 | `/paper/search/bulk` |

需要布尔控制、排序，或超过 1000 条结果时，升级到 `bulk`。

#### 3.5.3 匹配范围 = 标题 + 摘要（已实测确认）

**结论：摘要被索引。** 因此查询词可以放心包含只在摘要中出现的技术/方法术语
（如 `impedance`、`thermography`），有助于提高召回。

**验证方法（可复现）**：取一篇已知论文的摘要，挑出**逐字只存在于摘要、标题中完全没有**的词组
（实测用 `monocity valid certain size limit`；其中 `monocity` 疑似原文拼写错误、泛用性极低），
用该词组检索 —— 目标论文**排在第 1 位**，证明摘要参与了匹配。

> **方法论提醒（此结论曾一度测错）**：此类验证**必须带对照组**。
> 实测中先用单词 `quantification`、`monocity` 测试，均未召回目标论文，一度误判为"摘要未索引"；
> 真实原因是这些词过于泛用（结果集达 293 ~ 206 万条），
> **泛用查询下的"缺席"不构成证据**。只有"逐字唯一的词组 + 结果集足够小"才有效。

#### 3.5.4 多词的作用是"提高相关度"，不是严格求交

| 查询 | 结果数 | 目标论文 |
|---|---|---|
| `breathing type debonding stiffened panel`（标题词） | 43 | **rank 1** |
| `monocity valid certain size limit`（摘要唯一词组） | **538** | **rank 1** |

第二行是关键：若为严格 AND，逐字唯一的词组应只返回 1 条。返回 538 条说明引擎**按词命中程度排序**，
全命中的排最前，但**不是硬性过滤器**。

> ⚠️ 上表用的是 `total` 字段，而 `total` 本身不可信（见 3.5.5）。
> 真正决定候选池大小的是**实际返回条数** —— 它随查询变长断崖式下跌，见 3.5.4.1。

**实践含义**：多加精准词能有效提升准确度，但不要把它当作精确筛选器。

#### 3.5.4.1 ⚠️ 查询词一长，**实际返回量**断崖下跌（2026-09-18/20 实测，硬性）

同一次会话内，对照组与实验组**同时**请求（`limit=100`，均带
`publicationTypes=JournalArticle,Review`）：

| 查询 | 词数 | 实际返回 |
|---|---|---|
| `external thermal insulation composite system non-destructive testing` | 7 | **100** |
| 上一行 + `debonding detection` | 9 | **11** |
| `impact echo ultrasonic acoustic ground penetrating radar insulation debonding detection` | 13 | **6** |
| `infrared thermography building facade insulation defect detection` | 7 | **92** |
| 上一行 + `wall` `delamination` | 9 | **6** |
| `active thermography delamination detection building facade cladding` | 7 | **7** |

关键读数：**第 2 行只是第 1 行加了两个词，返回量从 100 掉到 11。**
所以"多写几个词让检索更精确"在这里是**反效果**：越精确越接近 0 条。
最后一行说明**不只是词数** —— 生僻词的组合同样让返回量锐减。

**实践含义（硬性，2026-09-20 用户定稿）**：

- **单条查询最多 5 个关键词**。这不是建议 —— 代码里 `check_keyword_count()`
  会直接抛 `ValueError`（`search_pipeline.run(strict=True)` 默认开启）。
- **不做探针轮**。早期做法是"先发 16 条查询试词、再挑 9 条正式跑"，
  实测代价很大：探针 21 次调用，其中 9 条与正式检索**重复问了 S2 两遍**、
  7 条白花；更糟的是 2 分钟内 21 次请求很可能把 key 推进了 429 封锁期，
  导致紧接着的正式检索第 2 条查询就被连封三次、整轮报废。
- **改用固定 3~4 轮微调变体**：轮的"微调"本身就承担了试探作用，
  跑完看逐轮的 `returned` 数即可判断哪一轮措辞不好（`< 50` 会打警告）。
- 候选池按"返回条数"累加。2026-09-18 实测：3 条长查询合计只拿到 **23 篇**（去重后 21）；
  2026-09-20 换成 9 条短查询拿到 **660 篇** —— 差一个数量级，与限流无关。
- 但**词写得越少，精度越低**：5 词查询会把大量泛领域论文一起召回。
  所以「主题闸门 + 人工复核」（§3.5.4.2）在 5 词方案下**更加不可省略**。

#### 3.5.4.2 ⚠️ BGE 是**词面**相似度，不等于语义相关度（2026-09-20 实测，须配主题闸门）

`relevance_scorer.py` 的打分是「标题向量 与 查询词向量 的余弦相似度**均值**」，
它奖励的是**用词重合**，不是"这篇论文是否真做这个题目"。实测后果：

| 现象 | 实例 |
|---|---|
| 纯综述排到第 2 | `External Thermal Insulation Composite Systems (ETICS) from Industry and Academia Perspective`（BGE 0.6480，全文不含任何无损检测内容） |
| 完全无关领域混入 | 电气设备热像、钢管道保温层、飞机电缆绝缘层、运动生理学热像 |
| 最对题的被压低 | `GPR 智能识别外保温层粘结状态` 排 18（0.6044）、`保温层成像雷达` 排 25（0.5839）、`ETICS 脱粘非线性振动` 排 33（0.5621） |

**因此：BGE 排序只能当候选排序，不能直接当最终交付。** 必须在排序之后加两道：

1. **主题闸门** —— 已固化为 skill 模块 **`topic_gate.py`**（用户 2026-09-20 定稿）。

   ⚠️ **闸门是按主题配置的，不是一套词表打天下。**
   内置的 `building-envelope-ndt` 只覆盖「建筑围护结构/外保温无损检测」；
   实测拿它去判别的题目：脱粘界面非线性振动 **0/4**、FEM+神经网络 **0/3**、
   光纤外立面检测 **0/3**（合计 3/13）—— 整套词表只对得上一个题目。

   **每次检索都要为本次题目准备一条 profile**，存进同目录 `gate_profiles.json`
   （`save_profile()` 或 `build_profile()` 写入）。

   **配置尺度（用户指定）：2~3 个闸门（轴），每闸门约 50 词。**

   ```
   通过 = 命中【全部】闸门（轴）
   ```

   ⚠️ **没有黑名单**（用户 2026-09-20 定稿）。排除非目标领域靠**选好轴里的词**，
   不再加排除表 —— 机制越多、误杀面越大。
   实测：去掉黑名单后，同一候选池通过数由 109 → **120**（+11），
   因为黑名单本身也在误杀（例：`vehicle` 命中 `Unmanned Aerial **Vehicle**`，
   而无人机恰恰是建筑巡检的合法平台）。

   轴不要超过 3 个 —— 每多一轴，"必须全中"的合取更紧，误杀风险越高。
   建议两轴：`方法/信号` + `对象/领域`。

   **已有的三套配置**：

   | id | 轴 | 每轴词数 |
   |---|---|---|
   | `building-envelope-ndt`（内置） | 检测方法 ∧ 对象领域 | 53 + 57 |
   | `debonding-nonlinear-vibration` | 对象现象 ∧ 方法信号 | 43 + 56 |
   | `fem-neural-network` | 数值方法 ∧ 学习方法 | 46 + 44 |

   **实测效果（同一 660 篇候选池）**：
   最初的三条件合取闸门 37 篇 → 692 词巨表 + 黑名单 109 篇 →
   **2 闸门 × 约 55 词、无黑名单 = 120 篇**，
   相对最初**放回 83 篇、漏掉 0 篇**。
   **110 个词的闸门打败了 692 个词的巨表**，而且机制更简单、不新增误杀。

   ⚠️ **没有黑名单的已知取舍**：电气绝缘、管道保温这类同名标题会一并放行
   （它们的标题里确实有 `insulation`）。实测它们 BGE 分普遍低于真领域论文
   （`Gas Insulation Switch` 0.6096，而真领域论文前排 0.62~0.66），
   所以由后续排序 + 人工复核兜住。这是"不要误杀"取向下的自觉选择。

   ⚠️ **短词（≤5 字符）必须用词边界匹配** —— 纯子串会把 `re`**`nde`**`ring` 当成 `nde`、
   `w`**`all`**`et` 当成 `wall`，实测因此把 3 篇"ETICS 抗冰雹/耐久性"误放进闸门。
   （实现上给了可选复数 `s?`，`wall` 即覆盖 walls。）

   ⚠️ **词干别当词边界词用**。实测：把 `facad` 当词位时，`facade`/`facades`
   **都匹配不上**（`facad` 后面还跟着 `e`），一口气误杀 5 篇幕墙论文。
   正确写法是完整词 `facade`（长度 >5 会自动走子串匹配，覆盖单复数与 `façade`）。

   ⚠️ **匹配前先折叠重音**（`façade` → `facade`），一个词位就能覆盖两种拼写。

   **过窄自检**：`apply_gate()` 在放行率低于阈值（默认 ≥20 篇且 ≥10%）时返回告警，
   提示"主题配置过窄、正在误杀"，而不是"该领域没有相关文献"。
   交付前必须先人工翻看 rejected 前若干条。

2. **人工复核**：闸门后仍可能有泛领域热像（幕墙污渍、相机选型评估）排在
   领域专指论文之前。按既定做法人工调整并**留痕说明**，
   **不得改动打分数值**；闸门配置与真实分数须落盘（如 `主题闸门_v3对比.json`）供回溯。

调试单个标题：`python topic_gate.py --list-profiles`、
`python topic_gate.py --explain "标题" --profile <id>`，
或用 `topic_gate.explain(标题, profile)` 打印逐轴命中情况。

#### 3.5.5 ⚠️ `total` 字段不可信 —— 禁止用于覆盖率汇报

实测（**可复现**，两次重复测量结果一致）：

| 查询 | `total` |
|---|---|
| `debonding` | 27,604 |
| `debonding nonlinear` | **1,803,131** ← 加一个词涨 **65 倍** |
| `debonding nonlinear vibration` | 62,460 |
| `debonding nonlinear vibration interface` | 2,633 |
| `debonding nonlinear vibration interface stiffened` | 31 |

加词导致结果暴涨 65 倍，逻辑上不可能是 AND；且重复测量稳定复现（1,803,131 / 1,803,134），
**不是随机噪声**。官方文档亦明确说明 `total` 是**近似值**，不应视为精确的语料计数。

**因此：绝不能用 `total` 得出或汇报"某领域共有 N 篇文献"。**

#### 3.5.6 查询词写法建议

| 做法 | 说明 |
|---|---|
| ✅ 用 3~6 个精准的领域英文词 | 标题用词与摘要用词**都有效** |
| ✅ 可包含方法/技术术语 | 因摘要被索引，能提升召回 |
| ❌ 不要写布尔语法 | 会被当普通文本 |
| ❌ 不要用 `total` 报覆盖率 | 数值不可信 |
| ⚠️ 中文可查但噪声大 | 实测 `脱粘 非线性 振动` 的首条结果与查询完全无关 |
| 🔁 需要精确控制 / 大规模召回 | 改用 `/paper/search/bulk` |

**对现有代码的结论**：`semantic_scholar_search.py` 一直传纯关键词串、且从未使用 `total` 做汇报，
**用法本就正确，无需改动**。本节只是把机制补成明文，避免后人（或 Agent）按 Crossref 的心智模型误用。

### 3.6 论文元数据表：一次性落盘 + 按需读取（`paper_table.py`，v1.6.0 新增）

**要解决的问题**：检索结果条数一多，把整份列表读进上下文代价极高。
实测：565 篇全量 JSON 约 **10.6 万 token**；Crossref 全量 15,370 篇约 **243 万 token**
（远超单次对话上下文，物理上读不进来）。

**做法**：把所有论文元数据一次性写进**一个** xlsx，之后只按需读取其中的一小部分。

```python
# 驱动程序里：一次性落盘（papers 可以是 PaperInfo 列表，也可以是 dict 列表）
from paper_table import save_papers_xlsx
save_papers_xlsx(papers, OUT, prefix="论文元数据")
```

```bash
# 也可从既有 json 直接生成
python paper_table.py --xlsx <目录>/论文元数据.xlsx --from-json <某结果>.json
python paper_table.py --xlsx <目录>/论文元数据.xlsx --from-json <某结果>.json --top 300   # 自定义篇数
python paper_table.py --xlsx <目录>/论文元数据.xlsx --from-json <某结果>.json --top 0     # 不限篇数

# 之后按需读取，输出刻意做得很小
python paper_table.py --xlsx <表> --stats                     # 只出统计
python paper_table.py --xlsx <表> --filter --year-min 2020 --min-citations 10 --limit 20
python paper_table.py --xlsx <表> --filter --zone 1 --select 标题,DOI,年份 --limit 30
python paper_table.py --xlsx <表> --filter --has-abstract --out <筛后.xlsx>
python paper_table.py --xlsx <表> --selected                  # 读用户勾选的「选中」列
python paper_table.py --xlsx <表> --enrich-zone              # 查中科院分区并回填「中科院分区」列
python paper_table.py --xlsx <表> --enrich-zone --refresh    # 连已有分区一起重查
python paper_table.py --xlsx <表> --enrich-zone --quiet      # 只出一行统计（省 token）
```

**🪙 省 token：`--quiet` 与 `--top-n`（直接出结果，不经过 Agent 阅读）**

数据本体在脚本里流转，**只有 stdout 进上下文**。实测各操作的 stdout 规模：

| 操作 | 行数 | ≈token |
|---|---|---|
| `--stats`（565 行表） | 57 | **629** |
| `--filter` 默认 `--limit 20` | 26 | **1159** |
| `--filter --limit 5` | 11 | **350** |
| **`--filter ... --top-n N --out <文件> --quiet`** | **2** | **≈47** |
| 对照：565 行**整表**若进上下文 | — | **≈96,988** |

**"只要一区、相关度最高的前 10 篇"用一条命令直接出结果，不必让 Agent 读论文内容**：

```bash
# 表本身已按 BGE 得分降序，所以 --top-n 取的就是"最相关的前 N 篇"
python paper_table.py --xlsx <表> --filter --zone 1 --top-n 10 \
    --out <一区前10.xlsx> --quiet
# 输出仅两行：
#   [QUIET] 筛前 553 | 中科院分区∈['1']区排除 539 | 筛后 14 | 取前 10
#   [SAVE] 已导出 10 条 -> ...
```

也可以按别的列排序后再取：

```bash
python paper_table.py --xlsx <表> --filter --sort-by 引用数 --desc --top-n 10 \
    --out <引用数前10.xlsx> --quiet
```

- `--top-n N`：筛选后**只取前 N 条**（`--limit` 只管显示，不管导出；两者不同）
- `--quiet`：**不打印任何论文明细**，只出一行统计（`--enrich-zone` 也支持）
- `--sort-by 列名 [--desc]`：按指定列排序（数值列按数值排，空值始终排最后）

**📋 只要某几类信息：`--select` 选列（同时作用于显示与导出）**

```bash
# 只要标题 -> 一行一个标题的纯文本
python paper_table.py --xlsx <表> --filter --select 标题 --out 标题.txt --quiet

# 只要 标题 + 期刊 + 分区 -> 正好 3 列的 xlsx
python paper_table.py --xlsx <表> --filter --select 标题,期刊,分区 \
    --out 标题_期刊_分区.xlsx --quiet

# 只要 标题 + 分区 -> csv
python paper_table.py --xlsx <表> --filter --select 标题,分区 --out 标题_分区.csv --quiet

# 只想看一眼标题，不导出
python paper_table.py --xlsx <表> --filter --select 标题 --limit 30
```

- **导出格式按扩展名自动决定**：`.txt`/`.md` → 纯文本（**单列时一行一个值**）；
  `.csv` → CSV（带 BOM，Excel 打开中文不乱码）；其它 → xlsx。
- `--select` **同时作用于显示和 `--out`**（v1.9.1 修正：此前只影响显示，
  导出仍给全部列，与 `--limit`/`--top-n` 是同一类缺口）。
- 选到不存在的列会**直接报错**，不会静默给空表。
- 可用列名：`序号 / PaperID / DOI / 标题 / 中文标题 / 作者 / 年份 / 期刊 / 分区 / 引用数 /
  BGE分数 / 是否期刊 / 是否会议 / 是否预印本 / PDF链接 / 摘要 / 中文摘要 / 选中`，
  以及来源特有字段（如 `文献类型`、`ax_fiber`）。不确定时先跑 `--stats` 看"字段："那一行。

**实测（6 行表）**：只要标题 → `6 条 × 1 列` 的 txt；标题+期刊+分区 → `6 条 × 3 列` 的 xlsx，
列名正好 `['标题','期刊','分区']`；标题+分区 → 2 列 csv。

**推荐的完整流程（用户约定的标准用法）**：

```
① 设 3 组英文关键词，**每组取「目标篇数」那么多**
     （页数 = 目标 ÷ 100 向上取整；目标 200 → 每组 2 页 = 200 篇；目标 300 → 每组 3 页 = 300 篇）
     → 候选池最多 = 组数 × 目标篇数（200 目标 → 最多 600）
② **BGE 每组独立评分**（`rank_groups`）→ 合并去重 → 全局按分降序 → 取前 200
③ save_papers_xlsx() 一次性落盘成一个 xlsx
④ 之后按需读，绝不整表进上下文：
     --stats                      看全局（含字段填充率）
     --filter --select 标题,...    看标题
     --filter --select 摘要        看某几篇摘要
     --enrich-zone                查中科院分区并写回表里（走只读的 searchts read）
     --selected                   读用户在表里勾选的「选中」列，进 Phase 2
```

**⚠️ 第 ② 步的排序规则（用户明确要求）**：

- **每一组关键词只给它自己检索到的文献评分**，绝不能用别组的关键词去给本组文献打分；
- 三组各自得到一组分数后，**合并去重、按分数全局降序**，取前 `total_limit` 篇；
- 一篇论文若被多组检索到，取它在各组中的**最高分**。

```python
from search_pipeline import run

# 一次调用做完：检索 -> BGE 每组独立评分 -> 合并排序 -> 落盘 Excel
res = run(
    ["query one", "query two", "query three"],   # 3 组英文关键词
    OUT,
    target=200,                                   # 入表目标篇数（每组自动取 200 篇 = 2 页）
    prefix="论文元数据",
)
# 返回后【请让进程结束】—— BGE 占的内存只有进程退出才归还（见下）
```

命令行等价写法：

```bash
python search_pipeline.py --out <目录> --target 200 \
  "query one" "query two" "query three"
python search_pipeline.py --out <目录> --target 200 --no-bge "q1" "q2" "q3"   # 不加载 BGE
```

**⚠️ 内存生命周期（2026-09-13 实测，必须遵守）**：

| 状态 | 进程内存 |
|---|---|
| 什么都没加载 | 30 MB |
| `import torch` + `sentence_transformers` | **732 MB** |
| 再加载英文模型 | 865 MB |
| `del` 引用 + `gc.collect()` | 858 MB（**只还了 7 MB**）|
| 清 transformers 内部缓存 | 858 MB（无变化）|
| Windows `SetProcessWorkingSetSize` 修剪工作集 | 1 MB（**假象**：再分配内存后回到 250 MB）|

**结论：进程内无法把 torch 占的内存真正还给操作系统，只有进程退出才能全部释放。**

因此本流程的用法是：
1. **一个进程做完整个检索任务**（`run()` 一次调用完成检索→评分→排序→落盘），
   BGE 只加载一次、**整个任务期间持续激活、不重复加载**；
2. **调用返回后让进程结束** —— 内存随进程退出全部归还，不长期占用；
3. **不要**在长驻进程里反复调用再指望 `unload()` 省内存；也不要为此做常驻服务
   （那反而会把内存一直占着）。

另外：论文检索**固定用英文分支**（`lang="en"`）。实测即使查询词是**纯中文**，
也只加载 `bge-base-en-v1.5`，不会把 780 MB 的中文分支拉起来。

**⚠️ 澄清：「关掉 torch」不影响别的程序（2026-09-13 实测）**

**torch 不是全局开关，而是每个 Python 进程各自 `import` 的一份库。**
本流程所在进程退出时，操作系统只回收该进程的内存，
**不会触碰**知识库常驻服务那个进程里的 torch 与中文模型。

```
实验前 : 端口 8767 监听中，PID=25604，832 MB；查询正常（命中 5，返回 9 条）
实验中 : PID 14992  865 MB   ← 论文检索进程（英文 BGE）
         PID 25604  832 MB   ← 知识库常驻服务（中文 BGE）
实验后 : 端口 8767 仍监听中，PID 仍是 25604（未被重启），832 MB；查询结果完全一致
```

**两个 PID、两份 torch、两份模型，互不影响。**
但**内存总量会叠加**：知识库常驻 832 MB + 论文检索峰值 865~1260 MB ≈ **1.7~2.1 GB**。
想腾内存可临时 `python search_reflect.py --stop-daemon`（释放约 825 MB），
下次用知识库时它会自动拉起。

**怎么看/怎么单独关掉英文侧**：`python bge_status.py`

```
【知识库侧（中文 BGE，长期驻留）】1 个
  PID  25604      832 MB  知识库常驻服务（中文分支），端口 [8767]
【论文检索侧（英文 BGE，跑完即退）】0 个
  （无）
  英文 BGE     :       0 MB   ← 当前没有英文 BGE 在跑（已关）
```

判据说明：**真正加载了 BGE 的进程内存 ≥ 700 MB**（光 `import torch` 就要 700+），
所以脚本用"路径匹配 + 内存 ≥ 300 MB"双重判定，避免把同目录下的普通脚本误认成 BGE 进程。

关闭英文侧的三条路：
1. **正常**：什么都不做，论文检索进程跑完自己退出；
2. **卡住**：`Stop-Process -Id <论文检索侧的 PID>`（只杀那一个，别动知识库侧）；
3. **反过来腾内存**：停掉知识库侧 `--stop-daemon`。

**为什么不能按查询顺序切片**（v1.7.3 的实测教训）：若改成"查询 1 的全部 → 查询 2 的全部 → …"
再取前 200，**查询 3~6 的结果会全部被丢弃**，连它们各自排第 1 的论文都进不了表。
候选池建到 600 也白建。BGE 全局排序从根上避免了这个问题。

**成本预期**：BGE 首次加载 **约 100 秒**（CPU，`bge-base`）；
之后对几百条标题编码 + 排序只需**零点几秒**。知识库那个常驻服务里的模型在它自己的进程内，
**跨进程用不了**，所以每次新开进程都要重付这 100 秒。

**表结构**：标准列在前（序号/PaperID/DOI/标题/中文标题/作者/年份/期刊/分区/引用数/BGE分数/
是否期刊/是否会议/是否预印本/PDF链接/摘要/中文摘要/选中），来源特有的字段自动追加在后
（如 stageA 的 `ax_fiber` / `ax_build` / `ax_method`）。已启用首行冻结与自动筛选。
最后一列「选中」留给用户在 Excel 里打勾，再用 `--selected` 取回。

**三条硬约束（均为实测踩坑所得，不得回退）**：

1. **`PaperID` / `DOI` 列写入时必须强制文本格式**。否则 Excel 会把长 ID 显示成科学计数法，
   事后从外观分辨不出来，Phase 2 会整批查不到。实测已验证可防（40 位 hex、40 位纯数字、
   前导零、DOI 全部保真）。
2. **筛选前必须校验字段**，且分两级：
   - 列不存在 → 报错退出；
   - **列存在但整列为空 → 同样报错退出**。
   
   第二级尤其重要：实测中数据里没有 `quartile`（分区在后续步骤才查），
   「只要 Q1」把 565 篇**静默全部排除**，结果看起来像「该领域没有 Q1 论文」。
   这是最危险的静默失败，`paper_table.py` 现在会直接报错并说明原因。
3. **xlsx 只是视图，json 仍是权威源**。`paper_table.py` 不改变既有 json/txt 产出，
   Phase 2 仍以 json 为准；不要把 xlsx 当作唯一数据源。

**用 `--stats` 先看字段填充率**：输出里会标出哪些字段大量缺失
（实测阶段 B 之前 `分区` 为 0/565），据此判断某个筛选条件是否根本不可用。

---

### 3.7 摘要补全（`abstract_fill.py`，v2.0 新增）

**问题**：S2 检索响应自带摘要，但覆盖率约 **84%**；缺的几乎全是 Elsevier——它自 2019 年起
不向 Crossref / OpenAlex / S2 等第三方平台提供摘要。实测 65 篇调研缺 13 篇 = Elsevier 10、
IEEE 1、T&F 1、Springer 1。

**降级顺序（每档都实测过）**

| 顺序 | 源 | 对 Elsevier | 备注 |
|---|---|---|---|
| 1 | Crossref | ❌ 0 篇 | 不上传摘要 |
| 2 | OpenAlex | ❌ 0 篇 | 200 但 `abstract_inverted_index` 为空；有值时按位置还原 |
| 3 | **Elsevier API** | ✅ 9/10 | 唯一能拿 Elsevier 摘要的通道 |
| 4 | PubMed（按 DOI 反查 PMID） | — | IEEE 等被 PubMed 收录的刊物常有完整摘要 |
| 5 | searchts 渲染 DOI 落地页 | ❌ | Springer / T&F 实测有效（T&F 能绕 Cloudflare）；ScienceDirect 403 |

**Elsevier 通道的三个要点**

1. 端点 `api.elsevier.com/content/abstract/doi/{DOI}`，取
   `abstracts-retrieval-response.coredata."dc:description"`。
2. **无权限时仍返回 HTTP 200**，只是没有 `dc:description`（只剩 9 个标识符字段）。
   判据必须是"有没有这个字段"，**不能看状态码**；否则会把"校外无权限"误记成"这篇没摘要"。
3. **授权按机构 IP，不按 key**（官方：API key identifies the calling application but does not
   grant data or content access）。同一 key 换到境外代理即失效（实测：校内 24 个字段含摘要，
   境外代理 22 个字段无摘要）。

**key 从哪来**：<https://dev.elsevier.com> → Create API Key（勾 API Service Agreement；
要全文/TDM 再勾 TDM Provisions）。需机构订阅 ScienceDirect 或 Scopus，且调用时出口 IP 属该机构。
**存放**：`~/.dsh/env.json` 的 `env.ELS_API_KEY`（与其它 key 统一），代码只从环境变量或该文件读，
**不得写进代码或 skill 目录**。

**用法**：`python abstract_fill.py 论文元数据.xlsx [--limit N] [--refresh] [--quiet]`，
回填「摘要」列并写「摘要来源」；逐篇打印各源失败原因。实测把覆盖率从 80% 提到 **98%**。

### 3.8 会议论文集识别（`venue_rules.py`，v2.0 新增）

**为什么需要**：S2 的 `publicationTypes` 挡不住会议论文集。实测 553 篇里标 `Conference` 的
仅 27 篇，**另有 99 篇刊名明显是会议论文集**却标 `JournalArticle`：

```
e-Journal of Nondestructive Testing            47 篇
Italian National Conference on Sensors         16 篇
Journal of Physics: Conference Series           8 篇
E3S Web of Conferences                          3 篇
INTER-NOISE / NOISE-CON                         2 篇
BigData Congress                                3 篇
```

**规则**：`CONF_VENUE_PATTERN` 命中即判会议；`KEEP_VENUE_PATTERN` 白名单优先放行
（`Proceedings of the Institution of Mechanical Engineers`、`... of Civil Engineers` 是**正规期刊**，
看到 proceedings 就排除会误杀）。

**接入点**：落盘时写「疑似会议」列；`filter_papers(paper_type="journal_only")` 已把
`is_conference_venue(venue)` 并入判据。

## 4. 注意点

### 4.1 失败重试与上报策略（v1.4.0，硬性规则）

`_make_request()` 的重试策略：

```
首次尝试失败 → 等 10s → 第 2 次尝试
             → 等 10s → 第 3 次尝试
共 3 次请求、累计等待约 20 秒
仍失败 → 抛 ApiUnavailableError，暂停检索，向用户提问二选一
```

**三次全败后【必须】向用户提出这个问题，不得自行决定：**

> Semantic Scholar 已连续失败并暂停检索。请选择：
> **1) 降级使用 Crossref 备用源**（代价：Crossref 只能检索到有 DOI 的文献，存在系统性覆盖缺口）；
> **2) 持续等待 3~5 分钟后重试**。

用户选择 2) 时：把 `searcher.long_wait_on_failure = True`（静默 `LONG_WAIT_S = 240` 秒）后重跑驱动脚本。
它会追加**一次**尝试（共 4 次请求），仍失败则再次抛错并重新提问；不会无限重试。
`long_wait_on_failure` 默认为 `False`，**只有用户明确选择等待后才能置 True**。

计入失败并触发的状态：

- `429`（限流）
- `401 / 403`（密钥无效或被拒）
- `5xx`（服务端/网关异常，如 504）
- 网络 / 代理 / VPN 异常（`requests` 抛异常）

不计入重试：其他 4xx（如 400/404）→ 抛 `RequestFailedError`，同样记为 `failed`（见本节末的不变量 1）。

**为什么短退避必然失败（2026-09-11 实测，本策略的直接依据）：**

| 观测项 | 实测结果 |
|---|---|
| 请求间隔 **1.5 秒** | **仍会 429**（两个都失败） |
| 请求间隔 1.0 秒 | 两个都 200（**无规律**，不能据此判断安全） |
| 无间隔连发 | 第 2 个 429（1 RPS 窗口是真实且严格的） |
| **静默 60 秒后单发** | **仍 429** |
| 触发限流后自行恢复 | 约 **44 秒**后恢复 200 |
| 封锁窗口估计 | **约 60 ~ 110 秒** |
| 限流施加方 | AWS API Gateway（429 带 `x-amzn-ErrorType: TooManyRequestsException`，且缺少 200 才有的 `x-amzn-Remapped-*`） |
| 限流按什么维度 | **按 API key**（封锁期内用伪造 key 仍返回 `403`，说明先鉴权后限流，key 本身有效） |

即：**秒级退避全部落在封锁期内，三次必然全败。**继续自动重试没有意义，只有分钟级等待才可能成功，
所以"是否长等"这个决定必须交给用户。此前 v1.3.x 的 10/20/40 秒策略（累计 70 秒）恰好卡在封锁窗口内，
因此总是"三次全败后报告 key 不可用"——**但 key 其实一直是好的**，这是最需要避免的误导。

**调用方必须做到：**

1. 捕获 `ApiUnavailableError` 后**立即终止整个检索**，不得继续下一个查询；
2. **立即向用户提出上面的二选一问题**，并说明"这不等于没有相关文献"；
3. 不得自行判断为"密钥失效"——实测表明限流来自服务端封锁，key 有效。

**为什么必须这样**：旧逻辑在失败后返回 `None`，上层继续遍历剩余查询，6 个查询逐个重试会空转数分钟；若脚本再自加大退避（实测曾用 5s→15s→30s→60s→90s），单查询就要 200 秒，整体十几分钟无输出，最终还静默返回 0 篇。

兼容性：`RateLimitError` 作为 `ApiUnavailableError` 的别名保留，旧调用方仍可捕获。

**两条实现不变量（v1.3.4 修复的真实缺陷，不得回退）：**

1. **任何请求层失败都必须记为 `failed`，绝不可记为 `ok`。**
   - 曾出现 bug：HTTP `504`（Network error communicating with endpoint）返回 `None`，
     被上层当成"结果为空 = 查询完成"，报告写成 `[ok] … 取回 0 篇`，
     把网络故障伪装成"确实未命中"。
   - 现已规定：`429 / 401 / 403 / 5xx / 网络异常` → 按重试策略处理，预算耗尽抛
     `ApiUnavailableError`；其他 4xx → 抛 `RequestFailedError`。
     两者在 `search_paginated` 中一律记为 `failed` 并向上抛出。
   - **只有 `200` 且 `data` 为空数组，才算"查询完成且确实无结果"（`ok`）。**

2. **失败时必须保全已取回的部分结果。**
   - 曾出现 bug：`search_paginated` 翻页中途抛异常时 `return` 未执行，
     上一页已取回的文献随异常丢失，报告里的"已取回 N 篇"无法交付。
   - 现已规定：中断前已取回的结果存入 `self.partial_papers[query]`，
     调用方在捕获异常后必须用 `searcher.collected_partial()` 取出并落盘
     （见 3.2 驱动模板），否则数据仍然会丢。

### 4.2 禁止使用匿名请求

**本 skill 一律使用配置的 API Key，任何情况下都不降级为匿名请求。**

- 未配置 `SEMANTIC_SCHOLAR_API_KEY`：直接抛 `ApiUnavailableError`，提示用户配置，**不匿名兜底**。
- 密钥请求失败：走 4.1 的重试与上报流程，**不切换到匿名请求**。

原因：Semantic Scholar 的两类限制性质完全不同。

| 请求方式 | 限制性质 | 官方口径 |
|---|---|---|
| 匿名（不带密钥） | **全局共享池** | 官方 product 页：**1000 请求/秒由所有匿名用户共享**；旧发布说明另载"5000 请求/5 分钟"（2024-03 起持续下调）。无论取哪个数字，都是**与他人共用**，稳定性不可控 |
| 带密钥 | **独立专属配额** | 官方邮件与 tutorial：**1 请求/秒，跨所有端点累计** |

**因此**：带密钥出现 429，与"共享池被抢"无关。把带密钥的 429 解释为共享池问题，是错误归因；用匿名请求去"绕过"更不可取。

诊断结论（2026-09-11 实测，取代此前的推断）：

```
真密钥 + x-api-key 头，低速率   → 200
真密钥 + x-api-key 头，连发     → 429
假密钥 + x-api-key 头           → 403 Forbidden（即使我方正处于封锁期也一样）
```

- **鉴权先于限流**：封锁期内假密钥仍返回 403 而非 429 → **429 不是密钥失效，密钥本身有效**；
- 限流由 **AWS API Gateway** 按 key 施加（429 带 `x-amzn-ErrorType: TooManyRequestsException`，
  且缺少 200 响应才有的 `x-amzn-Remapped-*` 头）；
- 触发后进入**约 60~110 秒的封锁期**，期内一律 429；静默 60 秒不足以解除；
- 因此**换传参方式（Header ↔ URL）毫无意义**——v1.3.1 起已彻底移除该重试路径，
  现在只使用 `x-api-key` 请求头。

### 4.3 并发

密钥约 1 请求/秒。**同一时间只运行一个检索进程**；多个查询放进同一个脚本串行循环。开多个后台任务并行请求会成倍叠加速率。

### 4.4 "一次 200 一次 429"的含义与推论

同一请求（同 URL、同密钥、同参数）在几秒内发两次，可能一次 200、一次 429。
2026-09-11 的受控实测给出了完整机制：

| 实测条件 | 结果 |
|---|---|
| 间隔 15 秒单发 ×3 | 全部 200 |
| **无间隔连发** | 第 1 个 200、第 2 个 **429**（1 RPS 窗口是真实且严格的） |
| 间隔 1.0 秒连发 | 两个都 200 |
| **间隔 1.5 秒连发** | **两个都 429** |
| **静默 60 秒后单发** | **仍 429** |
| 距上次 429 约 44 秒 | 自行恢复 200 |

**含义与推论：**

- 限流**不是**"配额用完就一律拒绝"，而是触发后进入**约 60~110 秒的封锁期**，期内一律 429；
- 因此**不能凭一次 429 判定密钥失效**，也**不能凭一次 200 判定已恢复**；
- 间隔大小与成败**无稳定关系**（1.0 秒成功、1.5 秒反而失败），不要试图靠"微调间隔"绕过；
- 由于封锁期是**分钟级**，**秒级退避必然三次全败**——这正是 v1.4.0 把策略改为
  "三次快试失败即暂停并询问用户"的实测依据；
- 推论：重试必须有硬性次数上限，失败如实上报，绝不"再等等看"。

### 4.5 分区：只做中科院《新锐期刊分区表》

**为什么不做 JCR**：JCR（Clarivate，基于 JIF 的四分位）**没有可用的免费数据源**——
需要 Web of Science / JCR 订阅，Clarivate API 需付费，LetPub 检索页也只给 IF、CiteScore、
中科院区，**不显示 JCR 四分位**。第三方站点（journalmetrics.org 等）给出的是它们**自算**
的四分位（OpenAlex 口径），既不是 JCR 也不是 Scopus 官方 CiteScore 四分位，拿它当 JCR
汇报会出错。**故 v2.0 删除 journalmetrics 分支，只保留中科院新锐分区（1–4 区）。**

| 项 | 做法 |
|---|---|
| 入口 | `journal_quartile.letpub_zone(issn=..., name=...)` → `("1"~"4"|None, 命中刊名, 来源说明)` |
| 批量 | `letpub_zone_many([(issn, name), ...])`，**必须串行**（并发 >4 会被 LetPub 限流返空） |
| 回填 | `paper_table.py --enrich-zone`，写「中科院分区」列，值 `1区`~`4区` / `未收录` |
| 筛选 | `paper_table.py --filter --zone 1,2`；传 `Q1/Q2` 会**显式报错**（见下） |
| 缓存 | `.letpub_zone_cache.json`，版本 2（带 `ts`） |

**查询顺序**：① `searchissn=`（**精确匹配 ISSN 单元格**，有 ISSN 一律用这条）
② `searchname=`（刊名模糊匹配，缩写会漏，如 `Comput. Aided Civ. Infrastructure Eng.`）。

**ISSN 从哪来**：元数据表没有 ISSN 列（Semantic Scholar 不返回）。`--enrich-zone` 会按刊名
去重、每刊取一个 DOI，用 **Crossref 反查 ISSN**，结果缓存到 `.issn_cache.json`。实测
82 篇 → 81 本刊 → 81 次请求（首次约 1 分钟），之后零成本。

**抓取方式**：两条路径都走 `searchts read`；`requests` 直连会被 LetPub 挡回约 639 字节壳页。

**⚠️ 耗时与提速（2026-10-30 实测）**

| 路径 | 单本耗时 | 成功率 |
|---|---|---|
| 默认三级升级链（curl_cffi → Jina relay → stealth 浏览器） | **7.0~9.3 s** | — |
| **强制 `--backend curl_cffi`** | **2.6~3.6 s** | **6/6**（返回数据还更全：15 KB vs 13 KB） |

`fetch_letpub_page()` 现在先试 curl_cffi，返回 < 2000 字符（空壳页约 1.2 KB，正常页 13~15 KB）
才退回默认链。215 本刊从约 29 分钟降到约 10 分钟；81 本约 3.8 分钟。

**⚠️ 缓存毒化（真实踩坑，必须保留修复）**：LetPub 单次查询会**偶发返回空页**
（实测 CBM 一次通一次空、MSSP 连续两次空、IEEE TUFFC 空）。旧实现把 `None` 永久写进缓存，
导致 Materials、Applied Sciences、IEEE TIM、MSSP、IEEE TUFFC 全被判「未收录」——
215 本刊里 185 本中计。现在：未收录项 **30 天 TTL 后自动重查**，`--refresh` 无条件重查。

**分区覆盖率现实（汇报时必须说明）**：中科院分区只覆盖约 30% 的期刊（实测 215 本里 65 本能查到），
其余是会议论文集、区域性期刊与新刊。按篇数算，1/2 区通常只占非会议文献的 **15~20%**
（实测 480 篇 → 82 篇）。**这不是检索不力**，要提前跟用户讲清。

**破坏性变更**：`quartile_filter` 取值从 `Q1/Q2` 改为 `"1"/"2"`；`--quartile` 改为 `--zone`。
传旧值一律**报错并提示**，绝不静默返回空表（静默空表会被误读成"该领域没论文"）。

### 4.6 检索质量

宽泛关键词（如单独用 `ground penetrating radar`、`thermal insulation`）会引入大量跨领域噪声（考古地球物理、电力绝缘子、保温材料回收等）。建议：

- 查询词锚定应用场景（`building facade insulation`、`ETICS`、`building envelope`）；
- 对标题做关键词相关性打分，排序后再呈现，并说明已人工去噪。

---

## 5. 如实汇报原则（最高优先级）

**文献调研过程中的任何异常都必须如实、完整地汇报，不得隐瞒、美化或省略。** 交付结果时，必须同时说明"结果的完整性与可信度"，而不是只给一个文献列表。

### 5.1 必须如实说明的情形

| 情形 | 必须如何说明 |
|---|---|
| **完全没有检索到文献** | 必须区分两种性质：① **确实未命中**：查询正常完成、API 返回 0 条 → 说明所用关键词与筛选条件，并明确"这不等于该领域空白"；② **检索失败/未完成**：429、密钥不可用、网络/代理异常 → 明确"**无法据此判断该领域是否存在文献**" |
| **只检索到部分文献** | 说明哪些查询 `failed`（失败）、哪些 `capped`（达到抓取上限，可能仍有更多未取回），以及范围被哪些条件收窄（年份、分区、文献类型） |
| **中途换过数据源** | **换源必须先经用户同意**；说明何时、为何换源（如 S2 → Crossref），以及口径差异。使用 Crossref 时**必须明确告知：它只能检索到有 DOI 的文献**、摘要覆盖率约 28%、且只返回相关性前 N 条 |
| **分区数据缺失** | 说明多少条因分区未收录而被排除，可能漏掉本属 1/2 区的期刊 |
| **翻译失败或被跳过** | 说明是保留英文原文，而不是"没有中文标题" |
| **Phase 2 取回不齐** | 说明"取回 X/Y 篇"及未取回的 paper_id |
| **人工去噪/人工筛选** | 说明剔除标准与数量，避免用户误以为那是全部命中 |

### 5.2 强制留痕：检索完整性报告

靠记忆汇报不可靠，必须由脚本留痕。`SemanticScholarSearcher` 会记录每个查询的结果：

```python
searcher.save_search_report(OUT)     # 产出 检索完整性报告.txt / .json，必须随结果一并交付
rep = searcher.search_report()       # 结构化数据，字段见下表
```

| 字段 | 含义 |
|---|---|
| `queries_total` | 查询总数 |
| `queries_failed` | 失败的查询数（结果不完整） |
| `queries_capped` | 达到抓取上限的查询数（可能仍有更多未取回） |
| `papers_returned` | 累计取回文献数 |
| `requests_sent` | 实际请求次数 |
| `complete` | **结果是否完整**（`True` 才可断言"检索已完成"） |
| `notes` | 自动生成的"必须如实告知用户的事项" |
| `detail` | 逐查询明细：`status`（`ok` / `capped` / `failed`）、`returned`、`note` |

### 5.3 换源决策权在用户（v1.3.3 新增，硬性规则）

Semantic Scholar 失败时，**不得自行改用 Crossref 或其他源**。必须按以下顺序执行：

1. **停止**检索，不再重试、不再等待；
2. **向用户汇报**：S2 因何失败（429 / 密钥 / 网络代理）、已重试几次、耗时多少；
3. **说明换源的代价**（由用户权衡）：
   - Crossref **只能检索到有 DOI 的文献**，无 DOI 的会议论文、区域性期刊、技术报告、标准检索不到；
   - 摘要覆盖率低（约 28%），只能靠标题判断相关性；
   - 结果是相关性排序的"前 N 条"，不是穷尽枚举；
   - OpenAlex 在本环境已转付费、不可用；
4. **等待用户决定**：等网络恢复后重试 S2 / 改用 Crossref（需带 `--approved-by-user`）/ 暂停本次调研。

脚本层已设门禁：`search_crossref()` 未传 `approved_by_user=True` 会抛 `UserApprovalRequired`，CLI 未加 `--approved-by-user` 返回退出码 3，且**不会发出任何网络请求**。

### 5.4 汇报模板

**存在异常时（必须主动说明）：**

> 本次检索共 N 个查询，取回 M 篇。其中 X 个查询失败、Y 个达到抓取上限，**结果不完整**；
> 另有 Z 条因分区未收录被排除。因此目前**不能**得出"未发现相关文献"的结论，需恢复网络后补检索。

**检索正常完成但为 0 篇时：**

> 本次检索正常完成（N 个查询全部成功），在所用关键词与筛选条件下**确实未命中文献**。
> 这不代表该领域空白，可能是关键词、年份或分区限制所致。

**仅部分取回时：**

> 本次共取回 M 篇，为**部分结果**：K 个查询达到抓取上限，可能仍有更多文献未取回。

---

## 6. 容易遇到的问题（排错手册）

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `ApiUnavailableError: Semantic Scholar 连续 3 次请求均失败` | 服务端限流封锁（实测约 60~110 秒），**不是密钥失效** | **立即向用户提问**：降级 Crossref，还是持续等待 3~5 分钟后重试（`long_wait_on_failure=True`） |
| `未配置 SEMANTIC_SCHOLAR_API_KEY` | 密钥缺失 | 提示用户配置；**不要**改用匿名请求兜底 |
| 长时间无输出，随后返回 0 篇 | 旧版静默失败 | v1.3.1 已由失败上报机制修复；确认脚本捕获了 `ApiUnavailableError` |
| 脚本十几分钟不结束 | 自定义脚本用了过长退避且未上报 | 删除自定义长退避，改用 skill 内置 `RETRY_WAITS_S`（10/10，共 3 次请求，累计约 20 秒） |
| 结果大量 off-topic | 查询词过宽 | 缩窄查询词，加相关性打分与人工筛选 |
| 分区大量 unknown | 期刊未被分区库收录 | 属预期；说明局限，必要时人工补录 |
| 保存目录不符预期 | 默认落在 `F:\Agent\DeepseekHarness\Interaction` | 设 `LITSEARCH_OUT_DIR`，或按第 6 节规范传 `save_dir` |
| 翻译未生效但结果正常 | 缺 `ANTHROPIC_AUTH_TOKEN` | 属预期的降级行为，保留英文即可 |
| 技能目录出现多余 HTML 文件 | `journal_quartile.py` 调 `searchts get` 的残留 | 定期清理；分区查询完毕检查当前工作目录 |
| 目标 docx 无法覆盖保存 | 文件被 WPS/Word 占用 | 另存新文件，或请用户关闭文档 |

### 6.1 故障复盘：2026-09 脱粘界面调研卡死

**经过**：检索"脱粘界面非线性振动"时，S2 全程 429；随后改用 OpenAlex（已转付费，返回 `Insufficient budget`），再用 Crossref 成功。

**根因**：
1. 数据源限流（S2 间歇性 429）；
2. 自定义脚本用 5s→15s→30s→60s→90s 退避，单查询最长 200 秒，6 查询串行导致十几分钟无输出；
3. skill 旧逻辑无重试上限、失败静默返回空；
4. 依据"一次 URL 参数返回 200"误判限流已恢复，重启长时间重试，再次全程 429；
5. 文档未区分密钥与共享池，导致错误归因。

**已落实的修复（v1.3.1）**：

- 重试策略硬性化：首次 + 3 次重试（10s / 20s / 40s），到点即停；
- 失败显式抛 `ApiUnavailableError`，要求优先向用户汇报，禁止静默返回空；
- **禁止匿名降级**：未配置密钥直接报错；密钥失败不切匿名；
- 移除无效的 Header/URL 切换重试（同一密钥，切换无意义）；
- 新增 Crossref 备用源；
- 文档补齐"密钥 vs 共享池""一次 200 一次 429 的含义"与本复盘。

**已落实的修复（v1.4.0，2026-09-11 实测驱动）**：

- 重试策略改为**首次 + 2 次重试（各等 10s，共 3 次请求）**，到点即停；
- 三次全败后**强制向用户提出二选一问题**（降级 Crossref / 等待 3~5 分钟），
  由用户决定是否把 `long_wait_on_failure=True` 重跑；
- `REQUEST_INTERVAL` 由 1.5 秒提高到 **2.5 秒**（实测 1.5 秒仍会触发 429）；
- 纠正**失败归因**：不再报"密钥失效"，改报"服务端限流封锁（约 60~110 秒）"。
  实测证据：封锁期内伪造 key 返回 `403`（先鉴权后限流）→ key 本身有效；
  静默 60s 后仍 429、约 44s 后自行恢复 200；限流由 AWS API Gateway 施加。
- 修正 `search_paginated` docstring 中失效的"总上限 10000 篇"（实为 `offset+limit ≤ 1000`），
  新增 `RELEVANCE_MAX_RESULTS` 常量与超限告警；
- 修正模块 docstring 中"使用 URL 参数方式传递 API Key"的错误描述（实为 `x-api-key` 请求头）。

**已落实的补齐（v1.5.0，2026-09-11 检索机制实测）**：

此前文档**只写了 Crossref 的检索机制，完全没有 S2 的**，导致两种相反的心智模型极易混用。本次补齐：

- **新增 §3.5「Semantic Scholar 的检索机制（实测）」**：关键词检索而非语义检索、
  无布尔语法、匹配范围含标题+摘要（含可复现的验证方法与"泛用查询下缺席不构成证据"的方法论提醒）、
  多词为相关度提升而非严格求交、`total` 不可信（含 65 倍跳变的实测序列）、中文查询噪声、查询词写法建议；
- **修正 §3.4.2 的混淆隐患**：该处"不是关键词 AND 匹配"描述的是 Crossref，
  与 S2 机制**恰好相反**，已显式标注并交叉引用 §3.5；
- **SKILL.md 注意点新增第 10 条**（机制浓缩版，避免每次都要翻详细文档）；
- **§2.3 参数表与 `references/examples-and-details.md`** 补充 `/paper/search/bulk` 的布尔语法与分页能力，
  并标注 `total` 禁止用于覆盖率汇报。

**代码无需改动**：`semantic_scholar_search.py` 一直传纯关键词串、从未用 `total` 做汇报，用法本就合规。

> ⚠️ **以下 v1.x 记录里的 `--enrich-quartile` / `--quartile` / `Q1` 属历史口径**：
> v2.0 起已分别改为 `--enrich-zone` / `--zone` / `1`~`4` 区，直接照抄旧命令会报错。
> 保留原文只为留存当时的实测证据，**不要当作现行用法**。

**已落实的修正（v1.7.0，2026-09-13，按用户确认的流程）**：

用户确认了标准流程：**按主题检索 → 不设分区要求 → 排除会议与预印本 → 取前 200 篇
一次性落盘成一个 Excel → 之后按需读取、按需查分区回填**。据此做了 7 处改动：

1. **`journal_quartile.py` 改用 `searchts read`**（旧版用 `searchts get`，属"下载"，
   违反"下载须先经用户同意"的规则，也是技能目录 HTML 残留的根源）；
2. **分区解析改为读 `| Category | Quartile |` markdown 表格**（旧版靠 `<title>` 正则，页面改版后不可靠），
   回退到 `Subfield Quartile`；
3. **新增缓存版本号**（`CACHE_VERSION = 2`）：实测旧缓存把 `Buildings` 记成 Q2、
   而实时页面是 Q1，**v1 旧缓存一律作废**；历史分区筛选结果可能不准；
4. **新增 `paper_table.py --enrich-quartile`**（v2.0 起改名 `--enrich-zone`）：查中科院分区并回填
   （`--refresh` 可全部重查）；
5. **统一"默认 200 篇"**：新增 `DEFAULT_TOTAL_LIMIT = 200`，
   `search_paginated()` 默认值由 400 改为 200（语义是"总共取前 200"）；
6. **`DEFAULT_QUARTILE_FILTER` 由 `["Q1","Q2"]` 改为 `None`**：消除"先查分区再筛选
   就会突然砍掉大量文献"的顺序陷阱；
7. **`journal_only` 放宽为"非会议 + 非预印本"**，去掉 `not is_journal` 强制条件
   （实测确认：该条件会丢掉 S2 未返回 `publicationTypes` 的正常期刊论文）；
   另外 `ApiUnavailableError` 的提问顺序改为**首选"等待恢复"、Crossref 退为最后手段**。

**实测验证（端到端）**：6 篇测试样本完整走通
落盘 → `--stats`（分区 0/6）→ `--enrich-quartile`（Buildings Q1 / Sensors Q1 /
Journal of Building Engineering Q1 / Optical Fiber Technology Q2 / Applied Sciences unknown /
Structural Health Monitoring unknown）→ `--stats`（Q1=3 Q2=1 unknown=2）→
`--filter --quartile Q1` 正确返回 3 条。4 个既有测试套件仍全部通过；
`filter_papers` 行为单测确认：无 `publicationTypes` 的论文被保留、会议与预印本被排除、
不传 `quartile_filter` 时不按分区筛。

**v1.7.1（同日，纠正 v1.7.0 的一处不一致）**：

v1.7.0 把 `search_paginated()` 的默认值由 400 改成 200，但那个参数的语义是
**"每个查询的上限"**，不是总数 —— 也就是"总共取前 200"当时**只在文档里写了、代码没实现**。
本次做实并澄清：

- 新增 `DEFAULT_PER_QUERY_LIMIT = 100`（每个查询的上限，= 一页），
  `search_paginated(total_limit=...)` 默认值改为它；
- `DEFAULT_TOTAL_LIMIT = 200` 明确定义为**写入元数据表的篇数上限**，
  由 `save_papers_xlsx(total_limit=N)` 在合并去重后截断，并新增 CLI `--top N`（0 = 不限）；
- **候选不足上限时有多少存多少**，不补齐、不报错（用户明确要求）；
- 实测四种情况：565 篇 `--top 200` → **200 行**；6 篇 `--top 200` → **6 行**；
  565 篇 `--top 50` → **50 行**；565 篇 `--top 0` → **565 行**，全部符合预期。

**v1.7.2（同日，修掉一个会无限发请求的死循环）**：

在实测"请求几次"时发现 `search_paginated()` 有个**真 bug**：

- 循环条件 `while len(all_papers) < total_limit` 看的是**去重后**的篇数，
  而 `offset` 每轮照涨；
- 一旦 S2 跨页返回**重复结果**（去重后篇数不增长），条件永远成立 →
  **请求无限发下去，永不终止**。
- mock 实测复现：让第 2 页起返回重复 ID，`offset` 一路涨到 **4800 万**仍未停。

已加**两个安全阀**：

1. **绝对页数上限** `max_pages = RELEVANCE_MAX_RESULTS // page_size`（默认 10 页，
   对应平台 `offset + limit ≤ 1000`），到点记为 `capped`；
2. **无进展即停**：某一页没有带来任何新论文（全部与已有重复）就停止翻页并记 note。

修复后实测：重复场景**2 次请求即停**（备注"第 2 页结果全部与已有重复"）；
`total_limit=5000` 场景**10 次请求即停**（备注"已达平台分页上限 10 页"）。
4 个既有测试套件仍全部通过。

**v1.7.3（同日，修掉「候选池白建」的选择偏差）**：

用户问"候选池是什么意思、为什么需要"时顺带查出一个**设计缺陷**：

- 驱动脚本原来按「查询 1 的全部 → 查询 2 的全部 → …」合并成一个列表，再取前 N；
- 实测（6 查询 × 100 篇 = 600 候选，取前 200）：**查询 1 拿 100 篇、查询 2 拿 100 篇，
  查询 3~6 全部 0 篇** —— 连它们各自排第 1 的论文都进不了表。
- 也就是说**候选池建到 600 也没用**，多轮关键词检索的意义被完全抵消。

已新增 `paper_table.interleave_by_query()`：按**轮转**融合各查询结果
（第 1 轮取各查询第 1 名，第 2 轮取各查询第 2 名，依此类推，同时去重），
再取前 `total_limit` 篇。各查询内部仍保持 S2 自己的相关性顺序。

修复后实测：

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 6 查询 × 100 篇互不重复，取前 200 | 查询 3~6 **全丢** | 各查询约 **33 篇**，6 个头名全部进表 |
| 查询间大量重叠（600 条 → 唯一 300） | — | 取前 200 全唯一，查询 1=84 / 其余各约 23 |
| 候选不足（60 篇） | — | 返回 60 篇（有多少给多少） |
| 空输入 | — | 返回 `[]`，不报错 |

驱动脚本模板已同步更新（§3.6）；4 个既有测试套件仍全部通过。

**v1.8.0（同日，按用户指定的检索与排序方案改造）**：

用户明确了标准方案：**设 3 组关键词，每组取「目标篇数」那么多（200 目标 → 每组 2 页 = 200 篇，
3 组 → 候选池最多 600）；然后用 BGE 对每组各自评分，合并后按分全局排序取前 200。**

1. **`relevance_scorer.py` 模型由 `bge-small-*` 换成 `bge-base-*`**，与结构化知识库
   （`Agent-Structured-KB` 的 `db.py` / `tools/reranker.py`）**统一**。
   两个模型本机 `~/.cache/huggingface/hub/` 已有缓存（418.7 MB / 780.9 MB），无需下载。
2. **单篇得分由「各关键词相似度之和」改为「平均」**。组内排序等价，
   但**跨组比较必须用平均** —— 否则关键词多的那组得分天然偏高、合并排序时整体占优。
3. **新增 `RelevanceScorer.rank_groups(groups, total_limit)`**，实现用户方案：
   **每组只用自己那组的关键词评分**（硬性约束），合并去重后按分全局降序。
   同一标题在多个组出现时标题编码只算一次（复用向量，不违反约束）；
   一篇论文被多组检索到时取**最高分**。
4. 顺带修掉 `get_sentence_embedding_dimension` 的 FutureWarning。

**实测验证**：

| 验证项 | 结果 |
|---|---|
| 模型在本地 | `HF_HUB_OFFLINE=1` 强制离线仍成功加载 `bge-base-en-v1.5`，768 维 |
| 首次加载耗时 | **101 秒**（CPU）；之后编码+排序 **0.2 秒** |
| **每组只用自己关键词评分** | 同一批文献用本组/别组关键词分别评分：本组平均 **0.5843** vs 别组 **0.5121**，**5/5 全部本组更高** |
| 跨组去重 | 同一 `paper_id` 出现在两组 → 只保留 1 篇 |
| 多组命中的取分 | 取较高分（第1组 0.5920 / 第3组 0.5963 → 保留 **0.5963**） |
| 全局降序 / 取前 N / 不足上限 / 空输入 | 全部正确 |

⚠️ **注意**：知识库常驻服务（端口 8767）里已加载的 BGE 在**它自己的进程**内，
**跨进程无法共享**，所以论文检索每次新开进程都要重付约 100 秒加载时间。

`interleave_by_query()`（v1.7.3 的轮转融合）**保留为 BGE 不可用时的降级路径**，
默认走 BGE 全局排序。

**v1.9.0（同日，语言分支与内存生命周期）**：

用户指出：论文检索用**英文**分支，而知识库常驻服务用的是**中文**分支
（该库 `.lklang` = `zh`，实测常驻进程占 829 MB），要求"检索期间持续激活英文分支、
检索完成关闭，不占运行内存"。据此做了三处改动：

1. **`RelevanceScorer(lang="en")`**：新增 `lang` 参数，**论文检索固定英文分支**。
   旧版按查询字符比例自动判断，实测**纯中文查询会选 `bge-base-zh-v1.5`**（780 MB）；
   现在即使用纯中文查询也只加载 `bge-base-en-v1.5`。`lang="auto"` 保留旧行为。
2. **新增 `unload()` / `with` 上下文管理**（同时如实标注它**不能**归还内存）。
3. **新增 `search_pipeline.py`**：一次调用做完 检索 → BGE 每组评分 → 合并排序 → 落盘，
   BGE 整个任务期间只加载一次；调用返回后让进程退出即全部归还。

**⚠️ 实测澄清（与用户预期有出入，必须如实说明）**：

用户希望"检索完成后关闭英文分支就不占内存"，但实测**进程内做不到**：

| 尝试 | 内存 |
|---|---|
| `import torch` + `sentence_transformers` | 732 MB（**这部分就还不了**）|
| 加载英文模型后 | 865 MB |
| `del` 引用 + `gc.collect()` | 858 MB（只还 **7 MB**）|
| 清 transformers 内部缓存 | 858 MB（无变化）|
| Windows `SetProcessWorkingSetSize` 修剪 | 1 MB（**假象**：再分配内存后回到 250 MB）|

**唯一可靠的释放方式是进程退出。** 所以正确做法不是"加载完再卸载"，
而是**让 BGE 工作发生在短命进程里，用完即退** —— `search_pipeline.py` 正是为此设计。

**端到端实测**（替身检索器 + 真实英文 BGE）：
`per_query_limit(200) = 200`（每组 2 页）、`(300) = 300`（每组 3 页）✓；
3 组各自评分、跨组去重 15→14、按分降序 ✓；落盘 14 行、BGE 分数列正确 ✓；
每组请求均带 `publication_types=["JournalArticle","Review"]`（排除会议）✓。

**已落实的新增（v1.6.0，2026-09-13）**：

- **新增 `paper_table.py`**：把全部论文元数据一次性写进一个 xlsx，之后按需读取，
  避免整表进上下文。含 `--stats`（只出统计）、`--filter`（按条件取少量明细）、
  `--selected`（读用户勾选的「选中」列）、`--from-json`（从既有 json 生成）四种模式；
- **两条防护**（实测踩坑所得）：`PaperID`/`DOI` 列强制文本格式（防 Excel 科学计数法毁掉 ID）；
  筛选前先校验字段，**列不存在或整列为空都直接报错**，绝不静默返回空；
- **文档**：`AGENTS.md` 新增 §3.6，`SKILL.md` 功能表与资源导航补指针；
- **实测验证**（565 篇真实数据）：落盘 184 KB / 22 列；`--filter` 输出
  `565 → 排除 294（年份）→ 排除 176（引用数）→ 95 条` 且审计可对账；
  用整列为空的 `分区` 筛选被正确拦下；4 个既有测试套件仍全部通过。

---

## 7. 文件管理

### 7.1 学术调研归档规范（本环境约定）

```
F:\Agent\DeepseekHarness\AcademicResearch\
├── 说明文件.md                     # 归档规则说明
├── 调研日志.md                     # 每次调研追加一条记录
└── 记录\
    └── <YYYYMMDD>_<主题>\          # 例：20260910_脱粘界面非线性振动
        ├── <脚本>.py               # 检索/处理脚本
        ├── ..._all.json            # 全量结果
        ├── ..._relevant.txt        # 相关结果
        ├── ..._core.txt            # 核心文献（含摘要）
        ├── 检索完整性报告.txt/.json # 必产：结果完整性与异常留痕
        └── <日志>.log
```

### 7.2 执行要求

1. **每次调研开始**先建目录：`AcademicResearch\记录\<YYYYMMDD>_<主题>\`（日期取当天）。
2. **所有中间与最终数据**写入该目录；**不要**使用 skill 默认的 `Interaction` 目录。
   - 编程时给 `save_filtered_results(..., save_dir=OUT)`；
   - 或设置环境变量 `LITSEARCH_OUT_DIR=<OUT>`。
3. **调研结束**在 `AcademicResearch\调研日志.md` 追加一条记录，格式：

```
### YYYY-MM-DD | 主题
- 结论：（简要结论）
- 检索完整性：（是否完整；失败/截断的查询数；若为部分结果须写明）
- 数据源说明：（若发生限流、密钥不可用、换源，务必写明）
- 数据位置：记录/日期_主题/
- 关键产出：（主要文件 / 交付物；含 检索完整性报告）
```

4. **交付物**（Word/报告等）按用户要求放置（桌面、交互文件夹等），但其**生成脚本与源数据仍归档**到对应 `记录\日期_主题\`。
5. **异常必须留痕且必须汇报**：发生 `ApiUnavailableError`、换源、分区缺失、翻译跳过、结果被截断等情况，一律写入 `检索完整性报告`、调研日志与交付说明，并在给用户的汇报中明确列出，避免后续把"取数失败/部分取回"误读为"无相关文献"。

**v2.0.0（2026-10-30，按用户决策重做分区与数据源）**：

1. **入表上限 200 → 不限**（`DEFAULT_TOTAL_LIMIT = None`，2026-10-30 用户定 A 方案）。理由：请求量已由 `DEFAULT_PER_ROUND` 唯一决定，再拍一个表上限只会白丢候选；要小表用 `--top N`。原 200 的问题：要按分区/年份再筛时，BGE 前 200 名
   常漏掉整本一区刊（实测 200 篇表里没有 NDT&E International、Automation in Construction），
   放大到闸门后全量（553 篇）才筛得出完整结果。
2. **每轮取回 200 → 500**（`DEFAULT_PER_ROUND`，= 5 页）。⚠️ 单页仍是 100，所以
   **每轮 500 篇 = 5 次请求**；4 轮 = 20 次请求，比默认 200 时的 8 次多一倍多，429 概率随之上升。
3. **分区只保留中科院新锐分区**，删除 journalmetrics 分支（约 100 行）与 `.quartile_cache.json`。
   `--enrich-quartile` / `--quartile` 废弃并报错，改用 `--enrich-zone` / `--zone 1,2`；
   `quartile_filter` 取值从 `Q1/Q2` 改为 `"1"/"2"`。
4. **分区查询提速**：优先 `searchts read --backend curl_cffi`（7~9 s → 2.6~3.6 s，6/6 成功）。
5. **分区缓存加 TTL**：未收录项 30 天后重查，修掉"偶发空页被永久缓存成未收录"的毒化。
6. **新增 `venue_rules.py`**：按刊名识别会议论文集（实测漏判 99 篇）。
7. **新增 `abstract_fill.py`**：Crossref → OpenAlex → Elsevier → PubMed → searchts 渲染，
   把摘要覆盖率从 80% 提到 98%；数据源明确为三层（S2 主源 / Crossref 备用 / Elsevier 仅自家论文）。
8. 环境坑记录：PowerShell 重定向中文输出被判 binary（需 `PYTHONIOENCODING=utf-8`）；
   `Start-Process -ArgumentList` 不自动加引号；`Test-Path`/`Remove-Item` 把 `[ ]` 当通配符（用 `-LiteralPath`）。
9. 语义澄清：`queries_capped` 是"每轮达到抓取上限"，**不等于失败**；`search_pipeline.run()`
   不做翻译（`中文标题` 列为空，需另跑 `translator`）。
