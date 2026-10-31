---
name: literature-search
description: 学术文献检索（主源 Semantic Scholar）：3~4 轮 ≤5 词关键词、每轮 500 篇，BGE 排序 + 主题闸门，落盘 Excel 后按需读取；可按中科院新锐分区（LetPub，1–4 区）回填与筛选，缺摘要的 Elsevier 论文可走官方 API 补齐。当用户要"搜索文献""文献检索""找论文""查文献""查期刊分区""补摘要"，或做综述的资料收集时使用。
---

# 文献检索 Skill

主数据源 Semantic Scholar 的学术文献检索，**一次检索 6~8 次 API 调用**：
3~4 轮关键词（每轮 ≤5 词，各取 500 篇）→ BGE 每组独立评分 → 主题闸门 → 落盘 Excel → 按需读取。

**排错手册、故障复盘、闸门配置、归档规范见同目录 `AGENTS.md`；保存格式、使用示例、速率限制、BGE 排序说明见 `references/examples-and-details.md`。**

## 目的

把"找文献"做成**少调用、快、可复现**的流程：一次抓够候选池并落盘，之后只按需读取其中一小部分，而不是把整份列表读进上下文。

## 功能

| 环节 | 做什么 | 产物 |
|---|---|---|
| **检索** | 3~4 轮 ≤5 词关键词，每轮 500 篇（5 页）→ 合并去重 | 候选池 |
| **排序** | BGE 每组独立评分（**绝不用别组关键词打分**）→ 合并降序 | 带 BGE 分的表 |
| **主题闸门** | 按本次题目配置的 2~3 个闸门（每闸门约 50 词）过滤 | 领域内候选 |
| **落盘** | 全部元数据一次写成 xlsx（含 DOI/摘要/PDF 链接、疑似会议标记） | `<前缀>.xlsx` |
| **分区** | `--enrich-zone` 回填**中科院新锐分区**（ISSN 精确匹配 + Crossref 反查 ISSN） | 「中科院分区」列 |
| **摘要补全** | `abstract_fill.py`：Crossref → OpenAlex → Elsevier → PubMed → searchts 渲染 | 回填「摘要」列 + 「摘要来源」 |
| **按需读取** | `--stats` / `--filter` / `--selected` / `--zone 1,2` | 只把需要的一小部分读进上下文 |
| **备用** | 逐篇补详情（**一般不需要** —— DOI/摘要已随检索带回） | 仅在缺口时用 |

模块：`semantic_scholar_search.py`（检索主流程）、`paper_details.py`（Phase 2）、`paper_table.py`（元数据表落盘与按需读取）、`topic_gate.py`（**主题闸门**）、`translator.py`（翻译）、`journal_quartile.py`（**分区：只做 LetPub 中科院新锐分区，按 ISSN 精确匹配**）、`venue_rules.py`（**会议论文集识别**）、`abstract_fill.py`（**摘要补全**）、`relevance_scorer.py`（BGE 排序）、`crossref_fallback.py`（备用源）。

## 使用方法

**Phase 1**（Agent 写驱动脚本调用函数，无命令行入口）：

```python
import sys
SKILL_DIR = r"<技能资源基底>"   # 取宿主给出的 Base directory，不要写死绝对路径
sys.path.insert(0, SKILL_DIR)
from semantic_scholar_search import SemanticScholarSearcher, ApiUnavailableError

s = SemanticScholarSearcher()
try:
    papers = s.search_paginated(query="...", total_limit=100,
                                publication_types=["JournalArticle", "Review"])
except ApiUnavailableError as e:
    ...   # 必须立即向用户汇报，不得当作"0 篇结果"，不得改用匿名请求
```

**Phase 2**：

```bash
python paper_details.py <paper_id> [<paper_id> ...]
python paper_details.py --json 文献检索_筛选结果_xxx.json
```

**分区回填与摘要补全**：

```bash
python paper_table.py --xlsx 论文元数据.xlsx --enrich-zone        # 回填中科院分区
python paper_table.py --xlsx 论文元数据.xlsx --filter --zone 1,2  # 只看 1~2 区
python abstract_fill.py 论文元数据.xlsx                            # 补齐缺摘要的行
```

**数据源三层**（各管一段，不可互相替代）：

