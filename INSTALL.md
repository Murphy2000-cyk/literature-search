# 安装说明

> **当前版本：v2.2.0**（六个环节 + 原始响应落盘 + 领域开关）。
> 版本历史见 `CHANGELOG.md`。

本文件面向**使用这个 skill 的人**。如果你是把本仓库交给 AI 的人，看完第一节就够了；
后面几节是给你的 AI 看的执行细节，也可以由你按需执行。

---

## 一、三句话上手

1. 把这个仓库地址给你的 AI：
   `https://github.com/Murphy2000-cyk/literature-search`
2. 告诉它：**「装到技能目录，然后按仓库里的 INSTALL.md 配置」**
3. **三个 API Key 需要你自己去申请**（AI 不能替你注册）：`SEMANTIC_SCHOLAR_API_KEY`（必需）、
   `EASYSCHOLAR_SECRET_KEY`（不配就没有分区数据）、`ELS_API_KEY`（可选，只影响 Elsevier 摘要），地址在第三节

> 如果你的 AI 只能读网页、不能执行命令，改用压缩包直链：
> `https://github.com/Murphy2000-cyk/literature-search/archive/refs/heads/main.zip`
>
> 仓库地址给的是**最新代码**；想拿某个已发布的稳定版，去 Releases 页下载对应 tag 的压缩包。

---

## 二、装到哪里

