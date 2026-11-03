# 期刊名异常与缩写匹配 · 实测记录

> 来源：2026 年 11 月 2 日在 `journal_quartile.py` 上用 easyScholar 开放接口实测，
> 样本取自某次检索记录中的 `04_删除会议与预印本.xlsx`（150 个刊名）。
> 只记录实测结果，不做推测。

## 一、批量结果

150 个刊名，1 秒间隔，149 秒跑完，零异常：

| 项 | 数量 |
|---|---|
| 命中 | **80** |
| 未命中 | 70 |
| 异常 | 0 |

## 二、70 个未命中的构成

### 2.1 根本不该有分区（约 55 个）

Semantic Scholar 把**会议论文集标题**塞进了「期刊」字段，这类本来就查不到分区：

```
Proceedings of the 15th International Workshop on Structural Health Monitoring
Proceedings of the 14th International Workshop on Structural Health Monitoring
SPIE Smart Structures and Materials + Nondestructive Evaluation
2021 48th Annual Review of Progress in Quantitative Nondestructive Evaluation
Volume 1
```

**这说明 `venue_rules.py` 的覆盖还不够。** 这些本该在第 4 级「删会议论文」被清掉，
却漏到了分区查询阶段。要查的是「它们为什么没被 `is_conference_venue()` 判出来」。

### 2.2 真期刊但刊名脏（约 15 个）

```
Pamm                                  ← 应为 Proceedings in Applied Mathematics and Mechanics
Smart Structures                      ← 应为 Smart Structures and Materials
Key Engineering Materials             ← 真刊，easyScholar 库里查不到这个写法
Structural Health Monitoring          ← 真刊，全名查得到但分区字段为空
Advances in Materials Science and Engineering   ← 真刊，全名也查不到
TheScientificWorldJournal             ← 缺空格，应为 The Scientific World Journal
Malaysian Journal of Civil Engineering ← 区域性期刊，大概率确实未收录
```

## 三、LetPub 能不能补上这 70 个

**不能，别指望。** 两家的覆盖范围是同类问题：

- 会议论文集：中科院分区表本身就不收，LetPub 也没有
- 真期刊的刊名脏：LetPub 按 ISSN 查能绕过刊名问题，但**前提是 ISSN 能拿到**；
  而这些漏查的刊里，ISSN 反查也大多失败了（表里 `issn` 为空）

结论：`ALLOW_LETPUB` 对这批漏查基本无帮助。它的价值在另一处——
**easyScholar 服务不可用时的兜底**，而不是补命中率。

## 四、缩写能不能查（实测）

截图上「使用完整英文名」的说法偏保守，实测 easyScholar 确实做了模糊匹配，但**只对一部分有效**：

| 缩写 | 结果 | 对应全名 | 结果 |
|---|---|---|---|
| `J. Struct. Eng.` | **命中 3 区** | Journal of Structural Engineering | 命中 3 区 |
| `Struct. Health Monit.` | **命中 2 区** | Structural Health Monitoring | 命中，但分区字段为空 |
| `Pamm` | 未命中 | Proceedings in Applied Mathematics and Mechanics | 未命中 |
| `Adv. Mater. Sci. Eng.` | 未命中 | Advances in Materials Science and Engineering | 未命中 |
| `Key Eng. Mater.` | 未命中 | Key Engineering Materials | 未命中 |
| `Smart Structures` | 未命中 | Smart Structures and Materials | 未命中 |

缩写命中率 2/6。

### ⚠️ 比漏查更危险的是「匹错」

`Struct. Health Monit.` 返回 2 区，而全名 `Structural Health Monitoring` 返回分区字段为空。
同一本刊，两种写法给出不同结果，说明**模糊匹配可能匹到了另一本刊**
（很可能是 `Structural Control & Health Monitoring` 或该系列的会议录）。

而接口**不返回它实际匹配到的刊名**，所以无法校验。

**因此：不能用缩写查询。** 宁可漏查记 NULL，也不能写入一个错的分区——
错的分区会直接改变「1 区 2 区」筛选结果，而且不会报错。

## 五、刊名规范化规则（待实现）

按优先级：

1. **`Volume N` 结尾** → 不是期刊，标记为会议录，跳过
2. **以 `Proceedings of` / `SPIE` / `Annual Review of Progress` 开头** → 会议录，跳过
3. **全大写且无空格**（`TheScientificWorldJournal`）→ 按驼峰拆词后再查
4. **带 `(Print)` / `(Online)` 后缀** → 去掉后缀再查
5. **纯缩写（含 `.` 且词长 ≤ 6）** → 不查，记 NULL。见第四节理由

## 六、按当前策略，正确的预期

- 命中率天花板约 **55%**（80/150），因为近一半是会议录，不该有分区
- 真正的漏查只有约 **10%**（15/150），且集中在刊名脏
- 所以**不要为了追命中率去放开缩写匹配**，那会引入错值

## 七、待办

- [ ] 把 2.1 那批会议录标题补进 `venue_rules.py` 的规则
- [ ] 实现第五节的规范化，加一个 `_normalize_venue()` 在查到之前先清洗
- [ ] 给「需授权(LetPub)」单独一个状态，别和「查询失败」混在一起（现在会被无限重试）