| 层 | 来源 | 能做什么 | 限制 |
|---|---|---|---|
| 一 | **Semantic Scholar** | 主检索：标题/作者/年份/期刊/DOI/摘要/OA | 关键词检索、无布尔、约 1 请求/秒、摘要覆盖约 84% |
| 二 | **Crossref** | 备用检索（S2 不可用时，需用户同意） | 只覆盖有 DOI 的文献；摘要约 28% |
| 三 | **Elsevier API** | **只补 Elsevier 旗下论文的摘要** | 仅 `10.1016/*` 的论文；需 API key + 机构订阅 + **机构 IP**（授权按 IP，不按 key） |

Elsevier key 从 <https://dev.elsevier.com> 注册获取，存 `~/.dsh/env.json` 的 `env.ELS_API_KEY`（代码不硬编码）。**校外无机构 IP 时该通道只会返回标识符、拿不到摘要**，属预期行为。

**备用源 Crossref（必须先获用户同意，不得擅自启用）**：

S2 不可用时，**先向用户汇报失败情况，由用户决定是否改用 Crossref**。脚本带硬门禁，未加 `--approved-by-user` 会直接拒绝执行（退出码 3）。

```bash
python crossref_fallback.py --approved-by-user --out <目录> \
  --keywords "nonlinear,contact,debonding" "查询1" "查询2"
```

**Crossref 的硬性局限（必须在汇报中同时告知用户）**：

- **只能检索到有 DOI 的文献**。无 DOI 的会议论文、区域性期刊、技术报告、标准等一律检索不到。
- **摘要覆盖率低**（实测约 28%），多数条目只能靠标题判断相关性。
- `query` 是**相关性排序检索**，只返回"最相关的前 N 条"，没有相关阈值，因此**不存在"确实未命中"**。
- 单次 `rows` 上限 **1000**（实测 2000 被拒）；要更多需用 `cursor` 翻页。
- ⚠️ **`cursor` 翻页必须同时显式传 `sort=relevance`**。实测漏掉它时，返回结果彻底失去相关性排序（前 100 条相关条目从 44 条跌到 1 条，内容变成牙种植体、超声心动图等无关文献）。已固化在 `crossref_fallback.py`，不得删除。

## 如实汇报（最高优先级）

**调研过程中的任何异常都必须如实、完整地汇报，不得隐瞒、美化或省略。** 结果交付时必须同时说明"完整性与可信度"。

必须如实说明的情形：

| 情形 | 必须说明 |
|---|---|
| 完全没检索到文献 | 区分两种性质：**确实未命中**（查询正常完成、返回 0 条）还是**检索失败**（429 / 密钥不可用 / 网络异常）。后者**不能**得出"该领域没有文献"的结论 |
| 只检索到部分文献 | 说明哪些查询失败、哪些达到抓取上限（可能仍有更多未取回）、范围被哪些筛选条件收窄 |
| 中途换过数据源 | **换源必须先经用户同意**；说明何时、为何换源（如 S2 → Crossref）及口径差异（Crossref 只覆盖有 DOI 文献） |
| 分区数据缺失 | 说明多少条因分区未收录被排除，可能漏掉本属 1/2 区的期刊 |
| 摘要补全不齐 | 说明"补到 X/Y 篇"、各来源分布、未补到的是哪些篇与原因（最有价值的是区分"这篇本来没摘要"和"无订阅权限/校外 IP"） |
| 人工去噪 | 说明剔除标准与数量，避免被当成全部命中 |
| Phase 2 取回不齐 | 说明"取回 X/Y 篇"及未取回的 ID |

**强制留痕**：`SemanticScholarSearcher` 会记录逐查询结果，必须调用并交付检索完整性报告：

```python
searcher.save_search_report(OUT)     # 产出 检索完整性报告.txt / .json
rep = searcher.search_report()       # 结构化数据，写入调研日志
```

关键字段：`complete`（结果是否完整）、`queries_failed`、`queries_capped`、`papers_returned`、`notes`（自动生成的"须告知用户事项"）、`detail`（逐查询 status：`ok` / `capped` / `failed`）。

## 注意点

