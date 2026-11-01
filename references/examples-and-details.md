# 文献检索 Skill — 参考文档

## 参数传递方式（唯一）

**只使用 `x-api-key` 请求头**，与官方 tutorial 和 API Key 邮件一致：

```python
headers = {"x-api-key": api_key}
```

- 不使用 URL 参数（`?api_key=`）传参；
- **任何情况下都不降级为匿名请求**（匿名走全局共享池，稳定性不可控）。

> 历史说明：v1.2.x 曾实现"Header ↔ URL 参数双通道切换"重试，理由是"某些代理会剥离 URL 参数或 Header"。
> 该机制已于 v1.3.1 **彻底移除**——同一密钥换传参方式对限流没有任何作用，
> 只会让排查更混乱（见 `AGENTS.md` §4.2 实测）。若在旧文档中看到"双通道"，均为过时描述。

Python 实现参考：`semantic_scholar_search.py` 中的 `_make_request` 方法。

## 保存文件格式

### 初始数据文件：`文献检索_初始数据_YYYYMMDD_HHMMSS.txt`

包含所有检索到的论文，只含基本信息（无摘要）。

```
============================================================
文献检索 - 初始数据
============================================================
搜索词: xxx
检索时间: xxx
检索结果: xxx 篇
------------------------------------------------------------

[论文1]
Title: xxx
Authors: xxx
Journal: xxx
Year: xxx
Citations: xxx

[论文2]
...

============================================================
```

### 筛选结果文件：`文献检索_筛选结果_YYYYMMDD_HHMMSS.txt`

包含筛选后的论文，含摘要、DOI、PDF链接、BGE分数。

```
============================================================
文献检索 - 筛选结果
============================================================
搜索词: xxx
筛选条件: xxx
检索时间: xxx
筛选结果: xxx 篇
------------------------------------------------------------

[论文1]
Title: xxx
Authors: xxx
Journal: xxx
Year: xxx
Citations: xxx
Quartile: 1区
DOI: 10.xxxx/xxxxx
PaperID: xxxxxxxxxx
BGE Score: 5.82
PDF: https://xxx
Abstract: xxx

[论文2]
...

============================================================
```

### JSON 文件：`文献检索_筛选结果_YYYYMMDD_HHMMSS.json`

```json
{
  "query": "external wall insulation NDT",
  "filter": "Year: 2020-2026, 中科院 1/2 区, journal_only",
  "timestamp": "2026-07-31T14:00:00",
  "count": 45,
  "papers": [
    {
      "paper_id": "abc123def456",
      "title": "Infrared thermography for building facade inspection",
      "authors": ["Zhang W", "Li X", "Wang Y"],
      "year": 2022,
      "venue": "Building and Environment",
      "quartile": "1",
      "citation_count": 35,
      "is_conference": false,
      "is_journal": true,
      "is_preprint": false,
      "doi": "10.1016/j.buildenv.2022.xxxxx",
      "pdf_url": "https://xxx",
      "abstract": "This paper presents...",
      "relevance_score": 5.82
    }
  ]
}
```

**索引字段**：
- `paper_id`：Semantic Scholar 论文 ID，Agent 通过此 ID 调用 API 查询论文详情（摘要、引用网络等），支持批量查询
- `doi`：DOI 标识符，供用户通过 https://doi.org/{doi} 访问论文原文

---

## 使用示例

### 示例1：基础搜索

```
输入：搜索 "knowledge graph construction" 的论文

预期：
  1. search API 获取基本信息
  2. 无筛选条件，全部通过
  3. batch API 获取摘要
  4. 保存初始数据 + 筛选结果（txt + json）
```

### 示例2：带 API 级筛选

```
输入：搜索 "deep learning"，仅期刊论文，中科院 1 区，2020-2026年，按引用量排序

预期：
  1. search API 带参数：publicationTypes=JournalArticle,Review, sort=citationCount:desc, year=2020-2026
  2. 客户端筛选：排除预印本
  3. 中科院新锐分区查询（`--enrich-zone`）+ 过滤 1 区
  4. BGE 排序
  5. batch API 获取摘要
  6. 保存结果
```

### 示例3：多轮检索 + BGE 排序

```
输入：搜索 "外墙保温层无损检测技术" 的相关论文

预期：
  1. Agent 将中文主题翻译为多个英文查询词：
     - "external wall insulation non-destructive testing"
     - "infrared thermography building facade insulation"
     - "ground penetrating radar wall insulation"
     - "acoustic impact tile debonding facade"
  2. 每个查询词分别检索 100 篇
  3. 合并 + 按 paper_id 去重
  4. 客户端筛选（排除会议/预印本）
  5. 中科院新锐分区查询（`--enrich-zone`）+ 过滤 1–2 区
  6. BGE 多关键词标题排序
  7. batch API 获取摘要
  8. 保存结果（含 BGE 分数和 paper_id/DOI 索引）
```

### 示例4：引用网络扩展

```
输入：从一篇种子论文扩展引用网络

预期：
  1. 使用 --expand 参数指定种子论文 ID
  2. 调用 get_paper_details() 获取引用和参考文献
  3. 返回扩展后的论文列表
  4. 可选：对扩展结果进行 BGE 排序
```

