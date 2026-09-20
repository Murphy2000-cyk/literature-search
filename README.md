# literature-search

`literature-search` 是用于文献检索的 skill，以 **Semantic Scholar** 为主数据源。通过内置的 BGE 模型对数据进行筛选和排序，根据用户需求返回学术文献。大模型只会接收到「检索、筛选、排序」后的最终数据，因此极大程度地节约了 token 消耗。当前版本 **v1.9.1**，默认使用 DSH 的 skill 格式；安装到其他 Agent 时需要修改 skill 的触发条件。

**English**

`literature-search` is a literature-retrieval skill for **DeepSeek Harness (DSH)**. It searches academic literature on **Semantic Scholar** as its primary data source, then uses a built-in BGE model to score and rank the candidates and filters them by a topic gate plus the user's requirements, so the model only ever receives the final retrieved, filtered and ranked data. That saves a large amount of tokens. Current version: **v1.9.1**. It ships in DSH's skill format, so installing it into another agent requires editing the skill's trigger conditions.

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
2. 3~4 轮每轮 ≤5 个词的关键词，每轮取 200 篇，**一次检索只需 6~8 次 Semantic Scholar 请求**；
   检索响应本身已直接带回 DOI（覆盖 88~95%）、摘要（74%）、OA 链接，**不需要再逐篇补详情**；
3. 全部元数据一次性写进**一个 xlsx**，数据在脚本里流转，**只有 stdout 进上下文**。
   实测"筛出 Q1 里最相关的前 10 篇"这条命令的 stdout **只有 2 行 ≈ 47 token**，
   而整表进上下文要 ≈ 9.7 万 token；
4. 通过 BGE 进行相关性排序，并且设置了**主题闸门**和**用户需求**作为筛选条件，返回最终数据。

---

## 功能

| 环节 | 做什么 | 产物 |
|---|---|---|
| **检索** | 3~4 轮 ≤5 词关键词，每轮 200 篇（2 页）→ 合并去重 | 候选池 |
| **排序** | BGE 对**每组关键词独立评分**（绝不用别组关键词打分）→ 合并降序 | 带 BGE 分的表 |
| **主题闸门** | 按本次题目配置的 2~3 个轴（每轴约 50 词）过滤；自带"过窄自检" | 领域内候选 |
| **落盘** | 全部元数据一次写成 xlsx（含 DOI / 摘要 / PDF 链接 / 分区） | `<前缀>.xlsx` |
| **按需读取** | `--stats` / `--filter` / `--select` / `--top-n` / `--quiet` / `--selected` | 只把需要的一小部分读进上下文 |
| **分区回填** | `--enrich-quartile` 查 JCR 分区并写回表里（走只读抓取，不落盘） | 带分区的表 |
| **备用源** | Semantic Scholar 不可用时，**经用户同意后**启用 Crossref（带硬门禁） | 备用结果 |
| **如实汇报** | 每次检索自动产出**检索完整性报告**（哪些查询失败/截断） | `检索完整性报告.txt/.json` |

**模块一览**

| 文件 | 职责 |
|---|---|
| `semantic_scholar_search.py` | 主流程：检索、分页、合并去重、筛选、分区集成、保存；含失败重试与上报 |
| `search_pipeline.py` | **一步流程**：检索 → BGE 每组独立评分 → 合并排序 → 落盘 Excel |
| `paper_table.py` | 元数据表落盘与按需读取（`--select` / `--top-n` / `--quiet` / `--enrich-quartile`） |
| `relevance_scorer.py` | BGE 多组关键词相关性排序（`rank_groups()` 每组独立评分后全局排序） |
| `topic_gate.py` | **主题闸门**：判断标题+摘要是否属于目标领域，含过窄自检 |
| `paper_details.py` | Phase 2：按 paper_id / 标题批量取详情 |
| `translator.py` | 批量翻译标题与摘要（可选，缺凭据自动跳过并保留英文） |
| `journal_quartile.py` | JCR 分区查询（journalmetrics.org，带本地缓存） |
| `crossref_fallback.py` | 备用数据源（未获用户同意会直接拒绝执行，退出码 3） |
| `bge_status.py` | 查看本机 BGE 内存占用、区分中英文分支 |
| `evals/` | 失败重试策略、如实汇报留痕、备用源门禁的自测套件 |

