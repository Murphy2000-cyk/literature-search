# literature-search

## v2.2.0 要点

- **六个环节**：原始检索 → 去重 → 近 N 年 → 记录合规 → 期刊等级 → BGE 排序，每环节一份 Excel。
- **第 0 层原始落盘**：每次 S2 请求的原始响应存进 `<输出目录>/raw/r{n}_p{n}.json`，
  不裁剪不丢记录，是全部表的权威源。
- **领域开关**：`--domain cs` 默认放行单篇会议论文（计算机以会议为主）；
  其它领域用默认 `general`（排除会议论文）。撤稿与会议录卷级条目永不放行。
- **淘汰原因分两类**：`不符合条件`（有数据但不满足）与 `无数据可判`（缺字段不纳入），
  分开统计，写进 `筛选淘汰记录.xlsx`。
- **分区默认 JCR 1 区**（`--zone-basis jcr|xr` 可切中科院新锐），时间默认近 10 年。
- **摘要非硬性**，六类必得字段（题目/DOI/作者/期刊/发表时间/关键词）缺失会在报告里列出。

`literature-search` 是用于文献检索的 skill，以 **Semantic Scholar** 为主数据源。通过内置的 BGE 模型对数据进行筛选和排序，根据用户需求返回学术文献。大模型只会接收到「检索、筛选、排序」后的最终数据，因此极大程度地节约了 token 消耗。当前版本 **v2.2.0**。`SKILL.md` 遵循 **Agent Skills** 规范，frontmatter 只有 `name` 和 `description` 两个字段，所以**文件格式本身不需要改**就能装到 DSH 以外的智能体（Agent）；要调整的是装到哪里、Key 放哪、以及一个可选的外部命令，见 [INSTALL.md 第七节](INSTALL.md#七装到-dsh-以外的-agent)。

**English**

`literature-search` is a literature-retrieval skill for **DeepSeek Harness (DSH)**. It searches academic literature on **Semantic Scholar** as its primary data source, then uses a built-in BGE model to score and rank the candidates and filters them by year, record type and journal quartile, so the model only ever receives the final retrieved, filtered and ranked data. That saves a large amount of tokens. Current version: **v2.2.0**. Its `SKILL.md` follows the **Agent Skills** spec (the frontmatter carries only `name` and `description`), so the file format itself needs no change to run on hosts other than DSH. What does need adjusting is where to install it, where the API keys live, and one optional external command; see section 7 of [INSTALL.md](INSTALL.md).

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [功能](#功能)
- [检索流程](#检索流程)
- [安装（DSH）](#安装dsh)
- [依赖](#依赖)
- [配置 Semantic Scholar API Key](#配置-semantic-scholar-api-key)
- [快速开始](#快速开始)
- [仓库结构](#仓库结构)
- [注意事项](#注意事项)
- [已知局限](#已知局限)
- [兼容性](#兼容性)
- [许可证](#许可证)

---

## 它解决什么问题

学术调研的痛点是**缺乏数据源和海量数据难以筛选**：你只想快速扫一眼标题、挑出十几篇真正要读的，
却往往被迫先把几百篇的摘要全部拉进上下文。

这个 skill 的做法是：

1. 基于 Semantic Scholar 的 API 获得数据；
2. 3–4 轮每轮 ≤5 个词的关键词，每轮取 300 篇，**一次检索需 9～12 次 Semantic Scholar 请求**；
   检索响应本身已直接带回 DOI（覆盖 88–95%）、摘要（74%）、OA 链接，**不需要再逐篇补详情**；
3. 全部元数据一次性写进**一个 xlsx**，数据在脚本里流转，**只有 stdout 进上下文**。
   实测"筛出 1 区里最相关的前 10 篇"这条命令的 stdout **只有 2 行 ≈ 47 token**，
   而整表进上下文要 ≈ 9.7 万 token；
4. 按年份、记录类型与期刊等级逐级筛选，再通过 BGE 做相关性排序，返回最终数据。

---

## 功能

| 环节 | 做什么 | 产物 |
|---|---|---|
| **检索** | 4 轮 ≤5 词关键词，每轮 300 篇（3 页），**不设年份限制** | 原始候选池 |
| **六环节流水线** | 原始检索 → 去重 → 近 N 年 → 记录合规 → 期刊等级 → 排序，逐环落盘 | 六份逐级递减的表 |
| **排序** | BGE 用**一句英文主题描述**整句打分，标题 0.6 + 关键词 0.4，摘要不参与 | 带 BGE 分的表 |
| **记录合规** | 剔除会议论文、会议录卷级条目、预印本、撤稿；标记版面元素与记录可疑 | 合规候选 |
| **分区** | `journal_quartile.py` 查 easyScholar，**默认 JCR 1 区**，结果落 `data/journal_zone.xlsx` | 带分区与查询日期的表 |
| **落盘** | 全部元数据一次写成 xlsx（含 DOI / 摘要 / PDF 链接 / 分区 / 淘汰原因） | `<前缀>.xlsx` |
| **按需读取** | `--stats` / `--filter` / `--select` / `--top-n` / `--quiet` / `--selected` | 只把需要的一小部分读进上下文 |
| **备用源** | Semantic Scholar 不可用时，**经用户同意后**启用 Crossref（带硬门禁） | 备用结果 |
| **摘要补全** | S2 原生 → OpenAlex → **Elsevier API** → PubMed，逐档降级，仍缺则标「无摘要」 | 带「摘要来源」的表 |
| **会议识别** | `venue_rules.py`：按刊名规则识别标成 JournalArticle 的会议论文集（带正规期刊白名单） | 带「疑似会议」列的表 |
| **如实汇报** | 每次检索自动产出**检索完整性报告**与**筛选淘汰记录** | `检索完整性报告.txt/.json`、`筛选淘汰记录.xlsx` |

**模块一览**

| 文件 | 职责 |
|---|---|
| `search_pipeline.py` | **六环节编排入口**：检索 → 去重 → 近 N 年 → 记录合规 → 期刊等级 → 排序，返回各环节产物路径 |
| `semantic_scholar_search.py` | 检索与筛选：分页、合并去重、失败重试与上报、检索完整性报告；含 S2 SDK 封装 |
| `keyword_fetch.py` | OpenAlex 按 DOI 批量取关键词与摘要（S2 无 `keywords` 字段，请求会 400） |
| `paper_table.py` | 元数据表落盘与按需读取（`--select` / `--top-n` / `--quiet` / `--enrich-zone`） |
| `relevance_scorer.py` | BGE 相关性排序，主用 `rank_topic()` 单句主题描述；`rank_groups()` 保留兼容 |
| `journal_quartile.py` | 分区查询（easyScholar 主通道，默认 JCR 四分位），结果归档到 `data/journal_zone.xlsx` |
| `venue_rules.py` | 会议论文集识别：刊名规则 + 正规期刊白名单 |
| `abstract_fill.py` | 摘要补全后两档：Elsevier + PubMed |
| `paper_details.py` | Phase 2：按 paper_id / 标题批量取详情 |
| `translator.py` | 批量翻译标题与摘要（可选，缺凭据自动跳过并保留英文） |
| `crossref_fallback.py` | 备用数据源（未获用户同意会直接拒绝执行，退出码 3） |
| `topic_gate.py` | 主题闸门（**已取消接入流水线**，文件保留） |
| `bge_status.py` | 查看本机 BGE 内存占用、区分中英文分支 |
| `evals/` | 失败重试策略、如实汇报留痕、备用源门禁、摘要补全、会议识别的自测套件 |

---

## 检索流程

### 标准流程（六环节，一次跑完）

```
① 设 4 组英文关键词，每组 ≤5 个词，互为微调变体，检索阶段不设年份限制
     每轮取 300 篇（= 3 页），4 组 → 候选池最多 1200 篇
② 按 paperId 去重，记录命中轮次与命中轮数
③ 近 N 年（默认 10 年）：只设下界，精确到日；无日期且无年份的排除
④ 记录合规：剔除会议论文、会议录卷级条目、预印本、撤稿
⑤ 期刊等级：easyScholar 查分区，默认保留 JCR 1 区
⑥ 排序：OpenAlex 取关键词 → BGE 用一句主题描述打分 → 补 Elsevier/PubMed 摘要
```

每环节落一份 Excel，行数逐级递减；另出 `筛选过程记录.md`（各环节进出与淘汰原因分类）与
`筛选淘汰记录.xlsx`（逐条淘汰原因，可直接筛「淘汰环节」列）。

**Semantic Scholar API 调用预算** = `ceil(每轮篇数 / 100) × 轮数`。默认即 `3 × 4 = 12 次请求`
（外加少量 429 重试）。检索阶段不设年份，改年份窗口只动参数，不重花配额。

### 为什么是「短关键词」

实测（同一次会话、同时请求、`limit=100`）：

| 查询 | 词数 | 实际返回 |
|---|---|---|
| 查询 A | 7 | **100** |
| 查询 A + 2 个词 | 9 | **11** |
| 查询 A + 6 个词 | 13 | **6** |

**只加了两个词，返回量从 100 掉到 11。** 在 Semantic Scholar 这里，
"多写几个词让检索更精确"是**反效果**：越精确越接近 0 条。
因此**单条查询最多 5 个关键词**（代码里 `check_keyword_count()` 会直接抛 `ValueError`），
**不做探针轮**（探针既费调用数，又可能把 key 推进 429 封锁期）。

### 为什么排序之后仍要人工复核

BGE 打分本质是**词面相似度**，奖励的是用词重合，不等于「这篇论文是否真做这个题目」。实测后果：
一篇与题目无关的纯综述被排到第 2（BGE 0.6480），而三篇真正对题的论文反被压到 18 / 25 / 33 名。

v2.2.0 起排序改用**一句主题描述整句编码**，比逐词求均值稳，但仍有三处要人工兜住：

1. **查询词外溢**：S2 是词面匹配，`× non-destructive testing` 这类组合会把整类方法召回。
   拿到表后先看前 30 篇标题，必要时按「命中轮次」列只保留命中窄查询的记录。
2. **分区未收录**：第 5 环节会因「该刊未被分区库收录」排除一批，其中可能有个别本属 1 区。
   汇报时必须说明这批数量，不能当成「没有相关文献」。
3. **主题闸门已下线**：`topic_gate.py` 只保留文件，不再参与流水线，不要照旧文档去配 profile。

---

## 安装（DSH）

> 完整安装步骤、API Key 的申请地址与常见问题，见 **[INSTALL.md](INSTALL.md)**。
> 需要自己申请三个 Key：`SEMANTIC_SCHOLAR_API_KEY`（必需）、`EASYSCHOLAR_SECRET_KEY`（分区必需）、
> `ELS_API_KEY`（可选，只影响 Elsevier 摘要）。

DSH 的 skill 发现深度**只有一层**，必须是 `<root>/<name>/SKILL.md` 或平铺的 `<root>/<name>.md`。
把本目录整体放进任一扫描根即可（rank 小者优先，同名时浅层胜出）：

| rank | 作用域 | 路径 |
|---|---|---|
| 100 | 项目 | `<项目根>/.dsh/skills/literature-search/` |
| 200 | 项目 | `<项目根>/.agents/skills/literature-search/` |
| 400 | 用户 | `$DSH_HOME/skills/literature-search/`（默认 `~/.dsh/skills/`） |
| 500 | 用户 | `$DSH_AGENTS_HOME/skills/literature-search/`（默认 `~/.agents/skills/`） |

`<项目根>` = 含 `.git` 的最近祖先目录。

**安装后确认生效：**

1. **模型侧目录是热重载的**：放进扫描根后立即生效，不需要重启 dsh；
2. **Web UI 的 `/` 菜单不会自动刷新**：新建或改名后按 **F5** 刷新页面才会出现；
3. 输入 `/lit` 试试：`/` 菜单的匹配是**不区分大小写的有序子序列**，前缀命中排最前。
   如果菜单里没有它，先刷新页面；两边都没有，检查目录层级与 `name` 字段是否合法。

> 目录名可以是 `literature-search`，但 frontmatter 里的 `name` 必须**全小写 kebab-case**，
> 否则整个 skill 会被**静默丢弃**、不报错。
>
> 装完可先跑 `python bge_status.py`（不联网）确认 Python 环境与依赖是否齐备。

---

## 依赖

### Python 包

```bash
pip install requests openpyxl numpy sentence-transformers torch
```

| 包 | 必需性 | 用途 |
|---|---|---|
| `requests` | **必需** | 所有 HTTP 请求（S2 / Crossref / 翻译） |
| `openpyxl` | **必需**（读表写表） | xlsx 落盘与按需读取 |
| `numpy` | BGE 排序用 | 相似度矩阵 |
| `sentence-transformers` + `torch` | BGE 排序用 | 加载 `BAAI/bge-base-en-v1.5` |
| `psutil` | 可选 | `bge_status.py` 查看内存占用 |

**缺失时的行为**：

- 缺 `sentence-transformers` / `torch` → 排序环节会抛 `ImportError`。此时加 `--no-bge` 可跳过排序，
  六环节照常产出（第 6 环节只是不按相关度重排）；
- 缺 `numpy` → `relevance_scorer.py` 导入即失败，属**必需项**，不要省；
- 缺 `ANTHROPIC_AUTH_TOKEN` → **跳过翻译**，保留英文标题/摘要；
- 缺 `psutil` → 只有 `bge_status.py` 不可用，其余功能不受影响。

### 模型缓存

BGE 用的是 `BAAI/bge-base-en-v1.5`（768 维，约 **419 MB**）。如果可以接受略低的排序质量，也可以换成更轻量的 `BAAI/bge-small-en-v1.5`（384 维）；模型名是 `relevance_scorer.py` 里的类常量 `MODEL_EN` / `MODEL_ZH`，**直接改常量即可，不走环境变量**。

⚠️ **首次加载实测约 100 秒**（CPU）。之后对几百条标题编码 + 排序只需**零点几秒**。
⚠️ **进程内无法把 torch 占的内存还给操作系统**（实测 `del` + `gc.collect()` 只还 7 MB，
工作集修剪是假象）。**唯一可靠的方式是让进程退出**。
所以本 skill 的用法是：**一个进程做完整个检索任务，返回后让进程结束**。

已有本地缓存时可用 `HF_HUB_OFFLINE=1` 强制离线加载。

### 外部 CLI（可选）

v2.2.0 的分区查询走 easyScholar 开放接口（HTTP），**不需要任何外部命令**，只需配置
`EASYSCHOLAR_SECRET_KEY`。LetPub 仅作兜底通道，默认关闭，需要时显式加 `--allow-letpub`；
它依赖 `searchts` 这个可选命令，缺了不影响其余功能。

分区数据落在 `data/journal_zone.xlsx`，**不随仓库分发**（`.gitignore` 已排除）。`已收录` 60 天内免查，
`未收录` 30 天，查询失败一律重试。接口限速约每秒一次，100 种刊首次约 2 分钟，之后命中本地表几乎瞬时。

---

## 配置 Semantic Scholar API Key

### 1. 申请 Semantic Scholar API Key

**申请地址：** <https://www.semanticscholar.org/product/api#api-key-form>

1. 打开上面的表单页，填写邮箱等信息；
2. **免费，通常秒批**，Key 会直接发到邮箱；
3. 格式为 `s2k-` 开头，后接 40 位字符；
4. 邮件里会写明速率上限：**1 请求/秒，跨所有端点累计**；
5. 传参方式：**`x-api-key` 请求头**（本 skill 只使用这一种方式，
   **不使用 URL 参数、任何情况下都不降级为匿名请求**）。

> ⚠️ **为什么绝不使用匿名请求**：匿名请求走的是**全局共享池**（官方说法：1000 请求/秒由所有匿名用户共享），
> 稳定性完全不可控。带密钥遇到 429 是**你自己那张 key 触发了服务端限流封锁**，
> 与"共享池被抢"无关，改用匿名请求既不能绕过，还会让排查更混乱。

### 2. 配置 Key

脚本启动时会调用 `_load_env_from_dsh_env()` 自动补载凭据，
**即使由不继承环境的子进程运行也能生效**。两种方式任选：

**方式 A（推荐）：写入 `~/.dsh/env.json`**

```json
{
  "env": {
    "SEMANTIC_SCHOLAR_API_KEY": "s2k-你的密钥"
  }
}
```

格式相同的旧位置 `~/.claude/settings.json` 也兼容（`~/.dsh/env.json` 优先）。

**方式 B：系统环境变量**

```powershell
setx SEMANTIC_SCHOLAR_API_KEY "s2k-你的密钥"
```

> ⚠️ `setx` **只影响之后新启动的进程**。已在运行的父进程派生的子进程不会继承，需重启或手动 set。

### 3. 全部环境变量

| 变量 | 必需 | 说明 |
|---|---|---|
| `SEMANTIC_SCHOLAR_API_KEY` | **是** | 缺失时**直接报错退出，不会退化为匿名请求** |
| `LITSEARCH_OUT_DIR` | 否 | 覆盖默认输出目录（默认 `~/literature-search-out`） |
| `EASYSCHOLAR_SECRET_KEY` | **建议必配** | 分区主通道。缺它且不加 `--allow-letpub` 时，分区查不到，第 5 环节会把所有记录按「无数据可判」排除 |
| `ANTHROPIC_AUTH_TOKEN` | 否 | 翻译用；缺失则跳过翻译、保留英文 |
| `ANTHROPIC_BASE_URL` | 否 | 翻译端点（默认 `https://api.deepseek.com/anthropic`） |
| `LITSEARCH_MODEL` | 否 | 翻译模型（默认 `deepseek-flash`） |
| `HF_HUB_OFFLINE` | 否 | 置 `1` 强制离线加载本地 BGE 缓存 |
| `BGE_KB_HINTS` / `BGE_KB_PORTS` | 否 | `bge_status.py` 识别其他常驻服务的特征 |

---

## 快速开始

**一步跑完整个流程：**

```bash
python search_pipeline.py --out ./记录/20260920_示例主题 --per-round 300 --top 200 \
  --topic "large language model reasoning with chain of thought prompting" \
  "large language model reasoning" \
  "chain of thought prompting" \
  "math word problem solving" \
  "reasoning step decomposition benchmark"
```

```python
# 或编程调用（run 的签名：queries, topic, out_dir, ...）

> **现行架构以 `SKILL.md` 的「六个环节」与 `AGENTS.md` §0 为准。** 本文中的历史章节
> （分区只做新锐、主题闸门、Crossref 反查 ISSN 等）已被取代，冲突处以那两处为准。

from search_pipeline import run

res = run(
    queries=["large language model reasoning",
             "chain of thought prompting",
             "math word problem solving",
             "reasoning step decomposition benchmark"],
    topic="large language model reasoning with chain of thought prompting",
    out_dir="./记录/20260920_示例主题",
    per_round=300,      # 每轮取多少篇（= 3 页）
    top_n=200,          # 可选：只保留前 200 篇，None 表示不截取
    zone_filter=("1",),  # 分区条件，默认 JCR 1 区
)
print(res["stage6"])   # 返回 stage1..stage6 各环节产物路径
# 返回后【请让进程结束】—— BGE 占的内存只有进程退出才归还
```

**之后按需读取（刻意做得很小的输出）：**

```bash
# 先看全局（含各字段填充率，据此判断某个筛选条件是否可用）
python paper_table.py --xlsx 论文元数据.xlsx --stats

# 只要一区、最相关的前 10 篇，直接导出，不经过阅读
python paper_table.py --xlsx 论文元数据.xlsx --filter --zone 1 --top-n 10 \
    --out 一区前10.xlsx --quiet

# 只要标题（一行一个）
python paper_table.py --xlsx 论文元数据.xlsx --filter --select 标题 --out 标题.txt --quiet

# 查分区并回填
python paper_table.py --xlsx 论文元数据.xlsx --enrich-zone --quiet

# 读你在 Excel 里勾选的「选中」列
python paper_table.py --xlsx 论文元数据.xlsx --selected
```

**主题闸门调试（仅诊断旧模块，不参与流水线）：**

```bash
python topic_gate.py --list-profiles
python topic_gate.py --explain "Chain-of-thought prompting elicits reasoning in LLMs" \
    --profile example-llm-reasoning
```

---

## 仓库结构

```
literature-search/
├── SKILL.md                      #  243 行，主指令：六环节摘要 + 硬性规则（触发用，不含细节）
├── AGENTS.md                     # 1411 行，驱动脚本模板、失败处理、排错手册、逐版本记录
├── README.md / CHANGELOG.md / INSTALL.md / LICENSE / .gitignore
├── search_pipeline.py            #  714 行，六环节编排入口（首选调用方式）
├── semantic_scholar_search.py    # 1472 行，检索主流程（分页、去重、失败上报、完整性报告）
├── paper_table.py                #  909 行，元数据表落盘 / 按需读取
├── journal_quartile.py           #  715 行，分区查询（easyScholar 主通道，默认 JCR）
├── relevance_scorer.py           #  571 行，BGE 评分（rank_topic 单句主题描述）
├── translator.py                 #  459 行，批量翻译（可选，缺凭据自动跳过）
├── topic_gate.py                 #  365 行，主题闸门（已不接入流水线，文件保留）
├── paper_details.py              #  296 行，Phase 2 详情
├── venue_rules.py                #  279 行，会议论文集按刊名识别（带正规期刊白名单）
├── crossref_fallback.py          #  270 行，备用源（未获同意直接拒绝执行）
├── abstract_fill.py              #  263 行，摘要补全（Elsevier + PubMed 两档）
├── keyword_fetch.py              #  217 行，OpenAlex 批量取关键词与摘要
├── bge_status.py                 #  138 行，BGE 内存占用查看
├── gate_profiles.json            #  324 行，主题闸门配置（含一套结构示例）
├── references/
│   ├── examples-and-details.md   #  281 行，产出格式、使用示例、速率限制、BGE 原理
│   ├── 实测记录.md                #  190 行，实测数字集中存放
│   ├── easyscholar-api.md        #  117 行，分区接口字段陷阱与调试清单
│   └── journal-name-issues.md    #  103 行，刊名匹配不上时的规范化规则
└── evals/
    ├── test_abstract_fill.py                   # 120 行，摘要补全自测
    ├── test_ratelimit.py                       # 120 行，失败重试策略自测
    ├── test_report_failure_classification.py   # 116 行，故障分类自测
    ├── test_report.py                          #  96 行，如实汇报留痕自测
    ├── test_crossref_gate.py                   #  52 行，备用源门禁自测
    ├── test_venue_rules.py                     #  43 行，会议识别自测
    └── test-cases/                             # 8 个用例说明
```

合计 39 个文件、479 KB，其中 Python 7215 行、Markdown 3604 行。
`AGENTS.md` 与 `references/` 是长文档，`SKILL.md` 只在需要精确细节时指向它们，这样触发成本才可控。
`data/journal_zone.xlsx`（分区表）与 `.letpub_zone_cache.json`（旧缓存）属于本地资产，已被 `.gitignore` 排除。

---

## 注意事项

### 一、失败处理：绝不静默

| 规则 | 说明 |
|---|---|
| **失败即上报** | 请求失败按指数退避重试（10s → 20s → 40s，累计约 70 秒，共 3 次请求）；仍失败即抛 `ApiUnavailableError`，**暂停检索并立即向用户提问** |
| **二选一，不自行决定** | **1)** 持续等待 3–5 分钟后重试（首选）；**2)** 降级使用 Crossref 备用源（最后手段） |
| **不得把失败当成「0 篇结果」** | 只有 `200` + 空 `data` 才算"确实未命中"；`5xx` / 网络错误一律记 `failed` |
| **失败时必须保全部分结果** | 用 `searcher.collected_partial()` 取回中断前已取回的部分并落盘 |
| **换源必须经用户同意** | 未加 `--approved-by-user` 时 Crossref 脚本**直接拒绝执行**（退出码 3），且不发任何网络请求 |

### 二、限流：429 不等于「密钥失效」

2026-09 的受控实测结论：

| 观测项 | 结果 |
|---|---|
| 无间隔连发 | 第 2 个 **429**（1 RPS 窗口真实且严格） |
| 静默 60 秒后单发 | **仍 429** |
| 触发后自行恢复 | 约 **44 秒** |
| 封锁窗口 | **约 60 – 110 秒** |
| 限流施加方 | AWS API Gateway（按 **Semantic Scholar API key** 施加） |
| 封锁期内用伪造 key | 返回 **403**（说明先鉴权后限流 → **key 本身有效**） |

**因此：**

- **不要向用户报告"密钥失效"**，应报告"服务端限流封锁中"；
- **不能凭一次 429 判定失败，也不能凭一次 200 判定已恢复**（实测 1.0 秒间隔成功、1.5 秒反而失败，无规律）；
- **禁止并发**：密钥约 1 请求/秒，同一时间只跑一个检索进程，多查询放进同一个脚本串行循环。

### 三、检索语义：S2 是关键词（词法）检索，不是语义检索

| 事实 | 说明 |
|---|---|
| **无布尔语法** | `AND` / `OR` / 引号 / 通配符**全部无效**，会被当普通文本（需要布尔控制请改用 `/paper/search/bulk`） |
| **匹配范围含摘要** | 所以只在摘要里出现的方法/技术术语可以放心写进查询词 |
| **多词是"提高相关度"而非严格求交** | 逐字唯一的 5 词词组检索返回了 538 条而非 1 条，全命中的排最前 |
| **单查询硬上限 1000 条** | `offset + limit ≤ 1000`；要更多必须改用 `/paper/search/bulk` |
| **`total` 字段不可信** | 实测给同一查询**加一个词**，`total` 从 27,604 涨到 **1,803,131**（**65 倍**）。**禁止用它汇报"某领域共有 N 篇文献"** |

### 四、分区数据的固有缺陷

分区来自 easyScholar（默认 JCR 四分位，可切中科院新锐），**收录不全**。第 5 环节把「无刊名」
与「未收录」一律按「无数据可判」排除，汇报时必须单独说明这批数量，因为被排除的记录里
可能包含本属 1 区的期刊。

实测（2026-11-03 一次检索）：521 条进入第 5 环节，其中 250 条因「该刊未被分区库收录」、
25 条因「无刊名」被排除，最终留下 91 条 1 区论文。

⚠️ **分区表带查询日期**：`已收录` 60 天、`未收录` 30 天后重查，查询失败一律重试。
早期实现靠网页 `<title>` 正则解析，实测把一本 1 区期刊记成 2 区，那类旧缓存已作废；
怀疑数据不准时用 `--enrich-zone --refresh` 全量重查。

### 五、如实汇报（最高优先级）

调研过程中的任何异常都必须如实、完整地汇报，**不得隐瞒、美化或省略**。交付结果时必须同时说明
"完整性与可信度"，而不是只给一个文献列表。必须说明的情形：

- **完全没检索到** → 区分「确实未命中」与「检索失败」，后者**不能**得出"该领域没有文献"的结论；
- **只检索到部分** → 说明哪些查询失败、哪些达到抓取上限；
- **中途换过数据源** → 说明何时、为何换源，以及判定标准不同（Crossref 只能检索到有 DOI 的文献，摘要覆盖率约 28%）；
- **分区数据缺失** → 说明多少条因未收录被排除；
- **翻译失败/被跳过** → 说明是保留了英文原文；
- **人工去噪** → 说明剔除标准与数量。

`save_search_report(OUT)` 会产出**检索完整性报告**（`complete` / `queries_failed` /
`queries_capped` / 逐查询 `status`），必须随结果一并交付。

### 六、其他

- **输出目录默认是 `~/literature-search-out`**，用 `LITSEARCH_OUT_DIR` 或 `save_dir=` 覆盖；
- **`data/journal_zone.xlsx` 不在仓库里**，首次跑分区查询会慢（接口限速约每秒一次）；
- **结构化筛选前会校验字段**：列不存在、或**列存在但整列为空**都会**直接报错退出**，
  后者是最危险的静默失败（实测"只要 1 区"曾把 565 篇静默全部排除，看起来像"该领域没有 1 区论文"）；
- **`PaperID` / `DOI` 列强制文本格式**，否则 Excel 会把长 ID 显示成科学计数法，Phase 2 会整批查不到；
- **xlsx 只是视图，json 仍是权威源**。

---

## 已知局限

1. **主数据源单一**：主力是 Semantic Scholar。备用源 Crossref **只能检索到有 DOI 的文献**，
   无 DOI 的会议论文、区域性期刊、技术报告、标准等一律检索不到，存在**系统性覆盖缺口**。
2. **分区数据不完整**：easyScholar 未收录的期刊标记为 unknown 并在第 5 环节排除。
3. **首次 BGE 加载约 100 秒**（CPU），且内存只有进程退出才归还。
4. **`evals/test-cases/` 下的部分用例描述早于当前架构**
   （例如仍写"默认 limit=50"、"用 Semantic Scholar batch API 取摘要"），与现行流程不一致，仅作历史参考。
5. **翻译默认指向一个 Anthropic 兼容端点**（`https://api.deepseek.com/anthropic` + `deepseek-flash`），
   换供应商需要自行设置 `ANTHROPIC_BASE_URL` 与 `LITSEARCH_MODEL`。
6. **`journal_quartile.py` 的兜底通道依赖外部 CLI `searchts`**。默认走 easyScholar 接口，
   不需要它；只有显式加 `--allow-letpub` 时才会用到，缺了则只有那一档不可用。

---

## 兼容性

| 项目 | 要求 |
|---|---|
| Python | 3.10+（开发与实测环境为 3.13） |
| 第三方包（必备） | `requests`、`openpyxl`（元数据表读写） |
| 第三方包（BGE 排序） | `numpy`、`sentence-transformers`、`torch`；缺 torch 时须加 `--no-bge` 跳过排序，缺 numpy 属必需项 |
| 第三方包（可选） | `psutil`，仅 `bge_status.py` 用 |
| 外部 CLI（可选） | `searchts`，仅在 `--allow-letpub` 兜底通道下使用；默认通道走 easyScholar 接口，不需要它 |
| 数据源 | Semantic Scholar（**需自备 API Key**）；Crossref（备用，**必须先获用户同意**） |
| 模型缓存 | `BAAI/bge-base-en-v1.5` 约 419 MB；首次加载约 100 秒（CPU） |
| 平台 | 在 Windows 上开发；代码有跨平台分支，但**非 Windows 平台的实测覆盖较少**，在 Linux / macOS 上遇到问题请提 issue |

关于 DSH 版本：本 skill 依赖的行为（skill 发现深度、`description` 截断、frontmatter 字段集）
均以写作时的 DSH 行为为准。DSH 若调整这些默认值，`SKILL.md` 与 `AGENTS.md` 里的说明可能随之漂移。

---

## 许可证

本项目采用 **MIT License**，Copyright (c) 2026 ChengYK (https://github.com/Murphy2000-cyk)，全文见 `LICENSE`。