---

## API Key 申请

1. 打开 https://www.semanticscholar.org/product/api#api-key-form
2. 填写表单，Key 秒批发送到邮箱
3. 格式：`s2k-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
4. 设置环境变量：`setx SEMANTIC_SCHOLAR_API_KEY "你的Key"`

## 速率限制

| 限制 | 值 |
|------|-----|
| 单次请求间隔 | **2.5 秒**（v1.4.0；此前为 1.5 秒，实测 1.5 秒仍会触发 429） |
| batch API 单次上限 | 500 篇 |
| search API 单页上限 | 100 篇 |
| 单查询结果硬上限 | **1000 条**（`offset + limit ≤ 1000`）。2023-10-31 起由 10000 下调；超出须改用 `/paper/search/bulk` |
| 查询语法 | `/paper/search` **纯文本关键词，无布尔语法**。需要 `+`(AND) / `\|`(OR) / `-`(排除) / `""`(短语) / `*`(前缀) / `~N`(编辑距离) 须改用 `/paper/search/bulk` |
| `total` 字段 | **近似值，不可信**。实测 `debonding` = 27,604 而 `debonding nonlinear` = 1,803,131（涨 65 倍）。**禁止用于覆盖率汇报** |

---

## BGE 排序说明

### 模型（v2.0 起）

**与结构化知识库（`Agent-Structured-KB` 的 `db.py` / `tools/reranker.py`）使用同一套模型。**

| 模型 | 维度 | 本地缓存 | 用途 |
|------|------|------|------|
| `BAAI/bge-base-en-v1.5` | 768 | ~419MB | 英文查询 |
| `BAAI/bge-base-zh-v1.5` | 768 | ~781MB | 中文查询 |

本机 `~/.cache/huggingface/hub/` 已有缓存，**不需要联网下载**（可用 `HF_HUB_OFFLINE=1` 强制离线验证）。
⚠️ **首次加载实测约 100 秒**（CPU）。知识库那个常驻服务里的模型在**它自己的进程**内，
Python 进程之间无法共享，所以论文检索每次开新进程都要重付这笔时间。

> v2.0 之前用的是 `bge-small-*`（384 维），与知识库不一致，已改。

### 评分逻辑

```
查询: "external wall insulation non-destructive testing"
    ↓ 按空格拆分 + 去停用词
关键词: ["external", "wall", "insulation", "non", "destructive", "testing"]
    ↓ BGE 编码 (每个关键词独立编码为 768 维向量)
    ↓ 相似度矩阵 (6个关键词 x N篇论文)
    ↓ 每篇论文得分 = 各关键词相似度的【平均值】（按列求均值）
    ↓ 按得分降序排列（仅排序，不删除任何论文）
```

**为什么是平均而不是求和**：组内排序两者等价（关键词数固定时单调），
但**跨组比较必须用平均** —— 否则关键词多的那组得分天然偏高，多组合并排序时会整体占优。

### 多组关键词：每组独立评分（`rank_groups`）

**硬性约束：每一组关键词只给它自己检索到的文献评分，绝不能用别组的关键词去给本组文献打分。**

```python
from relevance_scorer import RelevanceScorer
s = RelevanceScorer()
merged = s.rank_groups([(q1, papers1), (q2, papers2), (q3, papers3)],
                       total_limit=200)
```

- 同一标题在多个组出现时，**标题的编码只算一次**（复用向量），但**分数仍按组独立计算**；
- 一篇论文若被多组检索到，取它在各组中的**最高分**（它最匹配的那个查询决定其相关性）；
- 输出为合并去重、按得分全局降序的列表。

实测正负对照（同一批文献分别用本组/别组关键词评分）：本组平均 **0.5843** vs 别组 **0.5121**，
5/5 全部本组更高 —— 证明评分确实按组独立。

### 使用场景

1. **相关性排序**：搜索完成后，用 BGE 对结果按与查询的相关性重新排序
2. **多组关键词合并**：见上方 `rank_groups`
3. **已有文献对比**：用 BGE 语义相似度替代 Jaccard 字符相似度，更准确地识别重复论文（阈值 0.85）

---

## paper_id / DOI 索引使用

JSON 输出文件中每篇论文都包含 `paper_id` 和 `doi` 字段：

### paper_id — Agent 使用

Agent 通过 `paper_id` 调用 Semantic Scholar API 查询论文详情：

```python
# 查询单篇论文详情（含摘要、引用列表）
searcher = SemanticScholarSearcher()
details = searcher.get_paper_details("abc123def456")

# 批量查询论文摘要
paper_ids = ["id1", "id2", "id3", ...]
details = searcher.search_second_round(paper_ids)

# 引用网络扩展
papers = searcher.expand_citations(["abc123def456"], direction="both")
```

### DOI — 用户使用

DOI 供用户通过浏览器访问论文原文：
- 访问方式：`https://doi.org/{doi}`
- 示例：`https://doi.org/10.1016/j.buildenv.2022.xxxxx`