---

## 检索流程

### 标准流程（默认，一步到位）

```
① 设 3~4 组英文关键词，每组 ≤5 个词，互为微调变体
     每轮取 200 篇（= 2 页）→ 候选池最多 组数 × 200（3 组 → 最多 600）
② BGE 对【每一组各自】评分 → 合并去重 → 按分全局降序 → 取前 200
③ 过主题闸门（2~3 个轴，命中全部轴才通过）
④ 一次性落盘成一个 xlsx
⑤ 之后按需读取，绝不整表进上下文
```

**Semantic Scholar API 调用预算** = `ceil(每轮篇数 / 100) × 轮数`。默认即 `2 × 3~4 = 6~8 次请求`（外加少量 429 重试）。

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

### 为什么 BGE 排序之后还要过闸门

BGE 打分是「标题向量 与 查询词向量 的余弦相似度**均值**」，它奖励的是**用词重合**，
不是"这篇论文是否真做这个题目"。实测后果：一篇与题目无关的纯综述被排到第 2（BGE 0.6480），
而三篇真正对题的论文反被压到 18 / 25 / 33 名。

所以排序之后必须再加两道：

1. **主题闸门**：为**本次题目**准备一条 profile：**2~3 个轴、每轴约 50 词，命中全部轴才通过**。
   内置的两套 profile 只是**结构示例**，正式检索前必须另建。
   实测：一套只适配某个题目的词表拿去判别的题目，13 个真实标题只过 3 个。
2. **人工复核**：闸门后仍可能有别领域的撞名论文排在前排。

---

## 安装（DSH）

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
> 若你同时装了 `skill-create-debug-dsh`，可以用它的
> `python scripts/audit.py --skill literature-search` 做一次静态体检。

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

**缺失时的行为**（都会明确降级，不会静默出错）：

- 缺 `sentence-transformers` / `torch` → **跳过 BGE 排序**，退回按查询轮转融合的顺序，属降级而非失败；
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

`journal_quartile.py` 通过 `searchts read <url>` 抓取 journalmetrics.org 的分区页面。
**缺这个命令时分区查询不可用，其余功能全部正常**（脚本会返回 `找不到 searchts 命令` 并继续）。

分区缓存（`.quartile_cache.json`）**不随仓库分发**：它是本机生成的大文件，且时效性强。
首次批量查询每种期刊约 6~8 秒；200 篇若涉及 100 种期刊，首次约需 10~15 分钟，之后走缓存几乎瞬时。

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

> ⚠️ **为什么绝不使用匿名请求**：匿名请求走的是**全局共享池**（官方口径：1000 请求/秒由所有匿名用户共享），
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
| `LITSEARCH_OUT_DIR` | 否 | 覆盖默认输出目录（默认 `./litsearch_output`） |
| `ANTHROPIC_AUTH_TOKEN` | 否 | 翻译用；缺失则跳过翻译、保留英文 |
| `ANTHROPIC_BASE_URL` | 否 | 翻译端点（默认 `https://api.deepseek.com/anthropic`） |
| `LITSEARCH_MODEL` | 否 | 翻译模型（默认 `deepseek-flash`） |
| `HF_HUB_OFFLINE` | 否 | 置 `1` 强制离线加载本地 BGE 缓存 |
| `BGE_KB_HINTS` / `BGE_KB_PORTS` | 否 | `bge_status.py` 识别其他常驻服务的特征 |

---

## 快速开始

**一步跑完整个流程：**

```bash
python search_pipeline.py --out ./记录/20260920_示例主题 --per-round 200 --top-n 200 \
  "large language model reasoning" \
  "chain of thought prompting" \
  "math word problem solving"
```

```python
# 或编程调用
from search_pipeline import run

res = run(
    ["large language model reasoning",
     "chain of thought prompting",
     "math word problem solving"],
    "./记录/20260920_示例主题",
    per_round=200,      # 每轮取多少篇（= 2 页）
    top_n=200,          # 入表篇数上限
    gate_profile=None,  # 传 profile id 或 dict 才过闸门
)
print(res["api_calls"], res["rounds"])
# 返回后【请让进程结束】—— BGE 占的内存只有进程退出才归还
```