1. **失败即上报，绝不静默**：首次尝试失败后，各等待 **10s** 重试，共 **3 次请求**（累计约 20 秒）；仍失败即抛 `ApiUnavailableError`，**暂停检索并立即向用户提出二选一问题（按用户偏好排序）**：
   > **1) 持续等待 3~5 分钟后重试**（首选：一般情况下优先等 S2 自行恢复）；还是 **2) 降级使用 Crossref 备用源**（最后手段）？

   不得自行决定，不得继续遍历剩余查询，不得把失败当作"0 篇结果"。
2. **换源必须经用户同意，且 Crossref 是最后手段**：S2 不可用时**先提问、后等用户决策**，不得擅自改用 Crossref。**用户的默认倾向是等待恢复**，不是换源。启用 Crossref 时**必须同时说明它只能检索到有 DOI 的文献**、摘要覆盖率低、且只返回相关性前 N 条。（Crossref 自身的限流/邮箱问题暂不处理，优先级低。）
3. **不使用匿名请求**：未配置密钥直接报错；密钥请求失败也**不降级为匿名请求**（匿名走全局共享池，稳定性不可控）。不允许用"匿名兜底"。
4. **不持续等待（需用户授权才可长等）**：默认到点即停。用户选择"持续等待"分支时，才把 `long_wait_on_failure = True`（静默 240 秒）重跑，且**只追加一次尝试**（共 4 次请求）。
5. **失败不等于密钥失效**：2026-09-11 实测确认，S2 限流后会进入**约 60~110 秒的封锁期**，期间任何请求都是 429（静默 60s 后仍 429，约 44s 后自行恢复）；限流由 AWS API Gateway **按 key** 施加，**key 本身有效**。因此**不得向用户报告"密钥失效"**，应报告"服务端限流封锁中"。
6. **禁止并发**：密钥约 1 请求/秒（脚本已设 2.5 秒间隔）。同一时间只跑一个检索进程，多查询放进同一个脚本串行循环。
7. **不要凭单次成功判断恢复**：同一请求几秒内可能一次 200、一次 429；实测 1.5 秒间隔也会 429，1.0 秒间隔反而成功——**无规律，不能据此推断安全**。
8. **分区只做中科院《新锐期刊分区表》（LetPub，1–4 区）**：JCR（Clarivate，基于 JIF 的四分位）**没有可用的免费数据源**——需 Web of Science / JCR 订阅，Clarivate API 付费，LetPub 检索页也不显示 JCR 四分位。第三方站点自算的"四分位"（如 journalmetrics 的 OpenAlex 口径）**不是 JCR**，不能当 JCR 汇报。故本 skill 只提供中科院分区，`--enrich-zone` 回填「中科院分区」列，`--zone 1,2` 筛选；传 `Q1/Q2` 会被**显式报错**拦下。
   - **默认不按分区筛**：检索阶段分区是空的，要筛必须显式传 `quartile_filter=["1","2"]`。
   - **查分区必须优先用 ISSN**（`letpub_zone(issn=…)`）：按刊名查会因缩写漏匹配（`Comput. Aided Civ. Infrastructure Eng.` 这类）。表里没有 ISSN 列，`--enrich-zone` 会自动用 Crossref 按 DOI 反查并缓存到 `.issn_cache.json`。
   - ⚠️ **LetPub 单次查询会偶发返回空页**（实测 CBM 一次通一次空、MSSP 连续两次空）。旧实现把 `None` 永久写进缓存，导致 Materials、Applied Sciences、IEEE TIM、MSSP 等主流刊被判"未收录"。现在未收录项 **30 天后自动重查**，`--refresh` 可强制重查。
   - ⚠️ **必须串行**：并发 >4 会被 LetPub 限流返空。单本约 **2.6~3.6 秒**（已改为优先 `--backend curl_cffi`，默认三级升级链要 7~9 秒）。
