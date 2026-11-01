# 安装说明

本文件面向**使用这个 skill 的人**。如果你是把本仓库交给 AI 的人，看完第一节就够了；
后面几节是给你的 AI 看的执行细节，也可以由你按需执行。

---

## 一、三句话上手

1. 把这个仓库地址给你的 AI：
   `https://github.com/Murphy2000-cyk/literature-search`
2. 告诉它：**「装到技能目录，然后按仓库里的 INSTALL.md 配置」**
3. **两个 API Key 需要你自己去申请**（AI 不能替你注册），地址在第三节

> 如果你的 AI 只能读网页、不能执行命令，改用压缩包直链：
> `https://github.com/Murphy2000-cyk/literature-search/archive/refs/heads/main.zip`
>
> 仓库地址给的是**最新代码**；想拿某个已发布的稳定版，去 Releases 页下载对应 tag 的压缩包。

---

## 二、装到哪里

> **如果你用的不是 DSH**（Claude Code、或任何支持 Agent Skills 的宿主），直接跳到
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
python -m pip install requests sentence-transformers torch numpy openpyxl curl_cffi
```

| 包 | 缺了会怎样 |
|---|---|
| `requests` | **必需**，缺了直接不能用 |
| `sentence-transformers` + `torch` | 用于 BGE 相关性排序；缺了会跳过排序（降级，不算失败） |
| `openpyxl` | 写 Excel 用 |
| `curl_cffi` | 查分区时抓 LetPub 用；缺了会退回较慢的抓取链 |
| `numpy` | 依赖 |

BGE 模型首次使用会自动下载（约 400 MB），之后本地缓存。

---

## 三、必须自己申请的两个 Key

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
| `ELS_API_KEY` | Elsevier 摘要补全 |
| `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` | 标题与摘要翻译（可选） |
| `LITSEARCH_OUT_DIR` | 覆盖默认输出目录（默认为 `~/literature-search-out`） |

---

## 五、怎么用

直接用自然语言说需求即可，例如：

> 帮我检索「外墙外保温系统无损检测」的英文文献，只要中科院 1–2 区、2020 年以后、期刊论文。

能说清这五件事，结果就基本可控：

| 要说的 | 说明 |
|---|---|
| 研究方向 | 决定主题筛选轴怎么配 |
| 关键词 | 不给的话它自己组 3–4 组，每组不超过 5 个词 |
| 分区要求 | 中科院 1–4 区，可只取 1–2 区 |
| 时间范围 | 默认近 10 年 |
| 论文类型 | 默认排除会议论文集与预印本 |

产出：一张 Excel（含 DOI、摘要、分区、疑似会议标记等），外加一份检索完整性报告。
之后可以让它按需读这张表，不必整表塞进上下文。

**输出位置**：默认写到 `~/literature-search-out/`（Windows 上是
`C:\Users\<你的用户名>\literature-search-out\`）。想换地方就设 `LITSEARCH_OUT_DIR`，
或在调用时传 `save_dir`。

---

## 六、常见问题

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
<你的宿主的 skill 扫描根>/literature-search/SKILL.md
```

**不要多套一层文件夹。** 写成 `<扫描根>/literature-search/literature-search/SKILL.md`
不会被识别，而且多数宿主**不报错**，表现成「装了但用不了」，很难查。

装完这个目录里应当能直接看到 `SKILL.md`、`search_pipeline.py` 等文件。

| 宿主 | 放到 |
|---|---|
| DSH（本仓库的原生宿主） | `~/.agents/skills/` 或 `~/.dsh/skills/` |
| Claude Code | `~/.claude/skills/literature-search/` |
| 其他宿主 | 按它的文档找到 skills 目录，形态同上 |

深度是一层，这一点各宿主基本一致。装完记得重新加载，或重启一次会话。

### 2. Key 放哪：跨宿主请统一用环境变量

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

所以跨宿主时，**环境变量是唯一三个脚本都认的方式**。

### 3. 一个可选的外部命令：`searchts`

只有两处用到它：

| 位置 | 用途 | 缺了会怎样 |
|---|---|---|
| `journal_quartile.py` | 抓 LetPub 的分区页 | 分区查询整体不可用，其余功能正常 |
| `abstract_fill.py` | 摘要补全的最后一级，渲染论文落地页 | 少这一级，前四级照常 |

它**不在本仓库里**，是一个独立的第三方命令行工具（MIT 协议，作者是 GitHub 用户 `capad`）：
`https://github.com/capad-xyz/searchts`

**没有它也完全能用。** 检索、BGE 排序、主题闸门、落盘 Excel、按需读取，都不依赖它。
缺的时候 `--enrich-zone` 会直接报「找不到 searchts 命令」，不会崩，也不会给你一张空表。

### 4. 触发条件可能要改

`description` 目前是中文写的。如果你的模型主要用英文对话，它可能匹配不上。两种做法：

- 改成英文，或中英并列；
- 不改，用你的宿主提供的方式手动调用。

两个宿主对 `description` 的长度上限不同：DSH 这边按 400 字符算，Agent Skills 规范给 1024。
当前版本两百来字，两边都够，不用动。

### 5. 平台与路径

在 Windows 上开发，代码有跨平台分支，但 **Linux 与 macOS 的实测覆盖较少**。

文档里有几处示例路径是 Windows 形态（`E:\Anaconda3\envs\py313\python.exe`、
`C:\Users\<你的用户名>\.agents\skills\...`），**那些只是示例**，按你自己的环境改。

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

`skill-creator` 是教你怎么写一个 skill 的；**规范才是判断「我这个 skill 在别的宿主上能不能跑」的依据**，
两者不要混用。

---

## 八、许可

MIT License，见同目录 `LICENSE`。