**之后按需读取（刻意做得很小的输出）：**

```bash
# 先看全局（含各字段填充率，据此判断某个筛选条件是否可用）
python paper_table.py --xlsx 论文元数据.xlsx --stats

# 只要一区、最相关的前 10 篇，直接导出，不经过阅读
python paper_table.py --xlsx 论文元数据.xlsx --filter --quartile Q1 --top-n 10 \
    --out 一区前10.xlsx --quiet

# 只要标题（一行一个）
python paper_table.py --xlsx 论文元数据.xlsx --filter --select 标题 --out 标题.txt --quiet

# 查分区并回填
python paper_table.py --xlsx 论文元数据.xlsx --enrich-quartile --quiet

# 读你在 Excel 里勾选的「选中」列
python paper_table.py --xlsx 论文元数据.xlsx --selected
```

**主题闸门调试：**

```bash
python topic_gate.py --list-profiles
python topic_gate.py --explain "Chain-of-thought prompting elicits reasoning in LLMs" \
    --profile example-llm-reasoning
```

---

## 仓库结构

```
literature-search/
├── SKILL.md                      # 114 行，主指令：精简流程 + 硬性规则
├── AGENTS.md                     # 903 行，驱动脚本模板、失败处理、排错手册、逐版本修复记录
├── README.md / CHANGELOG.md / LICENSE / .gitignore
├── semantic_scholar_search.py    # 1076 行，检索主流程（分页、去重、筛选、完整性报告）
├── paper_table.py                # 675 行，元数据表落盘 / 按需读取
├── relevance_scorer.py           # 406 行，BGE 多组评分（每组独立）
├── translator.py                 # 367 行，批量翻译（可选，缺凭据自动跳过）
├── topic_gate.py                 # 283 行，主题闸门（内置一套示例 profile）
├── search_pipeline.py            # 268 行，一步流程入口
├── paper_details.py              # 231 行，Phase 2 详情
├── crossref_fallback.py          # 227 行，备用源（未获同意直接拒绝执行）
├── journal_quartile.py           # 159 行，JCR 分区查询（带缓存）
├── bge_status.py                 # 110 行，BGE 内存占用查看
├── gate_profiles.json            # 你自己的主题闸门配置（含一套结构示例）
├── references/
│   └── examples-and-details.md   # 213 行，产出格式、使用示例、速率限制、BGE 原理
└── evals/
    ├── test_report_failure_classification.py    # 90 行，故障分类自测
    ├── test_ratelimit.py                        # 85 行，失败重试策略自测
    ├── test_report.py                           # 72 行，如实汇报留痕自测
    ├── test_crossref_gate.py                    # 34 行，备用源门禁自测
    └── test-cases/                              # 8 个用例说明
```

合计 30 个文件、315 KB，其中 Python 4083 行、Markdown 1775 行。
`AGENTS.md` 与 `references/` 是长文档，`SKILL.md` 只在需要精确细节时指向它们，这样触发成本才可控。

---

## 注意事项

### 一、失败处理：绝不静默

| 规则 | 说明 |
|---|---|
| **失败即上报** | 首次请求失败后各等 **10s** 重试，共 **3 次请求**（累计约 20 秒）；仍失败即抛 `ApiUnavailableError`，**暂停检索并立即向用户提问** |
| **二选一，不自行决定** | **1)** 持续等待 3~5 分钟后重试（首选）；**2)** 降级使用 Crossref 备用源（最后手段） |
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
| 封锁窗口 | **约 60 ~ 110 秒** |
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

分区来自 journalmetrics.org，**收录不全**。`filter_papers()` 会**放行**分区未知的论文；
若要"只要 Q1/Q2"，必须自行改为严格过滤，并主动说明：
被丢弃的 `unknown` 里可能包含本属 Q1/Q2 的期刊。

⚠️ **v2.0 之前的分区缓存不可信**：旧实现靠网页 `<title>` 正则解析，实测把一个 Q1 期刊记成了 Q2。
缓存版本号已升到 v2，**v1 旧缓存一律视为失效**；必要时用 `--enrich-quartile --refresh` 重查。

### 五、如实汇报（最高优先级）