9. **翻译可选**：缺 `ANTHROPIC_AUTH_TOKEN` 时自动跳过并保留英文，不算失败。
10. **S2 是关键词检索，不是语义检索（2026-09 实测，详见 `AGENTS.md` §3.5）**：`/paper/search` **无布尔语法**（写 `AND`/`OR`/引号会被当普通文本）；匹配范围**含摘要**，所以技术术语可以放心写进查询词。⚠️ **查询词越长，实际返回量越少**（2026-09-20 对照实测：7 词→100 篇，同一查询加到 9 词→**11 篇**，13 词→**6 篇**）—— **单条查询最多 5 个关键词，超限代码直接报错**。详见 `AGENTS.md` §3.5.4.1。⚠️ **`total` 字段不可信，绝不能用它说明"某领域有多少文献"**（实测加一个词会导致数值涨 65 倍）。需要布尔语法或超过 1000 条时，改用 `/paper/search/bulk`。
11. **标准检索方案（用户 2026-09-20 定稿，详见 `AGENTS.md` §3.6）**：**固定 3~4 轮关键词，每轮 ≤5 词、互为微调变体，每轮取 500 篇（默认 = 5 页，可自定义）** → **BGE 每组独立评分** → 合并去重、按分全局降序 → 取前 N（**默认不截断**）→ 一次性落盘成一个 Excel → 之后按需读取。⚠️ **每组关键词只给它自己检索到的文献评分，绝不串组**。一步到位的入口是 `search_pipeline.run()`。
    - **每轮篇数默认 500，用户可自定义**：`run(per_round=300)` 或 `--per-round 300`（300 → 每轮 3 页）。**入表篇数另由 `top_n` / `--top-n` 控制（默认不限 = 闸门后全量入表），两者独立**。⚠️ **三个数别混**：单页 100 篇（S2 硬上限）→ 每轮 500 篇 = **5 次请求** → 入表默认不截断（要压小用 `--top N`）。只有改"每轮"才增加请求。
    - ⚠️ **不做探针轮**。轮的"微调变体"本身就承担试探作用，不要再单独发请求试词（那既费调用数，还可能把 key 推进 429 封锁期）。
    - ⚠️ **请求间隔 4 秒**（`SemanticScholarSearcher.REQUEST_INTERVAL = 4.0`，2026-09-20 由 2.5 上调）。官方限 1 RPS，留足余量换取更少 429。
    - ⚠️ **API 调用预算：`ceil(per_round/100) × 轮数`**，默认即 5×3~4 = **15~20 次请求**（+少量 429 重试；4 轮 500 篇的实测就是 20 次）。检索响应已直接带回 **DOI / 摘要 / OA 链接**，**不要再跑 Phase 2 逐篇补详情**（实测 DOI 覆盖 88~95%、摘要 74%，调用数 +0）。
    - ⚠️ **S2 不可用即刻上报**：任一轮失败即保全已取回结果、落盘完整性报告并抛出，立即向用户汇报（不静默、不换源、不用匿名请求）。
    - ⚠️ **BGE 排序是「词面」相似度，不等于语义相关度**：纯排序会把与题目无关的论文（纯综述、别的领域的同名术语）排到前面，把最对题的压到几十名开外。**向用户交付"最相关的 N 篇"时，必须先过主题闸门 `topic_gate.py` + 人工复核**。
    - ⚠️ **闸门是按主题配置的，不是一套词表打天下**。每次检索都要为**本次题目**准备一条 profile：**2~3 个闸门（轴），每轴约 50 词**。内置的 `building-envelope-ndt` 只覆盖「建筑围护结构/外保温无损检测」；实测拿它去判别的题目，13 个真实标题只过 3 个（通过率 0% 的题目有两个）。
    - ✅ **闸门已接进流水线**：`run(..., gate_profile="<id>")`，顺序是**全量排序 → 过闸门 → 截断到 top_n**（顺序颠倒会浪费名额）。返回值带 `gate` 统计（before/after/rejected/靠标题命中/靠摘要命中）。全被剔除时**直接报错**，不静默交付空表。
    - ✅ **闸门按「标题 + 摘要」判**（摘要随检索已带回，覆盖 74~76%）。方法只写在摘要里、标题没提的论文不再被误杀。返回的 `_source` 标明这次通过靠标题还是靠摘要 —— 靠标题的更可信。
    - 用法：`run(..., gate_profile=...)`／`topic_gate.gate(标题, profile, 摘要)`／`apply_gate(列表, profile)`（**自带"过窄自检"**：放行率低于阈值会告警"配置过窄、正在误杀"）／`save_profile(...)` 存进 `gate_profiles.json`；命令行 `python topic_gate.py --list-profiles`、`--explain "标题" --profile <id>`；检索侧 `--gate-profile <id>`。闸门配置与真实分数要落盘留痕，**不得改动打分数值**。详见 `AGENTS.md` §3.5.4.2。