> **如果你用的不是 DSH**（Claude Code、或任何支持 Agent Skills 的智能体（Agent）），直接跳到
> **[第七节](#七装到-dsh-以外的-agent)**。那里讲装到哪里、Key 放哪、哪些能力依赖外部命令。
> 本节的路径是 DSH 的惯例位置，不是通用要求。

技能目录（DeepSeek Harness 的惯例位置）：

```
~/.agents/skills/literature-search/
```

Windows 上是 `C:\Users\<你的用户名>\.agents\skills\literature-search\`。
装完这个目录里应当能直接看到 `SKILL.md`、`search_pipeline.py` 等文件，而不是多套一层文件夹。

### 依赖

Python 3.10 以上。第三方包：

```bash
python -m pip install requests sentence-transformers torch numpy openpyxl psutil
```

| 包 | 缺了会怎样 |
|---|---|
| `requests` | **必需**，缺了直接不能用 |
| `sentence-transformers` + `torch` | 用于 BGE 相关性排序；缺了会跳过排序（降级，不算失败） |
| `openpyxl` | 读写 Excel 表用，六环节每张表都依赖它 |
| `numpy` | 依赖 |
| `psutil` | `bge_status.py` 查内存占用必需；`relevance_scorer.py` 只用它打印内存，缺了照常排序 |
| `curl_cffi` | 不是本仓库的依赖，是可选命令 `searchts` 自带的要求；缺了只是抓取链退回较慢的一档 |

BGE 模型首次使用会自动下载（约 400 MB），之后本地缓存。

### 装完应该有的文件

```
literature-search/
├── SKILL.md / AGENTS.md / README.md / CHANGELOG.md / INSTALL.md / LICENSE / .gitignore
├── search_pipeline.py          六环节编排入口（首选调用方式）
├── semantic_scholar_search.py  检索、分页、去重、完整性报告
├── keyword_fetch.py            OpenAlex 批量取关键词与摘要
├── paper_table.py              xlsx 落盘与按需读取
├── journal_quartile.py         分区查询（easyScholar 主通道，默认 JCR）
├── relevance_scorer.py         BGE 排序（rank_topic 单句主题）
├── venue_rules.py              会议论文集识别
├── abstract_fill.py            摘要补全（Elsevier + PubMed）
├── paper_details.py / translator.py / crossref_fallback.py / topic_gate.py / bge_status.py
├── gate_profiles.json          主题闸门配置（模块已不接入流水线）
├── references/
│   ├── examples-and-details.md  产出格式与使用示例
│   ├── 实测记录.md               实测数字集中存放
│   ├── easyscholar-api.md       分区接口字段陷阱与调试清单
│   └── journal-name-issues.md   刊名匹配不上时的规范化规则
└── evals/                      六个自测脚本 + 8 个用例说明
```

`data/journal_zone.xlsx` 不在仓库里，首次查询分区时会自动创建。

---

## 三、必须自己申请的 Key

### 1. Semantic Scholar API Key（必需）

- 申请地址：`https://www.semanticscholar.org/product/api`
- 页面里点 **Request an API Key**，免费，通常几分钟内发到邮箱，形如 `s2k-` 加 40 位字符
- **在校学生建议用学校邮箱申请，单位填学校名称**
- 速率上限：**约 1 请求/秒**（跨所有端点累计），邮件里会写明

### 2. Elsevier API Key（可选，但缺了补不到 Elsevier 摘要）

- 申请地址：`https://dev.elsevier.com`
- 点 **My API Key** 或首页的 **I want an API Key**，注册开发者账号免费
- **注意：注册拿到 Key 不等于有数据权限。** Elsevier 按机构订阅加来源 IP 授权，
  **需要在校园网内使用**；离开校园网后取不到摘要
- 只影响「摘要补全」这一环；没有它，其余功能照常工作

---

## 四、Key 放在哪

**不要写进技能目录，也不要写进代码。**统一放这里：

```
~/.dsh/env.json
```

格式：

```json
{
  "env": {
    "SEMANTIC_SCHOLAR_API_KEY": "s2k-...",
    "ELS_API_KEY": "..."
  }
}
```

脚本启动时会自动从这个文件补载，即使由不继承环境的子进程运行也能生效。

也可以用环境变量（`setx` 只影响之后新启动的进程，改完要重启终端）：

| 变量 | 用途 |
|---|---|
| `SEMANTIC_SCHOLAR_API_KEY` | 必需 |
| `EASYSCHOLAR_SECRET_KEY` | **分区主通道**（中科院两版 + JCR + IF + 预警），不配就没有分区数据 |
| `ELS_API_KEY` | Elsevier 摘要补全 |
| `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` | 标题与摘要翻译（可选） |
| `LITSEARCH_OUT_DIR` | 覆盖默认输出目录（默认为 `~/literature-search-out`） |

### easyScholar 的分区密钥（v2.2.0 起为分区主通道）

分区数据的主来源是 **easyScholar**，一次请求同时拿到中科院分区（升级版与基础版）、JCR 四分位、
影响因子、五年影响因子、中科院预警。

1. 打开 <https://www.easyscholar.cc> 注册账号
2. 左侧菜单「开放接口」，页面上能看到 `SecretKey`
3. 写进 `~/.dsh/env.json`：

```json
{
  "env": {
    "EASYSCHOLAR_SECRET_KEY": "你的32位密钥"
  }
}
```

四条要记住：

- **这个接口不与会员绑定，免费对所有用户开放**，不充会员也能用
- **限速每秒最多 2 次请求**，代码里按 1 秒 1 次节流
- **SecretKey 无法更改**，泄露了只能换账号，不要贴到任何公开的地方
- 查询结果落到 `data/journal_zone.xlsx`，每行带查询日期，已收录 60 天、未收录 30 天过期
- ⚠️ **不配这把 Key 的后果**：分区查不到，第 5 环节会把全部记录按「无数据可判」排除，
  最后得到 0 篇且不报错。要么配上它，要么显式加 `--allow-letpub`，或把分区条件放宽为不限。

### LetPub 备用通道（需授权）

LetPub 只有中科院分区，没有 JCR 与影响因子。它是备用通道，**默认关闭**：
没有配置 easyScholar 密钥、或者 easyScholar 查不到时，只有显式传 `--allow-letpub`
或把代码里的 `ALLOW_LETPUB` 置 True，才会去抓 LetPub 页面。

```bash
python journal_quartile.py --allow-letpub --issn 0008-8846
```

`data/journal_zone.xlsx` 已在 `.gitignore` 里，属本地资产，不随仓库分发。

---

## 五、怎么用

直接用自然语言说需求即可，例如：

> 帮我检索「外墙外保温系统无损检测」的英文文献，只要中科院 1–2 区、2020 年以后、期刊论文。

能说清这五件事，结果就基本可控：

| 要说的 | 说明 |
|---|---|
| 研究方向 | 决定英文主题描述句，BGE 按它打分 |
| 关键词 | 不给的话它自己组 4 组，每组不超过 5 个词 |
| 分区要求 | **默认 JCR 1 区**；要中科院分区用 `--zone-basis xr` |
| 时间范围 | **默认近 10 年**（`--years` 可改） |
| 领域 | **`--domain cs` 默认放行会议论文**；其它领域用默认 `general` |
| 论文类型 | 撤稿与会议录卷级条目**永远排除**；预印本默认排除（`--allow-preprint` 开启） |

**六个环节各落一份 Excel**，行数递减、逐级可核对：

```
第1环节_原始检索.xlsx   原始返回行（含跨轮重复）
第2环节_去重.xlsx       按 paperId 去重
第3环节_近N年.xlsx      只设下界；无日期且无年份的排除
第4环节_记录合规.xlsx   撤稿/会议录卷级永远排除；会议按领域；预印本默认排除
第5环节_期刊等级.xlsx   easyScholar 查分区后按 --zone 筛
第6环节_相关度排序.xlsx BGE 打分 + 补摘要
raw/r{n}_p{n}.json      第 0 层：每次 S2 请求的原始响应，权威源
筛选过程记录.md          逐环节进出数 + 淘汰原因（分「不符合条件」「无数据可判」两类）
筛选淘汰记录.xlsx        逐条明细，带淘汰环节与原因类型
检索完整性报告.txt        是否有查询失败/被截断
```

之后可以让它按需读这些表，不必整表塞进上下文。

常用参数：

```bash
--years 10           近 N 年（默认 10）
--zone-basis jcr|xr  分区标准（默认 jcr）
--zone 1,2           要几区（默认 1）
--domain general|cs  领域（默认 general；cs 放行单篇会议论文）
--allow-preprint     放行预印本（默认关）
--allow-conference   强制放行会议论文（默认关）
--top N              只要前 N 篇（默认不限）
```

**输出位置**：默认写到 `~/literature-search-out/`（Windows 上是
`C:\Users\<你的用户名>\literature-search-out\`）。想换地方就设 `LITSEARCH_OUT_DIR`，
或在调用时传 `save_dir`。

---

## 六、常见问题

**Q：打印中文时报 `UnicodeEncodeError: 'gbk' codec can't encode`？**
Windows 控制台默认 GBK。脚本已内置 `sys.stdout.reconfigure(encoding="utf-8")` 兜底，
正常不会出现；若自己写驱动脚本，请照做或设环境变量 `PYTHONIOENCODING=utf-8`。

**Q：哪些数据是必须拿到的？**
题目、DOI、作者、期刊、发表时间、关键词六类必须拿到，缺了会在 `筛选过程记录.md` 里
明确列出并说明是数据源缺失。**摘要非硬性**，能拿多少算多少。

**Q：提示 429，是不是 Key 失效了？**
不是。Semantic Scholar 的 429 是**服务端限流封锁**，实测封锁期约 60–110 秒，
期间静默等待也不会立刻恢复。Key 本身是好的。等几分钟再试，或让它改用备用源（需要你同意）。

**Q：为什么很多文献显示「分区未收录」？**
中科院分区只覆盖约 30% 的期刊，会议论文集、区域性期刊与新刊都查不到。这是数据源的覆盖范围问题，
不是检索没做好。

**Q：有些文献摘要为空？**
多半是 Elsevier 的论文，而当前不在校园网内。Elsevier 的授权按机构 IP，
Key 本身不含内容权限。在校内网重跑一次补摘要即可。

**Q：能不能查中文文献？**
不适合。本 skill 的主数据源是 Semantic Scholar，以英文文献为主。中文文献建议用知网/万方。

**Q：BGE 排序占内存很大？**
`torch` 加载后约 700 MB 起，且进程内无法真正归还给操作系统。所以设计上是「一个进程跑完整个检索任务，
跑完进程退出」——不要在长驻进程里反复调用来指望释放内存。

**Q：我不用 DSH，能不能用？**
能。文件格式不用改，要调整的是装到哪里、Key 放哪、以及一个可选的外部命令。
见[第七节](#七装到-dsh-以外的-agent)。

---

## 七、装到 DSH 以外的 Agent

`SKILL.md` 遵循 **Agent Skills** 规范，frontmatter 只用了 `name` 和 `description` 两个字段，
而规范要求的正好就是这两个。**格式本身不需要改。**

真正要处理的是三件事：装到哪、Key 放哪、一个可选的外部命令。

### 1. 装到哪

形态是固定的：

```
<你的智能体的 skill 扫描根>/literature-search/SKILL.md
```

**不要多套一层文件夹。** 写成 `<扫描根>/literature-search/literature-search/SKILL.md`
不会被识别，而且多数智能体**不报错**，表现成「装了但用不了」，很难查。

装完这个目录里应当能直接看到 `SKILL.md`、`search_pipeline.py` 等文件。

| 智能体 | 放到 |
|---|---|
| DSH（本仓库的原生智能体） | `~/.agents/skills/` 或 `~/.dsh/skills/` |
| Claude Code | `~/.claude/skills/literature-search/` |
| 其他智能体 | 按它的文档找到 skills 目录，形态同上 |

深度是一层，这一点各智能体基本一致。装完记得重新加载，或重启一次会话。

### 2. Key 放哪：跨智能体请统一用环境变量

默认读 `~/.dsh/env.json`，那是 DSH 的约定。**不是 DSH 的话，用环境变量最省事**，三个脚本都认：

```bash
export SEMANTIC_SCHOLAR_API_KEY="s2k-..."
export ELS_API_KEY="..."        # 可选，只影响 Elsevier 摘要
```

Windows 用 `setx SEMANTIC_SCHOLAR_API_KEY "s2k-..."`，改完要重开终端才生效。

代码里还有两条备用路径，但不要依赖，因为它们**不一致**：

| 文件 | 认哪些来源 |
|---|---|
| `semantic_scholar_search.py` | `~/.dsh/env.json` → `~/.claude/settings.json` → 环境变量 |
| `translator.py` | 同上 |
| `abstract_fill.py` | **只有** `~/.dsh/env.json` 和进程环境变量 |
| `journal_quartile.py` / `keyword_fetch.py` | 同上（分区用 `EASYSCHOLAR_SECRET_KEY`，OpenAlex 用占位 mailto） |

所以跨智能体时，**环境变量是唯一三个脚本都认的方式**。

### 3. 一个可选的外部命令：`searchts`

只有一处用到它（且默认关闭）：

| 位置 | 用途 | 缺了会怎样 |
|---|---|---|
| `journal_quartile.py` | 仅 `--allow-letpub` 兜底通道下抓 LetPub 分区页 | 默认通道（easyScholar 接口）不受影响 |

它**不在本仓库里**，是一个独立的第三方命令行工具（MIT 协议，作者是 GitHub 用户 `capad`）：
`https://github.com/capad-xyz/searchts`

**没有它也完全能用。** 检索、BGE 排序、落盘 Excel、按需读取都不依赖它。
v2.2.0 的 `--enrich-zone` 默认走 easyScholar 接口，不需要这个命令；
只有显式加 `--allow-letpub` 时才会去找它，找不到会报一句提示并回落到默认通道，不会崩。

### 4. 触发条件可能要改

`description` 目前是中文写的。如果你的模型主要用英文对话，它可能匹配不上。两种做法：

- 改成英文，或中英并列；
- 不改，用你的智能体提供的方式手动调用。

两个智能体对 `description` 的长度上限不同：DSH 这边按 400 字符算，Agent Skills 规范给 1024。
当前版本两百来字，两边都够，不用动。

### 5. 平台与路径

在 Windows 上开发，代码有跨平台分支，但 **Linux 与 macOS 的实测覆盖较少**。

文档里有几处示例路径是 Windows 形态（`<Python安装目录>\python.exe`、
`%USERPROFILE%\.agents\skills\...`），**那些只是示例**，按你自己的环境改。

### 6. 装完怎么确认

| 检查 | 做法 | 说明 |
|---|---|---|
| 能不能被发现 | 说一句「帮我搜几篇文献」 | 没有任何反应，多半是多套了一层文件夹 |
| 依赖齐不齐 | `python bge_status.py` | 会列出当前加载情况 |
| 文件结构对不对 | `python topic_gate.py --list-profiles` | 能列出配置就说明模块都在 |
| Key 有没有读到 | 跑一次小检索 | 返回 401 或「未配置密钥」就是 Key 的问题 |

`bge_status.py` 和 `topic_gate.py --list-profiles` 都不联网，适合当第一步体检。

### 7. 规范与写法参考

- **判断能不能用，以规范为准**：`https://agentskills.io/specification`
- **写法参考**：`https://github.com/anthropics/skills` 里的 `skills/skill-creator`
- **最小模板**：同一个仓库的 `template/`

`skill-creator` 是教你怎么写一个 skill 的；**规范才是判断「我这个 skill 在别的智能体上能不能跑」的依据**，
两者不要混用。

---

## 八、从旧版升级

覆盖安装即可，不需要卸载；`data/` 目录（分区表）留着，不必删。从 v2.0.1 升上来有四处要注意：

1. **分区标准默认由中科院新锐改为 JCR 四分位 1 区**。沿用旧参数会得到不同的筛选结果，
   想继续用中科院口径就显式加 `--zone-basis xr`。
2. **数据表新增 14 列**（发表日期、ISSN、关键词、分区依据、分区状态、摘要来源、是否撤稿、
   版面元素、记录可疑、命中轮次、命中轮数、年份判定依据、淘汰环节、淘汰原因）。
   按旧列序读表的脚本需要调整。
3. **旧分区缓存 `.quartile_cache.json` 首次运行会自动并入 `data/journal_zone.xlsx`**，不用手工迁移。
4. **主题闸门不再接入流水线**，`topic_gate.py` 只保留文件，不要按旧文档去配 profile。

---

## 九、许可

MIT License，见同目录 `LICENSE`。