调研过程中的任何异常都必须如实、完整地汇报，**不得隐瞒、美化或省略**。交付结果时必须同时说明
"完整性与可信度"，而不是只给一个文献列表。必须说明的情形：

- **完全没检索到** → 区分「确实未命中」与「检索失败」，后者**不能**得出"该领域没有文献"的结论；
- **只检索到部分** → 说明哪些查询失败、哪些达到抓取上限；
- **中途换过数据源** → 说明何时、为何换源，以及口径差异（Crossref 只能检索到有 DOI 的文献，摘要覆盖率约 28%）；
- **分区数据缺失** → 说明多少条因未收录被排除；
- **翻译失败/被跳过** → 说明是保留了英文原文；
- **人工去噪** → 说明剔除标准与数量。

`save_search_report(OUT)` 会产出**检索完整性报告**（`complete` / `queries_failed` /
`queries_capped` / 逐查询 `status`），必须随结果一并交付。

### 六、其他

- **输出目录默认是 `./litsearch_output`**，用 `LITSEARCH_OUT_DIR` 或 `save_dir=` 覆盖；
- **`.quartile_cache.json` 不在仓库里**，首次跑分区查询会慢（每种期刊 6~8 秒）；
- **结构化筛选前会校验字段**：列不存在、或**列存在但整列为空**都会**直接报错退出**，
  后者是最危险的静默失败（实测"只要 Q1"曾把 565 篇静默全部排除，看起来像"该领域没有 Q1 论文"）；
- **`PaperID` / `DOI` 列强制文本格式**，否则 Excel 会把长 ID 显示成科学计数法，Phase 2 会整批查不到；
- **xlsx 只是视图，json 仍是权威源**。

---

## 已知局限

1. **主数据源单一**：主力是 Semantic Scholar。备用源 Crossref **只能检索到有 DOI 的文献**，
   无 DOI 的会议论文、区域性期刊、技术报告、标准等一律检索不到，存在**系统性覆盖缺口**。
2. **分区数据不完整**：journalmetrics.org 未收录的期刊分区为 `unknown`。
3. **首次 BGE 加载约 100 秒**（CPU），且内存只有进程退出才归还。
4. **`evals/test-cases/` 下的部分用例描述早于当前架构**
   （例如仍写"默认 limit=50"、"用 Semantic Scholar batch API 取摘要"），与现行流程不一致，仅作历史参考。
5. **翻译默认指向一个 Anthropic 兼容端点**（`https://api.deepseek.com/anthropic` + `deepseek-flash`），
   换供应商需要自行设置 `ANTHROPIC_BASE_URL` 与 `LITSEARCH_MODEL`。
6. **`journal_quartile.py` 依赖外部 CLI `searchts`**，缺它则分区查询不可用（其余功能正常）。

---

## 兼容性

| 项目 | 要求 |
|---|---|
| Python | 3.8+（开发与实测环境为 3.10；全仓无 3.9+ 专属语法） |
| 第三方包（必备） | `requests`、`openpyxl`（元数据表读写） |
| 第三方包（BGE 排序） | `numpy`、`sentence-transformers`、`torch`；缺失时**跳过排序**并降级，不算失败 |
| 第三方包（可选） | `psutil`，仅 `bge_status.py` 用 |
| 外部 CLI（可选） | `searchts`，仅 `journal_quartile.py` 分区查询用；缺失时**只有分区查询不可用**，其余功能正常 |
| 数据源 | Semantic Scholar（**需自备 API Key**）；Crossref（备用，**必须先获用户同意**） |
| 模型缓存 | `BAAI/bge-base-en-v1.5` 约 419 MB；首次加载约 100 秒（CPU） |
| 平台 | 在 Windows 上开发；代码有跨平台分支，但**非 Windows 平台的实测覆盖较少**，在 Linux / macOS 上遇到问题请提 issue |

关于 DSH 版本：本 skill 依赖的行为（skill 发现深度、`description` 截断、frontmatter 字段集）
均以写作时的 DSH 行为为准。DSH 若调整这些默认值，`SKILL.md` 与 `AGENTS.md` 里的说明可能随之漂移。

---

## 许可证

本项目采用 **MIT License**，Copyright (c) 2026 ChengYK (https://github.com/Murphy2000-cyk)，全文见 `LICENSE`。