12. **BGE 用英文分支，内存靠"退进程"归还（详见 `AGENTS.md` §3.6）**：论文检索固定 `lang="en"`（实测查询词即便是纯中文，也只加载 `bge-base-en-v1.5`，不会拉起 780 MB 的中文分支）。BGE 用 `bge-base`（与知识库同一套，本地有缓存），**首次加载约 100 秒**。⚠️ **进程内无法把内存还给系统**（实测 `del`+`gc` 只还 7 MB，工作集修剪是假象）——**唯一可靠的方式是让进程退出**，所以要么用 `search_pipeline.run()` 一次做完再退，要么别在一个长驻进程里反复加载。
13. **会议论文集要靠刊名识别，`publication_types` 挡不住**：S2 会把 `e-Journal of Nondestructive Testing`（NDT.net 会议录）、`Journal of Physics: Conference Series`、`E3S Web of Conferences`、`INTER-NOISE` 这类**标成 `JournalArticle`**。实测 553 篇里标 Conference 的仅 27 篇，另有 **99 篇**属此类。`venue_rules.is_conference_venue()` 负责按刊名判，落盘时写进「疑似会议」列；`filter_papers` 的 `journal_only` 已接入。⚠️ 白名单保证 `Proceedings of the Institution of Mechanical Engineers` 这类**正规期刊**不被误杀。

## 容易遇到的问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `ApiUnavailableError: Semantic Scholar 连续 3 次请求均失败` | 服务端限流封锁（约 60~110 秒） | **立即向用户提问**：降级 Crossref，还是等待 3~5 分钟后重试。**不要说成"密钥失效"** |
| 想直接换用 Crossref | — | **不得擅自换源**。先提问，并说明 Crossref 只能检索到有 DOI 的文献、摘要覆盖率低，由用户决定 |
| `未配置 SEMANTIC_SCHOLAR_API_KEY` | 密钥缺失 | 提示用户配置；不使用匿名请求兜底 |
| 长时间无输出，最后返回 0 篇 | 旧版静默失败 | 已由失败上报机制修复；确认捕获了 `ApiUnavailableError` |
| 检索结果为 0 篇 | 可能确实未命中，也可能是查询失败 | 用 `search_report()` 判断：`complete=true` 才算"确实未命中"，否则须说明是检索失败 |
| 只取回部分文献 | 部分查询失败或达到抓取上限 | 报告中的 `queries_failed` / `queries_capped` 会指出；汇报时必须说明结果不完整 |
| 报告把故障记成 `[ok]` | 请求失败被当成"空结果"（v1.3.4 已修） | 只有 `200` + 空 `data` 才是 `ok`；`5xx`/网络错误一律 `failed` |
| 失败后部分结果丢失 | 翻页中断时 `return` 未执行（v1.3.4 已修） | 捕获异常后必须用 `searcher.collected_partial()` 取回并落盘 |
| 结果大量 off-topic | 关键词过宽（如泛用 GPR/thermal） | 缩窄查询词，对标题做相关性打分后再呈现 |
| 分区大量 unknown | 该刊未被分区库收录 | 属预期，说明局限；需要中文分区时改用 `--letpub --issn <ISSN>` 重查 |
| 输出目录不符预期 | 默认 `F:\Agent\DeepseekHarness\Interaction` | 设 `LITSEARCH_OUT_DIR`，或按 `AGENTS.md` 归档规范指定 |

## 资源导航

| 资源 | 何时读 |
|---|---|
| `AGENTS.md` | 排错细节、2026-09 故障复盘、文件归档规范、驱动脚本模板 |
| `AGENTS.md` §3.6 | **论文元数据表**（Excel 落盘 + 按需读取）的完整用法与三条硬约束 |
| `references/examples-and-details.md` | 产出文件格式（初筛 / 筛选结果 / JSON）、4 个使用示例、API Key 申请、速率限制、BGE 排序原理、paper_id 与 DOI 索引用法 |

脚本都在本 skill 目录下。引用时按宿主给出的**技能资源基底**解析相对路径，不要写死绝对路径（技能可能来自远程来源）。
